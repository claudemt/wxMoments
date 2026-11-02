from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

LABELS_VERSION = 1
LABEL_ID_PREFIX = "wlab:"
LABEL_FOLDER_PREFIX = "朋友圈-微信标签-"

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_EMPTY = "empty"
STATUS_NEVER = "never"
STATUS_ERROR = "error"
STATUS_REMOVED = "removed"

STATUS_TEXT = {
    STATUS_OK: "已同步",
    STATUS_PARTIAL: "成员不完整",
    STATUS_EMPTY: "空标签",
    STATUS_NEVER: "未同步",
    STATUS_ERROR: "同步失败",
    STATUS_REMOVED: "微信中已删除",
}

_LABEL_TABLE = "contact_label"
_MEMBER_TABLE_RE = re.compile(r"(^|_)(label|tag)|(label|tag)($|_)", re.IGNORECASE)


def label_group_id(label_id: Any) -> str:
    return f"{LABEL_ID_PREFIX}{str(label_id).strip()}"


def is_label_group_id(value: Any) -> bool:
    return str(value or "").startswith(LABEL_ID_PREFIX)


def label_id_from_group(value: Any) -> str:
    text = str(value or "")
    return text[len(LABEL_ID_PREFIX):].strip() if text.startswith(LABEL_ID_PREFIX) else ""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def normalize_text(value: Any) -> str:
    text = str(value or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip().casefold()


def sanitize_folder_name(value: str, fallback: str = "标签") -> str:
    from wxmoments import sanitize_folder_name as sanitize

    return sanitize(value, fallback)


@dataclass
class MemberMatch:
    members: list[dict[str, str]] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> int:
        return len(self.members)

    def to_dict(self) -> dict[str, Any]:
        return {
            "members": list(self.members),
            "unmatched": list(self.unmatched),
            "ambiguous": list(self.ambiguous),
            "duplicates": list(self.duplicates),
        }


def build_contact_index(contacts: Iterable[Any]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for contact in contacts:
        username = str(getattr(contact, "username", "") or "").strip()
        if not username:
            continue
        for value in (getattr(contact, "remark", ""), getattr(contact, "nickname", "")):
            key = normalize_text(value)
            if not key:
                continue
            bucket = index.setdefault(key, [])
            if username not in bucket:
                bucket.append(username)
    return index


def candidate_texts(raw: str, label_names: Iterable[str] = ()) -> list[str]:
    text = str(raw or "").replace("\xa0", " ").strip()
    if not text:
        return []
    out: list[str] = [text]
    names = [n for n in label_names if n]
    for name in names:
        if text.endswith(" " + name):
            out.append(text[: -len(name) - 1].strip())
    if "  " in text:
        parts = [part.strip() for part in re.split(r"\s{2,}", text) if part.strip()]
        out.extend(parts)
        if len(parts) > 2:
            out.append(" ".join(parts[:-1]))
    elif " " in text:
        parts = [part for part in text.split(" ") if part]
        out.extend(parts)
        if len(parts) > 2:
            out.append(" ".join(parts[:-1]))
            out.append(" ".join(parts[1:]))
    seen: set[str] = set()
    uniq: list[str] = []
    for value in out:
        key = normalize_text(value)
        if key and key not in seen:
            seen.add(key)
            uniq.append(value.strip())
    return uniq


def resolve_members(rows: Iterable[str], contacts: Iterable[Any], *,
                    label_names: Iterable[str] = ()) -> MemberMatch:
    names = {str(getattr(c, "username", "")): (getattr(c, "remark", "") or getattr(c, "nickname", ""))
             for c in contacts}
    index = build_contact_index(contacts)
    result = MemberMatch()
    used: set[str] = set()
    for raw in rows:
        candidates: list[str] = []
        for text in candidate_texts(raw, label_names):
            for username in index.get(normalize_text(text), []):
                if username not in candidates:
                    candidates.append(username)
        if not candidates:
            result.unmatched.append(str(raw).strip())
            continue
        if len(candidates) > 1:
            result.ambiguous.append({"text": str(raw).strip(), "candidates": len(candidates)})
            continue
        username = candidates[0]
        if username in used:
            result.duplicates.append(str(raw).strip())
            continue
        used.add(username)
        result.members.append({
            "wxid": username,
            "name": names.get(username, "") or str(raw).strip(),
            "text": str(raw).strip(),
        })
    return result


@dataclass
class LabelDefinitions:
    available: bool = False
    reason: str = ""
    labels: list[dict[str, Any]] = field(default_factory=list)
    membership: str = "none"
    members: dict[str, list[str]] = field(default_factory=dict)
    member_names: dict[str, dict[str, str]] = field(default_factory=dict)
    schema: dict[str, Any] = field(default_factory=dict)


def _open_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]
    except Exception:
        return []


def _pick(columns: Iterable[str], *candidates: str) -> str:
    lower = {str(c).lower(): str(c) for c in columns}
    for name in candidates:
        found = lower.get(name.lower())
        if found:
            return found
    return ""


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        for encoding in ("utf-8", "utf-16-le"):
            try:
                text = raw.decode(encoding).replace("\x00", "").strip()
            except (UnicodeDecodeError, ValueError):
                continue
            if text:
                return text
        return ""
    return str(value).replace("\xa0", " ").strip()


def _decode_label_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        try:
            text = data.decode("utf-8")
            if text and all(ch.isdigit() or ch in ",; " for ch in text):
                return [item for item in re.split(r"[,; ]+", text) if item]
        except Exception:
            pass
        ids: list[str] = []
        index = 0
        while index < len(data):
            value_int = 0
            shift = 0
            while index < len(data):
                byte = data[index]
                index += 1
                value_int |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            ids.append(str(value_int))
        return ids
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item for item in re.split(r"[,; ]+", text) if item]


