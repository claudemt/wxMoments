
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import psutil

from .database_filters import should_skip_source_database

_DEBUG_DETECTION = os.environ.get("WXMOMENTS_DEBUG_DETECTION", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

COMMON_WECHAT_PATTERNS = (
    "xwechat_files",
    "wechat files",
    "weixin files",
    "wechatmsg",
    "wechat",
    "weixin",
    "微信",
)

SYSTEM_SCAN_SKIP_NAMES = {
    "$recycle.bin",
    "$winreagent",
    "config.msi",
    "documents and settings",
    "intel",
    "onedrivetemp",
    "perflogs",
    "program files",
    "program files (x86)",
    "programdata",
    "recovery",
    "system volume information",
    "windows",
    "windows.old",
    "windows.old(1)",
}


def _debug(message: str) -> None:
    if _DEBUG_DETECTION:
        print(f"[DEBUG] {message}")


def parse_global_config(base_path: str) -> dict[str, str] | None:
    config_path = Path(base_path).expanduser() / "all_users" / "config" / "global_config"
    if not config_path.is_file():
        return None

    try:
        full_data = config_path.read_bytes()
        if len(full_data) <= 4:
            return None
        encrypted_data = full_data[4:]

        key = b"xwechat_crypt_key"[:16]
        iv = b"\0" * 16
        try:
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

            try:
                from cryptography.hazmat.decrepit.ciphers.modes import CFB
            except ImportError:
                from cryptography.hazmat.primitives.ciphers.modes import CFB

            cipher = Cipher(algorithms.AES(key), CFB(iv), backend=default_backend())
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(encrypted_data) + decryptor.finalize()
        except ImportError:
            from Crypto.Cipher import AES

            cipher = AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128)
            decrypted = cipher.decrypt(encrypted_data)

        def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
            result = 0
            shift = 0
            while offset < len(data):
                byte = data[offset]
                offset += 1
                result |= (byte & 0x7F) << shift
                if not (byte & 0x80):
                    break
                shift += 7
            return result, offset

        def extract_mmkv_string(data: bytes, key_str: str) -> str | None:
            key_bytes = key_str.encode("utf-8")
            idx = data.find(key_bytes)
            if idx == -1:
                return None
            offset = idx + len(key_bytes)
            try:
                value_len, offset = decode_varint(data, offset)
                if value_len <= 0 or offset >= len(data):
                    return None
                str_len, offset = decode_varint(data, offset)
                if str_len > 0 and offset + str_len <= len(data):
                    return data[offset : offset + str_len].decode("utf-8", errors="ignore")
            except Exception:
                return None
            return None

        wxid = extract_mmkv_string(decrypted, "mmkv_key_user_name")
        nickname = extract_mmkv_string(decrypted, "mmkv_key_nick_name")
        avatar_url = extract_mmkv_string(decrypted, "mmkv_key_head_img_url")
        if not avatar_url and b"http" in decrypted:
            http_idx = decrypted.find(b"http")
            slash_zero_idx = decrypted.find(b"/0", http_idx)
            if slash_zero_idx != -1:
                avatar_url = decrypted[http_idx : slash_zero_idx + 2].decode(
                    "utf-8",
                    errors="ignore",
                )

        if wxid or nickname:
            return {"wxid": wxid, "nickname": nickname, "avatar": avatar_url}
    except Exception as exc:
        _debug(f"parse global_config failed: {exc}")
    return None


def get_process_list() -> list[tuple[int, str]]:
    processes: list[tuple[int, str]] = []
    for process in psutil.process_iter(("pid", "name")):
        try:
            processes.append((int(process.info["pid"]), str(process.info.get("name") or "")))
        except (psutil.Error, TypeError, ValueError):
            continue
    return processes


def get_process_exe_path(process_id: int) -> str | None:
    try:
        return psutil.Process(int(process_id)).exe()
    except (psutil.Error, OSError, TypeError, ValueError):
        return None


def _is_wechat_dir_candidate_name(name: str) -> bool:
    normalized = str(name or "").strip().lower()
    return bool(normalized) and any(pattern in normalized for pattern in COMMON_WECHAT_PATTERNS)


