
from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import unquote, urlsplit

import wxmoments
from wechat_decrypt_tool.modules.chat_media import ChatMedia, human_size, safe_filename
from wechat_decrypt_tool.modules.chat_reader import (
    KIND_LABELS,
    ChatStore,
)
from wechat_decrypt_tool.modules.logging_config import get_logger
from wechat_decrypt_tool.modules.wechat_decrypt import decrypt_database_snapshot

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHAT_RUNTIME_DIR = PROJECT_ROOT / "runtime" / "chat"
STICKER_CACHE_DIR = PROJECT_ROOT / "runtime" / "cache" / "stickers"
MAX_EXPORT_SESSIONS = 100
MAX_EXPORT_MESSAGES = 200000
MAX_MEDIA_TOTAL_BYTES = 2 * 1024 ** 3
MAX_MEDIA_FILE_BYTES = 300 * 1024 ** 2
MAX_IMAGE_FULL_BYTES = 4 * 1024 ** 2
MAX_ARTICLE_COUNT = 200
ARTICLE_TIME_BUDGET = 120.0
LINK_THUMB_BUDGET = 60.0

EXPORT_KIND_OPTIONS = [
    ("text", "文字"), ("image", "图片"), ("voice", "语音"), ("video", "视频"),
    ("file", "文件"), ("link", "链接"), ("quote", "引用"), ("forward", "合并转发"),
    ("other", "其他"), ("system", "系统消息"),
]
_KIND_BUCKET = {
    "text": "text", "image": "image", "voice": "voice", "video": "video",
    "file": "file", "link": "link", "quote": "quote", "forward": "forward",
    "system": "system",
}
KIND_BUCKETS = {key for key, _label in EXPORT_KIND_OPTIONS}

LOCAL_TZ = timezone(timedelta(hours=8))

_LOCK = threading.RLock()
_REFRESH_LOCK = threading.Lock()
_STATE: dict[str, Any] = {}
_REFRESH_JOB: Any = None
_EXPORT_JOBS: list[Any] = []


def _now() -> datetime:
    return datetime.now(LOCAL_TZ)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds")


def _clean_name(value: str, fallback: str = "会话") -> str:
    name = wxmoments.sanitize_folder_name(value, fallback)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80] or fallback


def _db_storage_of(wxid_dir: Path) -> Path:
    return wxid_dir / "db_storage"


def iter_chat_accounts(roots: Iterable[Path], account_hint: str = ""
                       ) -> list[wxmoments.AccountInfo]:
    seen: set[str] = set()
    out: list[wxmoments.AccountInfo] = []
    for root in roots:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        try:
            children = [p for p in root.iterdir() if p.is_dir()]
        except OSError:
            continue
        candidates = [root] + [p for p in children if p.name.lower().startswith("wxid_")]
        for wxid_dir in candidates:
            db_storage = _db_storage_of(wxid_dir)
            session_db = db_storage / "session" / "session.db"
            if not session_db.exists():
                session_db = db_storage / "session.db"
            if not session_db.exists():
                continue
            account = wxmoments.clean_account_name(wxid_dir.name)
            if account_hint and account_hint.lower() not in {account.lower(), wxid_dir.name.lower()}:
                continue
            key = os.path.normcase(str(db_storage))
            if key in seen:
                continue
            seen.add(key)
            out.append(wxmoments.AccountInfo(
                account=account, wxid_dir=wxid_dir, db_storage_dir=db_storage))
    return out


def find_chat_account(config: dict[str, Any], *, prefer: str = "") -> wxmoments.AccountInfo:
    hint = (prefer or str(config.get("account") or "")).strip()
    configured_root = str(config.get("wechat_data_root") or "").strip()
    roots = [Path(item.strip()) for item in re.split(r"[;\n]", configured_root) if item.strip()]
    if not roots:
        roots = wxmoments.default_wechat_roots()
    accounts = iter_chat_accounts(roots, hint)
    if not accounts and hint:
        accounts = iter_chat_accounts(roots, "")
    if not accounts:
        raise FileNotFoundError(
            "没有找到本机聊天记录数据库，请确认电脑版微信已登录并保留聊天记录")
    if len(accounts) == 1:
        return accounts[0]

    def weight(item: wxmoments.AccountInfo) -> int:
        total = 0
        for path in item.db_storage_dir.rglob("message_*.db"):
            if re.fullmatch(r"message_\d*\.db", path.name, re.IGNORECASE):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    return max(accounts, key=weight)


def _source_databases(db_storage: Path) -> list[tuple[str, Path]]:
    items: list[tuple[str, Path]] = []
    session_db = db_storage / "session" / "session.db"
    if not session_db.exists():
        session_db = db_storage / "session.db"
    if session_db.exists():
        items.append(("session.db", session_db))
    contact_db = db_storage / "contact" / "contact.db"
    if not contact_db.exists():
        contact_db = db_storage / "contact.db"
    if contact_db.exists():
        items.append(("contact.db", contact_db))
    message_dbs = [p for p in db_storage.rglob("message_*.db")
                   if re.fullmatch(r"message_\d*\.db", p.name, re.IGNORECASE)]
    for path in sorted(message_dbs, key=lambda p: p.name):
        items.append((path.name, path))
    media_dbs = [p for p in db_storage.rglob("media_*.db")
                 if re.fullmatch(r"media_\d*\.db", p.name, re.IGNORECASE)]
    for path in sorted(media_dbs, key=lambda p: p.name):
        items.append((path.name, path))
    return items


def _prune_snapshots(keep: str) -> None:
    CHAT_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    for path in CHAT_RUNTIME_DIR.iterdir():
        if not path.is_dir() or path.name == keep:
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _publish_snapshot(directory: Path, meta: dict[str, Any], account: wxmoments.AccountInfo,
                      warnings: list[str]) -> dict[str, Any]:
    store = ChatStore(directory, self_wxid=account.account, account=account.account)
    sessions = store.sessions()
    snapshot = {
        "snapshot_id": directory.name,
        "dir": directory,
        "store": store,
        "sessions": sessions,
        "account": {
            "account": account.account,
            "wxid_dir": str(account.wxid_dir),
            "db_storage": str(account.db_storage_dir),
        },
        "capabilities": list(store.capabilities),
        "warnings": list(warnings) + list(store.warnings),
        "databases": meta,
        "created_at": _iso(),
        "shard_count": len(store.message_dbs),
    }
    with _LOCK:
        _STATE.clear()
        _STATE.update(snapshot)
    _prune_snapshots(directory.name)
    return snapshot


def _run_refresh(job, account_info: wxmoments.AccountInfo, key: str) -> None:
    global _REFRESH_JOB
    warnings: list[str] = []
    sources = _source_databases(account_info.db_storage_dir)
    if not sources:
        raise RuntimeError("没有找到聊天数据库")
    snapshot_id = f"snap_{_now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}"
    directory = CHAT_RUNTIME_DIR / snapshot_id
    directory.mkdir(parents=True, exist_ok=True)
    job.total = len(sources)
    job.done = 0
    meta: dict[str, Any] = {}
    try:
        for index, (name, source) in enumerate(sources, start=1):
            job.check_canceled()
            job.message = f"读取聊天数据库：{name}"
            item = decrypt_database_snapshot(
                key, source, directory / name,
                progress=lambda pages, name=name: setattr(
                    job, "message", f"读取聊天数据库 {name}：{pages} 页"),
                canceled=job.check_canceled,
            )
            meta[name] = item
            if item.get("status") not in ("ok", "plain"):
                warnings.append(f"{name} 未完整读取：{'; '.join(item.get('diagnostics') or [])}")
            for note in item.get("diagnostics") or []:
                job.add_log(f"{name}：{note}")
            job.done = index
            if name in ("session.db",) and item.get("status") not in ("ok", "plain"):
                raise RuntimeError("会话数据库读取失败，请确认微信已登录后重试")
        job.message = "整理会话列表…"
        snapshot = _publish_snapshot(directory, meta, account_info, warnings)
        job.results.append({
            "account": account_info.account,
            "sessions": len(snapshot["sessions"]),
            "shards": snapshot["shard_count"],
            "dir": str(directory),
        })
        for warning in snapshot["warnings"]:
            job.add_log(warning)
        job.finish("done", f"聊天记录：{len(snapshot['sessions'])} 个会话 · "
                           f"{snapshot['shard_count']} 个消息库")
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        with _LOCK:
            if _REFRESH_JOB is job:
                _REFRESH_JOB = None


def status() -> dict[str, Any]:
    with _LOCK:
        state = dict(_STATE)
    if not state:
        return {"ok": True, "ready": False, "account": "", "snapshot_id": "",
                "sessions": 0, "refresh_job": _REFRESH_JOB.id if _REFRESH_JOB else "",
                "warnings": [], "capabilities": []}
    return {
        "ok": True,
        "ready": True,
        "account": state["account"]["account"],
        "snapshot_id": state["snapshot_id"],
        "sessions": len(state["sessions"]),
        "visible_sessions": sum(1 for item in state["sessions"] if not item.get("folded")),
        "shards": state["shard_count"],
        "created_at": state["created_at"],
        "warnings": state["warnings"],
        "capabilities": state["capabilities"],
        "refresh_job": _REFRESH_JOB.id if _REFRESH_JOB else "",
    }


def start_refresh(payload: dict, *, start_job: Callable, acquire_key: Callable) -> dict[str, Any]:
    global _REFRESH_JOB
    with _REFRESH_LOCK:
        if _REFRESH_JOB is not None and getattr(_REFRESH_JOB, "status", "") == "running":
            return {"ok": True, "job": _REFRESH_JOB.id}
        try:
            config = wxmoments.load_config(wxmoments.DEFAULT_CONFIG)
            prefer = str(payload.get("account") or "").strip()
            account_info = find_chat_account(config, prefer=prefer)
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
        try:
            key = acquire_key(account_info)
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}
        _REFRESH_JOB = start_job(1, _run_refresh, account_info, key)
        return {"ok": True, "job": _REFRESH_JOB.id, "account": account_info.account}


def _store() -> ChatStore:
    with _LOCK:
        store = _STATE.get("store")
    if store is None:
        raise RuntimeError("请先点击「刷新」")
    return store


def _sessions_index() -> dict[str, dict[str, Any]]:
    with _LOCK:
        sessions = list(_STATE.get("sessions") or [])
    return {item["conversation_id"]: item for item in sessions}


def _kind_bucket(kind: str | None) -> str:
    return _KIND_BUCKET.get((kind or "").strip(), "other")


def _parse_range(filters: dict[str, Any]) -> dict[str, Any]:
    start_raw = str(filters.get("start_date") or "").strip()
    end_raw = str(filters.get("end_date") or "").strip()
    start = wxmoments.parse_datetime(start_raw, end_of_day=False) if start_raw else None
    end = wxmoments.parse_datetime(end_raw, end_of_day=True) if end_raw else None
    if start and end and end < start:
        raise ValueError("结束日期需晚于开始日期")
    kind = str(filters.get("kind") or "all").strip() or "all"
    if kind not in KIND_LABELS and kind != "all":
        kind = "all"
    keyword = str(filters.get("keyword") or "").strip()[:60]
    kinds: set[str] | None = None
    raw_kinds = filters.get("kinds")
    if isinstance(raw_kinds, (list, tuple, set)):
        picked = {str(item).strip() for item in raw_kinds} & KIND_BUCKETS
        if picked and picked != KIND_BUCKETS:
            kinds = picked
        elif not picked:
            kinds = set()
    return {
        "start": int(start.timestamp()) if start else None,
        "end": int(end.timestamp()) if end else None,
        "kind": kind,
        "kinds": kinds,
        "keyword": keyword,
    }


def _scope_match(session: dict[str, Any], scope: str) -> bool:
    if scope in ("", "all"):
        return True
    if scope == "chat":
        return session["kind"] in ("private", "group")
    return session["kind"] == scope


def sessions(payload: dict) -> dict[str, Any]:
    with _LOCK:
        state = dict(_STATE)
    if not state:
        raise RuntimeError("请先点击「刷新」")
    query = str(payload.get("query") or "").strip().casefold()[:60]
    scope = str(payload.get("scope") or "all").strip()
    page = max(0, int(payload.get("page") or 0))
    page_size = max(1, min(int(payload.get("page_size") or 50), 5000))
    window = _parse_range(payload)
    has_range = window["start"] is not None or window["end"] is not None
    show_folded = bool(payload.get("show_folded"))
    store = _store() if has_range else None
    rows: list[dict[str, Any]] = []
    for item in state["sessions"]:
        if item.get("folded") and not show_folded:
            continue
        if not _scope_match(item, scope):
            continue
        if query:
            haystack = f"{item['display']} {item['conversation_id']} {item['last_summary']}".casefold()
            if query not in haystack:
                continue
        if store is not None and not store.has_messages_between(
                item["conversation_id"], window["start"], window["end"]):
            continue
        rows.append(item)
    total = len(rows)
    window_rows = rows[page * page_size:(page + 1) * page_size]
    trimmed = [{
        "conversation_id": item["conversation_id"],
        "display": item["display"],
        "kind": item["kind"],
        "last_time": item["last_time"],
        "last_summary": item["last_summary"],
        "unread": item["unread"],
        "folded": bool(item.get("folded")),
    } for item in window_rows]
    return {"ok": True, "snapshot_id": state["snapshot_id"], "sessions": trimmed,
            "all_ids": [item["conversation_id"] for item in rows],
            "total": total, "page": page, "page_size": page_size}


