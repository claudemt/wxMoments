"""从微信 PC 端自带的浏览器缓存里直接读登录态。

微信 4.x 的内置浏览器（XWEB，落盘在 %APPDATA%\\Tencent\\xwechat\\radium\\web\\profiles\\）
会在打开文章时把**带 key 的完整文章地址**缓存下来，例如：

    https://mp.weixin.qq.com/s?__biz=...&mid=...&idx=...&sn=...&key=daf9bdc5...（256 位十六进制）

这些 URL 落在 History / Favicons / Share Data 几个文件里，普通权限就能读。
所以只要在微信里点开过目标公众号的文章，我们就能把 uin / key / pass_ticket 直接捡回来，
既不用装证书，也不用改系统代理。

两个必须记住的性质：

* key 只对签发它的那个公众号有效，换一个 __biz 会得到 ret=-3（no session）。
* key 会过期，缓存里翻出来的老 key 基本都是 -3。

所以这里读 SQLite 时**连 rowid 一起读出来当新鲜度**：Favicons.icon_mapping 是每打开一个页面
就追加一行的小表，最大 rowid 就是最近打开的那篇文章，按它倒序排出来的第一个候选 key
就是最可能还活着的那个。这个顺序比原来按 key 长度猜靠谱得多。
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from urllib.parse import parse_qs, unquote, urlparse

KEY_RE = re.compile(r"^[0-9a-f]{64,300}$")


PROFILE_ROOTS = (
    os.path.expandvars(r"%APPDATA%\Tencent\xwechat\radium\web\profiles"),
    os.path.expandvars(r"%APPDATA%\Tencent\WeChat\radium\web\profiles"),
    os.path.expandvars(r"%LOCALAPPDATA%\Tencent\xwechat\radium\web\profiles"),
)

DATA_FILES = ("Favicons", "Share Data", "History", "History.wxbak")


SQL_TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    
    "Favicons": (("icon_mapping", "page_url"),),
    "Share Data": (("share_data_table", "real_url"),),
    "History": (("visits", "url"), ("urls", "url")),
}





URL_RE = re.compile(
    rb"https://mp\.weixin\.qq\.com/(?:s|mp/profile_ext)\?[^\x00-\x20\"'<>\\]{0,1200}"
)
PARAM_RE = re.compile(rb"[?&](__biz|uin|key|pass_ticket)=([A-Za-z0-9%_.+/\-=]{4,300})")


_SOURCE_ORDER = {name: i for i, name in enumerate(DATA_FILES)}


def find_profiles() -> list[str]:
    out: list[str] = []
    for root in PROFILE_ROOTS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if os.path.isdir(p) and any(os.path.exists(os.path.join(p, f)) for f in DATA_FILES):
                out.append(p)
    return out


def _snapshot(path: str) -> str:
    """先复制一份再读。

    微信正开着的时候会持有这几个 SQLite 文件的锁，直接连上去会 ``database is locked``
    （而且 sqlite 会先卡住重试几秒），所以一律读副本。
    """
    try:
        tmp = os.path.join(
            tempfile.gettempdir(),
            "wxdl_" + os.path.basename(os.path.dirname(path)) + "_" + os.path.basename(path),
        )
        shutil.copyfile(path, tmp)
        return tmp
    except OSError:
        return ""


def _sqlite_rows(path: str) -> list[tuple[str, int, str]]:
    """读出 (来源名, 新鲜度, url)。读不了就返回空列表。

    这些表是 WITHOUT ROWID 的，主键列就是 ``id``，所以新鲜度取 ``id``。
    """
    src = _snapshot(path)
    if not src:
        return []
    name = os.path.basename(path)
    out: list[tuple[str, int, str]] = []
    try:
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        for table, column in SQL_TABLES.get(name, ()):
            try:
                cols = [r[1] for r in con.execute(f"pragma table_info({table})")]
                if column not in cols:
                    continue
                order = "id" if "id" in cols else "rowid"
                rows = con.execute(
                    f"select {order}, {column} from {table} order by {order} desc"
                ).fetchall()
            except sqlite3.Error:
                continue
            for stamp, url in rows:
                if isinstance(url, str) and url.startswith("http"):
                    out.append((name, int(stamp or 0), url))
    finally:
        con.close()
    return out


def _text_urls(path: str) -> list[tuple[str, int, str]]:
    """非数据库文件（比如 .wxbak）里的 URL，按字节位置当新鲜度。"""
    src = _snapshot(path)
    if not src:
        return []
    try:
        with open(src, "rb") as fh:
            blob = fh.read()
    except OSError:
        return []
    name = os.path.basename(path)
    
    return [(name, len(blob) - m.start(), m.group(0).decode("latin-1"))
            for m in URL_RE.finditer(blob)]


def _clean(value: str) -> str:
    return unquote(value.replace("&amp;", "&"))


def _parse(url: str) -> tuple[str, str, str, str] | None:
    q = parse_qs(urlparse(url).query)
    biz = (q.get("__biz") or [""])[0]
    key = (q.get("key") or [""])[0]
    if not biz or not KEY_RE.match(key):
        return None
    return biz, key, (q.get("uin") or [""])[0], (q.get("pass_ticket") or [""])[0]


def scan(biz: str | None = None) -> list[dict]:
    """扫出缓存里的文章地址凭据，**最新打开的排在最前面**。"""
    records: list[dict] = []
    seen: set[tuple[str, str]] = set()
    mtimes: dict[tuple[str, str], float] = {}
    
    
    fallbacks: dict[tuple[str, str], dict[str, str]] = {}

    for profile in find_profiles():
        
        rows: list[tuple[str, int, str]] = []
        for name in DATA_FILES:
            path = os.path.join(profile, name)
            if not os.path.exists(path):
                continue
            try:
                mtimes[(profile, name)] = os.path.getmtime(path)
            except OSError:
                mtimes[(profile, name)] = 0.0
            if name in SQL_TABLES:
                rows.extend(_sqlite_rows(path))
            else:
                rows.extend(_text_urls(path))

        
        for name in ("History", "Share Data", "Favicons"):
            path = os.path.join(profile, name)
            if not os.path.exists(path):
                continue
            src = _snapshot(path)
            if not src:
                continue
            try:
                with open(src, "rb") as fh:
                    blob = fh.read()
            except OSError:
                continue
            slot = fallbacks.setdefault((profile, name), {"uin": "", "pass_ticket": ""})
            for field, value in PARAM_RE.findall(blob):
                if field == b"uin" and not slot["uin"]:
                    slot["uin"] = _clean(value.decode("latin-1"))
                elif field == b"pass_ticket" and not slot["pass_ticket"]:
                    slot["pass_ticket"] = _clean(value.decode("latin-1"))

        for source, rowid, raw in rows:
            url = _clean(raw)
            parsed = _parse(url)
            if not parsed:
                continue
            b, k, uin, pt = parsed
            if biz and b != biz:
                continue
            if (b, k) in seen:
                continue
            seen.add((b, k))
            records.append({
                "biz": b,
                "key": k,
                "uin": uin,
                "pass_ticket": pt,
                "url": url,
                "source": source,
                "profile": profile,
                "rank": rowid,
                "mtime": mtimes.get((profile, source), 0.0),
            })

    for rec in records:
        
        
        
        rec["borrowed"] = []
        slot = fallbacks.get((rec["profile"], rec["source"]), {})
        if not rec["uin"]:
            if slot.get("uin"):
                rec["uin"] = slot["uin"]
                rec["borrowed"].append("uin")
            else:
                rec["uin"] = "777"
        if not rec["pass_ticket"]:
            if slot.get("pass_ticket"):
                rec["pass_ticket"] = slot["pass_ticket"]
                rec["borrowed"].append("pass_ticket")

    
    
    
    records.sort(key=lambda r: (_SOURCE_ORDER.get(r["source"], 99), -r["rank"], -r["mtime"]))
    return records


def candidates(biz: str) -> list[dict]:
    """给某个公众号准备若干份待验证的凭据，最新的在前。"""
    return scan(biz)