def _safe_iter_subdirs(directory: str | Path) -> list[tuple[str, str]]:
    try:
        with os.scandir(directory) as entries:
            return [(entry.name, entry.path) for entry in entries if entry.is_dir()]
    except (PermissionError, OSError):
        return []


def _append_unique(paths: list[str], candidate: str | Path) -> None:
    raw = str(candidate or "").strip()
    if not raw:
        return
    normalized = os.path.normpath(raw)
    key = os.path.normcase(normalized)
    if key not in {os.path.normcase(item) for item in paths}:
        paths.append(normalized)


def _is_wechat_account_dir(path: Path) -> bool:
    try:
        return path.is_dir() and (
            (path / "db_storage").is_dir()
            or (path / "FileStorage" / "Image").is_dir()
            or (path / "FileStorage" / "Image2").is_dir()
            or any(item.is_file() and item.suffix.lower() == ".db" for item in path.iterdir())
        )
    except OSError:
        return False


def _contains_wechat_account_dirs(path: Path) -> bool:
    if _is_wechat_account_dir(path):
        return True
    try:
        return any(_is_wechat_account_dir(child) for child in path.iterdir() if child.is_dir())
    except OSError:
        return False


def _contains_wechat_accounts_within(path: Path, *, depth: int) -> bool:
    if _contains_wechat_account_dirs(path):
        return True
    if depth <= 0:
        return False
    try:
        return any(
            _contains_wechat_accounts_within(child, depth=depth - 1)
            for child in path.iterdir()
            if child.is_dir()
        )
    except OSError:
        return False


def _build_auto_detect_scan_paths() -> list[str]:
    scan_paths: list[str] = []
    seen: set[str] = set()

    def add(path_value: str | Path | None) -> None:
        raw = str(path_value or "").strip()
        if not raw:
            return
        normalized = os.path.normpath(raw)
        key = os.path.normcase(normalized)
        if key not in seen:
            seen.add(key)
            scan_paths.append(normalized)

    home_dir = Path.home()
    for item in (home_dir, home_dir / "Documents", home_dir / "Desktop", home_dir / "Downloads"):
        add(item)

    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        profile = Path(user_profile)
        for item in (profile, profile / "Documents", profile / "Desktop", profile / "Downloads"):
            add(item)

    drives: list[Path] = []
    try:
        partitions = psutil.disk_partitions(all=False)
        for part in partitions:
            mount = str(part.mountpoint or "").strip()
            if mount:
                drives.append(Path(mount))
    except Exception:
        pass

    if not drives:
        for drive_letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive_root = Path(f"{drive_letter}:{os.sep}")
            if drive_root.exists():
                drives.append(drive_root)

    for drive_root in drives:
        add(drive_root)
        users_dir = drive_root / "Users"
        add(users_dir)
        for child_name, child_path in _safe_iter_subdirs(drive_root):
            if child_name.strip().lower() not in SYSTEM_SCAN_SKIP_NAMES:
                add(child_path)
        for _user_name, user_dir in _safe_iter_subdirs(users_dir):
            user_path = Path(user_dir)
            for item in (user_path, user_path / "Documents", user_path / "Desktop", user_path / "Downloads", user_path / "OneDrive"):
                add(item)

    return scan_paths


def auto_detect_wechat_data_dirs() -> list[str]:
    detected_dirs: list[str] = []
    common_user_roots = {"documents", "desktop", "downloads", "onedrive"}

    for scan_path in _build_auto_detect_scan_paths():
        path = Path(scan_path)
        if not path.exists():
            continue

        path_name = path.name.strip().lower()
        if (
            (_is_wechat_dir_candidate_name(path_name) or path_name in common_user_roots)
            and _contains_wechat_accounts_within(path, depth=3)
        ):
            _append_unique(detected_dirs, path)
            continue

        for item_name, item_path in _safe_iter_subdirs(path):
            item = Path(item_path)
            if _contains_wechat_accounts_within(item, depth=2) and (
                _is_wechat_dir_candidate_name(item_name)
                or item_name.strip().lower() in common_user_roots
            ):
                _append_unique(detected_dirs, item)

    return detected_dirs