def messages(payload: dict) -> dict[str, Any]:
    store = _store()
    conversation_id = str(payload.get("conversation_id") or "").strip()
    index = _sessions_index()
    if conversation_id not in index:
        raise ValueError("没有这个会话，请刷新后重试")
    window = _parse_range(payload.get("filter") or {})
    limit = max(1, min(int(payload.get("limit") or 60), 1000))
    scan_budget = max(600, min(int(payload.get("scan_budget") or 6000), 60000))
    result = store.messages(
        conversation_id,
        cursor=payload.get("cursor") or None,
        limit=limit,
        start=window["start"],
        end=window["end"],
        kind=window["kind"],
        keyword=window["keyword"],
        scan_budget=scan_budget,
    )
    result["ok"] = True
    result["conversation"] = index[conversation_id]
    result["snapshot_id"] = _STATE.get("snapshot_id")
    return result


def _media_store() -> "ChatMedia":
    with _LOCK:
        state = dict(_STATE)
        media = state.get("media")
    if not state:
        raise RuntimeError("请先点击「刷新」")
    if media is None:
        account = state["account"] or {}
        media = ChatMedia(account.get("wxid_dir"))
        with _LOCK:
            _STATE["media"] = media
    return media


def media_blob(payload: dict) -> tuple[bytes, str]:
    media = _media_store()
    kind = str(payload.get("kind") or "image_full").strip()
    session = str(payload.get("session") or "").strip()
    if not session:
        raise ValueError("缺少会话标识")
    try:
        create_time = int(payload.get("create_time") or 0)
    except (TypeError, ValueError):
        create_time = 0
    if kind == "image_full":
        return media.image_bytes(session, create_time, prefer_thumb=False)
    if kind == "voice":
        store = _store()
        return media.voice_bytes(store.media_dbs, session,
                                 str(payload.get("server_id") or ""))[:2]
    if kind == "sticker":
        return _sticker_bytes(str(payload.get("md5") or ""),
                              str(payload.get("url") or ""),
                              str(payload.get("thumb") or ""))
    if kind == "video_thumb":
        thumb = media.video_thumb(create_time, str(payload.get("md5") or ""))
        if thumb is None:
            return b"", ""
        return media.read_media(thumb)
    if kind == "file":
        path = media.file_path(str(payload.get("filename") or ""),
                               payload.get("size") or 0)
        if path is None:
            return b"", ""
        try:
            return path.read_bytes(), "application/octet-stream"
        except OSError:
            return b"", ""
    raise ValueError("不支持的媒体类型")


def _iter_all(store: ChatStore, conversation_id: str, window: dict[str, Any],
              job, on_page: Callable[[int], None] | None = None
              ) -> Iterator[dict[str, Any]]:
    cursor = None
    count = 0
    scanned = 0
    allowed = window.get("kinds")
    while True:
        job.check_canceled()
        page = store.messages(
            conversation_id, cursor=cursor, limit=300,
            start=window["start"], end=window["end"], kind=window["kind"],
            scan_budget=20000,
        )
        for item in page["messages"]:
            scanned += 1
            if scanned > MAX_EXPORT_MESSAGES:
                return
            if allowed is not None and _kind_bucket(item.get("kind")) not in allowed:
                continue
            count += 1
            yield item
        if on_page is not None:
            on_page(scanned)
        if page.get("truncated"):
            raise RuntimeError("筛选范围内消息过多，请缩小时间范围后重试")
        if not page.get("has_more"):
            return
        next_cursor = page.get("next_cursor")
        if not next_cursor or next_cursor == cursor:
            return
        cursor = next_cursor


