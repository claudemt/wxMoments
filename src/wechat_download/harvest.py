"""从微信 XWEB 缓存收割一个公众号的「全部文章 URL」（替代 getmsg 列表）。

为什么需要它
------------
微信 4.x 的 profile_ext?action=getmsg 对本机这个账号始终回空壳
（ret:0 / msg_count:0，见 README「历史」一节）。但微信在用户**点开文章**时，
会把完整文章地址（含 __biz / mid / idx / sn / chksm）写进内置浏览器缓存
（History / Share Data / Favicons），这些普通权限就能读。

所以「作者全部文章」的可靠来源 = 用户先把文章都点开一遍 → 我们收割缓存。
chksm 顺手也拿到：抓取时带上它，微信才不弹风控验证页。

标题 / 发布时间从两处补：
1. 磁盘缓存 Cache_Data/f_*（gzip 存着完整文章页，有 og:title / var ct）；
2. 都没有时留空，批量导出下载正文时会补全（export_meta.json 里写的是补全后的）。
"""
from __future__ import annotations

import gzip
import os
import re
import threading
import time
from dataclasses import dataclass, field

from . import wxprofile


ARTICLE_RE = re.compile(
    rb"https://mp\.weixin\.qq\.com/s\?[^\x00-\x20\"'<>\\]{0,1200}"
)


PARAM_RE = re.compile(
    rb"[?&](__biz|mid|idx|sn|chksm)=([A-Za-z0-9%_.+/\-=]{1,300})"
)


OG_TITLE_RE = re.compile(r'property="og:title"\s+content="([^"]*)"')
OG_URL_RE = re.compile(r'property="og:url"\s+content="([^"]*)"')
CT_RE = re.compile(
    r"var\s+(?:ct|oriCreateTime)\s*=\s*'?(\d{8,12})'?"
)


@dataclass
class Harvested:
    url: str
    mid: str
    idx: str = "1"
    sn: str = ""
    chksm: str = ""
    title: str = ""
    publish_ts: int = 0

    @property
    def key(self) -> str:
        return f"{self.mid}_{self.idx}"

    @property
    def time_text(self) -> str:
        if not self.publish_ts:
            return ""
        from datetime import datetime
        return datetime.fromtimestamp(self.publish_ts).strftime("%Y-%m-%d %H:%M")

    @property
    def date_text(self) -> str:
        if not self.publish_ts:
            return ""
        from datetime import datetime
        return datetime.fromtimestamp(self.publish_ts).strftime("%Y%m%d")


_PAGE_MAP: dict[str, dict] = {}
_PAGE_MAP_KEY: tuple | None = None
_PAGE_MAP_LOCK = threading.RLock()


def _cache_sig() -> tuple:
    sig = []
    for profile in wxprofile.find_profiles():
        cache = os.path.join(profile, "Cache", "Cache_Data")
        try:
            sig.append((cache, os.path.getmtime(cache)))
        except OSError:
            continue
    return tuple(sig)


def _scan_cache_pages(biz: str) -> dict[str, dict]:
    with _PAGE_MAP_LOCK:
        return _scan_cache_pages_locked(biz)


def _scan_cache_pages_locked(biz: str) -> dict[str, dict]:
    """扫全部 profile 的 Cache_Data/f_*，建 mid_idx -> {url,title,ts}（只收目标号）。"""
    global _PAGE_MAP, _PAGE_MAP_KEY
    sig = (biz, _cache_sig())
    if sig and sig == _PAGE_MAP_KEY:
        return _PAGE_MAP
    out: dict[str, dict] = {}
    for profile in wxprofile.find_profiles():
        cache = os.path.join(profile, "Cache", "Cache_Data")
        if not os.path.isdir(cache):
            continue
        for name in os.listdir(cache):
            if not name.startswith("f_"):
                continue
            path = os.path.join(cache, name)
            try:
                with open(path, "rb") as fh:
                    head = fh.read(2)
                if head != b"\x1f\x8b":
                    continue
                with gzip.open(path, "rb") as fh:
                    body = fh.read()
            except (OSError, gzip.BadGzipFile):
                continue
            if b"og:url" not in body:
                continue
            try:
                text = body.decode("utf-8", "ignore")
            except Exception:
                continue
            u = OG_URL_RE.search(text)
            if not u:
                continue

            m = re.search(
                r"/s\?.*?__biz=([^&\"']+)&.*?mid=(\d+)&.*?idx=(\d+)&.*?sn=([0-9a-f]+)"
                r"(&.*?chksm=([0-9a-f]+))?", u.group(1))
            if not m or m.group(1) != biz:
                continue
            key = f"{m.group(2)}_{m.group(3)}"
            t = OG_TITLE_RE.search(text)
            c = CT_RE.search(text)
            out[key] = {
                "url": u.group(1),
                "title": t.group(1).strip() if t else "",
                "ts": int(c.group(1)) if c else 0,
            }
    _PAGE_MAP = out
    _PAGE_MAP_KEY = sig
    return out