def collect_account_databases(data_dir: str, account_name: str = "") -> list[dict[str, Any]]:
    databases: list[dict[str, Any]] = []
    root_dir = Path(data_dir).expanduser()
    if not root_dir.exists():
        return databases

    try:
        for root, _dirs, files in os.walk(root_dir):
            for file_name in files:
                if not file_name.endswith(".db") or should_skip_source_database(file_name):
                    continue
                db_path = Path(root) / file_name
                try:
                    file_size = db_path.stat().st_size
                except OSError:
                    file_size = 0
                databases.append(
                    {
                        "path": str(db_path),
                        "name": file_name,
                        "type": re.sub(r"\d*\.db$", "", file_name),
                        "size": file_size,
                        "relative_path": os.path.relpath(db_path, root_dir),
                    }
                )
    except (PermissionError, OSError):
        pass

    return databases


def detect_wechat_accounts_from_data_root(data_root_path: str | None = None) -> list[dict[str, Any]]:
    roots = [Path(data_root_path).expanduser()] if data_root_path else [
        Path(item) for item in auto_detect_wechat_data_dirs()
    ]
    accounts: list[dict[str, Any]] = []
    seen: set[str] = set()

    for root in roots:
        candidates: list[Path] = [root] if _is_wechat_account_dir(root) else []
        if not candidates:
            children = [Path(path) for _name, path in _safe_iter_subdirs(root)]
            candidates = [child for child in children if _is_wechat_account_dir(child)]
            if not candidates:
                for child in children:
                    candidates.extend(
                        Path(path)
                        for _name, path in _safe_iter_subdirs(child)
                        if _is_wechat_account_dir(Path(path))
                    )

        for account_dir in candidates:
            try:
                key = os.path.normcase(str(account_dir.resolve()))
            except OSError:
                key = os.path.normcase(str(account_dir))
            if key in seen:
                continue
            seen.add(key)
            databases = collect_account_databases(str(account_dir), account_dir.name)
            accounts.append(
                {
                    "account_name": account_dir.name,
                    "backup_dir": None,
                    "data_dir": str(account_dir),
                    "databases": databases,
                    "database_count": len(databases),
                }
            )

    return accounts


def _detect_running_wechat() -> dict[str, Any]:
    target_names = {"weixin.exe", "wechat.exe"}
    for pid, process_name in get_process_list():
        if process_name.lower() not in target_names:
            continue
        exe_path = get_process_exe_path(pid)
        if not exe_path:
            continue
        return {
            "wechat_exe_path": exe_path,
            "wechat_install_path": str(Path(exe_path).parent),
            "is_running": True,
        }
    return {"wechat_exe_path": None, "wechat_install_path": None, "is_running": False}


def detect_wechat_installation(data_root_path: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "wechat_version": None,
        "platform": "windows" if os.name == "nt" else sys.platform,
        "wechat_install_path": None,
        "wechat_exe_path": None,
        "is_running": False,
        "accounts": [],
        "total_accounts": 0,
        "total_databases": 0,
        "detection_errors": [],
        "detection_methods": [],
        "wechat_data_dirs": [],
        "message_dirs": [],
        "databases": [],
        "user_accounts": [],
    }

    process_info = _detect_running_wechat()
    result.update(process_info)
    if result["is_running"]:
        result["detection_methods"].append("detected running WeChat")
    else:
        result["detection_methods"].append("WeChat process not detected")

    try:
        accounts = detect_wechat_accounts_from_data_root(data_root_path)
    except Exception as exc:
        accounts = []
        result["detection_errors"].append(f"account detection failed: {exc}")

    result["accounts"] = accounts
    result["total_accounts"] = len(accounts)
    result["total_databases"] = sum(int(account.get("database_count") or 0) for account in accounts)

    for account in accounts:
        data_dir = account.get("data_dir")
        if data_dir:
            result["wechat_data_dirs"].append(data_dir)
            result["message_dirs"].append(data_dir)
        result["user_accounts"].append(account.get("account_name"))
        for db in account.get("databases", []):
            result["databases"].append(
                {
                    "path": db["path"],
                    "name": db["name"],
                    "type": db["type"],
                    "size": db["size"],
                    "user": account.get("account_name"),
                    "user_dir": data_dir,
                }
            )

    result["detection_methods"].append(
        f"detected {result['total_accounts']} account(s), {result['total_databases']} database(s)"
    )
    return result
