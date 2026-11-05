
from __future__ import annotations

import base64
import hashlib
import html as _html
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .logging_config import get_logger
from .wechat_emoji import emojify_wechat_shortcodes

logger = get_logger(__name__)

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
MAX_FIELD_BYTES = 4 * 1024 * 1024
MAX_XML_BYTES = 1024 * 1024
MAX_XML_NODES = 6000
MAX_FORWARD_ITEMS = 200
MAX_FORWARD_DEPTH = 3
DEFAULT_LIMIT = 60
MAX_LIMIT = 1000

# contact.flag 的位含义（微信 4.x）：bit11 置顶，bit28 折叠。
CONTACT_FLAG_PINNED = 1 << 11
CONTACT_FLAG_FOLDED = 1 << 28

SKIP_SESSIONS = frozenset({
    "@placeholder_foldgroup", "brandsessionholder", "fmessage", "medianote",
    "floatbottle", "qqmail", "weixin", "notifymessage", "weixinreminder",
})

# 系统账号的 contact.flag 也带置顶位，但微信侧栏不把它们排进置顶区
PIN_EXEMPT = frozenset({"filehelper", "fmessage", "medianote", "floatbottle",
                        "qqmail", "weixin", "notifymessage", "weixinreminder"})

BASE_KINDS = {
    1: "text", 3: "image", 34: "voice", 37: "card", 42: "card", 43: "video",
    47: "sticker", 48: "location", 49: "app", 50: "call", 51: "status",
    62: "video", 10000: "system", 10002: "system", 11000: "sticker",
    11001: "quote", 11002: "file",
}

APP_KINDS = {
    1: "text", 2: "image", 3: "music", 4: "video", 5: "link", 6: "file",
    7: "forward", 8: "sticker", 9: "miniprogram", 10: "channel", 11: "live",
    13: "card", 14: "forward", 15: "location", 16: "music", 17: "location",
    19: "forward", 20: "forward", 21: "card", 24: "note", 33: "miniprogram",
    36: "miniprogram", 44: "redpacket", 48: "channel", 51: "live",
    53: "solitaire", 57: "quote", 62: "channel", 63: "live", 87: "announce",
    88: "live", 2000: "transfer", 2001: "redpacket", 2003: "redpacket",
}

KIND_LABELS = {
    "text": "文字", "image": "图片", "voice": "语音", "video": "视频",
    "sticker": "表情", "file": "文件", "link": "链接", "quote": "引用",
    "forward": "聊天记录", "card": "名片", "location": "位置", "system": "系统消息",
    "call": "音视频通话", "redpacket": "红包", "transfer": "转账",
    "miniprogram": "小程序", "channel": "视频号", "live": "直播", "note": "笔记",
    "solitaire": "接龙", "announce": "群公告", "music": "音乐", "app": "应用消息",
    "other": "暂不支持",
}

KIND_PLACEHOLDERS = {
    "image": "[图片]", "voice": "[语音]", "video": "[视频]", "sticker": "[表情]",
    "file": "[文件]", "link": "[链接]", "quote": "[引用]", "forward": "[聊天记录]",
    "card": "[名片]", "location": "[位置]", "system": "[系统消息]", "call": "[音视频通话]",
    "redpacket": "[红包]", "transfer": "[转账]", "miniprogram": "[小程序]",
    "channel": "[视频号]", "live": "[直播]", "note": "[笔记]", "solitaire": "[接龙]",
    "announce": "[群公告]", "music": "[音乐]", "app": "[应用消息]", "other": "[暂不支持]",
}


class ChatSchemaError(RuntimeError):
    pass


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    conn.text_factory = bytes
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    try:
        return {_decode_bytes(r[0]) for r in conn.execute(
            "select name from sqlite_master where type='table'")}
    except sqlite3.Error:
        return set()