def _fmt_time(timestamp: int) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(int(timestamp), LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_clock(timestamp: int) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(int(timestamp), LOCAL_TZ).strftime("%H:%M")


def _fmt_day(timestamp: int) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(int(timestamp), LOCAL_TZ).strftime("%Y-%m-%d")


PLACEHOLDERS = {
    "image": "[图片]", "voice": "[语音]", "video": "[视频]", "sticker": "[动画表情]",    "file": "[文件]", "link": "[链接]", "card": "[名片]", "location": "[位置]",
    "system": "[系统消息]", "call": "[音视频通话]", "redpacket": "[红包]",
    "transfer": "[转账]", "miniprogram": "[小程序]", "channel": "[视频号]",
    "live": "[直播]", "note": "[笔记]", "solitaire": "[接龙]", "announce": "[群公告]",
    "music": "[音乐]", "app": "[应用消息]", "quote": "[引用]", "other": "[暂不支持]",
}


_BODY_SKIP = frozenset({"image", "voice", "video", "file", "link",
                        "miniprogram", "channel", "sticker"})


def _message_body(message: dict[str, Any]) -> str:
    kind = message.get("kind") or "other"
    text = message.get("text") or ""
    extra = message.get("extra") or {}
    if kind in ("text", "system"):
        return text
    if kind == "voice":
        duration = extra.get("duration")
        return f"[语音 {duration}\"]" if duration else "[语音]"
    if kind == "forward":
        items = extra.get("forward_items") or []
        lines = [message.get("title") or "转发的聊天记录"]
        for item in items[:60]:
            lines.append(f"　{item.get('name') or ''} {item.get('title') or ''}".strip())
        if extra.get("forward_truncated"):
            lines.append("　（已截断）")
        return "\n".join(lines)
    if kind == "file":
        name = extra.get("filename") or text
        size = human_size(extra.get("size"))
        return f"[文件] {name}" + (f" · {size}" if size else "")
    if kind == "card":
        name = extra.get("nickname") or text or message.get("title") or ""
        return f"[名片] {name}".strip()
    if kind == "location":
        label = extra.get("poi") or extra.get("label") or text or ""
        return f"[位置] {label}".strip()
    placeholder = PLACEHOLDERS.get(kind, "[消息]")
    if kind == "quote":
        quote = extra.get("quote") or {}
        head = f"「{quote.get('sender') or ''}：{quote.get('text') or ''}」" if quote else ""
        return "\n".join(part for part in (head, text) if part)
    detail = text or message.get("title") or ""
    return f"{placeholder} {detail}".strip() if detail else placeholder


def _html_escape(value: str) -> str:
    return html.escape(str(value or ""), quote=True)


def _avatar_text(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "?"
    return name[-1] if len(name) > 2 else name[:2]


def _avatar_class(name: str) -> int:
    return sum(ord(ch) for ch in (name or "?")) % 6


BASE_CSS = """:root{
  --brand:#07c160;--bg:#ededed;--panel:#fff;--text:#111;
  --muted:#8a9099;--line:#e4e6ea;
  --bubble:#fff;--bubble-text:#111;
  --mine:#95ec69;--mine-text:#0d1a0a;
  --sys-bg:rgba(0,0,0,.045);
  --shadow:0 1px 2px rgba(0,0,0,.06);
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#111214;--panel:#1b1d21;--text:#e9edf2;--muted:#9aa3ad;--line:#2a2e35;
    --bubble:#24272c;--bubble-text:#e9edf2;--mine:#3eb575;--mine-text:#08130a;
    --sys-bg:rgba(255,255,255,.07);--shadow:0 1px 2px rgba(0,0,0,.3);
  }
  .quote{color:var(--text);opacity:.72}
  .pdf{color:#4ade80;background:rgba(74,222,128,.12);border-color:rgba(74,222,128,.32)}
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:inherit}
.topbar{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--line)}
.bar{max-width:880px;margin:0 auto;display:flex;align-items:center;gap:11px;padding:10px 16px}
.tinfo{min-width:0}
.bar h1{font-size:15.5px;margin:0;font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.bar .sub{font-size:11.5px;color:var(--muted);margin-top:1px;line-height:1.4;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.grow{flex:1}
.back{flex:none;width:28px;height:28px;border-radius:8px;display:flex;align-items:center;
  justify-content:center;color:var(--muted);text-decoration:none;font-size:17px;line-height:1}
.back:hover{background:var(--sys-bg);color:var(--text)}
.search{display:flex;align-items:center;gap:6px;background:var(--bg);border:1px solid var(--line);
  border-radius:999px;padding:5px 12px}
.search svg{width:14px;height:14px;stroke:var(--muted);fill:none;stroke-width:1.8;flex:none}
.search input{border:0;background:transparent;color:inherit;font:inherit;font-size:12.5px;
  outline:none;width:150px;padding:0}
.search input::placeholder{color:var(--muted)}
#cnt{font-size:11.5px;color:var(--muted);white-space:nowrap}
.wrap{max-width:880px;margin:0 auto;padding:10px 16px 40px}
.ava{flex:0 0 40px;width:40px;height:40px;border-radius:5px;display:flex;align-items:center;
  justify-content:center;color:#fff;font-size:14px;font-weight:600;overflow:hidden;
  user-select:none;letter-spacing:.5px}
.ava.a0{background:linear-gradient(150deg,#6f9bf5,#4a7fe0)}
.ava.a1{background:linear-gradient(150deg,#46c48c,#2aa870)}
.ava.a2{background:linear-gradient(150deg,#eda85c,#dd8a34)}
.ava.a3{background:linear-gradient(150deg,#b57ce0,#9355cc)}
.ava.a4{background:linear-gradient(150deg,#e57a81,#cf5a63)}
.ava.a5{background:linear-gradient(150deg,#4db6c6,#2f97a9)}
.ava.me{background:linear-gradient(150deg,#07c160,#05a552)}
.day{text-align:center;margin:20px 0 12px}
.day span{display:inline-block;background:var(--sys-bg);color:var(--muted);font-size:11px;
  padding:3px 10px;border-radius:999px;letter-spacing:.3px}
.msg{display:flex;gap:10px;margin:13px 0;align-items:flex-start}
.msg.me{flex-direction:row-reverse}
.msg.cont{margin-top:5px}
.stack{max-width:min(74%,620px);display:flex;flex-direction:column;align-items:flex-start;min-width:0}
.msg.me .stack{align-items:flex-end}
.who{font-size:11.5px;color:var(--muted);margin:1px 2px 4px;line-height:1.2}
.msg.cont .who{display:none}
.bubble{position:relative;max-width:100%;background:var(--bubble);color:var(--bubble-text);
  border-radius:8px;padding:9px 12px;font-size:14.5px;line-height:1.58;white-space:pre-wrap;
  overflow-wrap:anywhere;box-shadow:var(--shadow)}
.msg.me .bubble{background:var(--mine);color:var(--mine-text)}
.bubble::after{content:"";position:absolute;top:13px;width:0;height:0;border:6px solid transparent}
.msg:not(.me) .bubble::after{left:-10px;border-right-color:var(--bubble)}
.msg.me .bubble::after{right:-10px;border-left-color:var(--mine)}
.bubble.naked{background:transparent!important;box-shadow:none!important;padding:0;border-radius:0}
.bubble.naked::after{display:none}
.clock{font-size:10.5px;color:var(--muted);margin:3px 3px 0;letter-spacing:.2px}
.sys{display:flex;justify-content:center;margin:12px 0}
.sys span{max-width:78%;background:var(--sys-bg);color:var(--muted);font-size:11.5px;
  padding:4px 10px;border-radius:6px;text-align:center;line-height:1.55}
.att img{display:block;max-width:260px;max-height:320px;border-radius:6px;cursor:zoom-in;
  box-shadow:var(--shadow);background:var(--line)}
.att img:hover{filter:brightness(.96)}
.att video{display:block;max-width:280px;max-height:320px;border-radius:6px;background:#000}
.sticker{display:block;max-width:150px;max-height:150px;background:transparent}
.sticker-miss{display:inline-block;padding:9px 13px;border-radius:8px;background:var(--sys-bg);
  color:var(--muted);font-size:12px}
.att .cap{font-size:11.5px;color:var(--muted);margin-top:5px}
.voice{display:flex;align-items:center;gap:10px;min-width:92px;cursor:pointer;user-select:none}
.voice .wave{display:flex;align-items:flex-end;gap:2.5px;height:15px}
.voice .wave i{width:2.5px;border-radius:1px;background:currentColor;opacity:.5}
.voice .wave i:nth-child(1){height:5px}
.voice .wave i:nth-child(2){height:9px}
.voice .wave i:nth-child(3){height:14px}
.voice .wave i:nth-child(4){height:8px}
.voice .wave i:nth-child(5){height:12px}
.voice .wave i:nth-child(6){height:6px}
.voice .wave i:nth-child(7){height:11px}
.voice .wave i:nth-child(8){height:15px}
.voice.playing .wave i{animation:beat .9s ease-in-out infinite}
.voice.playing .wave i:nth-child(2){animation-delay:.1s}
.voice.playing .wave i:nth-child(3){animation-delay:.2s}
.voice.playing .wave i:nth-child(4){animation-delay:.3s}
.voice.playing .wave i:nth-child(5){animation-delay:.4s}
.voice.playing .wave i:nth-child(6){animation-delay:.5s}
.voice.playing .wave i:nth-child(7){animation-delay:.6s}
.voice.playing .wave i:nth-child(8){animation-delay:.7s}
@keyframes beat{0%,100%{transform:scaleY(.55)}50%{transform:scaleY(1)}}
.voice .vs{font-size:12px;opacity:.7;margin-left:auto}
.voice .vnote{font-size:11.5px;opacity:.6}
.file{display:flex;align-items:center;gap:11px;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;padding:11px 12px;min-width:232px;max-width:330px;text-decoration:none;
  color:inherit;box-shadow:var(--shadow);transition:border-color .16s,transform .16s}
.file:hover{border-color:#c3ced8;transform:translateY(-1px)}
.file .ext{flex:0 0 40px;height:46px;border-radius:6px;background:#e64340;color:#fff;font-size:10px;
  display:flex;align-items:center;justify-content:center;text-transform:uppercase;
  font-weight:700;letter-spacing:.4px}
.file .ext.pdf{background:#e5484d}
.file .ext.doc{background:#3d7dd8}
.file .ext.xls{background:#2f9e53}
.file .ext.ppt{background:#e07a1f}
.file .ext.zip{background:#c98a1a}
.file .ext.media{background:#7a63d8}
.file .ext.audio{background:#d06a3d}
.file .fbody{flex:1;min-width:0}
.file .fname{display:block;font-size:13.5px;line-height:1.4;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.file .fsize{display:block;font-size:11.5px;color:var(--muted);margin-top:2px}
.file .dl{flex:none;width:24px;height:24px;border-radius:50%;background:var(--sys-bg);
  color:var(--text);opacity:.5;display:flex;align-items:center;justify-content:center;font-size:13px}
.file.missing{opacity:.7;cursor:default}
.file.missing:hover{transform:none;border-color:var(--line)}
.link{display:flex;gap:12px;text-decoration:none;color:inherit;background:var(--panel);
  border:1px solid var(--line);border-radius:8px;padding:11px 12px;min-width:250px;
  max-width:400px;box-shadow:var(--shadow);transition:border-color .16s}
.link:hover{border-color:#c3ced8}
.link .lbody{flex:1;min-width:0}
.link .lt{font-size:14px;font-weight:600;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden}
.link .ld{font-size:12px;color:var(--muted);margin-top:5px;line-height:1.5;display:-webkit-box;
  -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.link .ls{display:block;font-size:11px;color:var(--muted);margin-top:7px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.link .lbadge{display:inline-block;margin-right:6px;padding:1px 5px;border-radius:4px;
  background:rgba(7,193,96,.14);color:#078d47;font-size:10px;font-weight:600;vertical-align:1px}
.link .lsaved{display:inline-block;margin-right:6px;padding:1px 5px;border-radius:4px;
  background:rgba(64,120,220,.14);color:#3a6fc4;font-size:10px;font-weight:600;vertical-align:1px}
.link.mini .lsaved{display:none}
.voice .vdl{font-size:10.5px;color:inherit;opacity:.55;border:1px solid currentColor;
  border-radius:999px;padding:0 6px;line-height:15px;text-decoration:none;flex:none}
.voice .vdl:hover{opacity:.9}
.link .lsrc{color:var(--muted)}
.link.mini{border-style:dashed;max-width:360px}
.link.mini .lbadge{background:rgba(85,102,217,.14);color:#4d5ecb}
.link .lthumb{flex:none;width:52px;height:52px;border-radius:5px;object-fit:cover;background:var(--line)}
.quote{background:var(--sys-bg);border-radius:5px;padding:6px 9px;margin-bottom:6px;
  color:var(--muted);font-size:12.5px;line-height:1.5;display:-webkit-box;-webkit-line-clamp:4;
  -webkit-box-orient:vertical;overflow:hidden}
.quote b{font-weight:500;opacity:.8}
.forward{background:var(--panel);border:1px solid var(--line);border-radius:8px;min-width:252px;
  max-width:380px;overflow:hidden;box-shadow:var(--shadow)}
.forward .fh{padding:10px 12px 6px;font-size:13.5px;font-weight:600;line-height:1.4}
.forward ul{list-style:none;margin:0;padding:0 12px 10px;font-size:12.5px;color:var(--muted)}
.forward li{padding:3px 0;line-height:1.5;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.forward li b{font-weight:500;color:var(--text);opacity:.8;margin-right:4px}
.forward .ff{border-top:1px solid var(--line);padding:7px 12px;font-size:11px;color:var(--muted);
  display:flex;justify-content:space-between;align-items:center}
.chip{display:inline-flex;align-items:center;gap:7px;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;padding:8px 11px;font-size:12.5px;color:var(--text);box-shadow:var(--shadow)}
.chip b{font-weight:500;color:var(--muted);font-size:11.5px}
.mark{color:var(--muted);font-size:12px}
.foot{max-width:880px;margin:0 auto;padding:16px 16px 34px;border-top:1px solid var(--line);
  color:var(--muted);font-size:11.5px;text-align:center;line-height:1.7}
.foot a{color:var(--brand);text-decoration:none}
.foot a:hover{text-decoration:underline}
#lb{position:fixed;inset:0;background:rgba(10,12,14,.92);display:none;align-items:center;
  justify-content:center;z-index:99;cursor:zoom-out;padding:24px}
#lb.on{display:flex}
#lb img{max-width:94vw;max-height:88vh;border-radius:8px;box-shadow:0 18px 50px rgba(0,0,0,.5)}
.hero{padding:24px 0 4px}
.hero h1{font-size:22px;margin:0 0 6px;letter-spacing:-.2px}
.hero .sub{font-size:12.5px;color:var(--muted)}
.stats{display:flex;gap:10px;margin:16px 0 18px;flex-wrap:wrap}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:9px 14px;
  min-width:92px}
.stat b{display:block;font-size:17px;line-height:1.25}
.stat span{font-size:11.5px;color:var(--muted)}
.cards{display:flex;flex-direction:column;gap:10px}
.card{display:flex;align-items:center;gap:12px;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:11px 13px;transition:border-color .16s,box-shadow .16s,transform .16s}
.card:hover{border-color:#c3ced8;box-shadow:0 6px 18px rgba(0,0,0,.06);transform:translateY(-1px)}
.cmain{display:flex;align-items:center;gap:12px;flex:1;min-width:0;text-decoration:none;color:inherit}
.cbody{flex:1;min-width:0}
.ctitle{font-size:14.5px;font-weight:600;display:flex;align-items:center;gap:7px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{flex:none;font-size:10.5px;font-weight:500;color:var(--muted);border:1px solid var(--line);
  border-radius:999px;padding:0 7px;line-height:17px}
.csum{font-size:12.5px;color:var(--muted);margin-top:3px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.cmeta{flex:none;display:flex;flex-direction:column;align-items:flex-end;gap:8px}
.ccount{font-size:11.5px;color:var(--muted);white-space:nowrap}
.pdf{font-size:11px;color:#07934a;border:1px solid rgba(7,193,96,.28);
  border-radius:999px;padding:1px 9px;text-decoration:none;background:rgba(7,193,96,.08);
  white-space:nowrap;transition:.15s}
.pdf:hover{background:rgba(7,193,96,.16);border-color:rgba(7,193,96,.5)}
@media (max-width:640px){
  .bar{padding:8px 10px;gap:8px}
  .bar h1{font-size:14px}
  .bar .sub{display:none}
  .search input{width:92px}
  .wrap{padding:6px 10px 30px}
  .stack{max-width:calc(100% - 52px)}
  .att img{max-width:200px}
  .file{min-width:0;max-width:100%}
  .hero h1{font-size:19px}
}
@media print{
  @page{margin:14mm 12mm}
  :root{--bg:#fff;--panel:#fff;--text:#111;--muted:#666;--line:#dcdcdc;--bubble:#fff;
    --sys-bg:#f2f2f2;--shadow:none}
  body{background:#fff;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .topbar{position:static;border-bottom:1px solid #ddd}
  .bar{padding:0 0 10px}
  .search,.back,#lb,#cnt,.foot a{display:none!important}
  .wrap{max-width:none;padding:0}
  .msg,.sys,.day,.card{break-inside:avoid;page-break-inside:avoid}
  .bubble,.file,.link,.forward,.chip,.card,.att img{box-shadow:none}
  .msg{margin:10px 0}
  .foot{border-top:0;padding:12px 0 0;text-align:left}
  a{text-decoration:none}
}
"""

BASE_JS = """(function(){
function flt(){
  var q=(document.getElementById('q').value||'').trim().toLowerCase();
  var log=document.getElementById('log'),n=0;
  if(log){log.querySelectorAll('.msg,.sys').forEach(function(e){
    var hit=!q||(e.dataset.t||'').indexOf(q)>=0;
    e.style.display=hit?'':'none';if(hit)n++;});}
  var c=document.getElementById('cnt');
  if(c){c.textContent=q?('匹配 '+n+' 条'):'';}
}
function clean(){
  var lb=document.getElementById('lb');
  if(lb){lb.classList.remove('on');lb.querySelector('img').src='';}
}
document.addEventListener('DOMContentLoaded',function(){
  var lb=document.getElementById('lb');
  if(lb){lb.onclick=clean;}
  document.addEventListener('keydown',function(e){if(e.key==='Escape')clean();});
  document.querySelectorAll('.att img').forEach(function(img){
    img.addEventListener('click',function(){
      if(!lb)return;lb.querySelector('img').src=img.dataset.full||img.src;lb.classList.add('on');});
  });
  var q=document.getElementById('q');
  if(q){q.addEventListener('input',flt);}
  var current=null;
  document.querySelectorAll('.voice').forEach(function(box){
    var audio=box.querySelector('audio');
    if(!audio)return;
    box.addEventListener('click',function(e){
      if(e.target.closest&&e.target.closest('a'))return;
      if(current&&current!==box){current.classList.remove('playing');
        var prev=current.querySelector('audio');if(prev)prev.pause();}
      if(audio.paused){
        audio.play().then(function(){box.classList.add('playing');current=box;}).catch(function(){});
      }else{audio.pause();box.classList.remove('playing');}
    });
    audio.addEventListener('ended',function(){box.classList.remove('playing');});
  });
});
})();
"""


_EXT_CLASSES = {
    "pdf": "pdf",
    "doc": "doc", "docx": "doc", "rtf": "doc", "txt": "doc", "md": "doc",
    "xls": "xls", "xlsx": "xls", "csv": "xls",
    "ppt": "ppt", "pptx": "ppt",
    "zip": "zip", "rar": "zip", "7z": "zip", "tar": "zip", "gz": "zip",
    "mp4": "media", "mov": "media", "avi": "media", "mkv": "media", "webm": "media",
    "jpg": "media", "jpeg": "media", "png": "media", "gif": "media", "webp": "media",
    "mp3": "audio", "wav": "audio", "silk": "audio", "amr": "audio", "m4a": "audio",
}


def _file_ext_class(ext: str) -> str:
    return _EXT_CLASSES.get((ext or "").lower(), "")


def _link_display(title: str, url: str) -> tuple[str, str]:
    raw = (title or "").strip()
    source = raw or (url or "").strip()
    if raw and not re.match(r"^https?://", raw, re.I):
        return raw, (url or "").strip()
    if not source:
        return "链接", ""
    try:
        parsed = urlsplit(source)
        host = parsed.netloc or source
        path = unquote(parsed.path or "").strip("/")
    except Exception:
        return source, source
    if path:
        if len(path) > 30:
            path = path[:30] + "…"
        return f"{host}/{path}", source
    return host, source


def _media_html(item: dict[str, Any]) -> str:
    media = item.get("media") or {}
    kind = item.get("kind") or "other"
    extra = item.get("extra") or {}
    if kind == "image":
        thumb = media.get("thumb") or media.get("image")
        full = media.get("image") or thumb
        if not full:
            return '<div class="mark">图片未缓存到本机</div>'
        return (f'<div class="att"><img loading="lazy" src="{_html_escape(full)}" '
                f'data-full="{_html_escape(full)}" alt="图片"></div>')
    if kind == "video":
        if media.get("video"):
            return (f'<div class="att"><video controls preload="metadata" '
                    f'src="{_html_escape(media["video"])}"></video></div>')
        if media.get("thumb"):
            return (f'<div class="att"><img loading="lazy" src="{_html_escape(media["thumb"])}" '
                    f'data-full="{_html_escape(media["thumb"])}" alt="视频封面">'
                    f'<div class="cap">视频文件未保存到本机</div></div>')
        return '<div class="mark">视频未缓存到本机</div>'
    if kind == "file":
        name = extra.get("filename") or item.get("text") or "文件"
        size = human_size(extra.get("size"))
        ext = (extra.get("ext") or Path(name).suffix.lstrip(".") or "file")[:4]
        cls = _file_ext_class(ext)
        holder = (f'<span class="ext {cls}">{_html_escape(ext)}</span>'
                  f'<span class="fbody"><span class="fname">{_html_escape(name)}</span>'
                  f'<span class="fsize">{_html_escape(size or "点击保存")}</span></span>')
        if media.get("file"):
            return (f'<a class="file" href="{_html_escape(media["file"])}" '
                    f'download="{_html_escape(name)}">{holder}'
                    f'<span class="dl">↓</span></a>')
        note = f"{size} · 未包含在导出中" if size else "未包含在导出中"
        return (f'<div class="file missing" title="{_html_escape(note)}">{holder}'
                f'<span class="dl">–</span></div>')
    if kind == "link":
        url = (extra.get("url") or media.get("url") or "").strip()
        title, raw = _link_display(item.get("title") or item.get("text") or "", url)
        descr = (extra.get("descr") or "").strip()
        article = (media.get("article") or "").strip()
        target = article or url or raw
        source = (extra.get("source") or extra.get("app") or "").strip()
        if not source:
            try:
                source = urlsplit(raw).netloc if raw else ""
            except Exception:
                source = ""
        desc = f'<span class="ld">{_html_escape(descr)}</span>' if descr else ""
        badge = "公众号" if extra.get("official") else ""
        tag = f'<b class="lbadge">{badge}</b>' if badge else ""
        thumb = (media.get("thumb") or "").strip()
        cover = (f'<img class="lthumb" loading="lazy" src="{_html_escape(thumb)}" '
                 f'alt="">' if thumb else "")
        href = _html_escape(target) if target else "#"
        return (f'<a class="link" href="{href}" target="_blank" rel="noreferrer">'
                f'<span class="lbody"><span class="lt">{_html_escape(title)}</span>{desc}'
                f'<span class="ls">{tag}{_html_escape(source)}</span></span>'
                f'{cover}</a>')
    if kind == "miniprogram":
        title = item.get("title") or item.get("text") or "小程序"
        source = (extra.get("source") or extra.get("app") or "").strip()
        url = (extra.get("url") or media.get("url") or "").strip()
        src = f'<span class="lsrc">{_html_escape(source)}</span>' if source else ""
        thumb = (media.get("thumb") or "").strip()
        cover = (f'<img class="lthumb" loading="lazy" src="{_html_escape(thumb)}" '
                 f'alt="">' if thumb else "")
        body = (f'<span class="lbody"><span class="lt">{_html_escape(title)}</span>'
                f'<span class="ls"><b class="lbadge">小程序</b>{src}</span>'
                f'<span class="ld">小程序需在微信中打开，此处仅保留卡片信息</span></span>')
        if url:
            return (f'<a class="link mini" href="{_html_escape(url)}" target="_blank" '
                    f'rel="noreferrer">{body}{cover}</a>')
        return f'<div class="link mini">{body}{cover}</div>'
    if kind == "card":
        name = extra.get("nickname") or item.get("text") or ""
        return f'<span class="chip"><b>名片</b>{_html_escape(name)}</span>'
    if kind == "location":
        label = extra.get("poi") or item.get("text") or ""
        return f'<span class="chip"><b>位置</b>{_html_escape(label)}</span>'
    if kind in ("redpacket", "transfer", "call", "note", "solitaire", "announce",
                "music", "live", "channel"):
        label = KIND_LABELS.get(kind) or PLACEHOLDERS.get(kind, "消息")
        detail = (item.get("text") or item.get("title") or "").strip()
        return (f'<span class="chip"><b>{_html_escape(label)}</b>'
                f'{_html_escape(detail)}</span>')
    return ""


def _voice_html(item: dict[str, Any]) -> str:
    media = item.get("media") or {}
    extra = item.get("extra") or {}
    try:
        secs = int(float(extra.get("duration") or 0))
    except (TypeError, ValueError):
        secs = 0
    width = 98 + min(max(secs, 1), 60) * 2
    wave = "".join("<i></i>" for _ in range(8))
    label = f'{secs}"' if secs else "语音"
    src = media.get("voice")
    if not src:
        return (f'<div class="bubble"><span class="voice" style="min-width:0">'
                f'<span class="wave">{wave}</span>'
                f'<span class="vnote">语音未缓存到本机</span>'
                f'<span class="vs">{label}</span></span></div>')
    ext = Path(src).suffix.lstrip(".").lower() or "mp3"
    return (f'<div class="bubble voice" role="button" tabindex="0" style="width:{width + 42}px">'
            f'<span class="wave">{wave}</span>'
            f'<a class="vdl" href="{_html_escape(src)}" target="_blank" '
            f'title="在新窗口打开语音文件">{_html_escape(ext)}</a>'
            f'<span class="vs">{label}</span>'
            f'<audio preload="none" src="{_html_escape(src)}"></audio></div>')


def _bubble_html(item: dict[str, Any]) -> str:
    kind = item.get("kind") or "other"
    extra = item.get("extra") or {}
    if kind == "quote":
        quote = extra.get("quote") or {}
        head = ""
        if quote:
            sender = _html_escape(quote.get("sender") or "")
            body = _html_escape(quote.get("text") or quote.get("summary") or "")
            prefix = f"<b>{sender}</b>：" if sender else ""
            head = f'<div class="quote">{prefix}{body}</div>'
        return (f'<div class="bubble">{head}'
                f'<div>{_html_escape(item.get("text") or "")}</div></div>')
    if kind == "forward":
        items = extra.get("forward_items") or []
        rows = "".join(
            f'<li><b>{_html_escape(str(i.get("name") or ""))}</b>'
            f'{_html_escape(str(i.get("title") or "")[:120])}</li>' for i in items[:40])
        tail = "已截断" if extra.get("forward_truncated") else "聊天记录"
        return (f'<div class="forward"><div class="fh">'
                f'{_html_escape(item.get("title") or "聊天记录")}</div>'
                f'<ul>{rows}</ul>'
                f'<div class="ff"><span>{tail}</span><span>›</span></div></div>')
    if kind == "voice":
        return _voice_html(item)
    if kind == "sticker":
        target = str((item.get("media") or {}).get("sticker") or "").strip()
        if target:
            return (f'<div class="bubble naked"><img class="sticker" loading="lazy" '
                    f'src="{_html_escape(target)}" alt="动画表情"></div>')
        return ('<div class="bubble naked"><span class="sticker-miss">'
                '[动画表情]</span></div>')
    media_html = _media_html(item)
    if kind in ("image", "video", "file", "link"):
        if media_html:
            return f'<div class="bubble naked">{media_html}</div>'
        return f'<div class="bubble">{_html_escape(PLACEHOLDERS.get(kind, "[消息]"))}</div>'
    body = _html_escape(item.get("text") or item.get("title") or "")
    if body:
        return f'<div class="bubble">{body}</div>'
    if media_html:
        return f'<div class="bubble">{media_html}</div>'
    return f'<div class="bubble">{_html_escape(PLACEHOLDERS.get(kind, "[消息]"))}</div>'


def _session_html(session: dict[str, Any], items: list[dict[str, Any]],
                  account: str, exported_at: str) -> str:
    rows: list[str] = []
    last_day = ""
    last_stamp = 0
    last_sender: str | None = None
    last_mine = False
    for item in items:
        stamp = item.get("create_time") or 0
        day = _fmt_day(stamp)
        new_day = bool(day and day != last_day)
        if new_day:
            last_day = day
            rows.append(f'<div class="day"><span>{_html_escape(day)}</span></div>')
            last_sender = None
        show_clock = new_day or not last_stamp or (stamp - last_stamp) > 300
        if (item.get("kind") or "") == "system":
            text = (item.get("text") or "").strip()
            if text:
                rows.append(f'<div class="sys" data-t="{_html_escape(text.lower())}">'
                            f'<span>{_html_escape(text)}</span></div>')
            last_stamp = stamp
            last_sender = None
            continue
        who = item.get("sender_display") or ""
        mine = bool(item.get("is_self"))
        cont = (not show_clock and not new_day
                and ((mine and last_mine) or (not mine and last_sender == who)))
        clock = (f'<div class="clock">{_html_escape(_fmt_clock(stamp))}</div>'
                 if show_clock else "")
        who_html = "" if mine else f'<div class="who">{_html_escape(who)}</div>'
        avatar_cls = "me" if mine else f"a{_avatar_class(who)}"
        avatar = "我" if mine else _avatar_text(who)
        rows.append(
            f'<div class="msg{" me" if mine else ""}{" cont" if cont else ""}" '
            f'data-t="{_html_escape(_message_body(item).lower())}">'
            f'<div class="ava {avatar_cls}">{_html_escape(avatar)}</div>'
            f'<div class="stack">{who_html}{_bubble_html(item)}{clock}</div></div>')
        last_stamp = stamp
        last_sender = who
        last_mine = mine
    start = _fmt_time(items[0]["create_time"]) if items else ""
    end = _fmt_time(items[-1]["create_time"]) if items else ""
    subtitle = (f'共 {len(items)} 条 · {start} ～ {end}' if items
                else "没有符合条件的消息")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{_html_escape(session["display"])} · 聊天记录</title>'
        f'<style>{BASE_CSS}</style></head><body>'
        '<header class="topbar"><div class="bar">'
        '<a class="back" href="index.html" title="返回会话列表" aria-label="返回会话列表">‹</a>'
        f'<div class="ava a{_avatar_class(session["display"])}">'
        f'{_html_escape(_avatar_text(session["display"]))}</div>'
        f'<div class="tinfo"><h1>{_html_escape(session["display"])}</h1>'
        f'<div class="sub">{_html_escape(subtitle)}</div></div>'
        '<div class="grow"></div>'
        '<label class="search"><svg viewBox="0 0 16 16" aria-hidden="true">'
        '<circle cx="6.8" cy="6.8" r="4.6"/>'
        '<path d="M10.4 10.4L14 14" stroke-linecap="round"/></svg>'
        '<input id="q" placeholder="搜索消息" spellcheck="false" aria-label="搜索消息"></label>'
        '<span id="cnt"></span>'
        '</div></header>'
        f'<main class="wrap" id="log">{"".join(rows)}</main>'
        '<div id="lb" role="dialog" aria-label="图片预览"><img alt=""></div>'
        f'<footer class="foot">账号 {_html_escape(account)} · 导出 {_html_escape(exported_at)}'
        ' · <a href="index.html">返回会话列表</a></footer>'
        f'<script>{BASE_JS}</script>'
        '</body></html>'
    )


def _index_html(rows: list[dict[str, Any]], account: str, exported_at: str,
                total_messages: int) -> str:
    cards = []
    for row in rows:
        badge = '<span class="badge">群聊</span>' if row.get("kind") == "group" else ""
        pdf = (f'<a class="pdf" href="{_html_escape(row["pdf"])}">PDF</a>'
               if row.get("pdf") else "")
        cards.append(
            '<div class="card">'
            f'<a class="cmain" href="{_html_escape(row["file"])}">'
            f'<div class="ava a{_avatar_class(row["display"])}">'
            f'{_html_escape(_avatar_text(row["display"]))}</div>'
            f'<div class="cbody"><div class="ctitle">{_html_escape(row["display"])}'
            f'{badge}</div>'
            f'<div class="csum">{_html_escape(row["summary"])}</div></div></a>'
            f'<div class="cmeta"><span class="ccount">{row["count"]} 条</span>{pdf}</div>'
            '</div>')
    pdf_total = sum(1 for row in rows if row.get("pdf"))
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>聊天记录导出</title>'
        f'<style>{BASE_CSS}</style></head><body>'
        '<main class="wrap">'
        '<div class="hero"><h1>聊天记录导出</h1>'
        f'<div class="sub">账号 {_html_escape(account)} · 导出 {_html_escape(exported_at)}</div></div>'
        '<div class="stats">'
        f'<div class="stat"><b>{len(rows)}</b><span>会话</span></div>'
        f'<div class="stat"><b>{total_messages}</b><span>消息</span></div>'
        f'<div class="stat"><b>{pdf_total}</b><span>PDF</span></div>'
        '</div>'
        f'<div class="cards">{"".join(cards) or "<div class=mark>没有会话</div>"}</div>'
        '</main></body></html>'
    )