def _pb_fields(blob: bytes) -> Iterable[tuple[int, int, Any]]:
    data = bytes(blob or b"")
    index = 0
    while index < len(data):
        key = 0
        shift = 0
        while index < len(data):
            byte = data[index]
            index += 1
            key |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        else:
            return
        field_no, wire = key >> 3, key & 7
        if not field_no:
            return
        if wire == 0:
            value = 0
            shift = 0
            while index < len(data):
                byte = data[index]
                index += 1
                value |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            else:
                return
        elif wire == 2:
            length = 0
            shift = 0
            while index < len(data):
                byte = data[index]
                index += 1
                length |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            else:
                return
            if length < 0 or index + length > len(data):
                return
            value = data[index:index + length]
            index += length
        elif wire == 5:
            value = data[index:index + 4]
            index += 4
        elif wire == 1:
            value = data[index:index + 8]
            index += 8
        else:
            return
        yield field_no, wire, value


_LABEL_LIST_RE = re.compile(r"^\d+(,\d+)*$")


def _extra_buffer_label_ids(blob: Any, known: set[str]) -> list[str]:
    if blob is None:
        return []
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    if not isinstance(blob, (bytes, bytearray)):
        return []
    loose: list[str] = []
    for field_no, wire, value in _pb_fields(bytes(blob)):
        if wire != 2 or not isinstance(value, (bytes, bytearray)):
            continue
        try:
            text = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            continue
        text = text.strip()
        if not _LABEL_LIST_RE.match(text):
            continue
        ids = [item for item in text.split(",") if item]
        if known and all(item in known for item in ids):
            return ids
        if field_no == 30:
            loose.append(ids)
    return loose[0] if loose else []


