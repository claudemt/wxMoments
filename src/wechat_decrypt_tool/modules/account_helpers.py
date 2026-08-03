from __future__ import annotations

import base64
import html
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .account_context import resolve_account_context

try:
    import zstandard as zstd
except Exception:
    zstd = None


def _resolve_account_dir(account: Optional[str]) -> Path:
    return resolve_account_context(account).account_dir


def _decode_sqlite_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", errors="ignore")
        except Exception:
            return ""
    return str(value)


def _is_mostly_printable_text(text: str) -> bool:
    sample = str(text or "")[:600]
    if not sample:
        return False
    printable = sum(1 for char in sample if char.isprintable() or char in {"\n", "\r", "\t"})
    return (printable / len(sample)) >= 0.85


def _looks_like_xml(text: str) -> bool:
    value = str(text or "").lstrip()
    if value.startswith('"') and value.endswith('"'):
        value = value.strip('"').lstrip()
    return value.startswith("<")


def _decode_message_content(compress_value: Any, message_value: Any) -> str:
    def decode_text_blob(text: str) -> Optional[str]:
        value = str(text or "").strip()
        if not value:
            return None

        zstd_magic = b"\x28\xb5\x2f\xfd"
        raw: bytes | None = None
        if len(value) >= 16 and len(value) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", value):
            try:
                raw = bytes.fromhex(value)
            except Exception:
                raw = None
        elif len(value) >= 24 and len(value) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/=]+", value):
            try:
                raw = base64.b64decode(value)
            except Exception:
                raw = None

        if raw is None:
            return None

        if zstd is not None and raw.startswith(zstd_magic):
            try:
                decoded = zstd.decompress(raw).decode("utf-8", errors="ignore")
                decoded = html.unescape(decoded.strip())
                if _looks_like_xml(decoded) or _is_mostly_printable_text(decoded):
                    return decoded
            except Exception:
                pass

        decoded = raw.decode("utf-8", errors="ignore")
        decoded = html.unescape(decoded.strip())
        lowered = decoded.lower()
        if _looks_like_xml(decoded) or ("<msg" in lowered and "</msg>" in lowered) or "<appmsg" in lowered:
            return decoded
        return None

    message_text = _decode_sqlite_text(message_value)
    decoded_message = decode_text_blob(html.unescape(message_text.strip()))
    if decoded_message:
        message_text = decoded_message

    raw_message = bytes(message_value) if isinstance(message_value, memoryview) else message_value
    if isinstance(raw_message, (bytes, bytearray)) and zstd is not None and bytes(raw_message).startswith(b"\x28\xb5\x2f\xfd"):
        try:
            decoded = zstd.decompress(bytes(raw_message)).decode("utf-8", errors="ignore")
            decoded = html.unescape(decoded.strip())
            if _looks_like_xml(decoded) or _is_mostly_printable_text(decoded):
                message_text = decoded
        except Exception:
            pass

    if compress_value is None:
        return message_text

    if isinstance(compress_value, str):
        text = html.unescape(compress_value.strip())
        decoded = decode_text_blob(text)
        if decoded:
            return decoded
        if _looks_like_xml(text) or _is_mostly_printable_text(text):
            return text
        return message_text

    data: bytes | None = None
    if isinstance(compress_value, memoryview):
        data = compress_value.tobytes()
    elif isinstance(compress_value, (bytes, bytearray)):
        data = bytes(compress_value)
    if not data:
        return message_text

    if zstd is not None:
        try:
            text = zstd.decompress(data).decode("utf-8", errors="ignore")
            text = html.unescape(text.strip())
            if _looks_like_xml(text) or _is_mostly_printable_text(text):
                return text
        except Exception:
            pass

    try:
        text = html.unescape(data.decode("utf-8", errors="ignore").strip())
        decoded = decode_text_blob(text)
        if decoded:
            return decoded
        if _looks_like_xml(text) or _is_mostly_printable_text(text):
            return text
    except Exception:
        pass
    return message_text


def _row_get_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        pass
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def _normalize_contact_text(value: Any) -> str:
    return _decode_sqlite_text(value).replace("\xa0", " ").strip()


def _contact_row_to_dict(row: Any) -> dict[str, str]:
    return {
        "username": _normalize_contact_text(_row_get_value(row, "username", "")),
        "remark": _normalize_contact_text(_row_get_value(row, "remark", "")),
        "nick_name": _normalize_contact_text(_row_get_value(row, "nick_name", "")),
        "alias": _normalize_contact_text(_row_get_value(row, "alias", "")),
    }


def _pick_display_name(contact_row: Optional[Any], fallback_username: str) -> str:
    if contact_row is None:
        return fallback_username
    for key in ("remark", "nick_name", "alias"):
        value = _normalize_contact_text(_row_get_value(contact_row, key, ""))
        if value:
            return value
    return fallback_username


def _load_contact_rows(contact_db_path: Path, usernames: list[str]) -> dict[str, dict[str, str]]:
    targets = list(dict.fromkeys([str(item or "").strip() for item in usernames if str(item or "").strip()]))
    if not targets or not Path(contact_db_path).exists():
        return {}

    result: dict[str, dict[str, str]] = {}
    conn = sqlite3.connect(str(contact_db_path))
    conn.row_factory = sqlite3.Row
    conn.text_factory = bytes
    try:
        def query_table(table: str, remaining: list[str]) -> None:
            if not remaining:
                return
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (table,),
            ).fetchone()
            if not exists:
                return
            placeholders = ",".join(["?"] * len(remaining))
            rows = conn.execute(
                f"SELECT username, remark, nick_name, alias FROM {table} WHERE username IN ({placeholders})",
                remaining,
            ).fetchall()
            for row in rows:
                item = _contact_row_to_dict(row)
                username = item.get("username", "")
                if username:
                    result[username] = item

        query_table("contact", targets)
        query_table("stranger", [item for item in targets if item not in result])
    finally:
        conn.close()
    return result
