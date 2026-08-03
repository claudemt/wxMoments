from bisect import bisect_left, bisect_right
from functools import lru_cache
from pathlib import Path
import os
import base64
import hashlib
import json
import re
import html
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Optional


from .account_helpers import _load_contact_rows, _pick_display_name, _resolve_account_dir
from .media_decrypt import _read_and_maybe_decrypt_media
from .media_cache import _resolve_account_wxid_dir
from ..exceptions import WxMomentsError

from .constants import (
    MEDIA_TYPE_IMAGE, MEDIA_TYPE_VIDEO,
    POST_TYPE_NORMAL, POST_TYPE_ARTICLE, POST_TYPE_LINK, POST_TYPE_COVER,
    POST_TYPE_FINDER, POST_TYPE_MUSIC,
    DEFAULT_PAGE_LIMIT,
    IMAGE_CACHE_WINDOW_SECONDS, SNS_AUTO_CACHE_TTL_SECONDS,
)
from .logging_config import get_logger
from .source_fallback import build_source_fallback_meta, normalize_data_source
from .wcdb_realtime import (
    WCDBRealtimeError,
    WCDB_REALTIME,
    exec_query as _wcdb_exec_query,
    get_display_names as _wcdb_get_display_names,
    get_sns_timeline as _wcdb_get_sns_timeline,
)

try:
    import zstandard as zstd  # type: ignore
except Exception:
    zstd = None

logger = get_logger(__name__)

class ReaderError(WxMomentsError):
    def __init__(self, status_code: int = 500, detail: str = ""):
        super().__init__(detail or str(status_code))
        self.status_code = status_code
        self.detail = detail or str(status_code)


HTTPException = ReaderError

_SNS_VIDEO_KEY_RE = re.compile(r'<enc\s+key="(\d+)"', flags=re.IGNORECASE)
_MP_BIZ_RE = re.compile(r"__biz=([A-Za-z0-9_=+-]+)")
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_SNS_APP_NAME_RE = re.compile(r"<appname[^>]*>([\s\S]*?)</appname>", flags=re.IGNORECASE)
_SNS_XML_CDATA_BLOCK_RE = re.compile(r"<!\[CDATA\[[\s\S]*?\]\]>", flags=re.IGNORECASE)
_SNS_XML_BARE_AMP_RE = re.compile(r"&(?!(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);)")
_SNS_XML_INVALID_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_SNS_REALTIME_SYNC_STATE_FILE = "_sns_realtime_sync_state.json"
_SNS_DECRYPTED_DB_LOCKS: dict[str, threading.Lock] = {}
_SNS_DECRYPTED_DB_LOCKS_MU = threading.Lock()

_SNS_TIMELINE_AUTO_CACHE: dict[tuple[str, tuple[str, ...], str], tuple[float, bool]] = {}
_SNS_TIMELINE_AUTO_CACHE_MU = threading.Lock()


def _sns_timeline_auto_cache_key(account_dir: Path, users: list[str], kw: str) -> tuple[str, tuple[str, ...], str]:
    a = str(Path(account_dir).name)
    u = tuple(sorted([str(x or "").strip() for x in (users or []) if str(x or "").strip()]))
    k = str(kw or "").strip()
    return (a, u, k)


def _sns_timeline_auto_cache_get(key: tuple[str, tuple[str, ...], str]) -> Optional[bool]:
    now = time.time()
    with _SNS_TIMELINE_AUTO_CACHE_MU:
        rec = _SNS_TIMELINE_AUTO_CACHE.get(key)
        if not rec:
            return None
        exp_ts, val = rec
        if exp_ts <= now:
            try:
                del _SNS_TIMELINE_AUTO_CACHE[key]
            except Exception as exc:
                print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
            return None
        return bool(val)


def _sns_timeline_auto_cache_set(
    key: tuple[str, tuple[str, ...], str],
    val: bool,
    *,
    ttl_seconds: int = SNS_AUTO_CACHE_TTL_SECONDS,
) -> None:
    ttl = int(ttl_seconds or SNS_AUTO_CACHE_TTL_SECONDS)
    if ttl <= 0:
        ttl = SNS_AUTO_CACHE_TTL_SECONDS
    exp_ts = time.time() + float(ttl)
    with _SNS_TIMELINE_AUTO_CACHE_MU:
        _SNS_TIMELINE_AUTO_CACHE[key] = (exp_ts, bool(val))


def _sns_decrypted_db_lock(account: str) -> threading.Lock:
    key = str(account or "").strip()
    if not key:
        key = "_"
    with _SNS_DECRYPTED_DB_LOCKS_MU:
        lock = _SNS_DECRYPTED_DB_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SNS_DECRYPTED_DB_LOCKS[key] = lock
        return lock


def _parse_csv_list(raw: Optional[str]) -> list[str]:
    if raw is None:
        return []
    s = str(raw or "").strip()
    if not s:
        return []
    return [p.strip() for p in s.split(",") if p.strip()]


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def _count_sns_timeline_rows_in_decrypted_sqlite(
    sns_db_path: Path,
    *,
    users: list[str],
    kw: str,
) -> int:
    sns_db_path = Path(sns_db_path)
    try:
        if (not sns_db_path.exists()) or (not sns_db_path.is_file()):
            return 0
    except Exception:
        return 0

    filters: list[str] = []
    params: list[Any] = []

    if users:
        placeholders = ",".join(["?"] * len(users))
        filters.append(f"user_name IN ({placeholders})")
        params.extend(users)

    if kw:
        filters.append("content LIKE ?")
        params.append(f"%{kw}%")

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    sql = f"SELECT COUNT(*) AS c FROM SnsTimeLine {where_sql}"

    try:
        conn = sqlite3.connect(str(sns_db_path), timeout=2.0)
        try:
            conn.execute("PRAGMA busy_timeout=2000")
            row = conn.execute(sql, params).fetchone()
            return int((row[0] if row else 0) or 0)
        finally:
            try:
                conn.close()
            except Exception as exc:
                print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
    except Exception:
        return 0


def _count_sns_timeline_posts_in_decrypted_sqlite(
    sns_db_path: Path,
    *,
    users: list[str],
    kw: str,
) -> int:
    sns_db_path = Path(sns_db_path)
    try:
        if (not sns_db_path.exists()) or (not sns_db_path.is_file()):
            return 0
    except Exception:
        return 0

    filters: list[str] = []
    params: list[Any] = []

    filters.append("content IS NOT NULL")
    filters.append("content != ?")
    params.append("")
    filters.append("content NOT LIKE ?")
    params.append("%<type>7</type>%")

    if users:
        placeholders = ",".join(["?"] * len(users))
        filters.append(f"user_name IN ({placeholders})")
        params.extend(users)

    if kw:
        filters.append("content LIKE ?")
        params.append(f"%{kw}%")

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    sql = f"SELECT COUNT(*) AS c FROM SnsTimeLine {where_sql}"

    try:
        conn = sqlite3.connect(str(sns_db_path), timeout=2.0)
        try:
            conn.execute("PRAGMA busy_timeout=2000")
            row = conn.execute(sql, params).fetchone()
            return int((row[0] if row else 0) or 0)
        finally:
            try:
                conn.close()
            except Exception as exc:
                print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
    except Exception:
        return 0


def _to_signed_i64(v: int) -> int:
    x = int(v) & 0xFFFFFFFFFFFFFFFF
    if x >= 0x8000000000000000:
        x -= 0x10000000000000000
    return int(x)

def _to_unsigned_i64_str(v: Any) -> str:
    try:
        x = int(v)
    except Exception:
        return str(v or "").strip()
    return str(x & 0xFFFFFFFFFFFFFFFF)


def _read_sns_realtime_sync_state(account_dir: Path) -> dict[str, Any]:
    p = Path(account_dir) / _SNS_REALTIME_SYNC_STATE_FILE
    try:
        if not p.exists() or (not p.is_file()):
            return {}
    except Exception:
        return {}

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

    return data if isinstance(data, dict) else {}


