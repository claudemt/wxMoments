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
    url = (f"https://mp.weixin.qq.com/s?__biz={p['biz']}"
           f"&mid={p['mid']}&idx={p['idx']}&sn={p['sn']}")
    if p.get("chksm"):
        url += f"&chksm={p['chksm']}"
    return url


def public_url(art, fallback: str = "") -> str:
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