def _read_db_membership(conn: sqlite3.Connection, label_ids: Iterable[str]) -> tuple[str, dict[str, list[str]]]:
    wanted = {str(item) for item in label_ids}
    tables = [str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]

    for table in tables:
        if table.lower() == _LABEL_TABLE or not _MEMBER_TABLE_RE.search(table):
            continue
        columns = _columns(conn, table)
        label_col = _pick(columns, "label_id_", "label_id", "labelid", "tag_id", "tag_id_")
        member_col = _pick(columns, "member_id_", "member_id", "contact_id", "username")
        if not (label_col and member_col):
            continue
        try:
            rows = conn.execute(
                f'SELECT l."{label_col}" AS lid, c.username AS uname '
                f'FROM "{table}" l JOIN contact c ON c.id = l."{member_col}"').fetchall()
        except Exception:
            continue
        members: dict[str, list[str]] = {}
        for row in rows:
            lid = str(_decode(row["lid"]) or "").strip()
            uname = str(_decode(row["uname"]) or "").strip()
            if lid in wanted and uname:
                bucket = members.setdefault(lid, [])
                if uname not in bucket:
                    bucket.append(uname)
        if members:
            return "db", members

    for table in ("contact", "stranger"):
        columns = _columns(conn, table)
        label_col = _pick(columns, "label_id_list", "label_ids", "labelidlist")
        if not label_col:
            continue
        try:
            rows = conn.execute(
                f'SELECT username, "{label_col}" AS labels FROM "{table}"').fetchall()
        except Exception:
            continue
        members = {}
        for row in rows:
            uname = str(_decode(row["username"]) or "").strip()
            if not uname:
                continue
            for lid in _decode_label_ids(row["labels"]):
                if lid in wanted:
                    bucket = members.setdefault(lid, [])
                    if uname not in bucket:
                        bucket.append(uname)
        if members:
            return "db", members

    columns = _columns(conn, "contact")
    blob_col = _pick(columns, "extra_buffer", "extra_buf", "extrabuf")
    if blob_col:
        try:
            rows = conn.execute(
                f'SELECT username, "{blob_col}" AS blob FROM contact '
                "WHERE username NOT LIKE '%@chatroom' AND username NOT LIKE 'gh_%'").fetchall()
        except Exception:
            rows = []
        members = {}
        for row in rows:
            uname = str(_decode(row["username"]) or "").strip()
            if not uname:
                continue
            for lid in _extra_buffer_label_ids(row["blob"], wanted):
                if lid in wanted:
                    bucket = members.setdefault(lid, [])
                    if uname not in bucket:
                        bucket.append(uname)
        if members:
            return "db", members
    return "none", {}


