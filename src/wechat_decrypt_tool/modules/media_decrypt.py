import ctypes
import datetime
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import sqlite3
import struct
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .account_helpers import _decode_message_content
from .logging_config import get_logger
from .media_cache import _get_decrypted_resource_path, _resolve_account_wxid_dir, _try_find_decrypted_resource

logger = get_logger(__name__)


__all__ = [
    "MediaError",
    "_detect_image_media_type",
    "_is_probably_valid_image",
    "_is_safe_http_url",
    "_download_http_bytes",
    "_decrypt_emoticon_aes_cbc",
    "_normalize_emoticon_md5",
    "_normalize_emoticon_aes_key",
    "_first_emoticon_match",
    "_extract_emoticon_message_md5",
    "_extract_emoticon_message_extern_md5",
    "_extract_emoticon_message_aes_key",
    "_extract_emoticon_message_urls",
    "_emoticon_message_db_paths",
    "_emoticon_source_fingerprint",
    "_list_emoticon_message_tables",
    "_quote_sqlite_ident",
    "_iter_emoticon_varints",
    "_extract_emoticon_builtin_expr_id",
    "_lookup_emoticon_info",
    "_merge_emoticon_candidate",
    "_emoticon_catalog_public_stats",
    "_collect_emoticon_download_catalog_cached",
    "_collect_emoticon_download_catalog",
    "_collect_emoticon_download_candidates",
    "_find_emoticon_message_remote_source",
    "_try_fetch_emoticon_from_sources",
    "_try_fetch_emoticon_from_remote",
    "_get_wxam_decoder",
    "_WxAMConfig",
    "_wxgf_to_image_bytes",
    "_try_strip_media_prefix",
    "_guess_media_type_by_path",
    "_try_xor_decrypt_by_magic",
    "_detect_wechat_dat_version",
    "_save_media_keys",
    "_decrypt_wechat_dat_v3",
    "_decrypt_wechat_dat_v4",
    "_load_media_keys",
    "_detect_image_extension",
    "_read_and_maybe_decrypt_media",
    "_ensure_decrypted_resource_for_md5",
    "_collect_all_dat_files",
    "_decrypt_and_save_resource",
    "_convert_silk_to_wav",
    "_looks_like_mp3",
    "_find_ffmpeg_executable",
    "_convert_wav_to_mp3",
    "_convert_silk_to_browser_audio",
]


_MODULE_DIR = Path(__file__).resolve().parent
_PACKAGE_ROOT = _MODULE_DIR.parent if _MODULE_DIR.name == "modules" else _MODULE_DIR
_EMOTICON_MD5_RE = re.compile(r"(?i)^[0-9a-f]{32}$")
_EMOTICON_MD5_ATTR_RE = re.compile(r"(?i)\bmd5\s*=\s*['\"]([0-9a-f]{32})['\"]")
_EMOTICON_MD5_TAG_RE = re.compile(r"(?is)<md5>\s*([0-9a-f]{32})\s*</md5>")
_EMOTICON_EXTERN_MD5_ATTR_RE = re.compile(r"(?i)\bextern_?md5\s*=\s*['\"]([0-9a-f]{32})['\"]")
_EMOTICON_EXTERN_MD5_TAG_RE = re.compile(r"(?is)<extern_?md5>\s*([0-9a-f]{32})\s*</extern_?md5>")
_EMOTICON_AES_KEY_ATTR_RE = re.compile(r"(?i)\baes_?key\s*=\s*['\"]([0-9a-f]{32})['\"]")
_EMOTICON_AES_KEY_TAG_RE = re.compile(r"(?is)<aes_?key>\s*([0-9a-f]{32})\s*</aes_?key>")
_EMOTICON_HTTP_URL_RE = re.compile(r"(?i)https?://[^\s<>\"']+")


class MediaError(Exception):
    def __init__(self, status_code: int = 500, detail: str = ""):
        super().__init__(detail or str(status_code))
        self.status_code = status_code
        self.detail = detail or str(status_code)


