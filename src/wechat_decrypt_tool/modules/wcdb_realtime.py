from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .key_store import get_account_keys_from_store
from .logging_config import get_logger
from .media_cache import _resolve_account_db_storage_dir

logger = get_logger(__name__)

_KEY_HEX_LENGTH = 64
_MAX_LOG_ENTRIES = 6
_MIN_CONNECTION_TIMEOUT = 0.1
_MIN_REQUEST_TIMEOUT = 0.5
_REQUEST_TIMEOUT_BUFFER = 0.2
_MAX_WAIT_INTERVAL = 5.0
_CEILING_EPSILON = 0.999
_HEX_KEY_PATTERN = r"[0-9a-fA-F]{64}"

_VC_REDIST_HELP_URL = "https://learn.microsoft.com/zh-cn/cpp/windows/latest-supported-vc-redist?view=msvc-170"
_VC_REDIST_HELP_TEXT = (
    "如果这是首次运行或换电脑后出现，请下载安装最新版 Microsoft Visual C++ Redistributable"
    "（建议安装 x64 和 x86 两个版本），安装后重启电脑再运行。"
    f"下载地址：{_VC_REDIST_HELP_URL}"
)
_WCDB_OPEN_HELP_TEXT = (
    "请重新获取当前账号的数据库密钥，并确认密钥来源与当前 db_storage 一致；"
    "若密钥正确，请关闭微信后重试，以排除数据库占用或文件未写完整。"
)


class WCDBRealtimeError(RuntimeError):
    pass


def _with_vc_redist_help(message: str) -> str:
    text = str(message or "").strip()
    if not sys.platform.startswith("win") or _VC_REDIST_HELP_URL in text:
        return text
    return f"{text} {_VC_REDIST_HELP_TEXT}" if text else _VC_REDIST_HELP_TEXT


def _with_wcdb_open_help(message: str) -> str:
    text = str(message or "").strip()
    if _WCDB_OPEN_HELP_TEXT in text:
        return text
    return f"{text} {_WCDB_OPEN_HELP_TEXT}" if text else _WCDB_OPEN_HELP_TEXT


def _should_cache_open_failure(exc: Exception) -> bool:
    message = str(exc or "")
    non_key_failures = (
        "数据库密钥与当前 session.db 不匹配",
        "session.db 文件不完整",
        "无法读取 session.db 进行密钥校验",
        "Invalid db key",
    )
    return not any(marker in message for marker in non_key_failures)


def _clean_weflow_account_dir_name(dir_name: str) -> str:
    trimmed = str(dir_name or "").strip()
    if not trimmed:
        return trimmed

    if trimmed.lower().startswith("wxid_"):
        match = re.match(r"^(wxid_[^_]+)", trimmed, flags=re.IGNORECASE)
        return match.group(1) if match else trimmed

    suffix_match = re.match(r"^(.+)_([a-zA-Z0-9]{4})$", trimmed)
    return suffix_match.group(1) if suffix_match else trimmed


def _derive_weflow_wcdb_wxid(account: str, db_storage_dir: Optional[Path] = None) -> str:
    candidates: list[str] = []
    if db_storage_dir is not None:
        try:
            candidates.append(Path(db_storage_dir).parent.name)
        except Exception:
            pass
    candidates.append(str(account or ""))

    for item in candidates:
        cleaned = _clean_weflow_account_dir_name(item)
        if cleaned:
            return cleaned
    return str(account or "").strip()


_MODULE_DIR = Path(__file__).resolve().parent
_PACKAGE_ROOT = _MODULE_DIR.parent if _MODULE_DIR.name == "modules" else _MODULE_DIR
_NATIVE_DIR = _PACKAGE_ROOT / "native"


def _wcdb_native_relative_path() -> Path:
    return Path("wcdb_api.dll")


_DEFAULT_WCDB_API_DLL = _NATIVE_DIR / _wcdb_native_relative_path()
_WCDB_API_DLL_SELECTED: Optional[Path] = None
_lib_lock = threading.RLock()
_lib: Optional[ctypes.CDLL] = None
_initialized = False
_loaded_wcdb_api_dll: Optional[Path] = None
_preloaded_native_libs: list[ctypes.CDLL] = []
_protection_checked = False
_protection_result: Optional[tuple[int, str]] = None