def _read_member_names(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    try:
        rows = conn.execute("SELECT username, remark, nick_name FROM contact").fetchall()
    except Exception:
        return out
    for row in rows:
        username = str(_decode(row[0]) or "").strip()
        if not username:
            continue
        out[username] = {
            "remark": _decode(row[1]).strip(),
            "nick": _decode(row[2]).strip(),
        }
    return out


def read_label_definitions(account_dir: Path) -> LabelDefinitions:
    contact_db = Path(account_dir) / "contact.db"
    if not contact_db.exists():
        return LabelDefinitions(reason="还没有解密到 contact.db，请先点击「刷新」")
    try:
        conn = _open_readonly(contact_db)
    except Exception as exc:
        return LabelDefinitions(reason=f"contact.db 打开失败：{exc}")
    try:
        tables = [str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        if _LABEL_TABLE not in tables:
            return LabelDefinitions(
                reason="当前微信版本的联系人库没有标签表，无法读取原生标签",
                schema={"tables": tables[:40]},
            )
        columns = _columns(conn, _LABEL_TABLE)
        id_col = _pick(columns, "label_id_", "label_id", "labelid", "id")
        name_col = _pick(columns, "label_name_", "label_name", "name")
        order_col = _pick(columns, "sort_order_", "sort_order", "order")
        if not (id_col and name_col):
            return LabelDefinitions(
                reason="标签表结构与预期不一致（缺少 ID 或名称列）",
                schema={"contact_label": columns},
            )
        rows = conn.execute(f'SELECT * FROM "{_LABEL_TABLE}"').fetchall()
        labels: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            label_id = str(_decode(row[id_col]) or "").strip()
            name = _decode(row[name_col]).strip()
            if not label_id or not name or label_id in seen:
                continue
            seen.add(label_id)
            try:
                order = int(row[order_col]) if order_col else len(labels)
            except Exception:
                order = len(labels)
            labels.append({"id": label_id, "name": name, "order": order})
        labels.sort(key=lambda item: (item["order"], item["name"]))
        membership, members = _read_db_membership(conn, [item["id"] for item in labels])
        return LabelDefinitions(
            available=True,
            labels=labels,
            membership=membership,
            members=members,
            member_names=_read_member_names(conn) if membership == "db" else {},
            schema={"contact_label": columns, "tables": tables[:40]},
        )
    except Exception as exc:
        return LabelDefinitions(reason=f"读取标签定义失败：{exc}")
    finally:
        conn.close()


def empty_store() -> dict[str, Any]:
    return {"version": LABELS_VERSION, "accounts": {}}


def load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return empty_store()
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        return empty_store()
    data["version"] = int(data.get("version") or LABELS_VERSION)
    return data


def save_store(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def account_record(store: dict[str, Any], account: str) -> dict[str, Any]:
    accounts = store.setdefault("accounts", {})
    record = accounts.get(account)
    if not isinstance(record, dict):
        record = {"updated": "", "labels": []}
        accounts[account] = record
    if not isinstance(record.get("labels"), list):
        record["labels"] = []
    return record


def label_records(store: dict[str, Any], account: str) -> list[dict[str, Any]]:
    if not account:
        return []
    return [item for item in account_record(store, account)["labels"] if isinstance(item, dict)]


def _new_label(label_id: str, name: str, order: int) -> dict[str, Any]:
    return {
        "id": str(label_id),
        "name": name,
        "order": order,
        "source": "wechat_label",
        "status": STATUS_NEVER,
        "complete": False,
        "present": True,
        "displayed_total": 0,
        "read_total": 0,
        "members": [],
        "unmatched": [],
        "ambiguous": [],
        "duplicates": [],
        "synced_at": "",
        "sync_error": "",
        "error_code": "",
        "read_method": "",
        "last_export": "",
        "last_seen_posts": 0,
        "imported_at": "",
        "edited_locally": False,
        "remote_members": [],
        "wechat_members": [],
        "pending": None,
        "remote_checked_at": "",
    }


def recompute_status(record: dict[str, Any]) -> str:
    if not record.get("present", True):
        status = STATUS_REMOVED
    elif record.get("edited_locally"):
        status = STATUS_OK if record.get("members") else STATUS_EMPTY
    elif not record.get("synced_at"):
        status = STATUS_ERROR if record.get("sync_error") else STATUS_NEVER
    elif record.get("displayed_total", 0) == 0 and not record.get("members") \
            and not record.get("unmatched"):
        status = STATUS_EMPTY
    elif record.get("complete"):
        status = STATUS_OK
    else:
        status = STATUS_PARTIAL
    record["status"] = status
    return status


def apply_match(record: dict[str, Any], match: MemberMatch, *, source: str,
                displayed_total: int, read_total: int, complete: bool) -> None:
    record["members"] = match.members
    record["unmatched"] = match.unmatched
    record["ambiguous"] = match.ambiguous
    record["duplicates"] = match.duplicates
    record["displayed_total"] = int(displayed_total or 0)
    record["read_total"] = int(read_total or 0)
    record["complete"] = bool(complete)
    record["read_method"] = source
    record["synced_at"] = _now()
    record["sync_error"] = ""
    record["error_code"] = ""
    recompute_status(record)


def _clean_members(items: Iterable[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, str):
            item = {"wxid": item, "name": item}
        wxid = str(item.get("wxid") or "").strip()
        if not wxid or wxid in seen:
            continue
        seen.add(wxid)
        name = str(item.get("name") or item.get("text") or wxid)
        out.append({"wxid": wxid, "name": name, "text": name})
    return out


def diff_members(remote: Iterable[Any], baseline: Iterable[Any]) -> dict[str, list[dict[str, str]]]:
    remote = _clean_members(remote)
    baseline = _clean_members(baseline)
    remote_ids = {item["wxid"] for item in remote}
    base_ids = {item["wxid"] for item in baseline}
    return {
        "added": [item for item in remote if item["wxid"] not in base_ids],
        "removed": [item for item in baseline if item["wxid"] not in remote_ids],
    }


def is_imported(record: dict[str, Any]) -> bool:
    return bool(record.get("imported_at"))


def import_members(record: dict[str, Any], remote: Iterable[Any], *, source: str = "db",
                   displayed_total: int = 0, read_total: int = 0, complete: bool = True) -> None:
    members = _clean_members(remote)
    match = MemberMatch(members=members)
    apply_match(record, match, source=source, displayed_total=displayed_total,
                read_total=read_total, complete=complete)
    record["members"] = members
    record["remote_members"] = [dict(item) for item in members]
    record["wechat_members"] = [dict(item) for item in members]
    record["pending"] = None
    record["imported_at"] = _now()
    record["edited_locally"] = False
    record["present"] = True
    recompute_status(record)


def refresh_remote(record: dict[str, Any], remote: Iterable[Any], *, displayed_total: int = 0,
                   read_total: int = 0, complete: bool = True) -> dict[str, Any]:
    if not is_imported(record):
        baseline = _clean_members(record.get("members") or [])
        record["wechat_members"] = baseline
        record["imported_at"] = record.get("synced_at") or _now()
    members = _clean_members(remote)
    record["remote_members"] = [dict(item) for item in members]
    record["remote_checked_at"] = _now()
    record["present"] = True
    record["displayed_total"] = int(displayed_total or len(members))
    record["read_total"] = int(read_total or len(members))
    if complete:
        record["complete"] = True
    record["pending"] = diff_members(members, record.get("wechat_members") or [])
    if not record["pending"]["added"] and not record["pending"]["removed"]:
        record["pending"] = None
    recompute_status(record)
    return record["pending"] or {"added": [], "removed": []}


def apply_pending(record: dict[str, Any], *, mode: str = "sync") -> dict[str, Any]:
    remote = _clean_members(record.get("remote_members") or [])
    if mode == "sync":
        record["members"] = [dict(item) for item in remote]
        record["edited_locally"] = False
    record["wechat_members"] = [dict(item) for item in remote]
    record["pending"] = None
    record["synced_at"] = _now()
    record["sync_error"] = ""
    record["error_code"] = ""
    record["complete"] = True
    recompute_status(record)
    return record


def save_local_members(record: dict[str, Any], usernames: Iterable[Any],
                       contacts: Iterable[Any]) -> int:
    by_username: dict[str, Any] = {}
    for contact in contacts or []:
        username = str(getattr(contact, "username", "") or "").strip()
        if username:
            by_username[username] = contact
    members: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in usernames or []:
        username = str(raw or "").strip()
        if not username or username in seen:
            continue
        seen.add(username)
        contact = by_username.get(username)
        name = ""
        if contact is not None:
            name = (getattr(contact, "remark", "") or getattr(contact, "nickname", "") or "")
        members.append({"wxid": username, "name": name or username, "text": name or username})
    record["members"] = members
    record["edited_locally"] = True
    record["complete"] = True
    recompute_status(record)
    return len(members)


def pending_notice(record: dict[str, Any]) -> str:
    pending = record.get("pending") or {}
    added = [str(item.get("name") or item.get("wxid") or "") for item in pending.get("added") or []]
    removed = [str(item.get("name") or item.get("wxid") or "") for item in pending.get("removed") or []]
    if not added and not removed:
        return ""
    parts: list[str] = []
    if added:
        parts.append(f"增添了 {'、'.join(added)}")
    if removed:
        parts.append(f"删除了 {'、'.join(removed)}")
    return f"「{record.get('name')}」{'，'.join(parts)}"


def pending_summary(record: dict[str, Any]) -> str:
    pending = record.get("pending") or {}
    added = len(pending.get("added") or [])
    removed = len(pending.get("removed") or [])
    if not added and not removed:
        return ""
    parts = []
    if added:
        parts.append(f"增添 {added} 人")
    if removed:
        parts.append(f"删除 {removed} 人")
    return f"微信侧{'，'.join(parts)}"


def deleted_notice(record: dict[str, Any]) -> str:
    if record.get("present", True):
        return ""
    return f"「{record.get('name')}」"


def merge_definitions(store: dict[str, Any], account: str, definitions: LabelDefinitions,
                      contacts: Iterable[Any]) -> dict[str, Any]:
    if not account or not definitions.available:
        return {"ok": False, "reason": definitions.reason or "没有可用的标签定义",
                "imported": [], "removed": [], "renamed": []}
    contacts = list(contacts)
    record = account_record(store, account)
    existing = {str(item.get("id")): item for item in record["labels"] if isinstance(item, dict)}
    merged: list[dict[str, Any]] = []
    imported: list[str] = []
    removed: list[str] = []
    renamed: list[dict[str, str]] = []
    for definition in definitions.labels:
        label_id = str(definition["id"])
        item = existing.get(label_id)
        if item is None:
            item = _new_label(label_id, definition["name"], definition["order"])
            imported.append(definition["name"])
        elif str(item.get("name") or "") != str(definition["name"]):
            renamed.append({"from": str(item.get("name") or ""), "to": definition["name"]})
        item["name"] = definition["name"]
        item["order"] = definition["order"]
        item["present"] = True
        item["source"] = "wechat_label"
        recompute_status(item)
        merged.append(item)
    for label_id, item in existing.items():
        if label_id in {str(d["id"]) for d in definitions.labels}:
            continue
        if item.get("present", True):
            removed.append(str(item.get("name") or ""))
        item["present"] = False
        item["remote_members"] = []
        item["pending"] = None
        recompute_status(item)
        merged.append(item)
    merged.sort(key=lambda item: (0 if item.get("present", True) else 1,
                                  int(item.get("order") or 0), str(item.get("name") or "")))
    record["labels"] = merged
    record["updated"] = _now()
    record["definitions"] = {
        "available": True,
        "membership": definitions.membership,
        "read_at": _now(),
    }
    return {"ok": True, "count": len(definitions.labels), "membership": definitions.membership,
            "imported": imported, "removed": removed, "renamed": renamed}


def apply_db_membership(store: dict[str, Any], account: str, definitions: LabelDefinitions,
                        contacts: Iterable[Any]) -> dict[str, Any]:
    if definitions.membership != "db":
        return {"imported": 0, "changed": 0, "pending": []}
    contacts = list(contacts)
    by_username: dict[str, Any] = {}
    for contact in contacts:
        username = str(getattr(contact, "username", "") or "").strip()
        if not username:
            continue
        by_username[username] = contact
    imported = changed = 0
    pending: list[dict[str, Any]] = []
    for record in label_records(store, account):
        if not record.get("present", True):
            continue
        usernames = definitions.members.get(str(record.get("id"))) or []
        remote = _remote_members(usernames, by_username, definitions)
        if not is_imported(record):
            import_members(record, remote, source="db", displayed_total=len(usernames),
                           read_total=len(usernames), complete=True)
            imported += 1
            continue
        diff = refresh_remote(record, remote, displayed_total=len(usernames),
                              read_total=len(usernames))
        if diff["added"] or diff["removed"]:
            changed += 1
            pending.append({"id": str(record.get("id")), "name": str(record.get("name") or ""),
                            "added": [m["name"] for m in diff["added"]],
                            "removed": [m["name"] for m in diff["removed"]]})
    return {"imported": imported, "changed": changed, "pending": pending}


def _remote_members(usernames: Iterable[str], by_username: dict[str, Any],
                    definitions: LabelDefinitions) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for username in usernames or []:
        username = str(username or "").strip()
        if not username or username in seen:
            continue
        seen.add(username)
        contact = by_username.get(username)
        if contact is not None:
            name = (getattr(contact, "remark", "") or getattr(contact, "nickname", "")
                    or username)
        else:
            names = definitions.member_names.get(username) or {}
            name = names.get("remark") or names.get("nick") or username
        out.append({"wxid": username, "name": name, "text": name})
    return out


STALE_DAYS = 7
AUTO_SKIP_CODES = {"tree_cold", "no_backend", "no_window"}


def _stale(synced_at: Any, *, days: int = STALE_DAYS) -> bool:
    try:
        when = datetime.strptime(str(synced_at), "%Y-%m-%d %H:%M")
    except Exception:
        return False
    return (datetime.now() - when).days >= days


def needs_sync(record: dict[str, Any], *, days: int = STALE_DAYS,
               auto: bool = False) -> bool:
    if not record.get("present", True):
        return False
    if is_imported(record):
        return False
    if auto and record.get("error_code") in AUTO_SKIP_CODES:
        return False
    if not record.get("synced_at"):
        return True
    return _stale(record.get("synced_at"), days=days)


def _match_names(record: dict[str, Any], contact_names: dict[str, str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for member in record.get("members") or []:
        wxid = str(member.get("wxid") or "").strip()
        if not wxid:
            continue
        out.append({"wxid": wxid, "name": contact_names.get(wxid) or member.get("name") or wxid})
    return out


def folder_for(record: dict[str, Any], records: Iterable[dict[str, Any]]) -> str:
    name = sanitize_folder_name(str(record.get("name") or ""))
    same = [item for item in records
            if str(item.get("name") or "") == str(record.get("name") or "")
            and str(item.get("id")) != str(record.get("id"))]
    suffix = f"-{record.get('id')}" if same else ""
    return f"{LABEL_FOLDER_PREFIX}{name}{suffix}"


def label_view(record: dict[str, Any], *, records: Iterable[dict[str, Any]],
               contact_names: dict[str, str], post_counts: dict[str, int]) -> dict[str, Any]:
    members = _match_names(record, contact_names)
    matched_ids = {member["wxid"] for member in members}
    with_posts = {wxid for wxid in matched_ids if post_counts.get(wxid)}
    total_posts = sum(post_counts.get(wxid, 0) for wxid in matched_ids)
    status = recompute_status(record)
    displayed = int(record.get("displayed_total") or 0)
    read_total = int(record.get("read_total") or 0)
    unmatched = len(record.get("unmatched") or [])
    ambiguous = len(record.get("ambiguous") or [])
    duplicates = len(record.get("duplicates") or [])

    notes: list[str] = []
    changed = pending_notice(record)
    gone = deleted_notice(record)
    summary = pending_summary(record)
    if summary:
        notes.append(summary)
    if not record.get("present", True):
        notes.append("微信已删除此标签，本地名单保留")
    if status == STATUS_NEVER:
        notes.append(record.get("sync_error") or "成员未读取，请同步")
    elif status == STATUS_PARTIAL:
        if displayed and read_total < displayed:
            notes.append(f"微信 {displayed} 人，读到 {read_total} 人")
        if unmatched:
            notes.append(f"{unmatched} 人未匹配")
        if ambiguous:
            notes.append(f"{ambiguous} 人同名")
        if duplicates:
            notes.append(f"{duplicates} 行重复")
    elif status == STATUS_ERROR:
        notes.append(record.get("sync_error") or "同步失败")
    if record.get("sync_error") and record.get("synced_at"):
        notes.append(f"同步失败，用 {record['synced_at']} 的快照")
    elif record.get("synced_at") and _stale(record.get("synced_at")):
        notes.append(f"快照来自 {record['synced_at']}，可能已过期")
    return {
        "id": label_group_id(record.get("id")),
        "label_id": str(record.get("id") or ""),
        "name": str(record.get("name") or ""),
        "source": "wechat_label",
        "status": status,
        "status_text": STATUS_TEXT.get(status, status),
        "note": "；".join(dict.fromkeys(notes)),
        "present": bool(record.get("present", True)),
        "displayed_total": displayed,
        "read_total": read_total,
        "matched": len(members),
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "duplicates": duplicates,
        "members_with_posts": len(with_posts),
        "total_posts": total_posts,
        "new_posts": max(0, total_posts - int(record.get("last_seen_posts") or 0)),
        "synced_at": str(record.get("synced_at") or ""),
        "sync_error": str(record.get("sync_error") or ""),
        "read_method": str(record.get("read_method") or ""),
        "last_export": str(record.get("last_export") or ""),
        "folder": folder_for(record, records),
        "members": members,
        "can_export": bool(members),
        "complete": bool(record.get("complete")),
        "edited_locally": bool(record.get("edited_locally")),
        "pending": {
            "added": [m["name"] for m in (record.get("pending") or {}).get("added") or []],
            "removed": [m["name"] for m in (record.get("pending") or {}).get("removed") or []],
        },
        "notice": changed,
        "deleted_notice": gone,
        "imported_at": str(record.get("imported_at") or ""),
    }


def mark_seen(record: dict[str, Any], total_posts: int) -> None:
    record["last_seen_posts"] = int(total_posts)


def mark_exported(record: dict[str, Any], total_posts: int) -> None:
    record["last_export"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    record["last_seen_posts"] = int(total_posts)


def export_group(record: dict[str, Any], *, records: Iterable[dict[str, Any]],
                 contact_names: dict[str, str]) -> dict[str, Any]:
    records = list(records)
    return {
        "id": label_group_id(record.get("id")),
        "label_id": str(record.get("id") or ""),
        "name": str(record.get("name") or ""),
        "folder": folder_for(record, records),
        "source": "wechat_label",
        "split": False,
        "keep_interactions": False,
        "status": recompute_status(record),
        "complete": bool(record.get("complete")),
        "members": _match_names(record, contact_names),
    }


class MemberReader(Protocol):
    def list_labels(self) -> list[Any]: ...
    def read_members(self, label: str, expected_total: int = 0, *,
                     count_known: bool = True) -> Any: ...


def _summary_line(record: dict[str, Any]) -> str:
    return (f"{record.get('name')}：微信 {record.get('displayed_total')} 人，"
            f"匹配 {len(record.get('members') or [])} 人")


def sync_members(store: dict[str, Any], account: str, contacts: Iterable[Any], reader: MemberReader,
                 *, only_ids: Iterable[str] | None = None,
                 log: Callable[[str], None] | None = None,
                 progress: Callable[[int], None] | None = None,
                 canceled: Callable[[], bool] | None = None) -> dict[str, Any]:
    from wechat_uia import UiaUnavailable

    say = log or (lambda _message: None)
    step = progress or (lambda _done: None)
    canceled = canceled or (lambda: False)
    contacts = list(contacts)
    wanted = {str(item) for item in only_ids} if only_ids else None
    records = [item for item in label_records(store, account) if item.get("present", True)]
    targets = [item for item in records if not wanted or str(item.get("id")) in wanted]
    result = {"ok": True, "synced": 0, "partial": 0, "failed": 0, "items": []}

    try:
        rows = reader.list_labels()
    except UiaUnavailable as exc:
        for record in targets:
            record["sync_error"] = str(exc)
            record["error_code"] = exc.code
            recompute_status(record)
        return {"ok": False, "code": exc.code, "msg": str(exc), "detail": exc.detail,
                "synced": 0, "partial": 0, "failed": len(targets), "items": []}

    by_name: dict[str, list[Any]] = {}
    for row in rows:
        by_name.setdefault(str(getattr(row, "name", "") or ""), []).append(row)

    processed = 0
    for record in targets:
        if canceled():
            result["ok"] = False
            result["msg"] = "已取消"
            break
        processed += 1
        step(processed)
        name = str(record.get("name") or "")
        candidates = by_name.get(name) or []
        if len(candidates) > 1:
            record["sync_error"] = f"微信里有 {len(candidates)} 个同名标签，无法确定成员归属"
            record["error_code"] = "duplicate_label"
            recompute_status(record)
            result["failed"] += 1
            result["items"].append({"id": label_group_id(record["id"]), "ok": False,
                                    "msg": record["sync_error"]})
            say(record["sync_error"])
            continue
        if not candidates:
            record["sync_error"] = "微信界面里没有找到这个标签"
            record["error_code"] = "no_label"
            recompute_status(record)
            result["failed"] += 1
            result["items"].append({"id": label_group_id(record["id"]), "ok": False,
                                    "msg": record["sync_error"]})
            continue
        expected = int(getattr(candidates[0], "count", 0) or 0)
        count_known = bool(getattr(candidates[0], "has_count", True))
        say(f"读取标签「{name}」（微信显示 {expected if count_known else '未知'} 人）…")
        try:
            read = reader.read_members(name, expected, count_known=count_known)
        except UiaUnavailable as exc:
            record["sync_error"] = str(exc)
            record["error_code"] = exc.code
            recompute_status(record)
            result["failed"] += 1
            result["items"].append({"id": label_group_id(record["id"]), "ok": False,
                                    "msg": str(exc)})
            say(f"「{name}」读取失败：{exc}")
            continue

        rows_text = list(getattr(read, "rows", []) or [])
        match = resolve_members(rows_text, contacts, label_names=[name])
        complete = bool(getattr(read, "complete", False)) and not match.unmatched \
            and not match.ambiguous
        if is_imported(record):
            refresh_remote(record, match.members,
                           displayed_total=getattr(read, "displayed_total", expected) or expected,
                           read_total=len(rows_text), complete=complete)
            record["unmatched"] = match.unmatched
            record["ambiguous"] = match.ambiguous
            record["duplicates"] = match.duplicates
            record["read_method"] = "uia"
        else:
            import_members(record, match.members, source="uia",
                           displayed_total=getattr(read, "displayed_total", expected) or expected,
                           read_total=len(rows_text), complete=complete)
        if not complete:
            record["sync_error"] = ""
        if record["status"] == STATUS_PARTIAL:
            result["partial"] += 1
        else:
            result["synced"] += 1
        result["items"].append({
            "id": label_group_id(record["id"]),
            "ok": record["status"] == STATUS_OK,
            "status": record["status"],
            "msg": _summary_line(record),
        })
        say(_summary_line(record))
    return result


def probe(account_dir: str | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="检查微信标签读取能力（只读）")
    parser.add_argument("account_dir", nargs="?", default=account_dir,
                        help="解密后的账号目录（含 contact.db）")
    args = parser.parse_args()
    if not args.account_dir:
        parser.error("请提供解密后的账号目录")
    definitions = read_label_definitions(Path(args.account_dir))
    print(json.dumps({
        "available": definitions.available,
        "reason": definitions.reason,
        "membership": definitions.membership,
        "labels": definitions.labels,
        "db_members": {k: len(v) for k, v in definitions.members.items()},
        "schema": definitions.schema,
    }, ensure_ascii=False, indent=2))
    return 0 if definitions.available else 2


if __name__ == "__main__":
    raise SystemExit(probe())