def _decode_bytes(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return str(raw)
    data = bytes(raw)
    if not data:
        return ""
    if len(data) > MAX_FIELD_BYTES:
        data = data[:MAX_FIELD_BYTES]
    if data[:4] == ZSTD_MAGIC:
        try:
            import zstandard as zstd

            data = zstd.ZstdDecompressor().decompressobj().decompress(data)
        except Exception:
            return ""
    for encoding in ("utf-8", "utf-16-le", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return ""
    return text.replace("\x00", "").strip()


def _xml_root(text: str) -> ET.Element | None:
    if not text:
        return None
    start = text.find("<")
    if start < 0:
        return None
    body = text[start:start + MAX_XML_BYTES]
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        body = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", body)
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return None
    if sum(1 for _ in root.iter()) > MAX_XML_NODES:
        return None
    return root


def _node_text(root: ET.Element | None, path: str) -> str:
    if root is None or not path:
        return ""
    for candidate in (path, f".//{path.lstrip('./')}"):
        try:
            node = root.find(candidate)
        except SyntaxError:
            return ""
        if node is not None and node.text:
            return node.text.strip()
    return ""


def _node(root: ET.Element | None, path: str) -> ET.Element | None:
    if root is None or not path:
        return None
    for candidate in (path, f".//{path.lstrip('./')}"):
        try:
            node = root.find(candidate)
        except SyntaxError:
            return None
        if node is not None:
            return node
    return None


def _attr(root: ET.Element | None, path: str, name: str) -> str:
    node = _node(root, path)
    if node is None:
        return ""
    return str(node.get(name) or "").strip()


def _strip_markup(value: str) -> str:
    """系统消息里的 replacemsg 可能内嵌一段简易富文本，取出纯文字。"""
    text = str(value or "")
    if not text:
        return ""
    if "<" in text:
        text = re.sub(r"(?is)<br\s*/?>", "\n", text)
        text = re.sub(r"(?is)</(?:p|div|li)>", "\n", text)
        text = re.sub(r"(?is)<[^>]{0,400}>", "", text)
    text = _html.unescape(text)
    return re.sub(r"[ \t\u00a0]{2,}", " ", text).strip()


_MACHINE_NAME = re.compile(r"^(?:wxid_[A-Za-z0-9_\-]+|gh_[A-Za-z0-9_\-]+|\d+@chatroom)\s*[:：]?$")


def _clean_fragment(value: str) -> str:
    """引用 / 转述里的片段可能是一整段 XML（表情、图文、小程序），只取可读文字。"""
    text = str(value or "").strip()
    if not text:
        return ""
    start = text.find("<")
    if start < 0:
        return text
    prefix = _strip_markup(text[:start]).strip().rstrip(":：").strip()
    root = _xml_root(text[start:])
    body = ""
    if root is not None:
        tags = {node.tag for node in root.iter()}
        if "emoji" in tags:
            body = "[动画表情]"
        elif "img" in tags:
            body = "[图片]"
        elif "videomsg" in tags:
            body = "[视频]"
        elif "voicemsg" in tags:
            body = "[语音]"
        else:
            for path in ("appmsg/title", "appmsg/descr", "title", "descr", "content"):
                found = _node_text(root, path)
                if found:
                    body = _strip_markup(found)
                    break
            if not body:
                body = _strip_markup(" ".join(root.itertext()))
    if not body:
        body = _xml_fragment_text(text[start:])
    if prefix and not _MACHINE_NAME.match(prefix):
        return f"{prefix}：{body}" if body else prefix
    return body


def _xml_fragment_text(fragment: str) -> str:
    """XML 解析失败（被截断 / 属性过长）时的兜底：按标签名判断类型，再取标题或去标签。"""
    lowered = fragment[:4000]
    if "<emoji" in lowered:
        return "[动画表情]"
    if "<img" in lowered:
        return "[图片]"
    if "<videomsg" in lowered:
        return "[视频]"
    if "<voicemsg" in lowered:
        return "[语音]"
    title = re.search(r"(?is)<title>(.*?)</title>", lowered)
    if title:
        return _strip_markup(title.group(1))
    descr = re.search(r"(?is)<descr>(.*?)</descr>", lowered)
    if descr:
        return _strip_markup(descr.group(1))
    text = re.sub(r"(?is)<[^<>\n]{0,4000}>", "", fragment)
    return _strip_markup(text)


def _pat_text(text: str, names: dict[str, str]) -> str:
    """模板里的 ${wxid} / $username$ 之类占位符替换成显示名。"""
    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return names.get(key) or names.get("sender") or ""

    text = re.sub(r"\$\{([A-Za-z0-9_]+)\}", repl, text)
    return re.sub(r"\$([A-Za-z0-9_]+)\$", repl, text)


# 微信 4.x 的拍一拍文案在中文本地化没生效时会原样落成英文模板：
#   「张三 tickled 李四's 猫爪」「I  tickled 王五」「张三 tickled me  此功能暂时无法使用」
# 这里统一还原成微信侧栏的中文说法，并丢掉模板拼接带出来的无关句子。
_PAT_TICKLE = re.compile(r"^(?P<actor>.*?)\s+tickled\s+(?P<rest>.+)$", re.I)
_PAT_NOISE = ("撤回了一条消息", "此功能暂时无法使用", "拍了拍", "tickled")


def _pat_suffix(value: str) -> str:
    suffix = (value or "").strip()
    if not suffix or len(suffix) > 24:
        return ""
    if any(word in suffix for word in _PAT_NOISE):
        return ""
    if suffix[0] in "，,。.；;：:！!？?、":
        return ""
    return suffix


def _pat_chinese(text: str, sender: str) -> str:
    """中文模板：「X 拍了拍 Y 的 xxx」，顺带修掉 me / my 和模板噪声。"""
    match = re.match(r"^(?P<actor>.{0,80}?)拍了拍(?P<rest>.+)$", text)
    if not match:
        return ""
    actor = match.group("actor").strip() or sender
    rest = match.group("rest").strip()
    target, suffix = rest, ""
    if rest.startswith('"'):
        end = rest.find('"', 1)
        if end > 0:
            target, suffix = rest[: end + 1], rest[end + 1 :].strip()
    elif re.search(r"\s{2,}", rest):
        target, suffix = re.split(r"\s{2,}", rest, maxsplit=1)
    elif re.match(r"^(?:me|my)\b", rest, re.I):
        parts = rest.split(None, 1)
        target = "我"
        suffix = parts[1] if len(parts) > 1 else ""
    target = target.strip()
    if target.lower() in ("me", "my"):
        target = "我"
    if not target:
        return ""
    if suffix and any(word in suffix for word in ("撤回了一条消息", "此功能暂时无法使用")):
        suffix = ""
    if target == actor == "我":
        target = "自己"
    tail = f" {suffix}" if suffix else ""
    return emojify_wechat_shortcodes(f"{actor}拍了拍{target}{tail}")


def pat_text(text: str, sender: str = "") -> str:
    """把拍一拍还原成「A 拍了拍 B 的 xxx」；不是拍一拍返回空串。"""
    raw = _strip_markup(text)
    # 模板里的多段 replacemsg 会被拼进其它系统句（「…撤回了一条消息」），先剔掉
    for noise in ("撤回了一条消息", "此功能暂时无法使用"):
        raw = raw.replace(noise, " ")
    # 多行也压成一行，正则才好从整段里取主语和目标
    raw = re.sub(r"[ \t\r\n]+", " ", raw).strip()
    if not raw:
        return ""
    match = _PAT_TICKLE.match(raw)
    if match:
        actor = (match.group("actor") or "").strip()
        rest = (match.group("rest") or "").strip()
        suffix = ""
        if "'s" in rest:
            target, suffix = rest.split("'s", 1)
        elif re.search(r"\s{2,}", rest):
            target, suffix = re.split(r"\s{2,}", rest, maxsplit=1)
        elif re.match(r"^(?:me|my)\b", rest, re.I):
            parts = rest.split(None, 1)
            target = "me"
            suffix = parts[1] if len(parts) > 1 else ""
        else:
            target = rest
        target = target.strip()
        if not target:
            return ""
        if actor.lower() in ("i", "i'm"):
            actor = "我"
        if target.lower() in ("me", "my"):
            target = "我"
        suffix = _pat_suffix(suffix)
        if target == actor == "我":
            target = "自己"
        if suffix and not suffix.startswith("的"):
            suffix = f"的{suffix}"
        return emojify_wechat_shortcodes(f"{actor or sender}拍了拍{target}{suffix}")
    if "拍了拍" in raw:
        return _pat_chinese(raw, sender)
    return ""


_SYSTEM_EN_TEMPLATES: tuple[tuple[str, str], ...] = (
    (r"^You recalled a message\.?$", "你撤回了一条消息"),
    (r"^(?P<n>.+?) recalled a message\.?$", r"\g<n> 撤回了一条消息"),
    (r"^You joined the group chat\.?$", "你加入了群聊"),
    (r"^(?P<n>.+?) joined the group chat\.?$", r"\g<n> 加入了群聊"),
    (r"^(?P<n>.+?) left the group chat\.?$", r"\g<n> 退出了群聊"),
    (r"^(?P<a>.+?) joined the group chat via the QR Code shared by (?P<b>.+?)\.?$",
     r"\g<a> 通过 \g<b> 分享的二维码加入了群聊"),
    (r"^(?P<a>.+?) joined the group chat via (?P<b>.+?)\.?$",
     r"\g<a> 通过 \g<b> 加入了群聊"),
    (r"^(?P<a>.+?) invited (?P<b>.+?) to the group chat$", r"\g<a> 邀请 \g<b> 加入了群聊"),
    (r"^(?P<a>.+?) changed the group name to (?P<b>.+)$", r"\g<a> 修改群名为 \g<b>"),
    (r"^Group owner has set you as an admin\.?$", "群主已设置你为管理员"),
    (r"^(?P<n>.+?) is now the group manager\.?$", r"\g<n> 成为了群管理员"),
    (r"^You have added (?P<n>.+?) as your Weixin contact\. Start chatting!$",
     r"你已添加 \g<n> 为朋友，开始聊天吧"),
    (r"^You've added (?P<n>.+?)\.?\s*Greetings are shown above\.?$",
     r"你已添加 \g<n> 为朋友"),
    (r"^(?P<n>.+?) joined group chat via the QR code you shared\.?$",
     r"\g<n> 通过你分享的二维码加入了群聊"),
    (r"^Welcome back to the group chat\.?$", "欢迎回到群聊"),
    (r"^Greetings shown above\.?$", "问候语见上方"),
    (r"^Real-time Location session ended\.?$", "位置共享已结束"),
    (r"^Red packet has been sent\. View on phone\.?$", "红包已发出，请在手机上查看"),
    (r"^Opened Red Packet from (?P<n>.+?)\.?$", r"已领取 \g<n> 的红包"),
    (r"^You are now the group manager\.?$", "你成为了群管理员"),
    (r"^You invited (?P<n>.+?) to the group chat\.?.*$", r"你邀请 \g<n> 加入了群聊"),
    (r"^(?P<n>.+?) added as group admin\.?$", r"\g<n> 被添加为群管理员"),
    (r"^You've created a group chat\.?.*$", "你创建了群聊"),
    (r"^(?P<n>.+?) has been removed from the group admins\.?$", r"\g<n> 被移出了群管理员"),
    (r"^(?P<n>.+?) has been added as a group admin\.?$", r"\g<n> 被设置为群管理员"),
    (r'^Note: (?P<n>.+?) is not friends with anyone else in this group chat\.?$',
     r"\g<n> 不是你的朋友，也和群里其他人不是朋友关系"),
    (r"^Message from Split Bill\. View on phone\.$", "群收款消息，请在手机上查看"),
    (r"^Red packet received\. View on phone\.$", "收到红包，请在手机上查看"),
    (r"^(?P<n>.+?) sent a red packet\.?$", r"\g<n> 发了一个红包"),
    (r"^(?P<n>.+?) sent you a friend request\.?$", r"\g<n> 请求添加你为朋友"),
)


def _translate_system(text: str) -> str:
    """英文系统提示（撤回 / 入群 / 管理员变更）转成中文说法。"""
    value = (text or "").strip()
    if not value:
        return value
    # 群公告 / 群接龙偶尔会把结构化字段直接落成一行（房间号 类型 id ... wxid）
    if re.match(r"^\d+@chatroom\s+\d", value):
        return "群公告更新"
    if not re.search(r"[A-Za-z]{3,}", value):
        return value
    for pattern, repl in _SYSTEM_EN_TEMPLATES:
        match = re.match(pattern, value, re.I)
        if match:
            try:
                return match.expand(repl)
            except re.error:
                return value
    return value


def system_text(root: ET.Element | None, content: str, sender: str = "") -> str:
    """把 10000/10002 的系统消息转成可读中文，绝不把 XML 原样抛给前端。"""
    def finish(text: str) -> str:
        # 模板拼接出来的文本常带其它系统句（「…撤回了一条消息」），再洗一遍
        return pat_text(text, sender) or emojify_wechat_shortcodes(_translate_system(text))

    if root is None:
        return finish(_strip_markup(content) or "[系统消息]")
    names = {"sender": sender, "wxid": sender}
    for path in ("replacemsg", "content", "template", "pat/template", "delchatroommember/text"):
        node = _node(root, path)
        if node is not None:
            raw = "".join(node.itertext())
            text = _strip_markup(_pat_text(raw, names))
            if text:
                return finish(text)
    parts = [piece.strip() for piece in root.itertext() if piece and piece.strip()]
    text = _strip_markup(" ".join(parts))
    if text:
        return finish(text[:280])
    kind = str(root.get("type") or "").strip()
    fallback = {
        "revokemsg": "对方撤回了一条消息",
        "pat": "拍了拍",
        "delchatroommember": "群成员变动",
        "roomtoolstips": "群公告更新",
    }.get(kind, "")
    return fallback or "[系统消息]"


def _join(*values: str, sep: str = " · ") -> str:
    return sep.join(item for item in values if item)


def normalize_type(raw: Any) -> tuple[int, int]:
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return (0, 0)
    if value <= 0xFFFF:
        return (value, 0)
    return (value & 0xFFFFFFFF, value >> 32)


def message_table_for(username: str) -> str:
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def encode_message_id(shard: int, local_id: int) -> str:
    return base64.urlsafe_b64encode(f"{shard}:{local_id}".encode()).decode().rstrip("=")


def encode_cursor(positions: dict[int, tuple[int, int]]) -> str:
    """游标按分片记录各自的位置，避免多库合并分页时跳过数据。"""
    payload = json.dumps(
        {"v": 2, "p": {str(k): [v[0], v[1]] for k, v in sorted(positions.items())}},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(value: str) -> dict[int, tuple[int, int]] | None:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except Exception:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("p"), dict):
            return {int(k): (int(v[0]), int(v[1])) for k, v in data["p"].items()}
    except Exception:
        pass
    try:  # 兼容旧版单一游标
        create_time, local_id, shard = raw.split(":", 2)
        return {int(shard): (int(create_time), int(local_id))}
    except Exception:
        return None


def _strip_group_prefix(text: str, session: str, sender_known: bool) -> tuple[str, str | None]:
    if not session.endswith("@chatroom") or "\n" not in text[:160]:
        return text, None
    head, rest = text.split("\n", 1)
    head = head.strip()
    if not head.endswith(":"):
        return text, None
    candidate = head[:-1].strip()
    if not re.fullmatch(r"[A-Za-z0-9_@][A-Za-z0-9_.@-]{0,190}", candidate):
        return text, None
    if sender_known or candidate.startswith(("wxid_", "gh_")) or candidate.endswith("@chatroom"):
        return rest, candidate
    return text, None


class ChatStore:
    def __init__(self, root: str | Path, self_wxid: str = "", account: str = ""):
        self.root = Path(root).expanduser().resolve()
        self.self_wxid = (self_wxid or account or "").strip()
        self.account = (account or self.self_wxid).strip()
        self.warnings: list[str] = []
        self.capabilities: list[str] = []
        self.session_db = self._locate("session.db")
        self.contact_db = self._locate("contact.db")
        self.message_dbs = self._locate_message_dbs()
        self.media_dbs = self._locate_media_dbs()
        self._conns: dict[Path, sqlite3.Connection] = {}
        self._tables: dict[Path, set[str]] = {}
        self._name2id: dict[Path, dict[int, str]] = {}
        self._contacts: dict[str, dict[str, str]] = {}
        self._pinned: set[str] = set()
        self._folded: set[str] = set()
        self._sessions: list[dict[str, Any]] | None = None
        self._ranges: dict[str, tuple[int, int]] = {}
        self._validate()
        self._load_contacts()

    def _locate(self, name: str) -> Path | None:
        direct = self.root / name
        if direct.is_file():
            return direct
        for path in sorted(self.root.rglob(name)):
            if path.is_file():
                return path
        return None

    def _locate_message_dbs(self) -> list[Path]:
        found: list[Path] = []
        for path in sorted(self.root.rglob("*.db")):
            if re.fullmatch(r"(biz_)?message_\d*\.db", path.name, re.IGNORECASE):
                found.append(path)
        return found

    def _locate_media_dbs(self) -> list[Path]:
        found: list[Path] = []
        for path in sorted(self.root.rglob("*.db")):
            if re.fullmatch(r"media_\d*\.db", path.name, re.IGNORECASE):
                found.append(path)
        return found

    def session_range(self, conversation_id: str) -> tuple[int, int]:
        """会话内消息的时间跨度，用于会话列表的日期筛选。"""
        if conversation_id in self._ranges:
            return self._ranges[conversation_id]
        table = message_table_for(conversation_id)
        low = high = 0
        for path in self.message_dbs:
            if table not in self.tables(path):
                continue
            conn = self._conn(path)
            if conn is None:
                continue
            try:
                row = conn.execute(
                    f'select min(create_time), max(create_time) from "{table}"').fetchone()
            except sqlite3.Error:
                continue
            if row is None or row[0] is None:
                continue
            first, last = int(row[0] or 0), int(row[1] or 0)
            low = first if not low else min(low, first)
            high = max(high, last)
        self._ranges[conversation_id] = (low, high)
        return (low, high)

    def has_messages_between(self, conversation_id: str, start: int | None,
                             end: int | None) -> bool:
        if start is None and end is None:
            return True
        low, high = self.session_range(conversation_id)
        if not high:
            return False
        if start is not None and high < int(start):
            return False
        if end is not None and low > int(end):
            return False
        table = message_table_for(conversation_id)
        clauses = ["1=1"]
        params: list[Any] = []
        if start is not None:
            clauses.append("create_time >= ?")
            params.append(int(start))
        if end is not None:
            clauses.append("create_time <= ?")
            params.append(int(end))
        where = " and ".join(clauses)
        for path in self.message_dbs:
            if table not in self.tables(path):
                continue
            conn = self._conn(path)
            if conn is None:
                continue
            try:
                row = conn.execute(
                    f'select 1 from "{table}" where {where} limit 1', params).fetchone()
            except sqlite3.Error:
                continue
            if row is not None:
                return True
        return False

    def _conn(self, path: Path | None) -> sqlite3.Connection | None:
        if path is None:
            return None
        if path not in self._conns:
            try:
                self._conns[path] = _connect(path)
            except sqlite3.Error as exc:
                logger.warning("%s", exc)
                self._conns[path] = None  # type: ignore[assignment]
        return self._conns[path]

    def tables(self, path: Path) -> set[str]:
        if path not in self._tables:
            conn = self._conn(path)
            self._tables[path] = _table_names(conn) if conn else set()
        return self._tables[path]

    def _validate(self) -> None:
        if self.session_db is None:
            raise ChatSchemaError("没有找到会话数据库 session.db")
        if "SessionTable" not in self.tables(self.session_db):
            raise ChatSchemaError("当前会话数据库结构未适配")
        shards = [p for p in self.message_dbs if any(
            name.startswith("Msg_") for name in self.tables(p))]
        if not shards:
            raise ChatSchemaError("当前消息数据库结构未适配")
        self.message_dbs = shards
        self.capabilities = ["sessions", "messages", "text", "export"]
        if self.contact_db is not None:
            self.capabilities.append("contacts")

    def _load_contacts(self) -> None:
        conn = self._conn(self.contact_db)
        if conn is None:
            return
        if "contact" not in self.tables(self.contact_db):
            return
        cols = {_decode_bytes(r[1]) for r in conn.execute("pragma table_info(contact)")}
        picked = ", ".join(
            name for name in ("username", "remark", "nick_name", "alias", "flag")
            if name in cols)
        if not picked:
            return
        try:
            rows = conn.execute(f"select {picked} from contact")
        except sqlite3.Error:
            return
        for row in rows:
            username = _decode_bytes(row["username"])
            if not username:
                continue
            try:
                remark = _decode_bytes(row["remark"])
                nickname = _decode_bytes(row["nick_name"])
                alias = _decode_bytes(row["alias"])
            except (IndexError, KeyError):
                remark = nickname = alias = ""
            self._contacts[username] = {
                "remark": remark,
                "nickname": nickname,
                "alias": alias,
            }
            try:
                flag = int(row["flag"] or 0)
            except (IndexError, KeyError, TypeError, ValueError):
                continue
            if flag & CONTACT_FLAG_PINNED:
                self._pinned.add(username)
            if flag & CONTACT_FLAG_FOLDED:
                self._folded.add(username)

    def display_name(self, username: str) -> str:
        if not username:
            return ""
        if username == self.self_wxid:
            return "我"
        item = self._contacts.get(username) or {}
        return item.get("remark") or item.get("nickname") or item.get("alias") or username

    def _session_rows(self) -> list[dict[str, Any]]:
        conn = self._conn(self.session_db)
        if conn is None:
            return []
        cols = {_decode_bytes(r[1]) for r in conn.execute("pragma table_info(SessionTable)")}
        if "username" not in cols:
            return []
        order = "sort_timestamp" if "sort_timestamp" in cols else (
            "last_timestamp" if "last_timestamp" in cols else "rowid")
        picked = [name for name in (
            "username", "unread_count", "summary", "last_timestamp", "sort_timestamp",
            "last_msg_sender", "last_msg_type") if name in cols]
        sql = f'select {", ".join(picked)} from SessionTable order by {order} desc'
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error as exc:
            logger.warning("%s", exc)
            return []
        return [dict(row) for row in rows]

    def _message_present(self, table: str, paths: list[Path]) -> bool:
        """表存在不等于有消息：空表（已清空 / 只剩会话壳）不该出现在列表里。"""
        for path in paths:
            conn = self._conn(path)
            if conn is None:
                continue
            try:
                if conn.execute(f'select 1 from "{table}" limit 1').fetchone():
                    return True
            except sqlite3.Error:
                continue
        return False

    def sessions(self) -> list[dict[str, Any]]:
        if self._sessions is not None:
            return self._sessions
        tables_by_shard = {path: self.tables(path) for path in self.message_dbs}
        out: list[dict[str, Any]] = []
        for row in self._session_rows():
            username = _decode_bytes(row.get("username")).strip()
            if not username or username.lower() in SKIP_SESSIONS:
                continue
            if username == self.self_wxid:
                continue
            table = message_table_for(username)
            paths = [path for path in self.message_dbs if table in tables_by_shard[path]]
            if not paths or not self._message_present(table, paths):
                continue  # 没有可导出的消息，直接不进列表
            if username.endswith("@chatroom"):
                kind = "group"
            elif username.startswith("gh_") or username.endswith("@openim"):
                # 公众号会话只有文章推送，聊天记录导出对它们没有意义，直接跳过
                continue
            else:
                kind = "private"
            display = self.display_name(username)
            if kind == "group" and display == username:
                display = f"{username.replace('@chatroom', '')} 群聊"
            last_time = int(row.get("last_timestamp") or row.get("sort_timestamp") or 0)
            sort_time = int(row.get("sort_timestamp") or row.get("last_timestamp") or 0)
            out.append({
                "conversation_id": username,
                "display": display,
                "kind": kind,
                "shards": len(paths),
                # 置顶标记在 contact.flag 的 bit11，会话表里没有专门列；
                # 文件传输助手等系统账号同样带这一位，但微信侧栏不把它们放进置顶区
                "pinned": username in self._pinned and username not in PIN_EXEMPT,
                # 微信里「折叠该群聊」= contact.flag 的 bit28（实测 53 个命中全是群）
                "folded": username in self._folded,
                "unread": int(row.get("unread_count") or 0),
                "last_time": last_time,
                "sort_time": sort_time,
                "last_summary": _decode_bytes(row.get("summary"))[:120],
                "last_sender": _decode_bytes(row.get("last_msg_sender")),
            })
        # 与微信侧栏一致：置顶组在前，组内按最后消息时间倒序；非置顶同样按时间倒序
        out.sort(key=lambda item: (item["pinned"], item["last_time"], item["sort_time"]),
                 reverse=True)
        self._sessions = out
        if not self.message_dbs:
            self.warnings.append("没有找到消息分片")
        return out

    def session(self, conversation_id: str) -> dict[str, Any] | None:
        for item in self.sessions():
            if item["conversation_id"] == conversation_id:
                return item
        return None

    def _name_map(self, path: Path) -> dict[int, str]:
        if path in self._name2id:
            return self._name2id[path]
        mapping: dict[int, str] = {}
        conn = self._conn(path)
        if conn is not None and "Name2Id" in self.tables(path):
            try:
                for row in conn.execute("select rowid, user_name from Name2Id"):
                    mapping[int(row[0])] = _decode_bytes(row[1])
            except sqlite3.Error as exc:
                logger.warning("%s", exc)
        self._name2id[path] = mapping
        return mapping

    def _shard_rows(self, path: Path, table: str, start: int | None, end: int | None,
                    cursor: tuple[int, int] | None, limit: int
                    ) -> list[sqlite3.Row]:
        conn = self._conn(path)
        if conn is None:
            return []
        clauses = ["1=1"]
        params: list[Any] = []
        if start is not None:
            clauses.append("create_time >= ?")
            params.append(int(start))
        if end is not None:
            clauses.append("create_time <= ?")
            params.append(int(end))
        if cursor is not None:
            cutoff, local_id = cursor
            clauses.append("(create_time < ? or (create_time = ? and local_id < ?))")
            params.extend([cutoff, cutoff, local_id])
        params.append(int(limit))
        sql = (
            f'select local_id, server_id, local_type, sort_seq, real_sender_id, '
            f'create_time, message_content, compress_content from "{table}" '
            f'where {" and ".join(clauses)} '
            f'order by create_time desc, local_id desc limit ?'
        )
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            logger.warning("%s", exc)
            return []

    def messages(self, conversation_id: str, *, cursor: str | None = None,
                 limit: int = DEFAULT_LIMIT, start: int | None = None,
                 end: int | None = None, kind: str = "all", keyword: str = "",
                 scan_budget: int = 6000) -> dict[str, Any]:
        if not conversation_id:
            raise ValueError("缺少会话标识")
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        table = message_table_for(conversation_id)
        positions = decode_cursor(cursor) if cursor else {}
        if cursor and positions is None:
            raise ValueError("游标无效，请重新加载")
        keyword = (keyword or "").strip().casefold()[:80]
        wanted_kind = (kind or "all").strip()
        results: list[dict[str, Any]] = []
        scanned = 0
        truncated = False
        exhausted = False
        while len(results) < limit and scanned < scan_budget:
            # 每个分片都取满 limit 行再归并，保证本页截断点之前的行都已取到；
            # 若只取 400 行，某些库会「只露出更深的一段」，跨库合并时顺序会乱。
            want = max(1, min(limit, scan_budget - scanned))
            merged: list[tuple[tuple[int, int, int], int, Path, sqlite3.Row]] = []
            per_shard_full = False
            for shard, path in enumerate(self.message_dbs):
                if table not in self.tables(path):
                    continue
                rows = self._shard_rows(path, table, start, end, positions.get(shard), want)
                if len(rows) >= want:
                    per_shard_full = True
                for row in rows:
                    key = (int(row["create_time"] or 0), int(row["local_id"] or 0), shard)
                    merged.append((key, shard, path, row))
            if not merged:
                exhausted = True
                break
            merged.sort(key=lambda item: item[0], reverse=True)
            reached = False
            for key, shard, path, row in merged:
                scanned += 1
                # 每个分片各自推进游标（多库合并时单一游标会漏数据）
                positions[shard] = (key[0], key[1])
                message = self._build_message(row, self._name_map(path), conversation_id, shard)
                if wanted_kind not in ("", "all") and message["kind"] != wanted_kind:
                    continue
                if keyword:
                    haystack = f"{message['text']}\n{message['title']}".casefold()
                    if keyword not in haystack:
                        continue
                results.append(message)
                if len(results) >= limit:
                    reached = True
                    break
            if reached:
                break
            if not per_shard_full:
                exhausted = True
                break
        if not exhausted and len(results) < limit:
            truncated = True
        has_more = (not exhausted) and bool(positions)
        next_cursor = encode_cursor(positions) if has_more else None
        return {
            "messages": results,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "scanned": scanned,
            "truncated": truncated,
            "conversation_id": conversation_id,
        }

    def _build_message(self, row: sqlite3.Row, name_map: dict[int, str],
                       conversation_id: str, shard: int) -> dict[str, Any]:
        create_time = int(row["create_time"] or 0)
        local_id = int(row["local_id"] or 0)
        raw_type = int(row["local_type"] or 0)
        base, sub = normalize_type(raw_type)
        content = _decode_bytes(row["message_content"]) or _decode_bytes(row["compress_content"])
        sender_id = name_map.get(int(row["real_sender_id"] or 0), "")
        content, head_sender = _strip_group_prefix(content, conversation_id, bool(sender_id))
        if not sender_id and head_sender:
            sender_id = head_sender
        message = {
            "message_id": encode_message_id(shard, local_id),
            "conversation_id": conversation_id,
            "server_id": str(row["server_id"] or ""),
            "create_time": create_time,
            "sender_id": sender_id,
            "sender_display": self.display_name(sender_id) if sender_id else "系统",
            "is_self": bool(sender_id) and sender_id == self.self_wxid,
            "raw_type": raw_type,
            "sub_type": sub,
            "kind": "text",
            "label": "",
            "text": content,
            "title": "",
            "extra": {},
            "parse_status": "parsed",
        }
        self._describe(message, base, sub, content)
        message["label"] = KIND_LABELS.get(message["kind"], KIND_LABELS["other"])
        message["summary"] = message_summary(message)
        return message

    def _describe(self, message: dict[str, Any], base: int, sub: int, content: str) -> None:
        extra = message["extra"]
        message["kind"] = BASE_KINDS.get(base, "other")
        if base == 1:
            message["kind"] = "text"
            message["text"] = emojify_wechat_shortcodes(content)
            return
        if base in (10000, 10002):
            message["kind"] = "system"
            message["text"] = system_text(
                _xml_root(content), content, message.get("sender_display") or "")
            return
        if base == 3:
            message["kind"] = "image"
            message["text"] = ""
            root = _xml_root(content)
            extra.update({
                "md5": _attr(root, "img", "md5"),
                "size": _attr(root, "img", "length"),
                "width": _attr(root, "img", "cdnthumbwidth"),
                "height": _attr(root, "img", "cdnthumbheight"),
            })
            return
        if base in (47, 11000):
            message["kind"] = "sticker"
            root = _xml_root(content)
            extra.update({
                # 老表情常常只带 androidmd5，缺了它就彻底找不到原图
                "md5": _attr(root, "emoji", "md5") or _attr(root, "emoji", "androidmd5"),
                "product": _attr(root, "emoji", "productid"),
                "size": _attr(root, "emoji", "len") or _attr(root, "emoji", "androidlen"),
                # 表情缓存到本机的是加密副本，直接按消息里的 CDN 地址取原图更可靠
                "cdnurl": _attr(root, "emoji", "cdnurl"),
                "thumburl": _attr(root, "emoji", "thumburl"),
                "aeskey": _attr(root, "emoji", "aeskey"),
                "width": _attr(root, "emoji", "width"),
                "height": _attr(root, "emoji", "height"),
            })
            message["text"] = ""
            return
        if base == 34:
            message["kind"] = "voice"
            root = _xml_root(content)
            length = _attr(root, "voicemsg", "voicelength") or _attr(root, "voicemsg", "length")
            try:
                seconds = max(0, int(length) // 1000)
            except ValueError:
                seconds = 0
            extra.update({"duration": seconds, "length": length})
            message["text"] = f"{seconds}\"" if seconds else ""
            return
        if base in (43, 62):
            message["kind"] = "video"
            message["text"] = ""
            root = _xml_root(content)
            extra.update({
                "md5": _attr(root, "videomsg", "md5"),
                "size": _attr(root, "videomsg", "length"),
                "duration": _attr(root, "videomsg", "playlength"),
                "width": _attr(root, "videomsg", "cdnthumbwidth"),
                "height": _attr(root, "videomsg", "cdnthumbheight"),
            })
            return
        if base in (37, 42):
            message["kind"] = "card"
            root = _xml_root(content)
            nickname = _attr(root, "msg", "nickname")
            username = _node_text(root, "msg/username") or _attr(root, "msg", "username")
            message["text"] = nickname
            message["title"] = nickname
            extra.update({"nickname": nickname, "username": username})
            return
        if base == 48:
            message["kind"] = "location"
            root = _xml_root(content)
            label = _attr(root, "location", "label") or _attr(root, "location", "poiname")
            extra.update({
                "label": label,
                "poi": _attr(root, "location", "poiname"),
                "lat": _attr(root, "location", "x"),
                "lng": _attr(root, "location", "y"),
                "scale": _attr(root, "location", "scale"),
            })
            message["text"] = label
            return
        if base == 50:
            message["kind"] = "call"
            message["text"] = ""
            return
        if base == 51:
            message["kind"] = "system"
            message["text"] = system_text(_xml_root(content), content,
                                          message.get("sender_display") or "")
            return
        if base not in (49, 57, 11001):
            message["kind"] = BASE_KINDS.get(base, "other")
            message["text"] = _strip_markup(content)[:280] if content.startswith("<") else content[:280]
            return
        root = _xml_root(content)
        if root is None:
            message["kind"] = "other"
            message["text"] = _strip_markup(content)[:280]
            message["parse_status"] = "malformed"
            return
        app_type = _node_text(root, "appmsg/type") or _node_text(root, "type")
        try:
            app_type_int = int(app_type) if app_type else 0
        except ValueError:
            app_type_int = 0
        if base in (57, 11001) and not app_type_int:
            app_type_int = 57
        kind = APP_KINDS.get(app_type_int, "app")
        title = emojify_wechat_shortcodes(_node_text(root, "appmsg/title") or _node_text(root, "title"))
        descr = emojify_wechat_shortcodes(
            _node_text(root, "appmsg/descr") or _node_text(root, "appmsg/des")
            or _node_text(root, "descr") or _node_text(root, "des"))
        url = _node_text(root, "appmsg/url") or _node_text(root, "url")
        if not url and descr.startswith(("http://", "https://")):
            url, descr = descr, ""
        if "拍了拍" in title and not url:
            # 微信把「拍一拍」装在 appmsg/type=62 里，不是视频号内容
            message["kind"] = "system"
            message["text"] = (pat_text(title, message.get("sender_display") or "")
                               or emojify_wechat_shortcodes(title))
            message["title"] = ""
            return
        appname = _node_text(root, "appmsg/appinfo/appname") or _node_text(root, "appname")
        source = (_node_text(root, "appmsg/sourcedisplayname")
                  or _node_text(root, "sourcedisplayname"))
        source_id = (_node_text(root, "appmsg/sourceusername")
                     or _node_text(root, "sourceusername"))
        thumb = (_node_text(root, "appmsg/thumburl") or _attr(root, "appmsg", "thumburl"))
        song = _node_text(root, "appmsg/songalbumurl")
        if kind == "text" and title:
            message["kind"] = "text"
            message["text"] = title
            return
        message["kind"] = kind
        message["title"] = title
        if kind in ("link", "miniprogram", "channel", "music", "live"):
            message["text"] = title or descr
            extra.update({"descr": descr[:400], "thumb": thumb[:2048]})
            if kind == "music":
                extra["song_album"] = song[:2048]
        elif kind == "file":
            attach = _node(root, "appmsg/appattach")
            fname = _node_text(attach, "filename") or title
            message["text"] = fname
            message["title"] = fname
            extra.update({
                "filename": fname[:200],
                "size": _node_text(attach, "totallen"),
                "ext": _node_text(attach, "fileext") or Path(fname).suffix.lstrip("."),
                "md5": _node_text(attach, "md5") or _attr(attach, "md5", "md5"),
            })
        elif kind == "quote":
            message["text"] = title
            ref = _node(root, "appmsg/refermsg")
            if ref is not None:
                quote_summary = _clean_fragment(
                    _node_text(ref, "content") or _node_text(ref, "title"))
                quote_sender = _node_text(ref, "displayname")
                extra["quote"] = {
                    "summary": emojify_wechat_shortcodes(
                        f"{quote_sender}: {quote_summary}" if quote_sender else quote_summary)[:300],
                    "text": emojify_wechat_shortcodes(quote_summary)[:300],
                    "sender": quote_sender,
                    "type": _node_text(ref, "type"),
                    "create_time": _node_text(ref, "createtime"),
                    "message_id": "",
                    "server_id": _node_text(ref, "svrid") or _node_text(ref, "msgid"),
                }
        elif kind == "forward":
            items, truncated = self._forward_items(root)
            message["extra"]["forward_items"] = items
            message["extra"]["forward_truncated"] = truncated
            head = title or _node_text(root, "appmsg/descr") or "转发的聊天记录"
            message["text"] = head
            message["title"] = head
        elif kind == "location":
            message["text"] = _node_text(root, "location/label") or title
            extra.update({
                "label": _node_text(root, "location/label"),
                "poi": _node_text(root, "location/poiname"),
                "lat": _node_text(root, "location/x"),
                "lng": _node_text(root, "location/y"),
            })
        elif kind == "announce":
            message["text"] = title or descr
        elif kind == "card":
            message["text"] = _node_text(root, "msg/nickname") or title
        elif kind == "redpacket":
            message["text"] = title or descr
        elif kind == "transfer":
            message["text"] = _join(_node_text(root, "appmsg/pay_memo"), descr or title)
        else:
            message["text"] = title or descr
        if url.startswith(("http://", "https://")):
            extra["url"] = url[:2048]
        if appname:
            extra["app"] = appname[:80]
        if source:
            extra["source"] = source[:60]
        if source_id.lower().startswith("gh_") or source_id.lower().endswith("@app"):
            # 公众号文章 / 小程序：卡片上要能一眼看出是「内容号」
            extra["source_id"] = source_id[:80]
            extra["official"] = source_id.lower().startswith("gh_")
        # 兜底：任何分支都不允许把原始 XML 抛给前端
        for key in ("text", "title"):
            if str(message.get(key) or "").lstrip().startswith("<"):
                message[key] = _clean_fragment(message[key])
        if not message["text"] and not message["title"]:
            message["parse_status"] = "partial"
        _ = sub

    def _forward_items(self, root: ET.Element) -> tuple[list[dict[str, Any]], bool]:
        record = _node(root, "appmsg/recorditem") or _node(root, "recorditem")
        items: list[dict[str, Any]] = []
        if record is None:
            return items, False
        record_root = _xml_root(record.text or "")
        if record_root is None:
            return items, False
        truncated = False
        for node in record_root.iter("dataitem"):
            if len(items) >= MAX_FORWARD_ITEMS:
                truncated = True
                break
            datatype = node.get("datatype") or ""
            items.append({
                "title": _node_text(node, "title")[:200],
                "name": _node_text(node, "name")[:80],
                "time": _node_text(node, "sourcetime") or _node_text(node, "sourcetime"),
                "type": datatype,
            })
        return items, truncated

    def close(self) -> None:
        for conn in self._conns.values():
            if conn is None:
                continue
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._conns.clear()


def message_summary(message: dict[str, Any]) -> str:
    kind = message.get("kind") or "other"
    text = (message.get("text") or "").replace("\n", " ").strip()
    extra = message.get("extra") or {}
    if kind == "text":
        return text[:120]
    placeholder = KIND_PLACEHOLDERS.get(kind, "")
    if kind == "system":
        return text[:120]
    if kind == "file":
        return f"[文件] {extra.get('filename') or text}"[:120]
    if kind == "voice":
        duration = extra.get("duration")
        return f"[语音] {duration}\"" if duration else "[语音]"
    if kind == "video":
        return f"[视频] {text}"[:120] if text else "[视频]"
    if kind == "card":
        return f"[名片] {text}"[:120] if text else "[名片]"
    if kind == "location":
        return f"[位置] {text}"[:120] if text else "[位置]"
    if kind in ("link", "miniprogram", "channel"):
        return f"{placeholder} {text}"[:120] if text else placeholder
    if kind == "quote":
        prefix = placeholder
        return f"{prefix} {text}"[:120] if text else prefix
    if kind == "forward":
        count = len(extra.get("forward_items") or [])
        head = message.get("title") or ""
        if count:
            return f"[聊天记录] {head or f'{count} 条'}"[:120]
        return f"[聊天记录] {head}"[:120]
    return f"{placeholder} {text}"[:120].strip() if text else placeholder