def _media_markdown(item: dict[str, Any]) -> str:
    media = item.get("media") or {}
    kind = item.get("kind") or ""
    extra = item.get("extra") or {}
    lines: list[str] = []
    if kind == "image":
        target = media.get("image") or media.get("thumb")
        if target:
            lines.append(f"![图片]({target})")
    elif kind == "video":
        if media.get("video"):
            lines.append(f"[视频文件]({media['video']})")
        elif media.get("thumb"):
            lines.append(f"![视频封面]({media['thumb']})")
    elif kind == "voice" and media.get("voice"):
        try:
            secs = int(float(extra.get("duration") or 0))
        except (TypeError, ValueError):
            secs = 0
        lines.append(f"[语音{f' {secs} 秒' if secs else ''}]({media['voice']})")
    elif kind == "sticker":
        lines.append(f"![动画表情]({media['sticker']})" if media.get("sticker")
                     else "[动画表情]")
    elif kind == "file":
        name = extra.get("filename") or "文件"
        size = human_size(extra.get("size"))
        if media.get("file"):
            lines.append(f"[文件：{name}]({media['file']})" + (f" · {size}" if size else ""))
        else:
            lines.append(f"文件：{name}" + (f" · {size}" if size else "") + "（未包含在本导出中）")
    elif kind == "link":
        title = (item.get("title") or item.get("text") or "链接").strip()
        url = (extra.get("url") or "").strip()
        article = (media.get("article") or "").strip()
        author = (extra.get("source") or extra.get("app") or "").strip()
        target = article or url
        label = f"{title} · {author}" if author and author not in title else title
        lines.append(f"[{label}]({target})" if target else title)
    elif kind == "miniprogram":
        title = (item.get("title") or item.get("text") or "小程序").strip()
        source = (extra.get("source") or extra.get("app") or "").strip()
        url = (extra.get("url") or "").strip()
        label = f"{title} · {source}" if source else title
        lines.append(f"[{label}]({url})" if url else label)
    return "\n\n".join(lines)