def _write_sns_realtime_sync_state(account_dir: Path, data: dict[str, Any]) -> None:
    p = Path(account_dir) / _SNS_REALTIME_SYNC_STATE_FILE
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)


def _ensure_decrypted_sns_db(account_dir: Path) -> Path:
    account_dir = Path(account_dir)
    sns_db_path = account_dir / "sns.db"

    try:
        if sns_db_path.exists() and (not sns_db_path.is_file()):
            raise RuntimeError("sns.db path is not a file")
    except Exception as e:
        raise RuntimeError(f"Invalid sns.db path: {e}") from e

    conn = sqlite3.connect(str(sns_db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS SnsTimeLine(
              tid INTEGER PRIMARY KEY,
              user_name TEXT,
              content TEXT
            )
            """
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception as exc:
            print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)

    return sns_db_path


def _upsert_sns_timeline_rows_to_decrypted_db(
    account_dir: Path,
    rows: list[tuple[int, str, str, Optional[str]]],
    *,
    source: str,
) -> int:
    if not rows:
        return 0

    sns_db_path = _ensure_decrypted_sns_db(account_dir)

    with _sns_decrypted_db_lock(Path(account_dir).name):
        conn = sqlite3.connect(str(sns_db_path), timeout=2.0)
        try:
            conn.execute("PRAGMA busy_timeout=2000")
            cols: set[str] = set()
            try:
                info_rows = conn.execute("PRAGMA table_info(SnsTimeLine)").fetchall()
                for r in info_rows or []:
                    try:
                        cols.add(str(r[1] or "").strip())
                    except Exception:
                        continue
            except Exception:
                cols = set()

            has_pack = "pack_info_buf" in cols

            if has_pack:
                sql = """
                    INSERT INTO SnsTimeLine (tid, user_name, content, pack_info_buf)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(tid) DO UPDATE SET
                      user_name=excluded.user_name,
                      content=COALESCE(NULLIF(excluded.content, ''), SnsTimeLine.content),
                      pack_info_buf=COALESCE(excluded.pack_info_buf, SnsTimeLine.pack_info_buf)
                """
                data = [(int(tid), str(u or "").strip(), str(c or ""), p) for tid, u, c, p in rows]
            else:
                sql = """
                    INSERT INTO SnsTimeLine (tid, user_name, content)
                    VALUES (?, ?, ?)
                    ON CONFLICT(tid) DO UPDATE SET
                      user_name=excluded.user_name,
                      content=COALESCE(NULLIF(excluded.content, ''), SnsTimeLine.content)
                """
                data = [(int(tid), str(u or "").strip(), str(c or "")) for tid, u, c, _p in rows]

            conn.executemany(sql, data)
            conn.commit()
            return len(rows)
        except Exception as e:
            logger.debug("[sns] decrypted sns.db upsert failed source=%s err=%s", source, e)
            try:
                conn.rollback()
            except Exception as exc:
                print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
            return 0
        finally:
            try:
                conn.close()
            except Exception as exc:
                print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)

def _extract_mp_biz_from_url(url: str) -> str:
    u = html.unescape(str(url or "")).replace("&amp;", "&").strip()
    if not u:
        return ""
    m = _MP_BIZ_RE.search(u)
    if not m:
        return ""
    return str(m.group(1) or "").strip()


@lru_cache(maxsize=16)
def _build_biz_to_official_index(contact_db_path: str, mtime_ns: int, size: int) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not contact_db_path:
        return out

    conn = sqlite3.connect(str(contact_db_path))
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT username, brand_info, external_info, home_url FROM biz_info"
            ).fetchall()
        except Exception:
            rows = []

        for r in rows:
            try:
                uname = str(r["username"] or "").strip()
            except Exception:
                uname = ""
            if not uname:
                continue

            try:
                brand_info = str(r["brand_info"] or "")
            except Exception:
                brand_info = ""
            try:
                external_info = str(r["external_info"] or "")
            except Exception:
                external_info = ""
            try:
                home_url = str(r["home_url"] or "")
            except Exception:
                home_url = ""

            service_type: Optional[int] = None
            if external_info:
                try:
                    j = json.loads(external_info)
                    st = j.get("ServiceType")
                    if st is not None:
                        service_type = int(st)
                except Exception:
                    service_type = None

            blob = " ".join([brand_info, external_info, home_url])
            for biz in _MP_BIZ_RE.findall(blob):
                b = str(biz or "").strip()
                if not b:
                    continue
                prev = out.get(b)
                if prev is None:
                    out[b] = {"username": uname, "serviceType": service_type}
                else:
                    if prev.get("serviceType") is None and service_type is not None:
                        prev["serviceType"] = service_type
    finally:
        conn.close()

    return out


def _get_biz_to_official_index(contact_db_path: Path) -> dict[str, dict[str, Any]]:
    if not contact_db_path.exists():
        return {}
    st = contact_db_path.stat()
    mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    return _build_biz_to_official_index(str(contact_db_path), mtime_ns, int(st.st_size))


def _extract_sns_video_key(raw_xml: Any) -> str:
    text = _decode_sns_text_blob(raw_xml)
    m = _SNS_VIDEO_KEY_RE.search(text or "")
    return str(m.group(1) or "").strip() if m else ""


def _looks_like_xml_text(s: str) -> bool:
    if not s:
        return False
    t = str(s).lstrip()
    if t.startswith('"') and t.endswith('"'):
        t = t.strip('"').lstrip()
    return t.startswith("<")


def _sanitize_wechat_xml_for_et(xml_text: str) -> str:
    s = str(xml_text or "")
    if not s:
        return ""

    s = _SNS_XML_INVALID_CHARS_RE.sub("", s)

    parts: list[str] = []
    last = 0
    for m in _SNS_XML_CDATA_BLOCK_RE.finditer(s):
        head = s[last : m.start()]
        if head:
            parts.append(_SNS_XML_BARE_AMP_RE.sub("&amp;", head))
        parts.append(m.group(0))
        last = m.end()

    tail = s[last:]
    if tail:
        parts.append(_SNS_XML_BARE_AMP_RE.sub("&amp;", tail))

    return "".join(parts)


def _decode_sns_text_blob(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, memoryview):
        raw = bytes(value)
        if raw and zstd is not None and raw.startswith(_ZSTD_MAGIC):
            try:
                raw = zstd.decompress(raw)
            except Exception as exc:
                print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            s = raw.decode("utf-8", errors="ignore")
        except Exception as exc:
            print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
            s = ""
        s = html.unescape(str(s or "").strip())
        return s if _looks_like_xml_text(s) else (str(s or "").strip())

    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if raw and zstd is not None and raw.startswith(_ZSTD_MAGIC):
            try:
                raw = zstd.decompress(raw)
            except Exception as exc:
                print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            s = raw.decode("utf-8", errors="ignore")
        except Exception as exc:
            print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
            s = ""
        s = html.unescape(str(s or "").strip())
        return s if _looks_like_xml_text(s) else (str(s or "").strip())

    try:
        text = str(value or "")
    except Exception:
        return ""

    text = html.unescape(text.strip())
    if not text:
        return ""

    if _looks_like_xml_text(text):
        return text

    def _accept_xml(decoded: str) -> str:
        s2 = html.unescape(str(decoded or "").strip())
        return s2 if _looks_like_xml_text(s2) else ""

    t_hex = text[2:] if text.lower().startswith("0x") else text
    if len(t_hex) >= 16 and len(t_hex) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", t_hex):
        try:
            raw = bytes.fromhex(t_hex)
            if raw and zstd is not None and raw.startswith(_ZSTD_MAGIC):
                try:
                    raw = zstd.decompress(raw)
                except Exception:
                    raw = b""
            if raw:
                s2 = _accept_xml(raw.decode("utf-8", errors="ignore"))
                if s2:
                    return s2
        except Exception as exc:
            print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)

    if len(text) >= 24 and len(text) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/=]+", text):
        try:
            raw = base64.b64decode(text)
            if raw and zstd is not None and raw.startswith(_ZSTD_MAGIC):
                try:
                    raw = zstd.decompress(raw)
                except Exception:
                    raw = b""
            if raw:
                s2 = _accept_xml(raw.decode("utf-8", errors="ignore"))
                if s2:
                    return s2
        except Exception as exc:
            print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)

    return text


def _extract_sns_source_name(raw_xml: Any) -> str:
    text = _decode_sns_text_blob(raw_xml)
    if not text:
        return ""
    m = _SNS_APP_NAME_RE.search(text)
    if not m:
        return ""
    v = str(m.group(1) or "")
    v = v.replace("<![CDATA[", "").replace("]]>", "")
    v = re.sub(r"<[^>]+>", "", v)
    return html.unescape(v.strip())


def _build_location_text(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""

    def _get(key: str) -> str:
        return str(node.get(key) or node.findtext(key) or "").strip()

    def _clean(v: str) -> str:
        return (
            str(v or "")
            .replace("\u00a0", " ")
            .replace("\u2006", " ")
            .strip()
        )

    city = _clean(_get("city"))
    poi = _clean(_get("poiName") or _get("poi") or _get("label"))
    address = _clean(_get("address") or _get("poiAddress"))

    if city and poi and poi.startswith(city):
        rest = poi[len(city):].lstrip(" ·")
        if rest:
            poi = rest

    if city and (poi or address):
        return f"{city}·{poi or address}".strip()

    for cand in (poi, address, city):
        if cand:
            return cand
    return ""


def _parse_timeline_xml(xml_text: str, fallback_username: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "username": fallback_username,
        "createTime": 0,
        "contentDesc": "",
        "location": "",
        "sourceName": "",
        "media": [],
        "likes": [],
        "comments": [],
        "type": POST_TYPE_NORMAL,
        "title": "",
        "contentUrl": "",
        "finderFeed": {}
    }

    xml_str = _decode_sns_text_blob(xml_text)
    if not xml_str:
        return out


    try:
        root = ET.fromstring(_sanitize_wechat_xml_for_et(xml_str))
    except Exception:
        return out

    try:
        for el in root.iter():
            try:
                tag = str(el.tag or "").lower()
            except Exception:
                continue
            if tag in {"appname", "sourcename"}:
                v = str(el.text or "").strip()
                if v:
                    out["sourceName"] = html.unescape(v).strip()
                    break
            try:
                attrs = el.attrib or {}
            except Exception:
                attrs = {}
            for k, v in attrs.items():
                if str(k or "").lower() in {"appname", "sourcename"}:
                    vv = str(v or "").strip()
                    if vv:
                        out["sourceName"] = html.unescape(vv).strip()
                        break
            if out["sourceName"]:
                break
    except Exception as exc:
        print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)

    def _find_text(*paths: str) -> str:
        for p in paths:
            try:
                v = root.findtext(p)
            except Exception:
                v = None
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    def _clean_url(u: str) -> str:
        if not u:
            return ""

        cleaned = html.unescape(u)
        cleaned = cleaned.replace("&amp;", "&")
        return cleaned.strip()

    out["username"] = _find_text(".//TimelineObject/username", ".//TimelineObject/user_name",
                                 ".//username") or fallback_username
    out["createTime"] = _safe_int(_find_text(".//TimelineObject/createTime", ".//createTime"))
    out["contentDesc"] = _find_text(".//TimelineObject/contentDesc", ".//contentDesc")
    out["location"] = _build_location_text(root.find(".//location"))

    post_type = _safe_int(_find_text(".//ContentObject/type", ".//type"))
    out["type"] = post_type

    if post_type == POST_TYPE_ARTICLE:
        out["title"] = _find_text(".//ContentObject/title")
        out["contentUrl"] = _clean_url(_find_text(".//ContentObject/contentUrl"))

    if post_type == POST_TYPE_LINK:
        out["title"] = _find_text(
            ".//ContentObject/title",
            ".//ContentObject/linkTitle",
            ".//ContentObject/name",
            ".//ContentObject/desc",
            ".//ContentObject/description",
        )
        out["contentUrl"] = _clean_url(
            _find_text(
                ".//ContentObject/contentUrl",
                ".//ContentObject/linkUrl",
                ".//ContentObject/url",
                ".//ContentObject/jumpUrl",
            )
        )

    if post_type == POST_TYPE_MUSIC:
        out["title"] = _find_text(
            ".//ContentObject/title",
            ".//ContentObject/linkTitle",
            ".//ContentObject/name",
            ".//ContentObject/desc",
        )
        out["contentUrl"] = _clean_url(
            _find_text(
                ".//ContentObject/contentUrl",
                ".//ContentObject/linkUrl",
                ".//ContentObject/url",
                ".//ContentObject/jumpUrl",
            )
        )

    if post_type == POST_TYPE_FINDER:
        out["title"] = _find_text(".//ContentObject/title")
        out["contentUrl"] = _clean_url(_find_text(".//ContentObject/contentUrl"))
        out["finderFeed"] = {
            "nickname": _find_text(".//finderFeed/nickname"),
            "desc": _find_text(".//finderFeed/desc"),
            "thumbUrl": _clean_url(
                _find_text(".//finderFeed/mediaList/media/thumbUrl", ".//finderFeed/mediaList/media/coverUrl")),
            "url": _clean_url(_find_text(".//finderFeed/mediaList/media/url"))
        }

    media: list[dict[str, Any]] = []
    try:
        for m in root.findall(".//mediaList//media"):
            mt = _safe_int(m.findtext("type"))
            url_el = m.find("url") if m.find("url") is not None else m.find("urlV")
            thumb_el = m.find("thumb") if m.find("thumb") is not None else m.find("thumbV")

            url = _clean_url(url_el.text if url_el is not None else "")
            thumb = _clean_url(thumb_el.text if thumb_el is not None else "")

            url_attrs = dict(url_el.attrib) if url_el is not None and url_el.attrib else {}
            thumb_attrs = dict(thumb_el.attrib) if thumb_el is not None and thumb_el.attrib else {}
            media_id = str(m.findtext("id") or "").strip()
            size_el = m.find("size")
            size = dict(size_el.attrib) if size_el is not None and size_el.attrib else {}

            if not url and not thumb:
                continue

            media.append({
                "type": mt,
                "id": media_id,
                "url": url,
                "thumb": thumb,
                "urlAttrs": url_attrs,
                "thumbAttrs": thumb_attrs,
                "size": size,
            })
    except Exception as exc:
        print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
    out["media"] = media

    if post_type in (POST_TYPE_LINK, POST_TYPE_MUSIC):
        if (not str(out.get("contentUrl") or "").strip()) and media:
            m0 = media[0] if isinstance(media[0], dict) else {}
            u0 = str(m0.get("url") or "").strip()
            if u0:
                out["contentUrl"] = u0

    def _tag_lower(el: Optional[ET.Element]) -> str:
        if el is None:
            return ""
        try:
            return str(el.tag or "").strip().lower()
        except Exception:
            return ""

    def _direct_child_text(node: ET.Element, *names: str) -> str:
        wanted = {str(n or "").strip().lower() for n in names if str(n or "").strip()}
        if not wanted:
            return ""
        try:
            children = list(node)
        except Exception:
            children = []
        for child in children:
            if _tag_lower(child) not in wanted:
                continue
            v = str(child.text or "").strip()
            if v:
                return html.unescape(v).replace("&amp;", "&").strip()
        return ""

    def _has_descendant(node: ET.Element, *names: str) -> bool:
        wanted = {str(n or "").strip().lower() for n in names if str(n or "").strip()}
        if not wanted:
            return False
        try:
            return any(_tag_lower(el) in wanted for el in node.iter())
        except Exception:
            return False

    def _iter_comment_nodes() -> list[ET.Element]:
        nodes: list[ET.Element] = []
        seen: set[int] = set()
        comment_tags = {"comment", "commentuser", "user_comment", "commentitem"}
        for el in root.iter():
            if id(el) in seen:
                continue
            if _tag_lower(el) not in comment_tags:
                continue
            if not (
                _direct_child_text(el, "username", "user_name")
                or _direct_child_text(el, "content")
                or _direct_child_text(el, "nickname", "nickName", "displayName")
                or _has_descendant(el, "imageinfo")
            ):
                continue
            seen.add(id(el))
            nodes.append(el)
        return nodes

    def _parse_comment_images(comment_node: ET.Element) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        try:
            image_nodes = [el for el in comment_node.iter() if _tag_lower(el) == "imageinfo"]
        except Exception:
            image_nodes = []

        for img in image_nodes:
            url = _clean_url(_direct_child_text(img, "url", "cdn_url", "origin_url", "originurl"))
            thumb_url = _clean_url(_direct_child_text(img, "thumb_url", "thumburl", "thumb", "thumb_url_v", "thumburlv"))
            token = _direct_child_text(img, "token")
            key = _direct_child_text(img, "key")
            enc_idx = _direct_child_text(img, "enc_idx", "encidx")
            thumb_token = _direct_child_text(img, "thumb_url_token", "thumb_token", "thumburltoken") or token
            thumb_key = _direct_child_text(img, "thumb_key", "thumbkey") or key
            thumb_enc_idx = _direct_child_text(img, "thumb_enc_idx", "thumbencidx")
            media_id = _direct_child_text(img, "media_id", "mediaid", "id")
            md5 = _direct_child_text(img, "md5")
            width = _safe_int(_direct_child_text(img, "width", "w"))
            height = _safe_int(_direct_child_text(img, "height", "h"))
            file_size = _safe_int(_direct_child_text(img, "file_size", "filesize", "total_size", "totalsize"))
            height_percentage = _safe_int(_direct_child_text(img, "height_percentage", "heightpercentage"))
            min_area = _safe_int(_direct_child_text(img, "min_area", "minarea"))

            if not url and not thumb_url:
                continue

            images.append(
                {
                    "type": MEDIA_TYPE_IMAGE,
                    "id": media_id,
                    "mediaId": media_id,
                    "url": url,
                    "thumb": thumb_url or url,
                    "thumbUrl": thumb_url,
                    "token": token,
                    "key": key,
                    "encIdx": enc_idx,
                    "thumbUrlToken": thumb_token,
                    "thumbKey": thumb_key,
                    "thumbEncIdx": thumb_enc_idx,
                    "md5": md5,
                    "width": width,
                    "height": height,
                    "heightPercentage": height_percentage,
                    "fileSize": file_size,
                    "minArea": min_area,
                    "urlAttrs": {
                        "token": token,
                        "key": key,
                        "enc_idx": enc_idx,
                        "md5": md5,
                    },
                    "thumbAttrs": {
                        "token": thumb_token,
                        "key": thumb_key,
                        "enc_idx": thumb_enc_idx,
                        "md5": md5,
                    },
                    "size": {
                        "width": width,
                        "height": height,
                        "totalSize": file_size,
                    },
                }
            )
        return images

    likes: list[dict[str, str]] = []
    try:
        seen_like_users: set[str] = set()
        like_nodes: list[ET.Element] = []
        for container_name in ("like_user_list", "likeList", "like_list"):
            for container in root.findall(f".//{container_name}"):
                for child in list(container):
                    if _tag_lower(child) in {"user_comment", "like", "likeuser", "like_user", "user"}:
                        like_nodes.append(child)
        if not like_nodes:
            like_nodes = root.findall(".//likeList//like")
        for node in like_nodes:
            username = _direct_child_text(node, "username", "userName", "user_name")
            nickname = _direct_child_text(node, "nickname", "nickName", "displayName")
            key = username or nickname
            if not key or key in seen_like_users:
                continue
            seen_like_users.add(key)
            likes.append({"username": username, "nickname": nickname})
    except Exception as exc:
        print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
        likes = []
    out["likes"] = likes

    comments: list[dict[str, Any]] = []
    try:
        for c in _iter_comment_nodes():
            content = _direct_child_text(c, "content")
            images = _parse_comment_images(c)
            if not content and not images:
                continue
            comments.append(
                {
                    "id": _direct_child_text(c, "cmtid", "commentId", "comment_id", "id"),
                    "username": _direct_child_text(c, "username", "user_name"),
                    "nickname": _direct_child_text(c, "nickName", "nickname", "displayName"),
                    "content": content,
                    "refUsername": _direct_child_text(c, "refUserName", "ref_username", "replyUsername", "reply_user_name"),
                    "refNickname": _direct_child_text(c, "refNickName", "refNickname", "replyNickname", "reply_nickname"),
                    "images": images,
                }
            )
    except Exception as exc:
        print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
        comments = []
    out["comments"] = comments

    return out


@lru_cache(maxsize=16)
def _sns_video_roots(wxid_dir_str: str) -> tuple[str, ...]:
    wxid_dir = Path(str(wxid_dir_str or "").strip())
    cache_root = wxid_dir / "cache"
    try:
        month_dirs = [p for p in cache_root.iterdir() if p.is_dir()]
    except Exception:
        month_dirs = []

    roots: list[str] = []
    for mdir in month_dirs:
        video_root = mdir / "Sns" / "Video"
        try:
            if video_root.exists() and video_root.is_dir():
                roots.append(str(video_root))
        except Exception:
            continue
    roots.sort()
    return tuple(roots)


def _image_size_from_bytes(data: bytes, media_type: str) -> tuple[int, int]:
    mt = str(media_type or "").lower()
    if mt == "image/png":
        if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
            try:
                w = int.from_bytes(data[16:20], "big")
                h = int.from_bytes(data[20:24], "big")
                return w, h
            except Exception:
                return 0, 0
        return 0, 0

    if mt in {"image/jpeg", "image/jpg"}:
        if len(data) < 4 or data[0:2] != b"\xff\xd8":
            return 0, 0
        i = 2
        n = len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            i += 2
            while marker == 0xFF and i < n:
                marker = data[i]
                i += 1
            if marker in {0xD8, 0xD9}:
                continue
            if i + 2 > n:
                return 0, 0
            seg_len = (data[i] << 8) + data[i + 1]
            i += 2
            if seg_len < 2 or i + seg_len - 2 > n:
                return 0, 0
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                if i + 4 < len(data):
                    try:
                        h = (data[i + 1] << 8) + data[i + 2]
                        w = (data[i + 3] << 8) + data[i + 4]
                        return w, h
                    except Exception:
                        return 0, 0
            i += seg_len - 2
        return 0, 0
    return 0, 0


@lru_cache(maxsize=16)
def _sns_img_roots(wxid_dir_str: str) -> tuple[str, ...]:
    wxid_dir = Path(str(wxid_dir_str or "").strip())
    cache_root = wxid_dir / "cache"
    try:
        month_dirs = [p for p in cache_root.iterdir() if p.is_dir()]
    except Exception:
        month_dirs = []

    roots: list[str] = []
    for mdir in month_dirs:
        img_root = mdir / "Sns" / "Img"
        try:
            if img_root.exists() and img_root.is_dir():
                roots.append(str(img_root))
        except Exception:
            continue
    roots.sort()
    return tuple(roots)


@lru_cache(maxsize=16)
def _sns_img_time_index(wxid_dir_str: str) -> tuple[list[float], list[str]]:
    wxid_dir = Path(str(wxid_dir_str or "").strip())
    out: list[tuple[float, str]] = []

    cache_root = wxid_dir / "cache"
    try:
        month_dirs = [p for p in cache_root.iterdir() if p.is_dir()]
    except Exception:
        month_dirs = []

    for mdir in month_dirs:
        img_root = mdir / "Sns" / "Img"
        try:
            if not (img_root.exists() and img_root.is_dir()):
                continue
        except Exception:
            continue
        try:
            for sub in img_root.iterdir():
                if not sub.is_dir():
                    continue
                for f in sub.iterdir():
                    try:
                        if not f.is_file():
                            continue
                        st = f.stat()
                        out.append((float(st.st_mtime), str(f)))
                    except Exception:
                        continue
        except Exception:
            continue

    out.sort(key=lambda x: x[0])
    mtimes = [m for m, _p in out]
    paths = [_p for _m, _p in out]
    return mtimes, paths


def _normalize_hex32(value: Optional[str]) -> str:
    s = str(value or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"[^0-9a-f]", "", s)
    if len(s) < 32:
        return ""
    return s[:32]


def _sns_cache_key_from_path(p: Path) -> str:
    try:
        key = f"{p.parent.name}{p.name}"
    except Exception:
        return ""
    return _normalize_hex32(key)


def _generate_sns_cache_key(tid: str, media_id: str, media_type: int = MEDIA_TYPE_IMAGE) -> str:
    if not tid or not media_id:
        return ""
    raw_key = f"{tid}_{media_id}_{media_type}"
    try:
        return hashlib.md5(raw_key.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _resolve_sns_cached_image_path_by_cache_key(
    *,
    wxid_dir: Path,
    cache_key: str,
    create_time: int,
) -> Optional[str]:
    key32 = _normalize_hex32(cache_key)
    if not key32:
        return None

    sub = key32[:2]
    rest = key32[2:]
    roots = _sns_img_roots(str(wxid_dir))
    if not roots:
        return None

    best: tuple[float, str] | None = None
    for root_str in roots:
        try:
            p = Path(root_str) / sub / rest
            if not (p.exists() and p.is_file()):
                continue
            st = p.stat()
            score = abs(float(st.st_mtime) - float(create_time)) if create_time > 0 else -float(st.st_mtime)
            if best is None or score < best[0]:
                best = (score, str(p))
        except Exception:
            continue
    return best[1] if best else None


def _resolve_sns_cached_image_path_by_md5(
    *,
    wxid_dir: Path,
    md5: str,
    create_time: int,
) -> Optional[str]:
    md5_32 = _normalize_hex32(md5)
    if not md5_32:
        return None

    sub = md5_32[:2]
    rest = md5_32[2:]
    roots = _sns_img_roots(str(wxid_dir))
    if not roots:
        return None

    best: tuple[float, str] | None = None
    for root_str in roots:
        try:
            p = Path(root_str) / sub / rest
            if not (p.exists() and p.is_file()):
                continue
            st = p.stat()
            score = abs(float(st.st_mtime) - float(create_time)) if create_time > 0 else -float(st.st_mtime)
            if best is None or score < best[0]:
                best = (score, str(p))
        except Exception:
            continue
    return best[1] if best else None


@lru_cache(maxsize=4096)
def _resolve_sns_cached_image_path(
    *,
    account_dir_str: str,
    create_time: int,
    width: int,
    height: int,
    idx: int,
    total_size: int = 0,
) -> Optional[str]:
    total_size_i = int(total_size or 0)
    must_match_size = width > 0 and height > 0
    if (not must_match_size) and total_size_i <= 0:
        return None

    account_dir = Path(str(account_dir_str or "").strip())
    if not account_dir.exists():
        return None

    wxid_dir = _resolve_account_wxid_dir(account_dir)
    if not wxid_dir:
        return None

    mtimes, paths = _sns_img_time_index(str(wxid_dir))
    if not mtimes:
        return None

    create_time_i = int(create_time or 0)
    if create_time_i > 0:
        window = IMAGE_CACHE_WINDOW_SECONDS
        lo = create_time_i - window
        hi = create_time_i + window
        left = bisect_left(mtimes, lo)
        right = bisect_right(mtimes, hi)
        if left >= right:
            left = max(0, len(mtimes) - 800)
            right = len(mtimes)
    else:
        left = max(0, len(mtimes) - 800)
        right = len(mtimes)

    candidates: list[tuple[float, str]] = []
    for j in range(left, right):
        try:
            if create_time_i > 0:
                candidates.append((abs(mtimes[j] - float(create_time_i)), paths[j]))
            else:
                candidates.append((-mtimes[j], paths[j]))
        except Exception:
            continue
    candidates.sort(key=lambda x: x[0])

    matched: list[tuple[int, float, str]] = []
    for diff, pstr in candidates[:2000]:
        try:
            p = Path(pstr)
            payload, media_type = _read_and_maybe_decrypt_media(p, account_dir)
            if not payload or not str(media_type or "").startswith("image/"):
                continue
            if must_match_size:
                w0, h0 = _image_size_from_bytes(payload, str(media_type or ""))
                if (w0, h0) != (width, height):
                    continue
            size_diff = abs(len(payload) - total_size_i) if total_size_i > 0 else 0
            matched.append((int(size_diff), float(diff), pstr))
        except Exception:
            continue

    if not matched:
        return None
    if must_match_size:
        matched.sort(key=lambda x: (x[0], x[1], x[2]))
        if total_size_i > 0:
            return matched[0][2]
        idx0 = max(0, int(idx or 0))
        return matched[idx0][2] if idx0 < len(matched) else None
    if total_size_i > 0:
        matched.sort(key=lambda x: (x[0], x[1], x[2]))
        return matched[0][2]
    return None


def _resolve_sns_cached_video_path(
    wxid_dir: Path,
    post_id: str,
    media_id: str
) -> Optional[str]:
    if not post_id or not media_id:
        return None

    raw_key = f"{post_id}_{media_id}_3"
    try:
        key32 = hashlib.md5(raw_key.encode("utf-8")).hexdigest()
    except Exception:
        return None

    sub = key32[:2]
    rest = key32[2:]

    roots = _sns_video_roots(str(wxid_dir))
    for root_str in roots:
        try:
            base_path = Path(root_str) / sub / rest
            for ext in [".mp4", ".tmp"]:
                p = base_path.with_suffix(ext)
                if p.exists() and p.is_file():
                    return str(p)
        except Exception:
            continue

    return None

def _get_sns_covers(
    account_dir: Path,
    target_wxid: str,
    limit: int = 20,
    *,
    prefer_realtime: bool = True,
) -> list[dict[str, Any]]:
    wxid = str(target_wxid or "").strip()
    if not wxid:
        return []

    try:
        lim = int(limit or 20)
    except Exception:
        lim = 20
    if lim <= 0:
        lim = 1
    if lim > 50:
        lim = 50

    wxid_esc = wxid.replace("'", "''")
    cover_sql = (
        "SELECT tid, content FROM SnsTimeLine "
        f"WHERE user_name = '{wxid_esc}' AND content LIKE '%<type>7</type>%' "
        "ORDER BY tid DESC "
        f"LIMIT {lim}"
    )

    rows: list[dict[str, Any]] = []

    try:
        if prefer_realtime and WCDB_REALTIME.is_connected(account_dir.name):
            conn = WCDB_REALTIME.ensure_connected(account_dir)
            with conn.lock:
                sns_db_path = conn.db_storage_dir / "sns" / "sns.db"
                if not sns_db_path.exists():
                    sns_db_path = conn.db_storage_dir / "sns.db"
                rows = _wcdb_exec_query(conn.handle, kind="media", path=str(sns_db_path), sql=cover_sql) or []
    except Exception as e:
        logger.warning("[sns] WCDB cover fetch failed: %s", e)

    if not rows:
        sns_db_path = account_dir / "sns.db"
        if sns_db_path.exists():
            try:
                conn_sq = sqlite3.connect(f"file:{sns_db_path}?mode=ro", uri=True)
                conn_sq.row_factory = sqlite3.Row
                rows_sq = conn_sq.execute(cover_sql).fetchall()
                conn_sq.close()
                rows = [{"tid": r["tid"], "content": r["content"]} for r in (rows_sq or [])]
            except Exception as e:
                logger.warning("[sns] SQLite cover fetch failed: %s", e)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        cover_xml = rr.get("content")
        if not cover_xml:
            continue

        try:
            cover_tid = int(rr.get("tid") or 0)
        except Exception:
            cover_tid = 0

        parsed = _parse_timeline_xml(str(cover_xml or ""), wxid)
        media = parsed.get("media") or []
        if not isinstance(media, list) or not media:
            continue

        cid = _to_unsigned_i64_str(cover_tid or "")
        if cid in seen:
            continue
        seen.add(cid)

        out.append(
            {
                "id": cid,
                "tid": cover_tid,
                "username": wxid,
                "createTime": int(parsed.get("createTime") or 0),
                "media": media,
                "type": POST_TYPE_COVER,
            }
        )
    return out


def _get_sns_cover(account_dir: Path, target_wxid: str) -> Optional[dict[str, Any]]:
    covers = _get_sns_covers(account_dir, target_wxid, limit=1)
    return covers[0] if covers else None


def _build_timeline_item(
    tid,
    username,
    content_xml,
    contact_rows,
    biz_index,
    contact_db_path,
    account_dir,
    official_usernames=None,
):
    uname = str(username or "").strip()
    if not uname:
        return None

    parsed = _parse_timeline_xml(content_xml, uname)

    video_key = _extract_sns_video_key(content_xml)
    if video_key:
        pmedia = parsed.get("media")
        if isinstance(pmedia, list):
            for m0 in pmedia:
                if not isinstance(m0, dict):
                    continue
                if "videoKey" not in m0:
                    m0["videoKey"] = video_key
                lp = m0.get("livePhoto")
                if isinstance(lp, dict):
                    if not str(lp.get("key") or "").strip():
                        lp["key"] = video_key

    display = _pick_display_name(contact_rows.get(uname), uname) if uname else uname
    post_type = int(parsed.get("type", 1) or 1)

    if post_type == POST_TYPE_COVER:
        return None

    official = {}
    if post_type == POST_TYPE_ARTICLE:
        content_url = str(parsed.get("contentUrl") or "")
        biz = _extract_mp_biz_from_url(content_url)
        info = biz_index.get(biz) if biz else None
        off_username = str(info.get("username") or "").strip() if isinstance(info, dict) else ""
        off_service_type = info.get("serviceType") if isinstance(info, dict) else None
        official = {
            "biz": biz,
            "username": off_username,
            "serviceType": off_service_type,
            "displayName": "",
        }
        if off_username and official_usernames is not None:
            official_usernames.add(off_username)

    parsed_id = str(parsed.get("createTime") or "") or uname
    item_id = _to_unsigned_i64_str(tid) if tid is not None else parsed_id

    return {
        "id": item_id,
        "tid": tid,
        "username": uname or parsed.get("username") or "",
        "displayName": display,
        "createTime": int(parsed.get("createTime") or 0),
        "contentDesc": str(parsed.get("contentDesc") or ""),
        "location": str(parsed.get("location") or ""),
        "sourceName": str(parsed.get("sourceName") or ""),
        "media": parsed.get("media") or [],
        "likes": parsed.get("likes") or [],
        "comments": parsed.get("comments") or [],
        "type": post_type,
        "title": parsed.get("title", ""),
        "contentUrl": parsed.get("contentUrl", ""),
        "finderFeed": parsed.get("finderFeed", {}),
        "official": official,
    }


def _query_decrypted_sqlite(
    account_dir,
    contact_db_path,
    users,
    kw,
    limit,
    offset,
    cover_data,
    covers_data,
):
    sns_db_path = account_dir / "sns.db"
    if not sns_db_path.exists():
        raise HTTPException(status_code=404, detail="sns.db not found for this account.")

    filters = []
    params = []

    if users:
        placeholders = ",".join(["?"] * len(users))
        filters.append(f"user_name IN ({placeholders})")
        params.extend(users)

    if kw:
        filters.append("content LIKE ?")
        params.append(f"%{kw}%")

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""

    sql = f"""
        SELECT tid, user_name, content
        FROM SnsTimeLine
        {where_sql}
        ORDER BY tid DESC
        LIMIT ? OFFSET ?
    """
    params_with_page = params + [limit + 1, offset]

    conn = sqlite3.connect(str(sns_db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params_with_page).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning("[sns] query failed: %s", e)
        raise HTTPException(status_code=500, detail=f"sns.db query failed: {e}")
    finally:
        conn.close()

    has_more = len(rows) > limit
    rows = rows[:limit]

    post_usernames = [str(r["user_name"] or "").strip() for r in rows if str(r["user_name"] or "").strip()]
    contact_rows = _load_contact_rows(contact_db_path, post_usernames) if contact_db_path.exists() else {}
    biz_index = _get_biz_to_official_index(contact_db_path) if contact_db_path.exists() else {}
    official_usernames = set()

    timeline = []
    for r in rows:
        tid = r["tid"]
        uname = str(r["user_name"] or "").strip()
        content_xml = str(r["content"] or "")
        item = _build_timeline_item(
            tid, uname, content_xml, contact_rows, biz_index,
            contact_db_path, account_dir, official_usernames,
        )
        if item is not None:
            timeline.append(item)

    if official_usernames and contact_db_path.exists():
        official_rows = _load_contact_rows(contact_db_path, list(official_usernames))
        for item in timeline:
            off = item.get("official")
            if not isinstance(off, dict):
                continue
            u0 = str(off.get("username") or "").strip()
            if not u0:
                continue
            row = official_rows.get(u0)
            if row is None:
                continue
            off["displayName"] = str(_pick_display_name(row, u0)).strip()

    return {
        "timeline": timeline,
        "hasMore": has_more,
        "limit": limit,
        "offset": offset,
        "source": "sqlite",
        "cover": cover_data,
        "covers": covers_data,
    }


def _query_wcdb_snstimeline_table(
    wcdb_conn,
    contact_db_path,
    users,
    kw,
    limit,
    offset,
    cover_data,
    covers_data,
    account_dir,
):
    if not users:
        return None

    def _q(v):
        return "'" + str(v or "").replace("'", "''") + "'"

    try:
        sns_db_path = wcdb_conn.db_storage_dir / "sns" / "sns.db"
        if not sns_db_path.exists():
            sns_db_path = wcdb_conn.db_storage_dir / "sns.db"
    except Exception:
        return None

    if not (sns_db_path.exists() and sns_db_path.is_file()):
        return None

    filters = [
        "content IS NOT NULL",
        "content != ''",
        "content NOT LIKE '%<type>7</type>%'",
    ]

    ulist = [str(u or "").strip() for u in users if str(u or "").strip()]
    if ulist:
        filters.append(f"user_name IN ({','.join([_q(u) for u in ulist])})")

    if kw:
        kw_esc = str(kw).replace("'", "''")
        filters.append(f"content LIKE '%{kw_esc}%'")

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    sql = f"""
        SELECT tid, user_name, content, pack_info_buf
        FROM SnsTimeLine
        {where_sql}
        ORDER BY tid DESC
        LIMIT {int(limit) + 1} OFFSET {int(offset)}
    """

    sql_rows = []
    with wcdb_conn.lock:
        try:
            sql_rows = _wcdb_exec_query(wcdb_conn.handle, kind="media", path=str(sns_db_path), sql=sql)
        except Exception:
            sql = f"""
                SELECT tid, user_name, content
                FROM SnsTimeLine
                {where_sql}
                ORDER BY tid DESC
                LIMIT {int(limit) + 1} OFFSET {int(offset)}
            """
            sql_rows = _wcdb_exec_query(wcdb_conn.handle, kind="media", path=str(sns_db_path), sql=sql)

    if not sql_rows:
        return None

    has_more = len(sql_rows) > int(limit)
    sql_rows = sql_rows[: int(limit)]

    post_usernames = []
    upsert_rows = []

    for rr in sql_rows:
        if not isinstance(rr, dict):
            continue
        uname = str(rr.get("user_name") or rr.get("username") or "").strip()
        if uname:
            post_usernames.append(uname)

    contact_rows = _load_contact_rows(contact_db_path, post_usernames) if contact_db_path.exists() else {}
    biz_index = _get_biz_to_official_index(contact_db_path) if contact_db_path.exists() else {}
    official_usernames = set()

    timeline = []
    for rr in sql_rows:
        if not isinstance(rr, dict):
            continue

        try:
            tid = int(rr.get("tid") or 0)
        except Exception:
            continue

        uname = str(rr.get("user_name") or rr.get("username") or "").strip()
        if not uname:
            continue

        content_xml = _decode_sns_text_blob(rr.get("content"))
        if not content_xml:
            continue

        item = _build_timeline_item(
            tid, uname, content_xml, contact_rows, biz_index,
            contact_db_path, account_dir, official_usernames,
        )
        if item is None:
            continue

        pack = rr.get("pack_info_buf")
        upsert_rows.append((int(tid), uname, content_xml, None if pack is None else str(pack)))
        timeline.append(item)

    if official_usernames and contact_db_path.exists():
        official_rows = _load_contact_rows(contact_db_path, list(official_usernames))
        for item in timeline:
            off = item.get("official")
            if not isinstance(off, dict):
                continue
            u0 = str(off.get("username") or "").strip()
            if not u0:
                continue
            row = official_rows.get(u0)
            if row is None:
                continue
            off["displayName"] = str(_pick_display_name(row, u0) or "").replace("\xa0", " ").strip()

    if upsert_rows:
        _upsert_sns_timeline_rows_to_decrypted_db(account_dir, upsert_rows, source="timeline-wcdb-direct")

    if not timeline:
        return None

    return {
        "timeline": timeline,
        "hasMore": has_more,
        "limit": limit,
        "offset": offset,
        "source": "wcdb-direct",
        "cover": cover_data,
        "covers": covers_data,
    }


def _query_wcdb_realtime_timeline(
    account_dir,
    contact_db_path,
    users,
    kw,
    limit,
    offset,
    cover_data,
    covers_data,
):
    conn = WCDB_REALTIME.ensure_connected(account_dir)
    writeback_rows = []

    cached_posts_total = 0
    if users:
        try:
            with _sns_decrypted_db_lock(Path(account_dir).name):
                cached_posts_total = _count_sns_timeline_posts_in_decrypted_sqlite(
                    account_dir / "sns.db",
                    users=users,
                    kw=kw,
                )
        except Exception:
            cached_posts_total = 0

    def _clean_name(v):
        return str(v or "").replace("\xa0", " ").strip()

    with conn.lock:
        wcdb_fetch_limit = limit + 1
        wcdb_probe_total = None

        if users and offset == 0 and cached_posts_total > int(limit) and cached_posts_total <= DEFAULT_PAGE_LIMIT:
            wcdb_fetch_limit = DEFAULT_PAGE_LIMIT + 1

        rows = _wcdb_get_sns_timeline(
            conn.handle,
            limit=wcdb_fetch_limit,
            offset=offset,
            usernames=users,
            keyword=kw,
        )

        if wcdb_fetch_limit == DEFAULT_PAGE_LIMIT + 1:
            try:
                wcdb_probe_total = len(rows) if isinstance(rows, list) else 0
            except Exception:
                wcdb_probe_total = None

        if (
            users
            and offset == 0
            and isinstance(wcdb_probe_total, int)
            and wcdb_probe_total >= 0
            and wcdb_probe_total <= DEFAULT_PAGE_LIMIT
            and cached_posts_total > wcdb_probe_total
        ):
            try:
                auto_cache_key = _sns_timeline_auto_cache_key(account_dir, users, kw)
                _sns_timeline_auto_cache_set(auto_cache_key, True)
            except Exception as exc:
                print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
            out = _query_decrypted_sqlite(
                account_dir, contact_db_path, users, kw, limit, offset,
                cover_data, covers_data,
            )
            out["source"] = "sqlite-auto"
            return out

        username_by_tid = {}
        content_by_tid = {}
        try:
            sns_db_path = conn.db_storage_dir / "sns" / "sns.db"
            if not sns_db_path.exists():
                sns_db_path = conn.db_storage_dir / "sns.db"

            tids = []
            for r in (rows or [])[: int(limit)]:
                if not isinstance(r, dict):
                    continue
                uname0 = str(r.get("username") or "").strip()
                try:
                    tid_u = int(r.get("id") or 0)
                except Exception:
                    continue
                tid_s = _to_signed_i64(tid_u)
                tids.append(tid_s)
                if uname0:
                    username_by_tid[tid_s] = uname0

            tids = list(dict.fromkeys(tids))
            if tids and sns_db_path.exists():
                in_sql = ",".join([str(x) for x in tids])
                sql = f"SELECT tid, user_name, content, pack_info_buf FROM SnsTimeLine WHERE tid IN ({in_sql})"
                try:
                    sql_rows = _wcdb_exec_query(conn.handle, kind="media", path=str(sns_db_path), sql=sql)
                except Exception:
                    sql = f"SELECT tid, user_name, content FROM SnsTimeLine WHERE tid IN ({in_sql})"
                    sql_rows = _wcdb_exec_query(conn.handle, kind="media", path=str(sns_db_path), sql=sql)
                for rr in sql_rows:
                    try:
                        tid_val = int(rr.get("tid"))
                    except Exception:
                        continue
                    content_xml = _decode_sns_text_blob(rr.get("content"))
                    if content_xml:
                        content_by_tid[tid_val] = content_xml
                    uname1 = str(rr.get("user_name") or rr.get("username") or "").strip()
                    if not uname1:
                        uname1 = username_by_tid.get(tid_val, "")
                    if uname1 and content_xml:
                        pack = rr.get("pack_info_buf")
                        writeback_rows.append((tid_val, uname1, content_xml, None if pack is None else str(pack)))
        except Exception as exc:
            print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)
            content_by_tid = {}
            writeback_rows = []

    has_more = len(rows) > limit
    rows = rows[:limit]

    if writeback_rows:
        _upsert_sns_timeline_rows_to_decrypted_db(
            account_dir,
            writeback_rows,
            source="timeline-wcdb",
        )

    post_usernames = [str((r or {}).get("username") or "").strip() for r in rows if isinstance(r, dict)]
    post_usernames = [u for u in post_usernames if u]
    contact_rows = _load_contact_rows(contact_db_path, post_usernames) if contact_db_path.exists() else {}
    biz_index = _get_biz_to_official_index(contact_db_path) if contact_db_path.exists() else {}
    official_usernames = set()

    timeline = []
    for r in rows:
        if not isinstance(r, dict):
            continue

        uname = str(r.get("username") or "").strip()
        nickname = _clean_name(r.get("nickname"))
        display = nickname or (_pick_display_name(contact_rows.get(uname), uname) if uname else uname)

        create_time = _safe_int(r.get("createTime"))
        content_desc = str(r.get("contentDesc") or "")
        media = r.get("media") if isinstance(r.get("media"), list) else []
        likes = r.get("likes") if isinstance(r.get("likes"), list) else []
        likes = [_clean_name(x) for x in likes if _clean_name(x)]
        comments = r.get("comments") if isinstance(r.get("comments"), list) else []

        video_key = _extract_sns_video_key(r.get("rawXml"))
        if video_key and isinstance(media, list):
            for m0 in media:
                if not isinstance(m0, dict):
                    continue
                if "videoKey" not in m0:
                    m0["videoKey"] = video_key
                lp = m0.get("livePhoto")
                if isinstance(lp, dict):
                    if not str(lp.get("key") or "").strip():
                        lp["key"] = video_key

        location = str(r.get("location") or "")
        source_name = _extract_sns_source_name(r.get("rawXml"))

        post_type = 1
        title = ""
        content_url = ""
        finder_feed = {}
        try:
            tid_u = int(r.get("id") or 0)
            tid_s = (tid_u & 0xFFFFFFFFFFFFFFFF)
            if tid_s >= 0x8000000000000000:
                tid_s -= 0x10000000000000000
            xml = content_by_tid.get(int(tid_s))
            if xml:
                parsed = _parse_timeline_xml(xml, uname)
                if parsed.get("location"):
                    location = str(parsed.get("location") or "")
                sn0 = str(parsed.get("sourceName") or "").strip()
                if sn0:
                    source_name = sn0

                post_type = parsed.get("type", 1)

                if post_type == POST_TYPE_COVER:
                    continue

                title = parsed.get("title", "")
                content_url = parsed.get("contentUrl", "")
                finder_feed = parsed.get("finderFeed", {})

                plikes = parsed.get("likes") or []
                if isinstance(plikes, list) and plikes:
                    likes = plikes

                pcomments = parsed.get("comments") or []
                if isinstance(pcomments, list) and pcomments:
                    comments = pcomments

                pmedia = parsed.get("media") or []
                if isinstance(pmedia, list) and isinstance(media, list) and pmedia:
                    merged = []
                    for i, m0 in enumerate(media):
                        mp = pmedia[i] if i < len(pmedia) else None
                        if not isinstance(mp, dict):
                            merged.append(m0 if isinstance(m0, dict) else {})
                            continue
                        mm = dict(mp)
                        if isinstance(m0, dict):
                            for k in ("url", "thumb"):
                                v = m0.get(k)
                                if v:
                                    mm[k] = v
                            for k, v in m0.items():
                                if k not in mm:
                                    mm[k] = v
                        merged.append(mm)
                    media = merged

                if isinstance(media, list) and (not video_key):
                    video_key_xml = _extract_sns_video_key(xml)
                    if video_key_xml:
                        for m0 in media:
                            if not isinstance(m0, dict):
                                continue
                            if "videoKey" not in m0:
                                m0["videoKey"] = video_key_xml
                            lp = m0.get("livePhoto")
                            if isinstance(lp, dict):
                                if not str(lp.get("key") or "").strip():
                                    lp["key"] = video_key_xml
        except Exception as exc:
            print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)

        official = {}
        if post_type == POST_TYPE_ARTICLE:
            biz = _extract_mp_biz_from_url(content_url)
            info = biz_index.get(biz) if biz else None
            off_username = str(info.get("username") or "").strip() if isinstance(info, dict) else ""
            off_service_type = info.get("serviceType") if isinstance(info, dict) else None
            official = {
                "biz": biz,
                "username": off_username,
                "serviceType": off_service_type,
                "displayName": "",
            }
            if off_username:
                official_usernames.add(off_username)

        pid = str(r.get("id") or "") or str(create_time or "") or uname
        timeline.append(
            {
                "id": pid,
                "tid": r.get("id"),
                "username": uname,
                "displayName": _clean_name(display) or uname,
                "createTime": create_time,
                "contentDesc": content_desc,
                "location": str(location or ""),
                "sourceName": str(source_name or ""),
                "media": media,
                "likes": likes,
                "comments": comments,
                "type": post_type,
                "title": title,
                "contentUrl": content_url,
                "finderFeed": finder_feed,
                "official": official,
            }
        )

    if official_usernames and contact_db_path.exists():
        official_rows = _load_contact_rows(contact_db_path, list(official_usernames))
        for item in timeline:
            off = item.get("official")
            if not isinstance(off, dict):
                continue
            u0 = str(off.get("username") or "").strip()
            if not u0:
                continue
            row = official_rows.get(u0)
            if row is None:
                continue
            off["displayName"] = _clean_name(_pick_display_name(row, u0))

    wcdb_resp = {
        "timeline": timeline,
        "hasMore": has_more,
        "limit": limit,
        "offset": offset,
        "source": "wcdb",
        "cover": cover_data,
        "covers": covers_data,
    }

    if (not timeline) and users:
        try:
            direct = _query_wcdb_snstimeline_table(
                conn, contact_db_path, users, kw, limit, offset,
                cover_data, covers_data, account_dir,
            )
        except Exception:
            direct = None
        if isinstance(direct, dict) and direct.get("timeline"):
            return direct

        try:
            snapshot = _query_decrypted_sqlite(
                account_dir, contact_db_path, users, kw, limit, offset,
                cover_data, covers_data,
            )
        except (HTTPException, Exception):
            snapshot = None
        if isinstance(snapshot, dict) and snapshot.get("timeline"):
            return snapshot

    if users and timeline and (not has_more):
        try:
            with _sns_decrypted_db_lock(Path(account_dir).name):
                cached_total = _count_sns_timeline_posts_in_decrypted_sqlite(
                    account_dir / "sns.db",
                    users=users,
                    kw=kw,
                )
            wcdb_total = int(offset) + int(len(timeline))
            if cached_total > wcdb_total:
                auto_cache_key = _sns_timeline_auto_cache_key(account_dir, users, kw)
                _sns_timeline_auto_cache_set(auto_cache_key, True)
                out = _query_decrypted_sqlite(
                    account_dir, contact_db_path, users, kw, limit, offset,
                    cover_data, covers_data,
                )
                out["source"] = "sqlite-auto"
                return out
        except Exception as exc:
            print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)

    return wcdb_resp


def list_sns_timeline(
    account: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    usernames: Optional[str] = None,
    keyword: Optional[str] = None,
    source: str = "auto",
):
    if limit <= 0:
        raise HTTPException(status_code=400, detail="Invalid limit.")
    if limit > DEFAULT_PAGE_LIMIT:
        limit = DEFAULT_PAGE_LIMIT
    if offset < 0:
        offset = 0

    account_dir = _resolve_account_dir(account)
    contact_db_path = account_dir / "contact.db"

    requested_source = normalize_data_source(source, "auto")
    if requested_source not in {"auto", "realtime", "decrypted"}:
        raise HTTPException(status_code=400, detail="Invalid source. Use auto, realtime, or decrypted.")

    users = _parse_csv_list(usernames)
    kw = str(keyword or "").strip()

    cover_data = None
    covers_data = []
    if offset == 0:
        target_wxid = users[0] if users else account_dir.name
        covers_data = _get_sns_covers(
            account_dir,
            target_wxid,
            limit=20,
            prefer_realtime=requested_source != "decrypted",
        )
        cover_data = covers_data[0] if covers_data else None

    if requested_source == "decrypted":
        decrypted = _query_decrypted_sqlite(
            account_dir, contact_db_path, users, kw, limit, offset,
            cover_data, covers_data,
        )
        decrypted["source"] = "decrypted"
        decrypted.update(
            build_source_fallback_meta(
                requested_source="decrypted",
                active_source="decrypted",
            )
        )
        return decrypted

    auto_cache_key = _sns_timeline_auto_cache_key(account_dir, users, kw) if users else None
    if auto_cache_key is not None and offset > 0:
        try:
            if _sns_timeline_auto_cache_get(auto_cache_key):
                out = _query_decrypted_sqlite(
                    account_dir, contact_db_path, users, kw, limit, offset,
                    cover_data, covers_data,
                )
                out["source"] = "sqlite-auto"
                return out
        except Exception as exc:
            print(f"[warning] {type(exc).__name__}: {exc}", file=sys.stderr)

    try:
        return _query_wcdb_realtime_timeline(
            account_dir, contact_db_path, users, kw, limit, offset,
            cover_data, covers_data,
        )
    except WCDBRealtimeError as e:
        logger.info("[sns] wcdb realtime unavailable: %s", e)
        fallback_reason = str(e)
    except Exception as e:
        logger.warning("[sns] wcdb realtime failed: %s", e)
        fallback_reason = str(e)

    fallback = _query_decrypted_sqlite(
        account_dir, contact_db_path, users, kw, limit, offset,
        cover_data, covers_data,
    )
    retry_after_seconds = 0
    try:
        failure = WCDB_REALTIME.get_recent_failure(account_dir.name)
        retry_after_seconds = int(failure.get("retry_after_seconds") or 0)
    except Exception:
        retry_after_seconds = 0
    fallback.update(
        build_source_fallback_meta(
            requested_source=requested_source,
            active_source="decrypted",
            reason=fallback_reason,
            retry_after_seconds=retry_after_seconds,
        )
    )
    return fallback


def normalize_hex32(value: Any) -> str:
    return _normalize_hex32(str(value or "").strip() if value is not None else None)


def generate_sns_cache_key(tid: str, media_id: str, media_type: int = MEDIA_TYPE_IMAGE) -> str:
    return _generate_sns_cache_key(tid, media_id, media_type)


def resolve_sns_cached_image_path_by_cache_key(
    wxid_dir: Path, cache_key: str, create_time: int = 0
) -> Path | None:
    result = _resolve_sns_cached_image_path_by_cache_key(
        wxid_dir=wxid_dir, cache_key=cache_key, create_time=create_time
    )
    return Path(result) if result else None