def _detect_image_media_type(data: bytes) -> str:
    if not data:
        return "application/octet-stream"

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff") and len(data) >= 4:
        marker = data[3]
        if marker not in (0x00, 0xFF) and marker >= 0xC0:
            return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _is_probably_valid_image(data: bytes, media_type: str) -> bool:
    if not data:
        return False

    mt = str(media_type or "").strip().lower()
    if not mt.startswith("image/"):
        return False

    if mt == "image/jpeg":
        if _detect_image_media_type(data[:32]) != "image/jpeg":
            return False
        trimmed = data.rstrip(b"\x00")
        if len(trimmed) < 4 or not trimmed.startswith(b"\xff\xd8\xff"):
            return False
        if trimmed.endswith(b"\xff\xd9"):
            return True
        tail = trimmed[-4096:] if len(trimmed) > 4096 else trimmed
        i = tail.rfind(b"\xff\xd9")
        return i >= 0 and i >= len(tail) - 64 - 2

    if mt == "image/png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        trailer = b"\x00\x00\x00\x00IEND\xaeB`\x82"
        trimmed = data.rstrip(b"\x00")
        if trimmed.endswith(trailer):
            return True
        tail = trimmed[-256:] if len(trimmed) > 256 else trimmed
        i = tail.rfind(trailer)
        return i >= 0 and i >= len(tail) - 64 - len(trailer)

    if mt == "image/gif":
        if not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
            return False
        trimmed = data.rstrip(b"\x00")
        if trimmed.endswith(b"\x3B"):
            return True
        tail = trimmed[-256:] if len(trimmed) > 256 else trimmed
        i = tail.rfind(b"\x3B")
        return i >= 0 and i >= len(tail) - 16 - 1

    if mt == "image/webp":
        if len(data) < 12:
            return False
        return bool(data.startswith(b"RIFF") and data[8:12] == b"WEBP")

    return _detect_image_media_type(data[:32]) != "application/octet-stream"


def _is_safe_http_url(url: str) -> bool:
    u = str(url or "").strip()
    if not u:
        return False
    try:
        p = urlparse(u)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").strip()
    if not host:
        return False
    if host in {"localhost"}:
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return False
    except Exception:
        pass
    return True


def _download_http_bytes(url: str, *, timeout: int = 20, max_bytes: int = 30 * 1024 * 1024) -> bytes:
    if not _is_safe_http_url(url):
        raise MediaError(status_code=400, detail="Unsafe URL.")

    try:
        import requests
    except Exception as e:
        raise MediaError(status_code=500, detail=f"requests not available: {e}")

    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            try:
                cl = int(r.headers.get("content-length") or 0)
                if cl and cl > int(max_bytes):
                    raise MediaError(status_code=413, detail="Remote file too large.")
            except MediaError:
                raise
            except Exception:
                pass

            chunks: list[bytes] = []
            total = 0
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total > int(max_bytes):
                    raise MediaError(status_code=413, detail="Remote file too large.")
            return b"".join(chunks)
    except MediaError:
        raise
    except Exception as e:
        raise MediaError(status_code=502, detail=f"Download failed: {e}")


def _decrypt_emoticon_aes_cbc(data: bytes, aes_key_hex: str) -> Optional[bytes]:
    if not data:
        return None
    if len(data) % 16 != 0:
        return None

    khex = str(aes_key_hex or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", khex):
        return None

    try:
        key = bytes.fromhex(khex)
        if len(key) != 16:
            return None
    except Exception:
        return None

    try:
        from Crypto.Cipher import AES
        from Crypto.Util import Padding

        pt_padded = AES.new(key, AES.MODE_CBC, iv=key).decrypt(data)
        pt = Padding.unpad(pt_padded, AES.block_size)
        return pt
    except Exception:
        return None


def _normalize_emoticon_md5(value: Any) -> str:
    md5 = str(value or "").strip().lower()
    return md5 if _EMOTICON_MD5_RE.fullmatch(md5) else ""


def _normalize_emoticon_aes_key(value: Any) -> str:
    key = str(value or "").strip().lower()
    return key if _EMOTICON_MD5_RE.fullmatch(key) else ""


def _first_emoticon_match(text: str, patterns: tuple[re.Pattern[str], ...]) -> str:
    if not text:
        return ""
    for pattern in patterns:
        try:
            match = pattern.search(text)
        except Exception:
            match = None
        if match:
            return str(match.group(1) or "").strip()
    return ""


def _extract_emoticon_message_md5(text: str) -> str:
    return _normalize_emoticon_md5(_first_emoticon_match(text, (_EMOTICON_MD5_ATTR_RE, _EMOTICON_MD5_TAG_RE)))


def _extract_emoticon_message_extern_md5(text: str) -> str:
    return _normalize_emoticon_md5(
        _first_emoticon_match(text, (_EMOTICON_EXTERN_MD5_ATTR_RE, _EMOTICON_EXTERN_MD5_TAG_RE))
    )


def _extract_emoticon_message_aes_key(text: str) -> str:
    return _normalize_emoticon_aes_key(_first_emoticon_match(text, (_EMOTICON_AES_KEY_ATTR_RE, _EMOTICON_AES_KEY_TAG_RE)))


def _extract_emoticon_message_urls(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for match in _EMOTICON_HTTP_URL_RE.finditer(text):
        url = str(match.group(0) or "").strip()
        if not url or url in seen or not _is_safe_http_url(url):
            continue
        seen.add(url)
        out.append(url)
    return out


def _emoticon_message_db_paths(account_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in Path(account_dir).glob("message_*.db")
        if p.is_file() and p.name.lower() != "message_resource.db"
    )


def _emoticon_source_fingerprint(account_dir: Path) -> str:
    parts: list[str] = []
    paths = [Path(account_dir) / "emoticon.db", *_emoticon_message_db_paths(account_dir)]
    for path in paths:
        try:
            st = path.stat()
            parts.append(f"{path.name}:{st.st_size}:{st.st_mtime_ns}")
        except Exception:
            parts.append(f"{path.name}:missing")
    return "|".join(parts)


def _list_emoticon_message_tables(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except Exception:
        return []
    out: list[str] = []
    for row in rows:
        if not row:
            continue
        raw_name = row[0]
        if isinstance(raw_name, memoryview):
            raw_name = raw_name.tobytes()
        if isinstance(raw_name, (bytes, bytearray)):
            try:
                name = bytes(raw_name).decode("utf-8", errors="ignore")
            except Exception:
                continue
        else:
            name = str(raw_name or "")
        if name.lower().startswith(("msg_", "chat_")):
            out.append(name)
    return out


def _quote_sqlite_ident(name: str) -> str:
    return '"' + str(name or "").replace('"', '""') + '"'


def _iter_emoticon_varints(data: bytes) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    i = 0
    n = len(data)
    while i < n:
        key = int(data[i])
        i += 1
        field = key >> 3
        wire_type = key & 0x07
        if field <= 0:
            break

        if wire_type == 0:
            shift = 0
            value = 0
            while i < n:
                b = int(data[i])
                i += 1
                value |= (b & 0x7F) << shift
                if b < 0x80:
                    break
                shift += 7
            out.append((field, int(value)))
            continue

        if wire_type == 1:
            i += 8
            continue

        if wire_type == 2:
            shift = 0
            ln = 0
            while i < n:
                b = int(data[i])
                i += 1
                ln |= (b & 0x7F) << shift
                if b < 0x80:
                    break
                shift += 7
            i += int(ln)
            continue

        if wire_type == 5:
            i += 4
            continue

        break
    return out


def _extract_emoticon_builtin_expr_id(packed_info_data: Any) -> Optional[int]:
    data: bytes = b""
    if packed_info_data is None:
        return None
    if isinstance(packed_info_data, memoryview):
        data = packed_info_data.tobytes()
    elif isinstance(packed_info_data, (bytes, bytearray)):
        data = bytes(packed_info_data)
    elif isinstance(packed_info_data, str):
        s = packed_info_data.strip()
        if s:
            try:
                data = bytes.fromhex(s) if (len(s) % 2 == 0 and re.fullmatch(r"(?i)[0-9a-f]+", s)) else s.encode(
                    "utf-8",
                    errors="ignore",
                )
            except Exception:
                data = b""
    if not data:
        return None

    for field, value in _iter_emoticon_varints(data):
        if field == 2:
            return int(value)
    return None


@lru_cache(maxsize=2048)
def _lookup_emoticon_info(account_dir_str: str, md5: str) -> dict[str, str]:
    account_dir = Path(account_dir_str)
    md5s = str(md5 or "").strip().lower()
    if not md5s:
        return {}

    db_path = account_dir / "emoticon.db"
    if not db_path.exists():
        return {}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT md5, extern_md5, aes_key, cdn_url, encrypt_url, extern_url, thumb_url, tp_url "
            "FROM kNonStoreEmoticonTable "
            "WHERE lower(md5) = lower(?) OR lower(extern_md5) = lower(?) "
            "LIMIT 1",
            (md5s, md5s),
        ).fetchone()
        if not row:
            return {}
        return {k: str(row[k] or "") for k in row.keys()}
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _merge_emoticon_candidate(
    catalog: dict[str, dict[str, Any]],
    md5: str,
    *,
    urls: Optional[list[str]] = None,
    aes_key: str = "",
    source: str = "",
) -> None:
    md5s = _normalize_emoticon_md5(md5)
    if not md5s:
        return

    entry = catalog.get(md5s)
    if entry is None:
        entry = {"md5": md5s, "urls": [], "aes_keys": [], "sources": []}
        catalog[md5s] = entry

    if source and source not in entry["sources"]:
        entry["sources"].append(source)

    key = _normalize_emoticon_aes_key(aes_key)
    if key and key not in entry["aes_keys"]:
        entry["aes_keys"].append(key)

    seen = set(entry["urls"])
    for url in urls or []:
        u = str(url or "").strip()
        if not u or u in seen or not _is_safe_http_url(u):
            continue
        seen.add(u)
        entry["urls"].append(u)


def _emoticon_catalog_public_stats(
    stats: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    *,
    elapsed_ms: float,
) -> dict[str, Any]:
    source_counts: dict[str, int] = {}
    with_urls = 0
    for entry in catalog.values():
        if entry.get("urls"):
            with_urls += 1
        for source in entry.get("sources") or []:
            source_counts[source] = source_counts.get(source, 0) + 1

    return {
        "emoticon_db_rows": int(stats.get("emoticon_db_rows") or 0),
        "emoticon_db_md5": int(stats.get("emoticon_db_md5") or 0),
        "emoticon_db_extern_md5": int(stats.get("emoticon_db_extern_md5") or 0),
        "emoticon_db_with_remote": int(stats.get("emoticon_db_with_remote") or 0),
        "message_db_count": int(stats.get("message_db_count") or 0),
        "message_table_count": int(stats.get("message_table_count") or 0),
        "message_xml_rows": int(stats.get("message_xml_rows") or 0),
        "message_xml_md5": int(stats.get("message_xml_md5") or 0),
        "message_xml_md5_with_url": int(stats.get("message_xml_md5_with_url") or 0),
        "message_xml_extern_md5": int(stats.get("message_xml_extern_md5") or 0),
        "message_builtin_expr_ids": int(stats.get("message_builtin_expr_ids") or 0),
        "message_builtin_expr_rows": int(stats.get("message_builtin_expr_rows") or 0),
        "total_candidates": len(catalog),
        "total_candidates_with_url": with_urls,
        "source_counts": source_counts,
        "elapsed_ms": round(float(elapsed_ms), 1),
    }


@lru_cache(maxsize=8)
def _collect_emoticon_download_catalog_cached(
    account_dir_str: str,
    fingerprint: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    started_at = datetime.datetime.now().timestamp()
    account_dir = Path(account_dir_str)
    catalog: dict[str, dict[str, Any]] = {}
    stats: dict[str, Any] = {}
    emoticon_primary: set[str] = set()
    emoticon_extern: set[str] = set()
    emoticon_with_remote: set[str] = set()
    message_md5: set[str] = set()
    message_md5_with_url: set[str] = set()
    message_extern_md5: set[str] = set()
    builtin_expr_ids: set[int] = set()
    builtin_expr_rows = 0
    message_rows = 0
    message_table_count = 0

    db_path = account_dir / "emoticon.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
        except Exception as exc:
            conn = None
            logger.warning("[media] emoticon_catalog emoticon_db_open_failed: account=%s error=%s", account_dir.name, exc)
        if conn is None:
            rows = []
        else:
            rows = None
        if conn is not None:
            conn.row_factory = sqlite3.Row
        if conn is not None:
            try:
                rows = conn.execute(
                    "SELECT md5, extern_md5, aes_key, cdn_url, encrypt_url, extern_url, thumb_url, tp_url "
                    "FROM kNonStoreEmoticonTable ORDER BY rowid DESC"
                ).fetchall()
            except Exception as exc:
                logger.warning(
                    "[media] emoticon_catalog emoticon_db_scan_failed: account=%s error=%s",
                    account_dir.name,
                    exc,
                )
                rows = []
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        stats["emoticon_db_rows"] = len(rows or [])
        for row in rows or []:
            urls = [
                str(row[key] or "").strip()
                for key in ("cdn_url", "extern_url", "thumb_url", "tp_url", "encrypt_url")
                if str(row[key] or "").strip() and _is_safe_http_url(str(row[key] or "").strip())
            ]
            aes_key = str(row["aes_key"] or "").strip()
            md5s = _normalize_emoticon_md5(row["md5"])
            extern_md5 = _normalize_emoticon_md5(row["extern_md5"])
            if md5s:
                emoticon_primary.add(md5s)
                if urls:
                    emoticon_with_remote.add(md5s)
                    _merge_emoticon_candidate(catalog, md5s, urls=urls, aes_key=aes_key, source="emoticon_db_md5")
            if extern_md5:
                emoticon_extern.add(extern_md5)
                if urls:
                    emoticon_with_remote.add(extern_md5)
                    _merge_emoticon_candidate(
                        catalog,
                        extern_md5,
                        urls=urls,
                        aes_key=aes_key,
                        source="emoticon_db_extern_md5",
                    )

    message_db_paths = _emoticon_message_db_paths(account_dir)
    for message_db_path in message_db_paths:
        try:
            conn = sqlite3.connect(str(message_db_path))
        except Exception as exc:
            logger.warning(
                "[media] emoticon_catalog message_db_open_failed: account=%s db=%s error=%s",
                account_dir.name,
                message_db_path.name,
                exc,
            )
            continue
        conn.row_factory = sqlite3.Row
        try:
            for table_name in _list_emoticon_message_tables(conn):
                message_table_count += 1
                quoted = _quote_sqlite_ident(table_name)
                try:
                    rows = conn.execute(
                        f"SELECT compress_content, message_content, packed_info_data FROM {quoted} WHERE local_type = 47"
                    )
                except Exception:
                    continue

                for row in rows:
                    message_rows += 1
                    try:
                        builtin_id = _extract_emoticon_builtin_expr_id(row["packed_info_data"])
                    except Exception:
                        builtin_id = None
                    if builtin_id is not None:
                        builtin_expr_rows += 1
                        builtin_expr_ids.add(int(builtin_id))

                    try:
                        raw_text = _decode_message_content(row["compress_content"], row["message_content"])
                    except Exception:
                        raw_text = ""
                    md5s = _extract_emoticon_message_md5(raw_text)
                    if not md5s:
                        continue
                    message_md5.add(md5s)

                    extern_md5 = _extract_emoticon_message_extern_md5(raw_text)
                    if extern_md5:
                        message_extern_md5.add(extern_md5)

                    if md5s in message_md5_with_url:
                        continue

                    urls = _extract_emoticon_message_urls(raw_text)
                    if not urls:
                        continue
                    message_md5_with_url.add(md5s)
                    _merge_emoticon_candidate(
                        catalog,
                        md5s,
                        urls=urls,
                        aes_key=_extract_emoticon_message_aes_key(raw_text),
                        source="message_xml",
                    )
        except Exception as exc:
            logger.warning(
                "[media] emoticon_catalog message_db_scan_failed: account=%s db=%s error=%s",
                account_dir.name,
                message_db_path.name,
                exc,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    stats.update(
        {
            "fingerprint": fingerprint,
            "emoticon_db_md5": len(emoticon_primary),
            "emoticon_db_extern_md5": len(emoticon_extern),
            "emoticon_db_with_remote": len(emoticon_with_remote),
            "message_db_count": len(message_db_paths),
            "message_table_count": message_table_count,
            "message_xml_rows": message_rows,
            "message_xml_md5": len(message_md5),
            "message_xml_md5_with_url": len(message_md5_with_url),
            "message_xml_extern_md5": len(message_extern_md5),
            "message_builtin_expr_ids": len(builtin_expr_ids),
            "message_builtin_expr_rows": builtin_expr_rows,
        }
    )
    elapsed_ms = (datetime.datetime.now().timestamp() - started_at) * 1000.0
    public_stats = _emoticon_catalog_public_stats(stats, catalog, elapsed_ms=elapsed_ms)
    logger.info(
        "[media] emoticon_catalog scan_done: account=%s total_candidates=%s source_counts=%s "
        "emoticon_db_rows=%s emoticon_db_md5=%s emoticon_db_extern_md5=%s message_rows=%s "
        "message_md5=%s message_md5_with_url=%s message_extern_md5=%s builtin_expr_ids=%s elapsed_ms=%s",
        account_dir.name,
        public_stats["total_candidates"],
        public_stats["source_counts"],
        public_stats["emoticon_db_rows"],
        public_stats["emoticon_db_md5"],
        public_stats["emoticon_db_extern_md5"],
        public_stats["message_xml_rows"],
        public_stats["message_xml_md5"],
        public_stats["message_xml_md5_with_url"],
        public_stats["message_xml_extern_md5"],
        public_stats["message_builtin_expr_ids"],
        public_stats["elapsed_ms"],
    )
    return catalog, public_stats


def _collect_emoticon_download_catalog(account_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    fingerprint = _emoticon_source_fingerprint(Path(account_dir))
    return _collect_emoticon_download_catalog_cached(str(Path(account_dir)), fingerprint)


def _collect_emoticon_download_candidates(account_dir: Path) -> list[str]:
    catalog, _stats = _collect_emoticon_download_catalog(Path(account_dir))
    return list(catalog.keys())


def _find_emoticon_message_remote_source(account_dir: Path, md5: str) -> dict[str, Any]:
    md5s = _normalize_emoticon_md5(md5)
    if not md5s:
        return {}

    for message_db_path in _emoticon_message_db_paths(Path(account_dir)):
        try:
            conn = sqlite3.connect(str(message_db_path))
        except Exception:
            continue
        conn.row_factory = sqlite3.Row
        try:
            for table_name in _list_emoticon_message_tables(conn):
                quoted = _quote_sqlite_ident(table_name)
                try:
                    rows = conn.execute(
                        f"SELECT compress_content, message_content FROM {quoted} WHERE local_type = 47"
                    )
                except Exception:
                    continue

                for row in rows:
                    try:
                        raw_text = _decode_message_content(row["compress_content"], row["message_content"])
                    except Exception:
                        raw_text = ""
                    if _extract_emoticon_message_md5(raw_text) != md5s:
                        continue
                    urls = _extract_emoticon_message_urls(raw_text)
                    if not urls:
                        continue
                    aes_key = _extract_emoticon_message_aes_key(raw_text)
                    out = {"md5": md5s, "urls": urls, "aes_keys": [], "sources": ["message_xml"]}
                    if aes_key:
                        out["aes_keys"].append(aes_key)
                    return out
        except Exception:
            continue
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return {}


def _try_fetch_emoticon_from_sources(urls: list[str], aes_keys: list[str]) -> tuple[Optional[bytes], Optional[str]]:
    for url in urls:
        try:
            payload = _download_http_bytes(url)
        except Exception:
            continue

        candidates: list[bytes] = [payload]
        for aes_key_hex in aes_keys:
            dec = _decrypt_emoticon_aes_cbc(payload, aes_key_hex)
            if dec is not None:
                candidates.insert(0, dec)

        for data in candidates:
            if not data:
                continue
            try:
                data2, mt = _try_strip_media_prefix(data)
            except Exception:
                data2, mt = data, "application/octet-stream"

            if mt == "application/octet-stream":
                mt = _detect_image_media_type(data2[:32])
            if mt == "application/octet-stream":
                try:
                    if len(data2) >= 8 and data2[4:8] == b"ftyp":
                        mt = "video/mp4"
                except Exception:
                    pass

            if mt.startswith("image/") and (not _is_probably_valid_image(data2, mt)):
                continue
            if mt != "application/octet-stream":
                return data2, mt

    return None, None


def _try_fetch_emoticon_from_remote(
    account_dir: Path,
    md5: str,
    source: Optional[dict[str, Any]] = None,
) -> tuple[Optional[bytes], Optional[str]]:
    md5s = _normalize_emoticon_md5(md5)
    if not md5s:
        return None, None

    urls: list[str] = []
    aes_keys: list[str] = []

    if source:
        for u in source.get("urls") or []:
            u = str(u or "").strip()
            if u and u not in urls and _is_safe_http_url(u):
                urls.append(u)
        for key in source.get("aes_keys") or []:
            key = _normalize_emoticon_aes_key(key)
            if key and key not in aes_keys:
                aes_keys.append(key)
    else:
        info = _lookup_emoticon_info(str(account_dir), md5s)
        if info:
            for key in ("cdn_url", "extern_url", "thumb_url", "tp_url", "encrypt_url"):
                u = str(info.get(key) or "").strip()
                if u and u not in urls and _is_safe_http_url(u):
                    urls.append(u)
            aes_key = _normalize_emoticon_aes_key(info.get("aes_key"))
            if aes_key:
                aes_keys.append(aes_key)

    data, media_type = _try_fetch_emoticon_from_sources(urls, aes_keys)
    if data is not None and media_type:
        return data, media_type

    if source:
        return None, None

    message_source = _find_emoticon_message_remote_source(Path(account_dir), md5s)
    if not message_source:
        return None, None

    message_urls = [str(u or "").strip() for u in message_source.get("urls") or []]
    message_aes_keys = [
        _normalize_emoticon_aes_key(key) for key in (message_source.get("aes_keys") or []) if key
    ]
    return _try_fetch_emoticon_from_sources(
        [u for u in message_urls if u and _is_safe_http_url(u)],
        [k for k in message_aes_keys if k],
    )


class _WxAMConfig(ctypes.Structure):
    _fields_ = [
        ("mode", ctypes.c_int),
        ("reserved", ctypes.c_int),
    ]


@lru_cache(maxsize=1)
def _get_wxam_decoder():
    if os.name != "nt":
        return None
    dll_path = _PACKAGE_ROOT / "native" / "VoipEngine.dll"
    if not dll_path.exists():
        logger.warning(f"WxAM decoder DLL not found: {dll_path}")
        return None
    try:
        voip_engine = ctypes.WinDLL(str(dll_path))
        fn = voip_engine.wxam_dec_wxam2pic_5
        fn.argtypes = [
            ctypes.c_int64,
            ctypes.c_int,
            ctypes.c_int64,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_int64,
        ]
        fn.restype = ctypes.c_int64
        logger.info(f"WxAM decoder loaded: {dll_path}")
        return fn
    except Exception as e:
        logger.warning(f"Failed to load WxAM decoder DLL: {dll_path} ({e})")
        return None


def _wxgf_to_image_bytes(data: bytes) -> Optional[bytes]:
    if not data or not data.startswith(b"wxgf"):
        return None
    fn = _get_wxam_decoder()
    if fn is None:
        return None

    max_output_size = 52 * 1024 * 1024
    for mode in (0, 3):
        try:
            config = _WxAMConfig()
            config.mode = int(mode)
            config.reserved = 0

            input_buffer = ctypes.create_string_buffer(data, len(data))
            output_buffer = ctypes.create_string_buffer(max_output_size)
            output_size = ctypes.c_int(max_output_size)

            result = fn(
                ctypes.addressof(input_buffer),
                int(len(data)),
                ctypes.addressof(output_buffer),
                ctypes.byref(output_size),
                ctypes.addressof(config),
            )
            if result != 0 or output_size.value <= 0:
                continue
            out = output_buffer.raw[: int(output_size.value)]
            if _detect_image_media_type(out[:32]) != "application/octet-stream":
                return out
        except Exception:
            continue
    return None


def _try_strip_media_prefix(data: bytes) -> tuple[bytes, str]:
    if not data:
        return data, "application/octet-stream"

    try:
        head = data[: min(len(data), 256 * 1024)]
    except Exception:
        head = data

    try:
        idx = head.find(b"wxgf")
    except Exception:
        idx = -1
    if idx >= 0 and idx <= 128 * 1024:
        try:
            payload = data[idx:]
            converted = _wxgf_to_image_bytes(payload)
            if converted:
                mtw = _detect_image_media_type(converted[:32])
                if mtw != "application/octet-stream":
                    return converted, mtw
        except Exception:
            pass

    sigs: list[tuple[bytes, str]] = [
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
    ]
    for sig, mt in sigs:
        try:
            j = head.find(sig)
        except Exception:
            j = -1
        if j >= 0 and j <= 128 * 1024:
            sliced = data[j:]
            mt2 = _detect_image_media_type(sliced[:32])
            if mt2 != "application/octet-stream" and _is_probably_valid_image(sliced, mt2):
                return sliced, mt2

    try:
        j = head.find(b"RIFF")
    except Exception:
        j = -1
    if j >= 0 and j <= 128 * 1024:
        sliced = data[j:]
        try:
            if len(sliced) >= 12 and sliced[8:12] == b"WEBP":
                return sliced, "image/webp"
        except Exception:
            pass

    try:
        j = head.find(b"ftyp")
    except Exception:
        j = -1
    if j >= 4 and j <= 128 * 1024:
        sliced = data[j - 4 :]
        try:
            if len(sliced) >= 8 and sliced[4:8] == b"ftyp":
                return sliced, "video/mp4"
        except Exception:
            pass

    return data, "application/octet-stream"


def _guess_media_type_by_path(path: Path, fallback: str = "application/octet-stream") -> str:
    try:
        mt = mimetypes.guess_type(str(path.name))[0]
        if mt:
            return mt
    except Exception:
        pass
    return fallback


def _try_xor_decrypt_by_magic(data: bytes) -> tuple[Optional[bytes], Optional[str]]:
    if not data:
        return None, None

    candidates: list[tuple[int, bytes, str]] = [
        (0, b"\x89PNG\r\n\x1a\n", "image/png"),
        (0, b"GIF87a", "image/gif"),
        (0, b"GIF89a", "image/gif"),
        (0, b"RIFF", "application/octet-stream"),
        (4, b"ftyp", "video/mp4"),
        (0, b"wxgf", "application/octet-stream"),
        (1, b"wxgf", "application/octet-stream"),
        (2, b"wxgf", "application/octet-stream"),
        (3, b"wxgf", "application/octet-stream"),
        (4, b"wxgf", "application/octet-stream"),
        (5, b"wxgf", "application/octet-stream"),
        (6, b"wxgf", "application/octet-stream"),
        (7, b"wxgf", "application/octet-stream"),
        (8, b"wxgf", "application/octet-stream"),
        (9, b"wxgf", "application/octet-stream"),
        (10, b"wxgf", "application/octet-stream"),
        (11, b"wxgf", "application/octet-stream"),
        (12, b"wxgf", "application/octet-stream"),
        (13, b"wxgf", "application/octet-stream"),
        (14, b"wxgf", "application/octet-stream"),
        (15, b"wxgf", "application/octet-stream"),
        (0, b"\xff\xd8\xff", "image/jpeg"),
    ]

    for offset, magic, mt in candidates:
        if len(data) < offset + len(magic):
            continue
        key = data[offset] ^ magic[0]
        ok = True
        for i in range(len(magic)):
            if (data[offset + i] ^ key) != magic[i]:
                ok = False
                break
        if not ok:
            continue

        decoded = bytes(b ^ key for b in data)

        if magic == b"wxgf":
            try:
                payload = decoded[offset:] if offset > 0 else decoded
                converted = _wxgf_to_image_bytes(payload)
                if converted:
                    mtw = _detect_image_media_type(converted[:32])
                    if mtw != "application/octet-stream":
                        return converted, mtw
            except Exception:
                pass
            continue

        if offset == 0 and magic == b"RIFF":
            if len(decoded) >= 12 and decoded[8:12] == b"WEBP":
                if _is_probably_valid_image(decoded, "image/webp"):
                    return decoded, "image/webp"
            continue

        if mt == "video/mp4":
            try:
                if len(decoded) >= 8 and decoded[4:8] == b"ftyp":
                    return decoded, "video/mp4"
            except Exception:
                pass
            continue

        mt2 = _detect_image_media_type(decoded[:32])
        if mt2 != mt:
            continue
        if not _is_probably_valid_image(decoded, mt2):
            continue
        return decoded, mt2

    preview_len = 8192
    try:
        preview_len = min(int(preview_len), int(len(data)))
    except Exception:
        preview_len = 8192

    if preview_len > 0:
        for key in range(256):
            try:
                pv = bytes(b ^ key for b in data[:preview_len])
            except Exception:
                continue
            try:
                scan = pv
                if (
                    (scan.find(b"wxgf") >= 0)
                    or (scan.find(b"\x89PNG\r\n\x1a\n") >= 0)
                    or (scan.find(b"\xff\xd8\xff") >= 0)
                    or (scan.find(b"GIF87a") >= 0)
                    or (scan.find(b"GIF89a") >= 0)
                    or (scan.find(b"RIFF") >= 0)
                    or (scan.find(b"ftyp") >= 0)
                ):
                    decoded = bytes(b ^ key for b in data)
                    dec2, mt2 = _try_strip_media_prefix(decoded)
                    if mt2 != "application/octet-stream":
                        if mt2.startswith("image/") and (not _is_probably_valid_image(dec2, mt2)):
                            continue
                        return dec2, mt2
            except Exception:
                continue

    return None, None


def _detect_wechat_dat_version(data: bytes) -> int:
    if not data or len(data) < 6:
        return -1
    sig = data[:6]
    if sig == b"\x07\x08V1\x08\x07":
        return 1
    if sig == b"\x07\x08V2\x08\x07":
        return 2
    return 0


def _save_media_keys(account_dir: Path, xor_key: int, aes_key16: Optional[bytes] = None) -> None:
    try:
        aes_str = ""
        if aes_key16:
            try:
                aes_str = aes_key16.decode("ascii", errors="ignore")[:16]
            except Exception:
                aes_str = ""
        payload = {
            "xor": int(xor_key),
            "aes": aes_str,
        }
        (account_dir / "_media_keys.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _decrypt_wechat_dat_v3(data: bytes, xor_key: int) -> bytes:
    return bytes(b ^ xor_key for b in data)


def _decrypt_wechat_dat_v4(data: bytes, xor_key: int, aes_key: bytes) -> bytes:
    from Crypto.Cipher import AES
    from Crypto.Util import Padding

    header, rest = data[:0xF], data[0xF:]
    signature, aes_size, xor_size = struct.unpack("<6sLLx", header)
    aes_size += AES.block_size - aes_size % AES.block_size

    aes_data = rest[:aes_size]
    raw_data = rest[aes_size:]

    cipher = AES.new(aes_key[:16], AES.MODE_ECB)
    decrypted_data = Padding.unpad(cipher.decrypt(aes_data), AES.block_size)

    if xor_size > 0:
        raw_data = rest[aes_size:-xor_size]
        xor_data = rest[-xor_size:]
        xored_data = bytes(b ^ xor_key for b in xor_data)
    else:
        xored_data = b""

    return decrypted_data + raw_data + xored_data


def _load_media_keys(account_dir: Path) -> dict[str, Any]:
    try:
        from .key_store import get_account_keys_from_store, normalize_key_store_path

        verified_keys = get_account_keys_from_store(Path(account_dir).name)
        verified_xor_raw = str(verified_keys.get("image_xor_key") or "").strip()
        verified_aes = str(verified_keys.get("image_aes_key") or "").strip()[:16]
        stored_source = normalize_key_store_path(verified_keys.get("image_key_source_wxid_dir"))
        resolved_source_path = _resolve_account_wxid_dir(account_dir)
        resolved_source = normalize_key_store_path(
            str(resolved_source_path) if resolved_source_path is not None else ""
        )
        source_matches = bool(stored_source and resolved_source and stored_source == resolved_source)
        if (
            verified_keys.get("image_key_verified") is True
            and source_matches
            and verified_xor_raw
            and len(verified_aes) == 16
        ):
            if verified_xor_raw.lower().startswith("0x"):
                verified_xor = int(verified_xor_raw[2:], 16)
            else:
                try:
                    verified_xor = int(verified_xor_raw, 16)
                except ValueError:
                    verified_xor = int(verified_xor_raw)
            if 0 <= verified_xor <= 0xFF:
                return {
                    "xor": verified_xor,
                    "aes": verified_aes,
                    "verified": True,
                    "source": str(verified_keys.get("image_key_source") or "verified_store"),
                    "source_wxid_dir": str(verified_keys.get("image_key_source_wxid_dir") or ""),
                    "derived_wxid": str(verified_keys.get("image_key_derived_wxid") or ""),
                    "code": verified_keys.get("image_key_code"),
                }
    except Exception:
        pass

    p = account_dir / "_media_keys.json"
    data: dict[str, Any] = {}
    if not p.exists():
        data = {}
    else:
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            data = {}

    if data.get("xor") is None or not str(data.get("aes") or "").strip():
        try:
            from .key_store import get_account_keys_from_store

            keys = get_account_keys_from_store(Path(account_dir).name)
            if isinstance(keys, dict):
                if keys.get("image_key_verified") is True:
                    keys = {}
                if data.get("xor") is None:
                    xor_raw = str(keys.get("image_xor_key") or keys.get("xor_key") or "").strip()
                    if xor_raw:
                        if xor_raw.lower().startswith("0x"):
                            data["xor"] = int(xor_raw[2:], 16)
                        else:
                            try:
                                data["xor"] = int(xor_raw, 16)
                            except Exception:
                                data["xor"] = int(xor_raw)
                if not str(data.get("aes") or "").strip():
                    aes_raw = str(keys.get("image_aes_key") or keys.get("aes_key") or "").strip()
                    if aes_raw:
                        data["aes"] = aes_raw[:16]
        except Exception:
            pass
    data.setdefault("verified", False)
    data.setdefault("source", "media_cache")
    return data


def _detect_image_extension(data: bytes) -> str:
    if not data:
        return "dat"
    head = data[:32] if len(data) > 32 else data
    mt = _detect_image_media_type(head)
    if mt == "image/png":
        return "png"
    if mt == "image/jpeg":
        return "jpg"
    if mt == "image/gif":
        return "gif"
    if mt == "image/webp":
        return "webp"
    return "dat"


def _read_and_maybe_decrypt_media(
    path: Path,
    account_dir: Optional[Path] = None,
    weixin_root: Optional[Path] = None,
) -> tuple[bytes, str]:
    with open(path, "rb") as f:
        head = f.read(64)

    mt = _detect_image_media_type(head)
    if mt != "application/octet-stream":
        return path.read_bytes(), mt

    if head.startswith(b"wxgf"):
        data0 = path.read_bytes()
        converted0 = _wxgf_to_image_bytes(data0)
        if converted0:
            mt0 = _detect_image_media_type(converted0[:32])
            if mt0 != "application/octet-stream":
                return converted0, mt0

    try:
        idx = head.find(b"wxgf")
    except Exception:
        idx = -1
    if 0 < idx <= 4:
        try:
            data0 = path.read_bytes()
            payload0 = data0[idx:]
            converted0 = _wxgf_to_image_bytes(payload0)
            if converted0:
                mt0 = _detect_image_media_type(converted0[:32])
                if mt0 != "application/octet-stream":
                    return converted0, mt0
        except Exception:
            pass

    try:
        data_pref = path.read_bytes()
        stripped, mtp = _try_strip_media_prefix(data_pref)
        if mtp != "application/octet-stream":
            if mtp.startswith("image/") and (not _is_probably_valid_image(stripped, mtp)):
                pass
            else:
                return stripped, mtp
    except Exception:
        pass

    data = path.read_bytes()

    version = _detect_wechat_dat_version(data)
    if version in (0, 1, 2):
        xor_key: Optional[int] = None
        aes_key16 = b""
        if account_dir is not None:
            try:
                keys2 = _load_media_keys(account_dir)

                x2 = keys2.get("xor")
                if x2 is not None:
                    xor_key = int(x2)
                    if not (0 <= int(xor_key) <= 255):
                        xor_key = None
                    else:
                        logger.debug("使用 _media_keys.json 中保存的 xor key")

                aes_str = str(keys2.get("aes") or "").strip()
                if len(aes_str) >= 16:
                    aes_key16 = aes_str[:16].encode("ascii", errors="ignore")
            except Exception:
                xor_key = None
                aes_key16 = b""
        try:
            if version == 0 and xor_key is not None:
                out = _decrypt_wechat_dat_v3(data, xor_key)
                try:
                    out2, mtp2 = _try_strip_media_prefix(out)
                    if mtp2 != "application/octet-stream":
                        return out2, mtp2
                except Exception:
                    pass
                if out.startswith(b"wxgf"):
                    converted = _wxgf_to_image_bytes(out)
                    if converted:
                        out = converted
                        logger.info(f"wxgf->image: {path} -> {len(out)} bytes")
                    else:
                        logger.info(f"wxgf->image failed: {path}")
                mt0 = _detect_image_media_type(out[:32])
                if mt0 != "application/octet-stream":
                    return out, mt0
            elif version == 1 and xor_key is not None:
                out = _decrypt_wechat_dat_v4(data, xor_key, b"cfcd208495d565ef")
                try:
                    out2, mtp2 = _try_strip_media_prefix(out)
                    if mtp2 != "application/octet-stream":
                        return out2, mtp2
                except Exception:
                    pass
                if out.startswith(b"wxgf"):
                    converted = _wxgf_to_image_bytes(out)
                    if converted:
                        out = converted
                        logger.info(f"wxgf->image: {path} -> {len(out)} bytes")
                    else:
                        logger.info(f"wxgf->image failed: {path}")
                mt1 = _detect_image_media_type(out[:32])
                if mt1 != "application/octet-stream":
                    return out, mt1
                return out, "application/octet-stream"
            elif version == 2 and xor_key is not None and aes_key16:
                out = _decrypt_wechat_dat_v4(data, xor_key, aes_key16)
                try:
                    out2, mtp2 = _try_strip_media_prefix(out)
                    if mtp2 != "application/octet-stream":
                        return out2, mtp2
                except Exception:
                    pass
                if out.startswith(b"wxgf"):
                    converted = _wxgf_to_image_bytes(out)
                    if converted:
                        out = converted
                        logger.info(f"wxgf->image: {path} -> {len(out)} bytes")
                    else:
                        logger.info(f"wxgf->image failed: {path}")
                mt2b = _detect_image_media_type(out[:32])
                if mt2b != "application/octet-stream":
                    return out, mt2b
                return out, "application/octet-stream"
        except Exception:
            pass

    if version in (0, -1):
        dec, mt2 = _try_xor_decrypt_by_magic(data)
        if dec is not None and mt2:
            return dec, mt2

    mt3 = _guess_media_type_by_path(path, fallback="application/octet-stream")
    if mt3.startswith("image/") and (not _is_probably_valid_image(data, mt3)):
        mt3 = "application/octet-stream"
    if mt3 == "video/mp4":
        try:
            if not (len(data) >= 8 and data[4:8] == b"ftyp"):
                mt3 = "application/octet-stream"
        except Exception:
            mt3 = "application/octet-stream"
    return data, mt3


def _ensure_decrypted_resource_for_md5(
    account_dir: Path,
    md5: str,
    source_path: Path,
    weixin_root: Optional[Path] = None,
) -> Optional[Path]:
    if not md5 or not source_path:
        return None

    md5_lower = str(md5).lower()
    existing = _try_find_decrypted_resource(account_dir, md5_lower)
    if existing:
        return existing

    try:
        if not source_path.exists() or not source_path.is_file():
            return None
    except Exception:
        return None

    data, mt0 = _read_and_maybe_decrypt_media(source_path, account_dir=account_dir, weixin_root=weixin_root)
    mt2 = str(mt0 or "").strip()
    if (not mt2) or mt2 == "application/octet-stream":
        mt2 = _detect_image_media_type(data[:32])
    if mt2 == "application/octet-stream":
        try:
            data2, mtp = _try_strip_media_prefix(data)
            if mtp != "application/octet-stream":
                data = data2
                mt2 = mtp
        except Exception:
            pass
    if mt2 == "application/octet-stream":
        try:
            if len(data) >= 8 and data[4:8] == b"ftyp":
                mt2 = "video/mp4"
        except Exception:
            pass
    if mt2 == "application/octet-stream":
        return None

    if str(mt2).startswith("image/"):
        ext = _detect_image_extension(data)
    elif str(mt2) == "video/mp4":
        ext = "mp4"
    else:
        ext = Path(str(source_path.name)).suffix.lstrip(".").lower() or "dat"
    output_path = _get_decrypted_resource_path(account_dir, md5_lower, ext)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if not output_path.exists():
            output_path.write_bytes(data)
    except Exception:
        return None

    return output_path


def _collect_all_dat_files(wxid_dir: Path) -> list[tuple[Path, str]]:
    results: list[tuple[Path, str]] = []
    if not wxid_dir or not wxid_dir.exists():
        return results

    search_dirs = [
        wxid_dir / "msg" / "attach",
        wxid_dir / "cache",
    ]

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        try:
            for dat_file in search_dir.rglob("*.dat"):
                if not dat_file.is_file():
                    continue
                stem = dat_file.stem
                md5 = stem.split("_")[0] if "_" in stem else stem
                if len(md5) == 32 and all(c in "0123456789abcdefABCDEF" for c in md5):
                    results.append((dat_file, md5.lower()))
        except Exception as e:
            logger.warning(f"扫描目录失败 {search_dir}: {e}")

    return results


def _decrypt_and_save_resource(
    dat_path: Path,
    md5: str,
    account_dir: Path,
    xor_key: int,
    aes_key: Optional[bytes],
) -> tuple[bool, str]:
    try:
        data = dat_path.read_bytes()
        if not data:
            return False, "文件为空"

        version = _detect_wechat_dat_version(data)
        decrypted: Optional[bytes] = None

        if version == 0:
            decrypted = _decrypt_wechat_dat_v3(data, xor_key)
        elif version == 1:
            decrypted = _decrypt_wechat_dat_v4(data, xor_key, b"cfcd208495d565ef")
        elif version == 2:
            if aes_key and len(aes_key) >= 16:
                decrypted = _decrypt_wechat_dat_v4(data, xor_key, aes_key[:16])
            else:
                return False, "V4-V2版本需要AES密钥"
        else:
            dec, mt = _try_xor_decrypt_by_magic(data)
            if dec:
                decrypted = dec
            else:
                return False, f"未知加密版本: {version}"

        if not decrypted:
            return False, "解密结果为空"

        if decrypted.startswith(b"wxgf"):
            converted = _wxgf_to_image_bytes(decrypted)
            if converted:
                decrypted = converted

        ext = _detect_image_extension(decrypted)
        mt = _detect_image_media_type(decrypted[:32])
        if mt == "application/octet-stream":
            return False, "解密后非有效图片"

        output_path = _get_decrypted_resource_path(account_dir, md5, ext)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(decrypted)

        return True, str(output_path)
    except Exception as e:
        return False, str(e)


def _convert_silk_to_wav(silk_data: bytes) -> bytes:
    import tempfile

    try:
        import pilk
    except ImportError:
        return silk_data

    try:
        with tempfile.NamedTemporaryFile(suffix=".silk", delete=False) as silk_file:
            silk_file.write(silk_data)
            silk_path = silk_file.name

        wav_path = silk_path.replace(".silk", ".wav")

        try:
            pilk.silk_to_wav(silk_path, wav_path, rate=24000)
            with open(wav_path, "rb") as wav_file:
                wav_data = wav_file.read()
            return wav_data
        finally:
            import os

            try:
                os.unlink(silk_path)
            except Exception:
                pass
            try:
                os.unlink(wav_path)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"SILK to WAV conversion failed: {e}")
        return silk_data


def _looks_like_mp3(data: bytes) -> bool:
    if not data:
        return False
    if data.startswith(b"ID3"):
        return True
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


@lru_cache(maxsize=1)
def _find_ffmpeg_executable() -> str:
    import shutil

    env_value = str(os.environ.get("WECHAT_TOOL_FFMPEG") or "").strip()
    if env_value:
        resolved = shutil.which(env_value)
        if resolved:
            return resolved
        candidate = Path(env_value).expanduser()
        if candidate.is_file():
            return str(candidate)

    return shutil.which("ffmpeg") or ""


def _convert_wav_to_mp3(wav_data: bytes) -> bytes:
    import subprocess
    import tempfile

    if not wav_data or not wav_data.startswith(b"RIFF"):
        return b""

    ffmpeg_exe = _find_ffmpeg_executable()
    if not ffmpeg_exe:
        return b""

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            wav_path = tmp_path / "voice.wav"
            mp3_path = tmp_path / "voice.mp3"
            wav_path.write_bytes(wav_data)

            proc = subprocess.run(
                [
                    ffmpeg_exe,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(wav_path),
                    "-vn",
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "4",
                    str(mp3_path),
                ],
                check=False,
                capture_output=True,
            )
            if proc.returncode != 0 or not mp3_path.exists():
                err = proc.stderr.decode("utf-8", errors="ignore").strip()
                if err:
                    logger.warning(f"WAV to MP3 conversion failed: {err}")
                return b""

            mp3_data = mp3_path.read_bytes()
            if _looks_like_mp3(mp3_data):
                return mp3_data
    except Exception as e:
        logger.warning(f"WAV to MP3 conversion failed: {e}")

    return b""


def _convert_silk_to_browser_audio(
    silk_data: bytes,
    *,
    preferred_format: str = "mp3",
) -> tuple[bytes, str, str]:
    data = bytes(silk_data or b"")
    if not data:
        return b"", "silk", "audio/silk"

    if _looks_like_mp3(data):
        return data, "mp3", "audio/mpeg"

    wav_data = data if data.startswith(b"RIFF") else _convert_silk_to_wav(data)
    if wav_data.startswith(b"RIFF"):
        if str(preferred_format or "").strip().lower() == "mp3":
            mp3_data = _convert_wav_to_mp3(wav_data)
            if mp3_data:
                return mp3_data, "mp3", "audio/mpeg"
        return wav_data, "wav", "audio/wav"

    return data, "silk", "audio/silk"
