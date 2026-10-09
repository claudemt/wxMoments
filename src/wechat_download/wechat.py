"""微信公众号文章抓取、解析、图片下载。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from .config import FETCH_UA, SESSION_FILE






@dataclass
class Session:
    """一次微信抓包获得的登录态，按公众号（__biz）分别保存。"""
    biz: str
    key: str = ""
    uin: str = ""
    pass_ticket: str = ""
    cookie: str = ""
    user_agent: str = ""
    host: str = "mp.weixin.qq.com"
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @property
    def age_text(self) -> str:
        mins = int((time.time() - self.ts) / 60)
        if mins < 1:
            return "刚刚"
        if mins < 60:
            return f"{mins} 分钟前"
        return f"{mins // 60} 小时前"


def load_sessions() -> dict[str, Session]:
    if not SESSION_FILE.exists():
        return {}
    raw = _read_session_raw()
    if raw is None:
        return {}
    out = {}
    for biz, d in raw.items():
        try:
            out[biz] = Session(**d)
        except Exception:
            continue
    return out


def _read_session_raw() -> dict | None:
    """读 session.json，损坏时尽量从残留段里救回最新一份有效 JSON。

    旧实现：文件一损坏 load_sessions 就静默返回 {}，下次 save_session 会把
    之前的全部凭据覆盖掉，坏文件只会越来越空。这里改为：
    - 整文件合法 → 直接用；
    - 不合法 → 用 raw_decode 逐段扫描，收集所有完整 JSON 段，
      取最后一段当数据（多进程并发写撕裂时，最后写完的那段通常最新）；
    - 一段都救不回 → None（调用方按无会话处理）。
    """
    text = SESSION_FILE.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    decoder = json.JSONDecoder()
    found: dict | None = None
    idx = 0
    n = len(text)
    while idx < n:
        start = text.find("{", idx)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except Exception:
            idx = start + 1
            continue
        if isinstance(obj, dict):
            found = obj
        idx = end
    return found


def _write_session_atomic(data: dict) -> None:
    """原子写 session.json：先写临时文件再 replace，避免并发写撕裂文件。"""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(
            prefix="session.", suffix=".tmp", dir=str(SESSION_FILE.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, SESSION_FILE)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass





@dataclass
class Article:
    url: str
    title: str = ""
    account: str = ""       
    author: str = ""        
    biz: str = ""
    sn: str = ""
    mid: str = ""
    idx: str = ""
    chksm: str = ""         
    publish_ts: int = 0
    content_html: str = ""  
    cover: str = ""
    digest: str = ""

    @property
    def publish_text(self) -> str:
        if not self.publish_ts:
            return ""
        return datetime.fromtimestamp(self.publish_ts).strftime("%Y-%m-%d %H:%M")

    @property
    def publish_date(self) -> str:
        if not self.publish_ts:
            return ""
        return datetime.fromtimestamp(self.publish_ts).strftime("%Y%m%d")


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    m = re.search(r"https?://mp\.weixin\.qq\.com/[^\s\"'<>]+", url)
    if m:
        url = m.group(0)
    return url.replace("&amp;", "&")


def _meta(html: str, name: str) -> str:
    m = re.search(
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]*content=["\']([^"\']*)["\']',
        html,
    )
    if not m:
        m = re.search(
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(name)}["\']',
            html,
        )
    return m.group(1).strip() if m else ""


def _var(html: str, name: str) -> str:
    m = re.search(rf"var\s+{re.escape(name)}\s*=\s*[\"']([^\"']*)[\"']", html)
    return m.group(1).strip() if m else ""






_CHKSM_MAP: dict[tuple[str, str, str, str], str] | None = None


def _load_chksm_map() -> dict[tuple[str, str, str, str], str]:
    """从 XWEB 缓存（Share Data/Favicons/History）构建 (biz,mid,idx,sn)->chksm。"""
    global _CHKSM_MAP
    if _CHKSM_MAP is not None:
        return _CHKSM_MAP
    out: dict[tuple[str, str, str, str], str] = {}
    try:
        from . import wxprofile
        for rec in wxprofile.scan():
            url = rec.get("url") or ""
            m = re.search(
                r"__biz=([^&]+).*?mid=(\d+).*?idx=(\d+).*?sn=([0-9a-f]+).*?chksm=([0-9a-f]+)",
                url)
            if m:
                out[(m.group(1), m.group(2), m.group(3), m.group(4))] = m.group(5)
    except Exception:
        pass
    _CHKSM_MAP = out
    return out


def ensure_chksm(url: str) -> str:
    """URL 缺 chksm 时从缓存补上；补不上就原样返回。"""
    if "chksm=" in url:
        return url
    m = re.search(r"__biz=([^&]+).*?mid=(\d+).*?idx=(\d+).*?sn=([0-9a-f]+)", url)
    if not m:
        return url
    key = (m.group(1), m.group(2), m.group(3), m.group(4))
    chksm = _load_chksm_map().get(key)
    if not chksm:
        return url
    sep = "&" if "?" in url else "?"
    return url + f"{sep}chksm={chksm}"


def fetch_html(url: str, session: Session | None = None, timeout: int = 25) -> str:
    url = ensure_chksm(url)  
    headers = {
        "User-Agent": FETCH_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if session and session.cookie:
        headers["Cookie"] = session.cookie
        headers["User-Agent"] = session.user_agent or FETCH_UA
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_article(html: str, url: str) -> Article:
    soup = BeautifulSoup(html, "lxml")
    art = Article(url=url)

    art.title = _var(html, "msg_title") or _meta(html, "og:title") or ""
    if not art.title:
        t = soup.find("h1", class_="rich_media_title") or soup.find("title")
        art.title = t.get_text(strip=True) if t else "未命名文章"

    node = soup.select_one("#js_name")
    art.account = node.get_text(strip=True) if node else ""

    art.author = _var(html, "author")
    if not art.author:
        n = soup.select_one("#js_author_name") or soup.select_one(".rich_media_meta_text.author")
        art.author = n.get_text(strip=True) if n else ""

    
    def _url_param(pattern: str) -> str:
        m = re.search(pattern, url)
        return m.group(1) if m else ""

    art.biz = _url_param(r"__biz=([A-Za-z0-9=+%/]+)") or _var(html, "biz")
    if not art.biz:
        m = re.search(r"__biz=([A-Za-z0-9=+%/]+)", html)
        art.biz = m.group(1) if m else ""
    else:
        m = re.search(r"__biz=([A-Za-z0-9=+%/]+)", art.biz or "")
        art.biz = m.group(1) if m else art.biz

    art.sn = _url_param(r"sn=([0-9a-f]{32})") or _var(html, "sn")
    if not art.sn:
        m = re.search(r"\bsn=([0-9a-f]{32})", html)
        art.sn = m.group(1) if m else ""
    art.mid = _url_param(r"mid=(\d+)") or _var(html, "mid")
    if not art.mid:
        m = re.search(r"\bmid=(\d+)", html)
        art.mid = m.group(1) if m else ""
    art.idx = _url_param(r"idx=(\d+)") or "1"
    art.chksm = _url_param(r"chksm=([0-9a-f]+)")

    ct = _var(html, "ct")
    if ct.isdigit():
        art.publish_ts = int(ct)
    else:
        n = soup.select_one("#publish_time")
        txt = n.get_text(strip=True) if n else ""
        m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", txt)
        if m:
            art.publish_ts = int(
                datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).timestamp()
            )

    art.cover = _meta(html, "og:image")
    art.digest = _meta(html, "og:description")

    content = soup.select_one("#js_content")
    if content is not None:
        from .render import clean_content
        art.content_html = clean_content(content)
    return art


def get_article(url: str, session: Session | None = None) -> Article:
    return parse_article(fetch_html(normalize_url(url), session), normalize_url(url))





def download_image(url: str, referer: str = "https://mp.weixin.qq.com/") -> tuple[bytes, str]:
    """下载图片，返回 (bytes, 扩展名)。"""
    headers = {
        "User-Agent": FETCH_UA,
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.content
    ctype = (r.headers.get("Content-Type") or "").lower()
    ext = "jpg"
    for key, e in (("png", "png"), ("gif", "gif"), ("webp", "webp"),
                   ("jpeg", "jpg"), ("jpg", "jpg"), ("bmp", "bmp"), ("svg", "svg")):
        if key in ctype:
            ext = e
            break
    else:
        m = re.search(r"wx_fmt=([a-z0-9]+)", url)
        if m and m.group(1) in ("png", "gif", "webp", "jpeg", "jpg", "bmp"):
            ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
    return data, ext


def full_size_url(url: str) -> str:
    """把 mmbiz 的 640 缩略图换成原图。"""
    url = url.replace("&amp;", "&")
    if "mmbiz.qpic.cn" not in url:
        return url
    return re.sub(r"/(?:640|300|100)/?\?", "/0?", url)