def parse_article_url(raw: str) -> dict | None:
    q = {}
    for k, v in PARAM_RE.findall(raw.encode("latin-1")):
        q[k.decode("latin-1")] = v.decode("latin-1")
    biz, mid, idx, sn = q.get("__biz", ""), q.get("mid", ""), q.get("idx", ""), q.get("sn", "")
    if not biz or not mid or not sn:
        return None
    return {
        "biz": biz, "mid": mid, "idx": idx or "1", "sn": sn,
        "chksm": q.get("chksm", ""),
    }


def clean_url(p: dict) -> str:
    """裁剪成真实的微信发布链接：只留 __biz/mid/idx/sn/chksm。

    微信客户端点开文章时，缓存 URL 会带一长串会话参数
    （key/pass_ticket/exportkey/uin/clicktime/…），它们几小时就失效、
    还会暴露账号身份。sn 本身是文章唯一签名，这种标准链接长期有效，
    是「真实发布链接」的形态。
    """
    url = (f"https://mp.weixin.qq.com/s?__biz={p['biz']}"
           f"&mid={p['mid']}&idx={p['idx']}&sn={p['sn']}")
    if p.get("chksm"):
        url += f"&chksm={p['chksm']}"
    return url


def public_url(art, fallback: str = "") -> str:
    """导出成品里展示的「真实发布链接」。

    形态：https://mp.weixin.qq.com/s?__biz=…&mid=…&idx=…&sn=…&chksm=…
    - 不含任何隐私会话参数（key/pass_ticket/uin/clicktime 等），长期有效；
    - 带 chksm 才能在浏览器/微信里免风控直接打开（实测：不带则弹验证页）；
    - 微信客户端分享时才生成的 /s/<22位> 短链无法离线获取（页面无该字段、
      算法不可逆、官方接口需登录态），所以成品统一用这个标准形态。
    """
    try:
        chksm = getattr(art, "chksm", "") or ""
        if not chksm:

            return fallback
        url = clean_url({
            "biz": art.biz, "mid": art.mid, "idx": art.idx or "1",
            "sn": art.sn, "chksm": chksm,
        })
        if url.startswith("https://mp.weixin.qq.com/s?"):
            return url
    except Exception:
        pass
    return fallback


def harvest_articles(biz: str, use_pages: bool = True) -> list[Harvested]:
    """收割公众号 biz 的文章 URL（新→旧）。

    use_pages=True：额外扫磁盘缓存 f_*（能捞回数据库已淘汰的 URL + 补标题时间），
    成本约 1-2s，适合「点一下刷新」这种低频操作。
    use_pages=False：只读三个 SQLite（<200ms），适合后台轮询增量收集。
    """
    found: dict[str, Harvested] = {}
    for _src, _stamp, raw in wxprofile.iter_cached_urls():
        m = ARTICLE_RE.search(raw.encode("latin-1"))
        if not m:
            continue
        p = parse_article_url(m.group(0).decode("latin-1"))
        if not p or p["biz"] != biz:
            continue
        key = f"{p['mid']}_{p['idx']}"
        old = found.get(key)
        if old and old.chksm:
            continue
        found[key] = Harvested(
            url=clean_url(p),
            mid=p["mid"], idx=p["idx"], sn=p["sn"], chksm=p["chksm"],
        )


    if use_pages:

        pages = _scan_cache_pages(biz)
        for key, pg in pages.items():
            if key in found:
                continue
            mid, idx = key.split("_", 1)
            sm = re.search(r"sn=([0-9a-f]{32})", pg["url"])
            cm = re.search(r"chksm=([0-9a-f]+)", pg["url"])
            found[key] = Harvested(
                url=clean_url({
                    "biz": biz, "mid": mid, "idx": idx,
                    "sn": sm.group(1) if sm else "",
                    "chksm": cm.group(1) if cm else "",
                }),
                mid=mid, idx=idx,
                sn=sm.group(1) if sm else "",
                chksm=cm.group(1) if cm else "",
            )

        for h in found.values():
            pg = pages.get(h.key)
            if pg and pg.get("title"):
                h.title = pg["title"]
            if pg and pg.get("ts"):
                h.publish_ts = pg["ts"]

    out = sorted(found.values(), key=lambda h: int(h.mid), reverse=True)
    return out


def write_export_meta(root: str, account: str, mode: str,
                      items: list[dict]) -> str:
    """批量导出完成后写一份导出记录 JSON。

    items: [{"title","url","publish_time","status",...}]（按导出顺序）
    返回 JSON 文件路径；只写「已成功」的文章。
    """
    ok_items = [i for i in items if i.get("status") == "ok"]
    meta = {
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "account": account,
        "mode": mode,
        "total": len(items),
        "success": len(ok_items),
        "articles": [
            {"title": i.get("title", ""), "url": i.get("url", ""),
             "publish_time": i.get("publish_time", "")}
            for i in ok_items
        ],
    }
    path = os.path.join(root, "export_meta.json")
    import json
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    return path