def _write_markdown(path: Path, session: dict[str, Any], items: list[dict[str, Any]],
                    account: str, exported_at: str) -> None:
    kind_label = {"private": "私聊", "group": "群聊"}.get(
        session["kind"], "会话")
    start = _fmt_time(items[0]["create_time"]) if items else ""
    end = _fmt_time(items[-1]["create_time"]) if items else ""
    with open(path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(f"# {session['display']}\n\n")
        fp.write(f"- 账号：{account}\n")
        fp.write(f"- 类型：{kind_label}\n")
        fp.write(f"- 条数：{len(items)}\n")
        fp.write(f"- 时间范围：{start} ～ {end}\n")
        fp.write(f"- 导出时间：{exported_at}\n\n---\n\n")
        last_day = ""
        for item in items:
            day = _fmt_day(item.get("create_time") or 0)
            if day and day != last_day:
                last_day = day
                fp.write(f"## {day}\n\n")
            if (item.get("kind") or "") == "system":
                fp.write(f"> {item.get('text') or ''}\n\n")
                continue
            who = item.get("sender_display") or ""
            fp.write(f"**{_fmt_time(item.get('create_time') or 0)} · {who}**\n\n")
            media_text = _media_markdown(item)
            body = "" if media_text and (item.get("kind") in _BODY_SKIP) \
                else _message_body(item).rstrip()
            if body:
                fp.write(body + "\n\n")
            if media_text:
                fp.write(media_text + "\n\n")


def _article_index(output_root: Path) -> dict[str, str]:
    by_title: dict[str, str] = {}
    try:
        for folder in output_root.iterdir():
            if not folder.is_dir():
                continue
            if folder.name == "databases" or folder.name.startswith("聊天记录"):
                continue
            pages = list(folder.glob("*.html"))
            if not pages:
                continue
            html_path = pages[0]
            by_title[folder.name.casefold()] = str(html_path)
            if "-" in folder.name:
                by_title.setdefault(folder.name.split("-", 1)[1].casefold(), str(html_path))
    except OSError:
        return by_title
    return by_title


def _is_article(item: dict[str, Any]) -> bool:
    if (item.get("kind") or "") != "link":
        return False
    extra = item.get("extra") or {}
    if extra.get("official"):
        return True
    return "mp.weixin.qq.com" in str(extra.get("url") or "")


def _article_field(item: dict[str, Any]) -> tuple[str, str, str, str]:
    extra = item.get("extra") or {}
    title = (item.get("title") or item.get("text") or "未命名文章").strip()
    source = (extra.get("source") or extra.get("app") or "").strip()
    descr = (extra.get("descr") or "").strip()
    url = (extra.get("url") or "").strip()
    return title, source, descr, url


ARTICLE_CSS = """:root{--bg:#f6f7f9;--panel:#fff;--text:#1b1e22;--muted:#7d858f;--line:#e5e8ec;
  --brand:#07c160}
@media (prefers-color-scheme:dark){:root{--bg:#131417;--panel:#1c1e22;--text:#e9edf2;
  --muted:#98a1ab;--line:#2a2e35;--brand:#3eb575}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:16px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased}
.top{background:var(--panel);border-bottom:1px solid var(--line);padding:10px 18px}
.top a{color:var(--muted);text-decoration:none;font-size:13px}
.top a:hover{color:var(--text)}
main{max-width:720px;margin:0 auto;padding:26px 18px 60px}
.src{font-size:13px;color:var(--brand);margin-bottom:6px}
h1{font-size:23px;line-height:1.42;margin:0 0 10px;letter-spacing:-.2px}
.meta{font-size:12.5px;color:var(--muted);display:flex;gap:10px;flex-wrap:wrap}
.meta a{color:var(--brand);text-decoration:none}
.cover{width:100%;border-radius:10px;margin:18px 0;display:block;background:var(--line)}
.desc{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--brand);
  border-radius:8px;padding:11px 14px;color:var(--text);font-size:14.5px;margin:16px 0}
.note{background:var(--panel);border:1px dashed var(--line);border-radius:10px;padding:22px;
  color:var(--muted);font-size:14px;text-align:center;margin-top:18px;line-height:1.9}
.note b{color:var(--text);font-weight:600}
.body{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:20px 22px}
.body img{max-width:100%;height:auto;border-radius:6px}
.body a{color:var(--brand)}
.foot{margin-top:26px;font-size:12px;color:var(--muted);text-align:center;line-height:1.8}
"""


def _write_article_snapshot(folder: Path, title: str, source: str, descr: str, url: str,
                            cover: Path | None, exported_at: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    cover_html = ""
    if cover is not None and cover.exists():
        rel = os.path.relpath(cover, folder).replace("\\", "/")
        cover_html = f'<img class="cover" src="{_html_escape(rel)}" alt="封面">'
    descr_html = f'<div class="desc">{_html_escape(descr)}</div>' if descr else ""
    src_html = _html_escape(f"公众号 · {source}" if source else "公众号文章")
    link_html = (f'<a href="{_html_escape(url)}" target="_blank" rel="noreferrer">打开原文</a>'
                 if url else "")
    body = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html_escape(title)}</title><style>{ARTICLE_CSS}</style></head><body>
<div class="top"><a href="../../index.html">‹ 返回导出目录</a></div>
<main>
<div class="src">{src_html}</div>
<h1>{_html_escape(title)}</h1>
<div class="meta"><span>保存于 {_html_escape(exported_at.replace("T", " ")[:19])}</span>{link_html}</div>
{cover_html}
{descr_html}
<div class="note"><b>正文未能保存到本地</b><br>
微信对文章正文做了访问限制，本次只保留了标题、摘要与原文链接。<br>
联网状态下可点上方「打开原文」在浏览器中查看。</div>
<div class="foot">由 wxMoments 聊天记录导出 · 原文链接：{_html_escape(url)}</div>
</main></body></html>"""
    page = folder / "index.html"
    page.write_text(body, encoding="utf-8")

    md = [f"# {title}", ""]
    if source:
        md.append(f"> 公众号：{source}  ")
    if url:
        md.append(f"> 原文：{url}  ")
    md.append(f"> 保存于：{exported_at}")
    md.append("")
    if descr:
        md += [descr, ""]
    if cover is not None and cover.exists():
        rel = os.path.relpath(cover, folder).replace("\\", "/")
        md += [f"![封面]({rel})", ""]
    md += ["> 正文未能保存到本地：微信对文章正文做了访问限制，"
           "以上为标题、摘要与原文链接。", ""]
    (folder / "index.md").write_text("\n".join(md), encoding="utf-8")
    return page


def _copy_existing_article(src_page: Path, dest_folder: Path) -> Path:
    dest_folder.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_page.parent, dest_folder, dirs_exist_ok=True)
    return dest_folder / src_page.name


def _fetch_article(url: str):
    try:
        from wechat_download import wechat

        art = wechat.parse_article(wechat.fetch_html(url, timeout=12), url)
    except Exception as exc:
        logger.info("抓取文章失败 %s: %s", url[:80], exc)
        return None
    if not (art.content_html or "").strip() or not art.title:
        return None
    return art


def _article_slug(source: str, title: str, used: set[str]) -> str:
    base = safe_filename(f"{source or '公众号'}-{title or '文章'}", "article")[:80].strip(" .-")
    base = base or "article"
    slug = base
    index = 2
    while slug.casefold() in used:
        slug = f"{base}-{index}"
        index += 1
    used.add(slug.casefold())
    return slug


def _export_articles(items: list[dict[str, Any]], out_dir: Path,
                     output_root: Path | None, job,
                     on_step: Callable[[int, int], None] | None = None,
                     cache: dict[str, str] | None = None,
                     budget: dict[str, float] | None = None) -> dict[str, int]:
    stats = {"saved": 0, "snapshot": 0, "reused": 0, "missing": 0}
    grouped: list[tuple[str, list[dict[str, Any]], str]] = []
    seen: dict[str, int] = {}
    for item in items:
        if not _is_article(item):
            continue
        title, _source, _descr, url = _article_field(item)
        key = (url or title).strip().casefold()
        if not key:
            continue
        if key in seen:
            grouped[seen[key]][1].append(item)
            continue
        seen[key] = len(grouped)
        grouped.append((key, [item], title))
    if not grouped:
        return stats

    existing = _article_index(output_root) if output_root else {}
    articles_root = out_dir / "articles"
    articles_root.mkdir(parents=True, exist_ok=True)
    used_slugs: set[str] = {path.name.casefold() for path in articles_root.iterdir()
                            if path.is_dir()}
    budget = budget if budget is not None else {"left": ARTICLE_TIME_BUDGET}
    network_failures = 0
    for position, (_key, group, title) in enumerate(grouped, start=1):
        job.check_canceled()
        if on_step is not None and (position % 5 == 0 or position == len(grouped)):
            on_step(position, len(grouped))
        if position > MAX_ARTICLE_COUNT:
            stats["missing"] += 1
            continue
        sample = group[0]
        title, source, descr, url = _article_field(sample)
        rel_html = ""
        if cache is not None and _key in cache:
            rel_html = cache[_key]
            stats["reused"] += 1
            for item in group:
                item.setdefault("media", {})["article"] = rel_html
            continue
        source_page = existing.get(title.casefold()) if title else None
        if source_page and Path(source_page).is_file():
            slug = _article_slug(source, title, used_slugs)
            try:
                copied = _copy_existing_article(Path(source_page), articles_root / slug)
                rel_html = copied.relative_to(out_dir).as_posix()
                stats["reused"] += 1
            except OSError as exc:
                logger.warning("复用文章失败 %s: %s", source_page, exc)
        if not rel_html and url and network_failures < 2 and budget["left"] > 0:
            fetch_started = time.time()
            art = _fetch_article(url)
            budget["left"] -= time.time() - fetch_started
            if art is not None:
                try:
                    from wechat_download import exporter

                    result = exporter.export_article(art, articles_root)
                    rel_html = result.html_file.relative_to(out_dir).as_posix()
                    used_slugs.add(result.article_dir.name.casefold())
                    stats["saved"] += 1
                    network_failures = 0
                except Exception as exc:
                    logger.warning("保存文章失败 %s: %s", url[:80], exc)
            else:
                network_failures += 1
        if not rel_html:
            slug = _article_slug(source, title, used_slugs)
            folder = articles_root / slug
            cover_rel = (sample.get("media") or {}).get("thumb") or ""
            cover = (out_dir / cover_rel) if cover_rel else None
            _write_article_snapshot(folder, title, source, descr, url,
                                    cover, _iso())
            rel_html = (folder / "index.html").relative_to(out_dir).as_posix()
            stats["snapshot"] += 1
        for item in group:
            payload = item.setdefault("media", {})
            payload["article"] = rel_html
        if cache is not None and rel_html:
            cache[_key] = rel_html
    logger.info("文章导出：%s", stats)
    return stats


def _image_suffix(data: bytes) -> str:
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".jpg"


_THUMB_CACHE: dict[str, bytes] = {}
_IMAGE_MIME = {
    ".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg",
    ".webp": "image/webp", ".bmp": "image/bmp",
}


_STICKER_DB_MISS: set[str] = set()


def _sticker_from_db(md5: str) -> bytes:
    key = re.sub(r"[^0-9a-f]", "", (md5 or "").lower())[:32]
    if not key or key in _STICKER_DB_MISS:
        return b""
    try:
        from wechat_decrypt_tool.modules import media_decrypt as media_decrypt

        source = media_decrypt._find_emoticon_message_remote_source(_store().root, key)
        urls = [str(url) for url in (source.get("urls") or []) if url]
        keys = [str(item) for item in (source.get("aes_keys") or []) if item]
        if not urls:
            _STICKER_DB_MISS.add(key)
            return b""
        data, _mime = media_decrypt._try_fetch_emoticon_from_sources(urls, keys)
    except Exception as exc:
        logger.debug("表情远程源查询失败 %s: %s", key, exc)
        _STICKER_DB_MISS.add(key)
        return b""
    if not data:
        _STICKER_DB_MISS.add(key)
        return b""
    return data


def _sticker_bytes(md5: str, cdn: str, thumb: str = "") -> tuple[bytes, str]:
    key = re.sub(r"[^0-9a-f]", "", (md5 or "").lower())[:32]
    if not key:
        key = str(abs(hash((cdn or thumb or "")[:200])))[:16]
    if key:
        STICKER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for path in STICKER_CACHE_DIR.glob(f"{key}.*"):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if data:
                return data, _IMAGE_MIME.get(path.suffix.lower(), "image/gif")
    for url in (cdn, thumb):
        data = _fetch_remote_image(url, timeout=6.0)
        if not data:
            continue
        suffix = _image_suffix(data)
        mime = _IMAGE_MIME.get(suffix, "image/gif")
        if mime == "image/gif" and not data.startswith(b"GIF8"):
            continue
        try:
            STICKER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (STICKER_CACHE_DIR / f"{key}{suffix}").write_bytes(data)
        except OSError:
            pass
        return data, mime
    if key and len(key) == 32:
        data = _sticker_from_db(key)
        if data:
            suffix = _image_suffix(data)
            try:
                STICKER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                (STICKER_CACHE_DIR / f"{key}{suffix}").write_bytes(data)
            except OSError:
                pass
            return data, _IMAGE_MIME.get(suffix, "image/gif")
    return b"", ""


def _fetch_remote_image(url: str, timeout: float = 8.0) -> bytes:
    link = (url or "").strip()
    if not link.startswith(("http://", "https://")):
        return b""
    if link in _THUMB_CACHE:
        return _THUMB_CACHE[link]
    try:
        import requests

        resp = requests.get(link, timeout=timeout, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
            "Referer": "https://mp.weixin.qq.com/",
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        })
        resp.raise_for_status()
        data = resp.content
    except Exception as exc:
        logger.info("封面下载失败 %s: %s", link[:80], exc)
        return b""
    if not data or len(data) > 8 * 1024 ** 2:
        return b""
    if len(_THUMB_CACHE) > 400:
        _THUMB_CACHE.clear()
    _THUMB_CACHE[link] = data
    return data


def _collect_media(media: ChatMedia, session_id: str, items: list[dict[str, Any]],
                   base_dir: Path, session_slug: str, job,
                   budget: dict[str, int] | None = None,
                   on_step: Callable[[int, int], None] | None = None) -> dict[str, int]:
    stats = {"images": 0, "files": 0, "voices": 0, "videos": 0, "links": 0,
             "stickers": 0, "missing": 0, "skipped": 0}
    budget = budget if budget is not None else {"left": MAX_MEDIA_TOTAL_BYTES}
    media_root = base_dir / "media" / session_slug
    used_images: set[Path] = set()
    thumb_deadline = time.time() + LINK_THUMB_BUDGET
    sticker_deadline = time.time() + 120.0

    def spend(size: int) -> bool:
        if size > MAX_MEDIA_FILE_BYTES or size > budget["left"]:
            stats["skipped"] += 1
            return False
        budget["left"] -= size
        return True

    def target(sub: str, name: str) -> Path:
        folder = media_root / sub
        folder.mkdir(parents=True, exist_ok=True)
        candidate = folder / name
        stem, suffix = candidate.stem, candidate.suffix
        index = 2
        while candidate.exists():
            candidate = folder / f"{stem}-{index}{suffix}"
            index += 1
        return candidate

    total_items = len(items)
    for position, item in enumerate(items, start=1):
        job.check_canceled()
        if on_step is not None and (position % 20 == 0 or position == total_items):
            on_step(position, total_items)
        kind = item.get("kind") or ""
        extra = item.get("extra") or {}
        created = int(item.get("create_time") or 0)
        payload = item.setdefault("media", {})
        if kind == "image":
            thumb_path = media.image_path(session_id, created, prefer_thumb=True,
                                          used=used_images)
            full_path = media.image_path(session_id, created, prefer_thumb=False,
                                         used=used_images)
            if full_path is None and thumb_path is None:
                stats["missing"] += 1
                continue
            for key, path in (("thumb", thumb_path), ("image", full_path or thumb_path)):
                if path is None:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                if key == "image" and size > MAX_IMAGE_FULL_BYTES:
                    continue
                if size and not spend(size):
                    continue
                data, _mime = media.read_media(path)
                if not data:
                    continue
                suffix = path.suffix if path.suffix.lower() != ".dat" else ".jpg"
                stem = f"{position:04d}_t" if key == "thumb" else f"{position:04d}"
                written = target("image", f"{stem}{suffix}")
                written.write_bytes(data)
                payload[key] = written.relative_to(base_dir).as_posix()
            if payload:
                stats["images"] += 1
            if full_path is not None:
                used_images.add(full_path)
            if thumb_path is not None:
                used_images.add(thumb_path)
        elif kind == "sticker":
            if time.time() > sticker_deadline:
                stats["skipped"] += 1
                continue
            data, _mime = _sticker_bytes(
                str(extra.get("md5") or ""), str(extra.get("cdnurl") or ""),
                str(extra.get("thumburl") or ""))
            if not data:
                stats["missing"] += 1
                continue
            if not spend(len(data)):
                continue
            written = target("sticker", f"{position:04d}{_image_suffix(data)}")
            written.write_bytes(data)
            payload["sticker"] = written.relative_to(base_dir).as_posix()
            stats["stickers"] += 1
        elif kind == "voice":
            data, ext, _mime = media.voice_bytes(_store().media_dbs, session_id,
                                                 str(item.get("server_id") or ""))
            if not data:
                stats["missing"] += 1
                continue
            written = target("voice", f"{position:04d}.{ext or 'mp3'}")
            written.write_bytes(data)
            payload["voice"] = written.relative_to(base_dir).as_posix()
            stats["voices"] += 1
        elif kind in ("link", "miniprogram", "channel", "music"):
            thumb_url = str(extra.get("thumb") or "").strip()
            if thumb_url and time.time() < thumb_deadline:
                data = _fetch_remote_image(thumb_url)
                if data and spend(len(data)):
                    written = target("link", f"{position:04d}{_image_suffix(data)}")
                    written.write_bytes(data)
                    payload["thumb"] = written.relative_to(base_dir).as_posix()
                    stats["links"] += 1
        elif kind == "file":
            path = media.file_path(extra.get("filename") or item.get("text") or "",
                                   extra.get("size") or 0)
            if path is None:
                stats["missing"] += 1
                continue
            try:
                if path.stat().st_size and not spend(path.stat().st_size):
                    continue
                written = target("file", safe_filename(path.name, "file"))
                shutil.copy2(path, written)
            except OSError as exc:
                logger.warning("复制文件失败 %s: %s", path, exc)
                stats["missing"] += 1
                continue
            payload["file"] = written.relative_to(base_dir).as_posix()
            stats["files"] += 1
        elif kind == "video":
            thumb = media.video_thumb(created, extra.get("md5") or "")
            if thumb is not None:
                data, _mime = media.read_media(thumb)
                if data:
                    written = target("video", f"{position:04d}.jpg")
                    written.write_bytes(data)
                    payload["thumb"] = written.relative_to(base_dir).as_posix()
            video_path = media.video_path(created, extra.get("md5") or "")
            if video_path is not None:
                try:
                    if video_path.stat().st_size and not spend(video_path.stat().st_size):
                        video_path = None
                    else:
                        written = target("video", f"{position:04d}{video_path.suffix or '.mp4'}")
                        shutil.copy2(video_path, written)
                        payload["video"] = written.relative_to(base_dir).as_posix()
                except OSError as exc:
                    logger.warning("复制视频失败 %s: %s", video_path, exc)
            if payload:
                stats["videos"] += 1
            else:
                stats["missing"] += 1
    return stats


_PDF_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3\u2190-\u21FF]")


_PDF_FONT_CACHE: tuple[str, str] | None = None


def _pdf_fonts() -> tuple[str, str]:
    global _PDF_FONT_CACHE
    if _PDF_FONT_CACHE:
        return _PDF_FONT_CACHE
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    try:
        pdfmetrics.registerFont(TTFont("ChatSans", r"C:\Windows\Fonts\msyh.ttc",
                                       subfontIndex=0))
        pdfmetrics.registerFont(TTFont("ChatSansBold", r"C:\Windows\Fonts\msyhbd.ttc",
                                       subfontIndex=0))
        pdfmetrics.getFont("ChatSans")
        pdfmetrics.getFont("ChatSansBold")
        _PDF_FONT_CACHE = ("ChatSans", "ChatSansBold")
        return _PDF_FONT_CACHE
    except Exception:
        pass
    try:
        pdfmetrics.registerFont(TTFont("ChatSans", r"C:\Windows\Fonts\simhei.ttf"))
        pdfmetrics.getFont("ChatSans")
        _PDF_FONT_CACHE = ("ChatSans", "ChatSans")
        return _PDF_FONT_CACHE
    except Exception:
        pass
    with suppress(Exception):
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    _PDF_FONT_CACHE = ("STSong-Light", "STSong-Light")
    return _PDF_FONT_CACHE


def _pdf_safe(value: str) -> str:
    return _PDF_EMOJI.sub("", str(value or ""))


def _pdf_wrap(text: str, font: str, size: float, width: float) -> list[str]:
    from reportlab.pdfbase import pdfmetrics

    lines: list[str] = []
    for para in _pdf_safe(text).replace("\r", "").split("\n"):
        para = para.replace("\t", "    ")
        if not para:
            lines.append("")
            continue
        line = ""
        for char in para:
            if pdfmetrics.stringWidth(line + char, font, size) <= width:
                line += char
            else:
                lines.append(line or " ")
                line = char
        if line:
            lines.append(line)
    return lines or [""]


def _pdf_thumb(item: dict[str, Any], base_dir: Path, limit: float = 54.0):
    rel = (item.get("media") or {}).get("thumb")
    if not rel:
        return None
    return _read_pdf_image(base_dir / str(rel), limit, limit, 1.0)


def _read_pdf_image(path: Path, limit_w: float, limit_h: float, upscale: float):
    try:
        from reportlab.lib.utils import ImageReader

        if not path.exists():
            return None
        reader = ImageReader(str(path))
        width, height = reader.getSize()
    except Exception:
        return None
    if not width or not height:
        return None
    scale = min(limit_w / width, limit_h / height, upscale)
    return reader, width * scale, height * scale


def _pdf_image(item: dict[str, Any], base_dir: Path):
    kind = item.get("kind") or ""
    if kind not in ("image", "sticker", "video"):
        return None
    media = item.get("media") or {}
    if kind == "sticker":
        rel = media.get("sticker")
        limit_w, limit_h, upscale = 132.0, 132.0, 1.6
    elif kind == "video":
        rel = media.get("thumb")
        limit_w, limit_h, upscale = 190.0, 140.0, 1.0
    else:
        rel = media.get("thumb") or media.get("image")
        limit_w, limit_h, upscale = 180.0, 200.0, 1.0
    if not rel:
        return None
    return _read_pdf_image(base_dir / str(rel), limit_w, limit_h, upscale)


def _session_pdf(session: dict[str, Any], items: list[dict[str, Any]],
                 pdf_path: Path, base_dir: Path, exported_at: str,
                 account: str = "") -> bool:
    try:
        from reportlab.lib.colors import HexColor
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfgen import canvas
    except Exception as exc:
        logger.warning("PDF 依赖不可用：%s", exc)
        return False

    font, font_bold = _pdf_fonts()
    try:
        pdfmetrics.getFont(font)
    except Exception:
        return False

    page_w, page_h = A4
    margin_x = 42.0
    margin_top = 46.0
    margin_bottom = 52.0
    bubble_max = min(360.0, page_w - margin_x * 2 - 44)
    pad = 9.0
    font_size = 11.0
    line_h = 16.0
    meta_h = 14.0

    ink = HexColor("#111111")
    muted = HexColor("#8b939c")
    line_color = HexColor("#d9dde3")
    mine_bubble = HexColor("#95ec69")
    other_bubble = HexColor("#ffffff")
    ava_colors = ["#5b8def", "#39b980", "#e08a3c", "#a86ad6", "#d9636b", "#3aa6b9"]

    start = _fmt_time(items[0]["create_time"]) if items else ""
    end = _fmt_time(items[-1]["create_time"]) if items else ""
    subtitle = (f"共 {len(items)} 条 · {start} ～ {end}" if items
                else "没有符合条件的消息")
    kind_label = {"private": "私聊", "group": "群聊"}.get(session["kind"], "会话")

    try:
        pdf = canvas.Canvas(str(pdf_path), pagesize=A4)
    except Exception as exc:
        logger.warning("创建 PDF 失败：%s", exc)
        return False
    pdf.setTitle(f"{session['display']} - 聊天记录")

    state = {"y": page_h - margin_top, "page": 1}

    def footer() -> None:
        pdf.setFillColor(muted)
        pdf.setFont(font, 8)
        pdf.drawString(margin_x, 26, _pdf_safe(account or "微信"))
        pdf.drawRightString(page_w - margin_x, 26, f"第 {state['page']} 页")

    def new_page() -> None:
        footer()
        pdf.showPage()
        state["page"] += 1
        state["y"] = page_h - margin_top
        pdf.setFillColor(muted)
        pdf.setFont(font, 8.5)
        pdf.drawString(margin_x, state["y"] - 8, f"{_pdf_safe(session['display'])[:42]} · 续")
        state["y"] -= 18

    def need(height: float) -> None:
        if state["y"] - height < margin_bottom:
            new_page()

    pdf.setFillColor(HexColor("#07c160"))
    pdf.roundRect(margin_x, state["y"] - 3, 24, 3, 1.5, stroke=0, fill=1)
    state["y"] -= 12
    pdf.setFillColor(ink)
    pdf.setFont(font_bold, 16)
    pdf.drawString(margin_x, state["y"] - 14, _pdf_safe(session["display"])[:52])
    state["y"] -= 27
    pdf.setFillColor(muted)
    pdf.setFont(font, 9)
    pdf.drawString(margin_x, state["y"] - 9, f"{kind_label} · {subtitle}")
    state["y"] -= 18
    pdf.setFont(font, 8.5)
    pdf.drawString(margin_x, state["y"] - 8,
                   f"账号 {account or '微信'} · 导出 {exported_at}")
    state["y"] -= 20
    pdf.setStrokeColor(line_color)
    pdf.setLineWidth(0.7)
    pdf.line(margin_x, state["y"], page_w - margin_x, state["y"])
    state["y"] -= 14
    if not items:
        pdf.setFillColor(muted)
        pdf.setFont(font, 10)
        pdf.drawString(margin_x, state["y"] - 12, "这段时间内没有可导出的消息。")
        state["y"] -= 30

    last_day = ""
    last_stamp = 0

    def draw_avatar(top: float, who: str, mine: bool, size: float = 30.0) -> None:
        ava_x = (page_w - margin_x - size) if mine else margin_x
        color = "#07c160" if mine else ava_colors[_avatar_class(who or "?")]
        pdf.setFillColor(HexColor(color))
        pdf.roundRect(ava_x, top - size, size, size, 5, stroke=0, fill=1)
        pdf.setFillColor(HexColor("#ffffff"))
        pdf.setFont(font, 10)
        pdf.drawCentredString(ava_x + size / 2, top - size + 10,
                              "我" if mine else _avatar_text(who))

    def draw_meta(top: float, who: str, mine: bool, clock_text: str) -> None:
        pdf.setFillColor(muted)
        pdf.setFont(font, 8.5)
        if mine:
            if clock_text:
                pdf.drawRightString(page_w - margin_x - 40, top - 8, clock_text)
        else:
            label = who[:32] + (f"   {clock_text}" if clock_text else "")
            pdf.drawString(margin_x + 40, top - 8, label)

    def side_x(width: float, mine: bool) -> float:
        return (page_w - margin_x - 40 - width) if mine else margin_x + 40

    def clip_line(text: str, font_name: str, size: float, width: float) -> str:
        if pdfmetrics.stringWidth(text, font_name, size) <= width:
            return text
        cut = text
        while cut and pdfmetrics.stringWidth(cut + "…", font_name, size) > width:
            cut = cut[:-1]
        return (cut + "…") if cut else ""

    for item in items:
        stamp = item.get("create_time") or 0
        day = _fmt_day(stamp)
        new_day = bool(day and day != last_day)
        if new_day:
            last_day = day
            need(34)
            label = _pdf_safe(day)
            pill_w = pdfmetrics.stringWidth(label, font, 8.5) + 20
            pdf.setFillColor(HexColor("#f1f2f4"))
            pdf.roundRect(page_w / 2 - pill_w / 2, state["y"] - 19, pill_w, 16, 8,
                          stroke=0, fill=1)
            pdf.setFillColor(muted)
            pdf.setFont(font, 8.5)
            pdf.drawCentredString(page_w / 2, state["y"] - 14.5, label)
            state["y"] -= 26
        show_clock = new_day or not last_stamp or (stamp - last_stamp) > 300
        last_stamp = stamp

        if (item.get("kind") or "") == "system":
            text = (item.get("text") or "").strip()
            if not text:
                continue
            wide = page_w - margin_x * 2 - 70
            lines = _pdf_wrap(text, font, 9, wide)
            widest = max((pdfmetrics.stringWidth(row, font, 9) for row in lines),
                         default=0.0)
            height = len(lines) * 13 + 8
            need(height + 10)
            pill_w = min(wide + 26, widest + 28)
            pdf.setFillColor(HexColor("#f2f3f5"))
            pdf.roundRect(page_w / 2 - pill_w / 2, state["y"] - height + 2,
                          pill_w, height, 9, stroke=0, fill=1)
            pdf.setFillColor(muted)
            pdf.setFont(font, 9)
            text_y = state["y"] - 5
            for row in lines:
                text_y -= 12
                pdf.drawCentredString(page_w / 2, text_y, row)
            state["y"] -= height + 10
            continue

        mine = bool(item.get("is_self"))
        who = _pdf_safe(item.get("sender_display") or "")
        kind = item.get("kind") or ""
        extra = item.get("extra") or {}
        clock_text = _fmt_clock(stamp) if show_clock else ""
        top = state["y"]

        if kind == "file":
            name = _pdf_safe(extra.get("filename") or item.get("text") or "文件")
            size = human_size(extra.get("size")) or ""
            ext = (extra.get("ext") or Path(name).suffix.lstrip(".") or "file")[:3].upper()
            card_w = min(bubble_max, 300.0)
            card_h = 48.0
            need(meta_h + card_h + 8)
            top = state["y"]
            draw_avatar(top, who, mine)
            draw_meta(top, who, mine, clock_text)
            card_x = side_x(card_w, mine)
            card_y = top - meta_h - card_h
            pdf.setFillColor(other_bubble)
            pdf.setStrokeColor(line_color)
            pdf.setLineWidth(0.6)
            pdf.roundRect(card_x, card_y, card_w, card_h, 6, stroke=1, fill=1)
            pdf.setFillColor(HexColor("#e64340"))
            pdf.roundRect(card_x + 8, card_y + 9, 26, 30, 4, stroke=0, fill=1)
            pdf.setFillColor(HexColor("#ffffff"))
            pdf.setFont(font, 7)
            pdf.drawCentredString(card_x + 21, card_y + 21, ext)
            pdf.setFillColor(ink)
            pdf.setFont(font, 9.5)
            shown = clip_line(name, font, 9.5, card_w - 56)
            pdf.drawString(card_x + 42, card_y + 28, shown)
            pdf.setFillColor(muted)
            pdf.setFont(font, 8)
            pdf.drawString(card_x + 42, card_y + 13, size or "文件")
            state["y"] = card_y - 8
            continue

        if kind in ("link", "miniprogram", "channel", "music", "live"):
            badge = {"link": ("公众号" if extra.get("official") else "链接"),
                     "miniprogram": "小程序", "channel": "视频号",
                     "music": "音乐", "live": "直播"}[kind]
            title = _pdf_safe(item.get("title") or item.get("text") or "") or badge
            source = _pdf_safe(extra.get("source") or extra.get("app") or "")
            descr = _pdf_safe(extra.get("descr") or "")
            thumb = _pdf_thumb(item, base_dir)
            card_w = min(bubble_max, 300.0)
            thumb_w = (thumb[1] + 10) if thumb else 0.0
            inner = card_w - 20 - thumb_w
            title_lines = _pdf_wrap(title, font, 10, inner)[:2]
            show_descr = bool(descr) and descr.strip() != title.strip()
            descr_line = clip_line(descr, font, 8.5, inner) if show_descr else ""
            card_h = 12 + len(title_lines) * 14 + (12 if descr_line else 0) + 14
            card_h = max(card_h, (thumb[2] + 16) if thumb else 0.0, 56.0)
            need(meta_h + card_h + 8)
            top = state["y"]
            draw_avatar(top, who, mine)
            draw_meta(top, who, mine, clock_text)
            card_x = side_x(card_w, mine)
            card_y = top - meta_h - card_h
            pdf.setFillColor(HexColor("#fafbfc"))
            pdf.setStrokeColor(line_color)
            pdf.setLineWidth(0.6)
            pdf.roundRect(card_x, card_y, card_w, card_h, 6, stroke=1, fill=1)
            text_y = card_y + card_h - 12
            pdf.setFillColor(ink)
            pdf.setFont(font, 10)
            for row in title_lines:
                pdf.drawString(card_x + 10, text_y, row)
                text_y -= 14
            if descr_line:
                pdf.setFillColor(muted)
                pdf.setFont(font, 8.5)
                pdf.drawString(card_x + 10, text_y, descr_line)
                text_y -= 12
            pdf.setFillColor(HexColor("#07c160"))
            pdf.setFont(font_bold, 7.5)
            pdf.drawString(card_x + 10, card_y + 8, badge)
            if source and source != badge:
                pdf.setFillColor(muted)
                pdf.setFont(font, 8)
                pdf.drawString(card_x + 12 + pdfmetrics.stringWidth(
                    badge, font_bold, 7.5), card_y + 8, clip_line(
                        source, font, 8, inner - pdfmetrics.stringWidth(badge, font_bold, 7.5) - 4))
            if thumb:
                with suppress(Exception):
                    pdf.drawImage(thumb[0], card_x + card_w - thumb[1] - 8,
                                  card_y + card_h - thumb[2] - 8,
                                  width=thumb[1], height=thumb[2],
                                  preserveAspectRatio=True, mask="auto")
            state["y"] = card_y - 8
            continue

        if kind == "video":
            image = _pdf_image(item, base_dir)
            caption = _pdf_safe(item.get("text") or "")
            img_w, img_h = (image[1], image[2]) if image else (200.0, 120.0)
            cap_lines = _pdf_wrap(caption, font, 9, img_w - 6)[:2] if caption else []
            bubble_w = img_w + pad * 2
            bubble_h = pad * 2 + img_h + (len(cap_lines) * 13 + 6 if cap_lines else 0)
            if image is None:
                img_w, img_h = 0.0, 0.0
                bubble_w = 0.0
                bubble_h = 0.0
            if image is not None:
                need(meta_h + bubble_h + 8)
                top = state["y"]
                draw_avatar(top, who, mine)
                draw_meta(top, who, mine, clock_text)
                bubble_x = side_x(bubble_w, mine)
                bubble_y = top - meta_h - bubble_h
                pdf.setFillColor(mine_bubble if mine else other_bubble)
                pdf.setStrokeColor(line_color)
                pdf.setLineWidth(0.6)
                pdf.roundRect(bubble_x, bubble_y, bubble_w, bubble_h, 5,
                              stroke=0 if mine else 1, fill=1)
                img_y = bubble_y + bubble_h - pad - img_h
                with suppress(Exception):
                    pdf.drawImage(image[0], bubble_x + pad, img_y,
                                  width=img_w, height=img_h,
                                  preserveAspectRatio=True, mask="auto")
                cx, cy = bubble_x + pad + img_w / 2, img_y + img_h / 2
                with suppress(Exception):
                    pdf.setFillAlpha(0.82)
                    pdf.setFillColor(HexColor("#ffffff"))
                    pdf.circle(cx, cy, 15, stroke=0, fill=1)
                    pdf.setFillAlpha(1)
                pdf.setFillColor(HexColor("#3a3f45"))
                path = pdf.beginPath()
                path.moveTo(cx - 4, cy + 7)
                path.lineTo(cx + 8, cy)
                path.lineTo(cx - 4, cy - 7)
                path.close()
                pdf.drawPath(path, stroke=0, fill=1)
                if cap_lines:
                    pdf.setFillColor(HexColor("#0d1a0a" if mine else "#111111"))
                    pdf.setFont(font, 9)
                    text_y = img_y - 12
                    for row in cap_lines:
                        pdf.drawString(bubble_x + pad, text_y, row)
                        text_y -= 13
                state["y"] = bubble_y - 8
                continue

        if kind == "voice":
            try:
                secs = int(float(extra.get("duration") or 0))
            except (TypeError, ValueError):
                secs = 0
            bar_w = 118.0 + min(max(secs, 1), 60) * 1.6
            bar_h = 34.0
            need(meta_h + bar_h + 8)
            top = state["y"]
            draw_avatar(top, who, mine)
            draw_meta(top, who, mine, clock_text)
            bar_x = side_x(bar_w, mine)
            bar_y = top - meta_h - bar_h
            pdf.setFillColor(mine_bubble if mine else other_bubble)
            pdf.setStrokeColor(line_color)
            pdf.setLineWidth(0.6)
            pdf.roundRect(bar_x, bar_y, bar_w, bar_h, 5,
                          stroke=0 if mine else 1, fill=1)
            peaks = [7, 12, 9, 15, 11, 6, 13, 8]
            wave_x = bar_x + 10
            pdf.setFillColor(HexColor("#3f7a4f" if mine else "#7c848d"))
            for index, peak in enumerate(peaks):
                h = float(peak)
                pdf.roundRect(wave_x + index * 5.0, bar_y + bar_h / 2 - h / 2, 2.6, h,
                              1.3, stroke=0, fill=1)
            label = f'{secs}"' if secs else "语音"
            pdf.setFillColor(HexColor("#0d1a0a" if mine else "#111111"))
            pdf.setFont(font, 9)
            pdf.drawRightString(bar_x + bar_w - 10, bar_y + bar_h / 2 - 3.2, label)
            state["y"] = bar_y - 8
            continue

        if kind == "forward":
            title = _pdf_safe(item.get("title") or "转发的聊天记录")
            rows = extra.get("forward_items") or []
            card_w = min(bubble_max, 300.0)
            filled = [entry for entry in rows
                      if str(entry.get("name") or "").strip()
                      or str(entry.get("title") or "").strip()]
            shown = filled[:5]
            card_h = 30 + max(len(shown), 1) * 14 + 18
            need(meta_h + card_h + 8)
            top = state["y"]
            draw_avatar(top, who, mine)
            draw_meta(top, who, mine, clock_text)
            card_x = side_x(card_w, mine)
            card_y = top - meta_h - card_h
            pdf.setFillColor(other_bubble if not mine else HexColor("#f7fdf7"))
            pdf.setStrokeColor(line_color)
            pdf.setLineWidth(0.6)
            pdf.roundRect(card_x, card_y, card_w, card_h, 6, stroke=1, fill=1)
            pdf.setFillColor(ink)
            pdf.setFont(font_bold, 10)
            pdf.drawString(card_x + 10, card_y + card_h - 16,
                           clip_line(title, font_bold, 10, card_w - 20))
            pdf.setStrokeColor(line_color)
            pdf.setLineWidth(0.5)
            pdf.line(card_x + 10, card_y + card_h - 22,
                     card_x + card_w - 10, card_y + card_h - 22)
            pdf.setFont(font, 9)
            row_y = card_y + card_h - 36
            if not shown:
                pdf.setFillColor(muted)
                pdf.drawString(card_x + 10, row_y, "（子消息内容未解析）")
            for entry in shown:
                who_txt = _pdf_safe(str(entry.get("name") or ""))
                text_txt = _pdf_safe(str(entry.get("title") or ""))
                line_txt = clip_line(f"{who_txt} {text_txt}".strip(), font, 9,
                                     card_w - 24)
                pdf.setFillColor(ink)
                pdf.drawString(card_x + 10, row_y, line_txt)
                row_y -= 14
            tail = "已截断" if extra.get("forward_truncated") else f"共 {len(rows)} 条"
            pdf.setFillColor(muted)
            pdf.setFont(font, 8)
            pdf.drawString(card_x + 10, card_y + 8, tail)
            pdf.drawRightString(card_x + card_w - 10, card_y + 8, "聊天记录 ›")
            state["y"] = card_y - 8
            continue

        chips = {
            "video": ("视频", item.get("text") or "视频未缓存到本机"),
            "card": ("名片", extra.get("nickname") or item.get("text") or ""),
            "location": ("位置", extra.get("poi") or extra.get("label") or item.get("text") or ""),
            "redpacket": ("红包", item.get("text") or extra.get("descr") or ""),
            "transfer": ("转账", item.get("text") or extra.get("descr") or ""),
            "call": ("通话", item.get("text") or extra.get("descr") or ""),
            "note": ("笔记", item.get("title") or item.get("text") or ""),
            "solitaire": ("接龙", item.get("title") or item.get("text") or ""),
            "announce": ("群公告", item.get("title") or item.get("text") or ""),
        }
        if kind in chips:
            chip_label, chip_value = chips[kind]
            chip_value = _pdf_safe(chip_value) or {
                "video": "视频未缓存到本机",
                "card": "微信名片", "location": "位置", "redpacket": "微信红包",
                "transfer": "转账", "call": "语音通话", "note": "收藏笔记",
                "solitaire": "群接龙", "announce": "群公告",
            }[kind]
            chip_h = 26.0
            label_w = pdfmetrics.stringWidth(chip_label, font_bold, 8.5) + 12
            value_max = bubble_max - label_w - 24
            chip_value = clip_line(chip_value, font, 10, value_max)
            value_w = pdfmetrics.stringWidth(chip_value, font, 10)
            chip_w = min(bubble_max, 10 + label_w + 6 + value_w + 10)
            need(meta_h + chip_h + 8)
            top = state["y"]
            draw_avatar(top, who, mine)
            draw_meta(top, who, mine, clock_text)
            chip_x = side_x(chip_w, mine)
            chip_y = top - meta_h - chip_h
            pdf.setFillColor(HexColor("#eef7f0" if mine else "#f4f5f7"))
            pdf.setStrokeColor(line_color)
            pdf.setLineWidth(0.6)
            pdf.roundRect(chip_x, chip_y, chip_w, chip_h, 13, stroke=1, fill=1)
            pdf.setFillColor(HexColor("#07c160"))
            pdf.setFont(font_bold, 8.5)
            pdf.drawString(chip_x + 10, chip_y + 9, chip_label)
            pdf.setFillColor(ink)
            pdf.setFont(font, 10)
            pdf.drawString(chip_x + 10 + label_w + 6, chip_y + 9, chip_value)
            state["y"] = chip_y - 8
            continue

        quote = extra.get("quote") if kind == "quote" else None
        raw_body = _message_body(item)
        if quote:
            raw_body = item.get("text") or ""
        body = _pdf_safe(raw_body)
        image = _pdf_image(item, base_dir)
        if image:
            body = ""
        if not body.strip() and not image:
            if kind == "sticker":
                body = "[动画表情]"
            else:
                body = "[表情]" if raw_body.strip() else f"[{item.get('label') or '消息'}]"
        inner = bubble_max - pad * 2
        quote_lines: list[str] = []
        quote_h = 0.0
        if quote:
            qtext = (f"{quote.get('sender') or ''}："
                     f"{quote.get('text') or quote.get('summary') or ''}").strip("：")
            if qtext:
                quote_lines = _pdf_wrap(_pdf_safe(qtext), font, 8.5, inner - 14)[:4]
                quote_h = len(quote_lines) * 12 + 10
        lines = _pdf_wrap(body, font, font_size, inner) if body.strip() else []
        widest = max((pdfmetrics.stringWidth(row, font, font_size) for row in lines),
                     default=0.0)
        bubble_w = min(bubble_max, max(60.0, widest + pad * 2))
        if image:
            bubble_w = max(bubble_w, min(198.0, image[1] + pad * 2))
            if body.strip():
                lines = _pdf_wrap(body, font, font_size, bubble_w - pad * 2)
        if quote_h:
            bubble_w = max(bubble_w, min(bubble_max, 150.0))
            quote_lines = _pdf_wrap(_pdf_safe(
                f"{quote.get('sender') or ''}："
                f"{quote.get('text') or quote.get('summary') or ''}").strip("："),
                font, 8.5, bubble_w - pad * 2 - 14)[:4]
            quote_h = len(quote_lines) * 12 + 10
        text_h = ((len(lines) - 1) * line_h + font_size * 0.98) if lines else 0.0
        img_h = (image[2] + (8 if lines else 0)) if image else 0.0
        bubble_h = pad * 2 + quote_h + (6 if quote_h and (lines or image) else 0) \
            + text_h + img_h
        need(meta_h + bubble_h + 8)

        top = state["y"]
        draw_avatar(top, who, mine)
        draw_meta(top, who, mine, clock_text)

        bubble_x = side_x(bubble_w, mine)
        bubble_y = top - meta_h - bubble_h
        bare = bool(image) and (kind == "sticker")
        if not bare:
            pdf.setFillColor(mine_bubble if mine else other_bubble)
            if mine:
                pdf.roundRect(bubble_x, bubble_y, bubble_w, bubble_h, 5, stroke=0, fill=1)
            else:
                pdf.setStrokeColor(line_color)
                pdf.setLineWidth(0.6)
                pdf.roundRect(bubble_x, bubble_y, bubble_w, bubble_h, 5, stroke=1, fill=1)
            tip_y = bubble_y + bubble_h - 14
            path = pdf.beginPath()
            if mine:
                pdf.setFillColor(mine_bubble)
                path.moveTo(bubble_x + bubble_w + 6, tip_y)
                path.lineTo(bubble_x + bubble_w, tip_y - 5)
                path.lineTo(bubble_x + bubble_w, tip_y + 5)
                pdf.drawPath(path, stroke=0, fill=1)
            else:
                pdf.setFillColor(other_bubble)
                pdf.setStrokeColor(line_color)
                pdf.setLineWidth(0.6)
                path.moveTo(bubble_x - 6, tip_y)
                path.lineTo(bubble_x, tip_y - 5)
                path.lineTo(bubble_x, tip_y + 5)
                pdf.drawPath(path, stroke=1, fill=1)
        text_top = bubble_y + bubble_h - pad
        if quote_h:
            quote_x = bubble_x + pad
            quote_w = max(80.0, bubble_w - pad * 2)
            quote_y = text_top - quote_h
            pdf.setFillColor(HexColor("#f2f3f5"))
            pdf.roundRect(quote_x, quote_y, quote_w, quote_h, 4, stroke=0, fill=1)
            pdf.setFillColor(HexColor("#b7bec7"))
            pdf.roundRect(quote_x, quote_y, 2.6, quote_h, 1.3, stroke=0, fill=1)
            pdf.setFillColor(muted)
            pdf.setFont(font, 8.5)
            row_y = quote_y + quote_h - 4
            for row in quote_lines:
                row_y -= 10
                pdf.drawString(quote_x + 8, row_y, row)
            text_top = quote_y - 6
        pdf.setFillColor(HexColor("#0d1a0a" if mine else "#111111"))
        pdf.setFont(font, font_size)
        text_y = text_top - font_size * 0.80
        for row in lines:
            pdf.drawString(bubble_x + pad, text_y, row)
            text_y -= line_h
        if image:
            img_y = bubble_y + pad
            with suppress(Exception):
                pdf.drawImage(image[0], bubble_x + pad, img_y,
                              width=image[1], height=image[2],
                              preserveAspectRatio=True, mask="auto")
        state["y"] = bubble_y - 8

    footer()
    try:
        pdf.save()
    except Exception as exc:
        logger.warning("写入 PDF 失败：%s", exc)
        with suppress(OSError):
            pdf_path.unlink()
        return False
    return pdf_path.exists() and pdf_path.stat().st_size > 0


def _run_export(job, picked: list[dict[str, Any]], window: dict[str, Any],
                out_dir: Path, output_root: Path | None = None) -> None:
    store = _store()
    with _LOCK:
        account = (_STATE.get("account") or {}).get("account", "")
        snapshot_id = _STATE.get("snapshot_id", "")
    exported_at = _iso()
    rows: list[dict[str, Any]] = []
    total_messages = 0
    unknown = 0
    used: set[str] = set()
    pdf_files = 0
    media = _media_store() if _media_store().available() else None
    media_totals = {"images": 0, "files": 0, "voices": 0, "videos": 0, "links": 0,
                    "stickers": 0, "missing": 0, "skipped": 0}
    article_totals = {"saved": 0, "snapshot": 0, "reused": 0, "missing": 0}
    article_cache: dict[str, str] = {}
    article_budget = {"left": ARTICLE_TIME_BUDGET}
    media_budget = {"left": MAX_MEDIA_TOTAL_BYTES}
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        for index, session in enumerate(picked, start=1):
            job.check_canceled()
            base = index - 1
            label = session["display"]
            job.done = base
            job.message = f"读取消息：{label}"

            def on_page(scanned: int, _i: int = index, _label: str = label) -> None:
                job.done = (_i - 1) + 0.4 * (1 - math.exp(-scanned / 4000.0))
                job.message = f"读取消息：{_label}"

            items = list(_iter_all(store, session["conversation_id"], window, job, on_page))
            items.reverse()
            job.done = base + 0.4
            base_name = _clean_name(label)
            name = base_name
            suffix = 2
            while name.casefold() in used:
                name = f"{base_name}-{suffix}"
                suffix += 1
            used.add(name.casefold())
            page_name = f"{name}.html"
            md_name = f"{name}.md"
            pdf_name = f"{name}.pdf"
            if media is not None:
                job.message = f"整理媒体：{label}"

                def on_step(step: int, total: int, _i: int = index,
                            _label: str = label) -> None:
                    job.done = (_i - 1) + 0.4 + 0.4 * (step / max(1, total))
                    job.message = f"整理媒体：{_label}"

                stats = _collect_media(media, session["conversation_id"], items,
                                       out_dir, _clean_name(name, "chat"), job,
                                       media_budget, on_step=on_step)
                for key in media_totals:
                    media_totals[key] += stats.get(key, 0)
            job.done = base + 0.8
            job.message = f"保存公众号文章：{label}"

            def on_article(step: int, total: int, _i: int = index,
                           _label: str = label) -> None:
                job.done = (_i - 1) + 0.8 + 0.08 * (step / max(1, total))
                job.message = f"保存公众号文章：{_label}"

            stats = _export_articles(items, out_dir, output_root, job,
                                     on_step=on_article, cache=article_cache,
                                     budget=article_budget)
            for key in article_totals:
                article_totals[key] += stats.get(key, 0)
            job.done = base + 0.88
            job.message = f"写入文件：{label}"
            (out_dir / page_name).write_text(
                _session_html(session, items, account, exported_at),
                encoding="utf-8")
            _write_markdown(out_dir / md_name, session, items, account, exported_at)
            job.done = base + 0.92
            job.message = f"生成 PDF：{label}"
            if _session_pdf(session, items, out_dir / pdf_name, out_dir,
                            exported_at, account):
                pdf_files += 1
            total_messages += len(items)
            unknown += sum(1 for item in items if item.get("kind") in ("other", "app"))
            rows.append({
                "display": session["display"],
                "kind": session.get("kind"),
                "file": page_name,
                "markdown": md_name,
                "pdf": pdf_name if (out_dir / pdf_name).exists() else "",
                "count": len(items),
                "summary": (items[-1].get("summary") if items
                            else session.get("last_summary") or "")[:80],
            })
            job.done = index
        job.message = "写入索引与清单"
        (out_dir / "index.html").write_text(
            _index_html(rows, account, exported_at, total_messages), encoding="utf-8")
        manifest = {
            "application": "wxMoments",
            "schema_version": 1,
            "account": account,
            "snapshot_id": snapshot_id,
            "created_at": exported_at,
            "time_zone": "Asia/Shanghai",
            "format": "html+markdown+pdf",
            "sessions_requested": len(picked),
            "sessions_exported": len(rows),
            "messages_exported": total_messages,
            "messages_unknown_type": unknown,
            "media": media_totals,
            "articles": article_totals,
            "pdf_exported": pdf_files,
            "filters": {
                "start_date": window.get("start"),
                "end_date": window.get("end"),
                "kinds": (sorted(window["kinds"]) if window.get("kinds") is not None
                          else "all"),
            },
            "files": ([row["file"] for row in rows]
                      + [row["markdown"] for row in rows]
                      + [row["pdf"] for row in rows if row["pdf"]]),
        }
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        job.results.append({
            "sessions": len(rows), "messages": total_messages, "dir": str(out_dir)})
        extra = ""
        if media_totals["images"] or media_totals["files"] or media_totals["voices"]:
            extra = (f" · 图片 {media_totals['images']} · 文件 {media_totals['files']}"
                     f" · 语音 {media_totals['voices']}")
        article_saved = (article_totals["saved"] + article_totals["reused"]
                         + article_totals["snapshot"])
        if article_saved:
            extra += f" · 公众号文章 {article_saved}"
        if media_totals["skipped"]:
            extra += f" · 跳过 {media_totals['skipped']} 个过大附件"
        if pdf_files < len(rows):
            extra += f" · PDF {pdf_files}/{len(rows)}"
        job.finish("done", f"已导出 {len(rows)} 个会话 · {total_messages} 条消息{extra}")
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise


def start_export(payload: dict, *, start_job: Callable, output_root: Path) -> dict[str, Any]:
    with _LOCK:
        state = dict(_STATE)
    if not state:
        return {"ok": False, "msg": "请先点击「刷新」"}
    if payload.get("snapshot_id") and str(payload["snapshot_id"]) != state["snapshot_id"]:
        return {"ok": False, "msg": "数据已更新，请重新选择会话"}
    index = {item["conversation_id"]: item for item in state["sessions"]}
    requested = [str(item) for item in (payload.get("sessions") or [])][:MAX_EXPORT_SESSIONS]
    picked = [index[item] for item in requested if item in index]
    if not picked:
        return {"ok": False, "msg": "没有选择会话"}
    try:
        window = _parse_range(payload.get("filters") or {})
    except ValueError as exc:
        return {"ok": False, "msg": str(exc)}
    if window.get("kinds") == set():
        return {"ok": False, "msg": "请至少选择一种导出内容"}
    stamp = f"{_now():%Y%m%d_%H%M%S}"
    out_dir = Path(output_root) / f"聊天记录-{stamp}"
    suffix = 2
    while out_dir.exists():
        out_dir = Path(output_root) / f"聊天记录-{stamp}-{suffix}"
        suffix += 1
    job = start_job(len(picked), _run_export, picked, window, out_dir, Path(output_root))
    _EXPORT_JOBS.append(job)
    return {"ok": True, "job": job.id, "dir": str(out_dir), "count": len(picked)}