def _is_windows() -> bool:
    return sys.platform.startswith("win")




def _is_supported_native_platform() -> bool:
    return _is_windows()


def _iter_runtime_wcdb_api_dll_paths() -> tuple[Path, ...]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def add_anchor(anchor: str | Path | None) -> None:
        if not anchor:
            return
        try:
            base = Path(anchor).resolve()
        except Exception:
            base = Path(anchor)
        for candidate in (
            base / "native" / _wcdb_native_relative_path(),
            base / "wechat_decrypt_tool" / "native" / _wcdb_native_relative_path(),
        ):
            key = os.path.normcase(str(candidate.resolve(strict=False)))
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)

    add_anchor(os.environ.get("WECHAT_TOOL_DATA_DIR", "").strip())
    add_anchor(Path.cwd())
    if getattr(sys, "frozen", False):
        add_anchor(Path(sys.executable).resolve().parent)

    return tuple(candidates)


def _is_project_wcdb_api_dll_path(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except Exception:
        resolved = path

    try:
        default_resolved = _DEFAULT_WCDB_API_DLL.resolve(strict=False)
    except Exception:
        default_resolved = _DEFAULT_WCDB_API_DLL

    if resolved == default_resolved:
        return True

    for candidate in _iter_runtime_wcdb_api_dll_paths():
        try:
            if resolved == candidate.resolve(strict=False):
                return True
        except Exception:
            if resolved == candidate:
                return True

    parts = tuple(str(part).lower() for part in resolved.parts)
    relative_parts = tuple(str(part).lower() for part in _wcdb_native_relative_path().parts)
    allowed_suffixes = (
        ("backend", "native", *relative_parts),
        ("wechat_decrypt_tool", "native", *relative_parts),
    )
    return any(parts[-len(suffix) :] == suffix for suffix in allowed_suffixes)


def _resolve_wcdb_api_dll_path() -> Path:
    global _WCDB_API_DLL_SELECTED
    if _WCDB_API_DLL_SELECTED is not None:
        return _WCDB_API_DLL_SELECTED

    env = str(os.environ.get("WECHAT_TOOL_WCDB_API_DLL_PATH", "") or "").strip()
    candidates: list[Path] = []
    if env:
        env_path = Path(env)
        if _is_project_wcdb_api_dll_path(env_path):
            candidates.append(env_path)
        else:
            logger.warning("[wcdb] ignored external wcdb_api override: %s", env_path)
    candidates.append(_DEFAULT_WCDB_API_DLL)

    for path in candidates:
        try:
            if path.exists() and path.is_file():
                _WCDB_API_DLL_SELECTED = path
                return path
        except Exception:
            continue

    _WCDB_API_DLL_SELECTED = _DEFAULT_WCDB_API_DLL
    return _WCDB_API_DLL_SELECTED


def _iter_wcdb_resource_paths(wcdb_api_dll: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path: str | Path | None) -> None:
        if not path:
            return
        try:
            resolved = Path(path).resolve()
        except Exception:
            resolved = Path(path)
        key = str(resolved).replace("/", "\\").rstrip("\\").lower()
        if key not in seen:
            seen.add(key)
            candidates.append(resolved)

    dll_dir = wcdb_api_dll.parent
    add(dll_dir)
    add(dll_dir.parent)
    add(_NATIVE_DIR)
    add(_NATIVE_DIR.parent)
    add(Path.cwd())

    data_dir = str(os.environ.get("WECHAT_TOOL_DATA_DIR", "") or "").strip()
    if data_dir:
        add(data_dir)

    if getattr(sys, "frozen", False):
        try:
            add(Path(sys.executable).resolve().parent)
        except Exception:
            add(Path(sys.executable).parent)

    return tuple(candidates)


def _preload_wcdb_dependencies(wcdb_api_dll: Path) -> None:
    dll_dir = wcdb_api_dll.parent
    dependency_paths = tuple(dll_dir / name for name in ("WCDB.dll", "SDL2.dll", "VoipEngine.dll"))

    for dep_path in dependency_paths:
        if not dep_path.exists():
            continue
        try:
            mode = 0
            _preloaded_native_libs.append(ctypes.CDLL(str(dep_path), mode=mode))
            logger.info("[wcdb] preloaded dependency: %s", dep_path)
        except Exception as exc:
            logger.warning("[wcdb] preload dependency failed: %s err=%s", dep_path, exc)


def _run_init_protection(lib: ctypes.CDLL, wcdb_api_dll: Path) -> None:
    global _protection_checked, _protection_result
    if _protection_checked:
        return
    _protection_checked = True

    fn = getattr(lib, "InitProtection", None)
    if not fn:
        return

    try:
        fn.argtypes = [ctypes.c_char_p]
        fn.restype = ctypes.c_int32
    except Exception:
        pass

    best: Optional[tuple[int, str]] = None
    for resource_path in _iter_wcdb_resource_paths(wcdb_api_dll):
        try:
            rc = int(fn(str(resource_path).encode("utf-8")))
            logger.info("[wcdb] InitProtection rc=%s path=%s", rc, resource_path)
            if rc == 0:
                _protection_result = (rc, str(resource_path))
                return
            if best is None:
                best = (rc, str(resource_path))
        except Exception as exc:
            logger.warning("[wcdb] InitProtection exception path=%s err=%s", resource_path, exc)
    _protection_result = best


def _format_protection_hint() -> str:
    if not _protection_result:
        return ""
    rc, resource_path = _protection_result
    return f" protection_rc={rc} protection_path={resource_path}"


def _load_wcdb_lib() -> ctypes.CDLL:
    global _lib, _loaded_wcdb_api_dll
    with _lib_lock:
        if _lib is not None:
            return _lib

        if not _is_supported_native_platform():
            raise WCDBRealtimeError("WCDB realtime mode is only supported on Windows.")

        wcdb_api_dll = _resolve_wcdb_api_dll_path()
        if not wcdb_api_dll.exists():
            raise WCDBRealtimeError(f"Missing WCDB native library at: {wcdb_api_dll}")

        if _is_windows():
            try:
                os.add_dll_directory(str(wcdb_api_dll.parent))
            except Exception:
                pass

        _preload_wcdb_dependencies(wcdb_api_dll)
        lib = ctypes.CDLL(str(wcdb_api_dll))
        logger.info("[wcdb] using native library: %s", wcdb_api_dll)

        lib.wcdb_init.argtypes = []
        lib.wcdb_init.restype = ctypes.c_int
        lib.wcdb_shutdown.argtypes = []
        lib.wcdb_shutdown.restype = ctypes.c_int
        lib.wcdb_open_account.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int64)]
        lib.wcdb_open_account.restype = ctypes.c_int
        lib.wcdb_close_account.argtypes = [ctypes.c_int64]
        lib.wcdb_close_account.restype = ctypes.c_int

        try:
            lib.wcdb_set_my_wxid.argtypes = [ctypes.c_int64, ctypes.c_char_p]
            lib.wcdb_set_my_wxid.restype = ctypes.c_int
        except Exception:
            pass

        lib.wcdb_get_display_names.argtypes = [ctypes.c_int64, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
        lib.wcdb_get_display_names.restype = ctypes.c_int

        try:
            lib.wcdb_exec_query.argtypes = [
                ctypes.c_int64,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.POINTER(ctypes.c_char_p),
            ]
            lib.wcdb_exec_query.restype = ctypes.c_int
        except Exception:
            pass

        try:
            lib.wcdb_get_sns_timeline.argtypes = [
                ctypes.c_int64,
                ctypes.c_int32,
                ctypes.c_int32,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_int32,
                ctypes.c_int32,
                ctypes.POINTER(ctypes.c_char_p),
            ]
            lib.wcdb_get_sns_timeline.restype = ctypes.c_int
        except Exception:
            pass

        lib.wcdb_get_logs.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
        lib.wcdb_get_logs.restype = ctypes.c_int
        lib.wcdb_free_string.argtypes = [ctypes.c_char_p]
        lib.wcdb_free_string.restype = None

        _loaded_wcdb_api_dll = wcdb_api_dll
        _lib = lib
        return lib


def _ensure_initialized() -> None:
    global _initialized
    lib = _load_wcdb_lib()
    with _lib_lock:
        if _initialized:
            return
        wcdb_api_dll = _loaded_wcdb_api_dll or _resolve_wcdb_api_dll_path()
        _run_init_protection(lib, wcdb_api_dll)
        rc = int(lib.wcdb_init())
        if rc != 0:
            logs = get_native_logs(require_initialized=False)
            hint = _format_protection_hint()
            if logs:
                hint += f" logs={logs[:_MAX_LOG_ENTRIES]}"
            raise WCDBRealtimeError(_with_vc_redist_help(f"wcdb_init failed: {rc}.{hint}"))
        _initialized = True


def _safe_load_json(payload: str) -> Any:
    try:
        return json.loads(payload)
    except Exception:
        return None


def _call_out_json(fn, *args) -> str:
    lib = _load_wcdb_lib()
    out = ctypes.c_char_p()
    rc = int(fn(*args, ctypes.byref(out)))
    try:
        if rc != 0:
            logs = get_native_logs()
            hint = f" logs={logs[:_MAX_LOG_ENTRIES]}" if logs else ""
            raise WCDBRealtimeError(f"wcdb api call failed: {rc}.{hint}")
        raw = out.value or b""
        return raw.decode("utf-8", errors="replace")
    finally:
        try:
            if out.value:
                lib.wcdb_free_string(out)
        except Exception:
            pass


def get_native_logs(*, require_initialized: bool = True) -> list[str]:
    if require_initialized:
        try:
            _ensure_initialized()
        except Exception:
            return []
    lib = _load_wcdb_lib()
    out = ctypes.c_char_p()
    rc = int(lib.wcdb_get_logs(ctypes.byref(out)))
    try:
        if rc != 0 or not out.value:
            return []
        decoded = _safe_load_json(out.value.decode("utf-8", errors="replace"))
        if isinstance(decoded, list):
            return [str(x) for x in decoded]
        return []
    except Exception:
        return []
    finally:
        try:
            if out.value:
                lib.wcdb_free_string(out)
        except Exception:
            pass


def _validate_session_db_key(session_db_path: Path, key_hex: str) -> str:
    path = Path(session_db_path)
    if not path.exists():
        raise WCDBRealtimeError(f"session db not found: {path}")

    key = str(key_hex or "").strip()
    if not re.fullmatch(_HEX_KEY_PATTERN, key):
        raise WCDBRealtimeError("Invalid db key (must be 64 hex chars).")

    try:
        from .wechat_decrypt import PAGE_SIZE, SQLITE_HEADER, _resolve_page1_key_material

        with path.open("rb") as stream:
            page1 = stream.read(PAGE_SIZE)
    except WCDBRealtimeError:
        raise
    except Exception as exc:
        raise WCDBRealtimeError(f"无法读取 session.db 进行密钥校验: {path}: {exc}") from exc

    if page1.startswith(SQLITE_HEADER):
        return "plaintext"
    if len(page1) < PAGE_SIZE:
        raise WCDBRealtimeError(f"session.db 文件不完整（不足 {PAGE_SIZE} 字节）: {path}")

    resolved = _resolve_page1_key_material(bytes.fromhex(key), page1)
    if resolved is None:
        raise WCDBRealtimeError(
            f"数据库密钥与当前 session.db 不匹配: {path}。"
            "请重新获取当前账号的数据库密钥，勿复用其他账号或旧 db_storage 的密钥。"
        )
    return str(resolved[2] or "unknown")


def open_account(session_db_path: Path, key_hex: str, *, timeout: float = 30.0) -> int:
    path = Path(session_db_path)
    key = str(key_hex or "").strip()
    key_mode = _validate_session_db_key(path, key)
    logger.info("[wcdb] session db key preflight passed mode=%s path=%s", key_mode, path)

    _ensure_initialized()
    lib = _load_wcdb_lib()
    out_handle = ctypes.c_int64(0)
    rc = int(lib.wcdb_open_account(str(path).encode("utf-8"), key.encode("utf-8"), ctypes.byref(out_handle)))
    if rc != 0 or int(out_handle.value) <= 0:
        logs = get_native_logs()
        hint = f" logs={logs[:_MAX_LOG_ENTRIES]}" if logs else ""
        raise WCDBRealtimeError(_with_wcdb_open_help(f"wcdb_open_account failed: {rc}.{hint}"))
    return int(out_handle.value)


def set_my_wxid(handle: int, wxid: str) -> bool:
    try:
        _ensure_initialized()
    except Exception:
        return False

    wxid_text = str(wxid or "").strip()
    if not wxid_text:
        return False

    lib = _load_wcdb_lib()
    fn = getattr(lib, "wcdb_set_my_wxid", None)
    if not fn:
        return False

    try:
        return int(fn(ctypes.c_int64(int(handle)), wxid_text.encode("utf-8"))) == 0
    except Exception:
        return False


def close_account(handle: int) -> None:
    try:
        value = int(handle)
    except Exception:
        return
    if value <= 0:
        return
    try:
        _ensure_initialized()
        _load_wcdb_lib().wcdb_close_account(ctypes.c_int64(value))
    except Exception:
        return


def get_display_names(handle: int, usernames: list[str]) -> dict[str, str]:
    _ensure_initialized()
    uniq = list(dict.fromkeys(str(item or "").strip() for item in usernames if str(item or "").strip()))
    if not uniq:
        return {}

    lib = _load_wcdb_lib()
    payload = json.dumps(uniq, ensure_ascii=False).encode("utf-8")
    out_json = _call_out_json(lib.wcdb_get_display_names, ctypes.c_int64(int(handle)), payload)
    decoded = _safe_load_json(out_json)
    if isinstance(decoded, dict):
        return {str(key): str(value) for key, value in decoded.items()}
    return {}


def exec_query(handle: int, *, kind: str, path: Optional[str], sql: str) -> list[dict[str, Any]]:
    _ensure_initialized()
    kind_text = str(kind or "").strip()
    sql_text = str(sql or "").strip()
    path_text = None if path is None else str(path or "").strip()
    if not kind_text:
        raise WCDBRealtimeError("Missing kind for exec_query.")
    if not sql_text:
        return []

    lib = _load_wcdb_lib()
    fn = getattr(lib, "wcdb_exec_query", None)
    if not fn:
        raise WCDBRealtimeError("Current wcdb_api.dll does not support exec_query.")

    out_json = _call_out_json(
        fn,
        ctypes.c_int64(int(handle)),
        kind_text.encode("utf-8"),
        None if path_text is None else path_text.encode("utf-8"),
        sql_text.encode("utf-8"),
    )
    decoded = _safe_load_json(out_json)
    if isinstance(decoded, list):
        return [item for item in decoded if isinstance(item, dict)]
    return []


def get_sns_timeline(
    handle: int,
    *,
    limit: int = 20,
    offset: int = 0,
    usernames: Optional[list[str]] = None,
    keyword: str | None = None,
    start_time: int = 0,
    end_time: int = 0,
) -> list[dict[str, Any]]:
    _ensure_initialized()
    users = list(dict.fromkeys(str(item or "").strip() for item in (usernames or []) if str(item or "").strip()))
    users_json = json.dumps(users, ensure_ascii=False) if users else ""
    keyword_text = str(keyword or "").strip()

    lib = _load_wcdb_lib()
    fn = getattr(lib, "wcdb_get_sns_timeline", None)
    if not fn:
        raise WCDBRealtimeError("Current wcdb_api.dll does not support sns timeline.")

    payload = _call_out_json(
        fn,
        ctypes.c_int64(int(handle)),
        ctypes.c_int32(max(0, int(limit or 0))),
        ctypes.c_int32(max(0, int(offset or 0))),
        users_json.encode("utf-8"),
        keyword_text.encode("utf-8"),
        ctypes.c_int32(int(start_time or 0)),
        ctypes.c_int32(int(end_time or 0)),
    )
    decoded = _safe_load_json(payload)
    if isinstance(decoded, list):
        return [item for item in decoded if isinstance(item, dict)]
    return []


def shutdown() -> None:
    global _initialized
    lib = _load_wcdb_lib()
    with _lib_lock:
        if not _initialized:
            return
        try:
            lib.wcdb_shutdown()
        finally:
            _initialized = False


def _resolve_session_db_path(db_storage_dir: Path) -> Path:
    candidates = [
        db_storage_dir / "session" / "session.db",
        db_storage_dir / "session.db",
        db_storage_dir / "Session.db",
        db_storage_dir / "MicroMsg.db",
    ]
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return candidate
        except Exception:
            continue

    for name in ("session.db", "MicroMsg.db"):
        try:
            for candidate in db_storage_dir.rglob(name):
                if candidate.exists() and candidate.is_file():
                    return candidate
        except Exception:
            pass

    raise WCDBRealtimeError(f"Cannot find session db in: {db_storage_dir}")


@dataclass(frozen=True)
class WCDBRealtimeConnection:
    account: str
    native_wxid: str
    handle: int
    db_storage_dir: Path
    session_db_path: Path
    connected_at: float
    lock: threading.Lock


class WCDBRealtimeManager:
    _FAILED_TTL = 60.0

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._conns: dict[str, WCDBRealtimeConnection] = {}
        self._connecting: dict[str, threading.Event] = {}
        self._failed: dict[str, tuple[float, str]] = {}

    def _recent_failure_locked(self, account: str) -> dict[str, Any]:
        cached = self._failed.get(str(account or ""))
        if cached is None:
            return {"active": False, "reason": "", "retry_after_seconds": 0}

        failed_at = float(cached[0])
        reason = str(cached[1] or "").strip()
        remaining = self._FAILED_TTL - (time.monotonic() - failed_at)
        if remaining <= 0:
            self._failed.pop(str(account or ""), None)
            return {"active": False, "reason": "", "retry_after_seconds": 0}
        return {
            "active": True,
            "reason": reason,
            "retry_after_seconds": max(1, int(remaining + _CEILING_EPSILON)),
        }

    def get_recent_failure(self, account: str) -> dict[str, Any]:
        with self._mu:
            return dict(self._recent_failure_locked(str(account or "")))

    def _record_failure(self, account: str, reason: Any) -> None:
        with self._mu:
            self._failed[str(account or "")] = (time.monotonic(), str(reason or "").strip())

    def get_status(self, account_dir: Path) -> dict[str, Any]:
        account = str(account_dir.name)
        key_item = get_account_keys_from_store(account)
        key_hex = str((key_item or {}).get("db_key") or "").strip()
        db_storage_dir = None
        session_db_path = None
        native_wxid = ""
        error = ""
        try:
            db_storage_dir = _resolve_account_db_storage_dir(account_dir)
            if db_storage_dir is not None:
                native_wxid = _derive_weflow_wcdb_wxid(account, db_storage_dir)
                session_db_path = _resolve_session_db_path(db_storage_dir)
        except Exception as exc:
            error = str(exc)
            native_wxid = _derive_weflow_wcdb_wxid(account, db_storage_dir)

        dll_path = _resolve_wcdb_api_dll_path()
        recent_failure = self.get_recent_failure(account)
        return {
            "account": account,
            "dll_present": dll_path.exists(),
            "wcdb_api_dll": str(dll_path),
            "key_present": len(key_hex) == _KEY_HEX_LENGTH,
            "native_wxid": native_wxid,
            "db_storage_dir": str(db_storage_dir) if db_storage_dir else "",
            "session_db_path": str(session_db_path) if session_db_path else "",
            "connected": self.is_connected(account),
            "error": error,
            "recent_failure": bool(recent_failure.get("active")),
            "failure_reason": str(recent_failure.get("reason") or ""),
            "retry_after_seconds": int(recent_failure.get("retry_after_seconds") or 0),
        }

    def is_connected(self, account: str) -> bool:
        with self._mu:
            conn = self._conns.get(str(account))
            return bool(conn and conn.handle > 0)

    def ensure_connected(
        self,
        account_dir: Path,
        *,
        key_hex: Optional[str] = None,
        timeout: float = 5.0,
    ) -> WCDBRealtimeConnection:
        account = str(account_dir.name)
        with self._mu:
            recent_failure = self._recent_failure_locked(account)
        if recent_failure.get("active"):
            retry_after = int(recent_failure.get("retry_after_seconds") or self._FAILED_TTL)
            reason = str(recent_failure.get("reason") or "").strip()
            message = f"WCDB connection recently failed; retry after {retry_after}s."
            if reason:
                message += f" Last error: {reason}"
            raise WCDBRealtimeError(message)

        deadline = time.monotonic() + max(_MIN_CONNECTION_TIMEOUT, float(timeout or 5.0))
        while True:
            with self._mu:
                existing = self._conns.get(account)
                if existing is not None and existing.handle > 0:
                    return existing
                waiter = self._connecting.get(account)
                if waiter is None:
                    waiter = threading.Event()
                    self._connecting[account] = waiter
                    break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WCDBRealtimeError("Timed out waiting for WCDB connection.")
            waiter.wait(timeout=min(remaining, _MAX_WAIT_INTERVAL))

        key = str(key_hex or "").strip()
        if not key:
            key_item = get_account_keys_from_store(account)
            key = str((key_item or {}).get("db_key") or "").strip()

        try:
            if not re.fullmatch(_HEX_KEY_PATTERN, key):
                raise WCDBRealtimeError("Missing db key for this account.")

            db_storage_dir = _resolve_account_db_storage_dir(account_dir)
            if db_storage_dir is None:
                raise WCDBRealtimeError("Cannot resolve db_storage directory for this account.")

            session_db_path = _resolve_session_db_path(db_storage_dir)
            native_wxid = _derive_weflow_wcdb_wxid(account, db_storage_dir)
            remaining = max(_MIN_CONNECTION_TIMEOUT, deadline - time.monotonic())
            request_timeout = max(_MIN_REQUEST_TIMEOUT, remaining - _REQUEST_TIMEOUT_BUFFER)

            handle_box: list[int] = []
            error_box: list[Exception] = []

            def do_open() -> None:
                try:
                    handle_box.append(open_account(session_db_path, key, timeout=request_timeout))
                except Exception as exc:
                    error_box.append(exc)

            thread = threading.Thread(target=do_open, daemon=True)
            thread.start()
            thread.join(timeout=remaining)

            if thread.is_alive():
                message = f"open_account timed out after {timeout:.0f}s for {session_db_path}."
                self._record_failure(account, message)
                raise WCDBRealtimeError(_with_wcdb_open_help(message))
            if error_box:
                if _should_cache_open_failure(error_box[0]):
                    self._record_failure(account, error_box[0])
                raise error_box[0]
            if not handle_box:
                raise WCDBRealtimeError(_with_wcdb_open_help("open_account returned no handle."))

            handle = handle_box[0]
            try:
                set_my_wxid(handle, native_wxid)
            except Exception:
                pass

            conn = WCDBRealtimeConnection(
                account=account,
                native_wxid=native_wxid,
                handle=handle,
                db_storage_dir=db_storage_dir,
                session_db_path=session_db_path,
                connected_at=time.time(),
                lock=threading.Lock(),
            )
            with self._mu:
                self._conns[account] = conn
                self._failed.pop(account, None)
            return conn
        finally:
            with self._mu:
                event = self._connecting.pop(account, None)
                if event is not None:
                    event.set()

    def disconnect(self, account: str) -> None:
        account_text = str(account or "").strip()
        if not account_text:
            return
        with self._mu:
            conn = self._conns.pop(account_text, None)
            self._failed.pop(account_text, None)
        if conn is None:
            return
        try:
            with conn.lock:
                close_account(conn.handle)
        except Exception:
            pass

    def close_all(self, *, lock_timeout_s: float | None = None) -> bool:
        with self._mu:
            conns = list(self._conns.values())
            self._conns.clear()
        ok = True
        for conn in conns:
            try:
                if lock_timeout_s is None:
                    with conn.lock:
                        close_account(conn.handle)
                    continue

                acquired = conn.lock.acquire(timeout=float(lock_timeout_s))
                if not acquired:
                    ok = False
                    continue
                try:
                    close_account(conn.handle)
                finally:
                    conn.lock.release()
            except Exception:
                ok = False
        return ok


WCDB_REALTIME = WCDBRealtimeManager()
