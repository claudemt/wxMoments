"""统一网页服务：朋友圈导出 + 公众号文章导出。http://127.0.0.1:8756

由 wechatDownload（公众号文章）与 wxMoments（朋友圈）合并而来。
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
import base64
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

BASE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = BASE_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

os.environ.setdefault("WECHAT_TOOL_DATA_DIR", str(BASE_DIR / "runtime"))
os.environ.setdefault("WECHAT_TOOL_OUTPUT_DIR", str(BASE_DIR / "runtime" / "output"))
os.environ.setdefault("WECHAT_TOOL_BUILD_SESSION_LAST_MESSAGE", "0")

from wechat_download import exporter, harvest, render, wechat, wxprofile  
from wechat_download.config import HOST, OUTPUT_DIR, PORT, PROXY_PORT, WEB_DIR  
import wxmoments  

app = FastAPI(title="微信数据导出工具")




_ARTICLE_CACHE: dict[str, wechat.Article] = {}
_JOBS: dict[str, "Job"] = {}


class Job:
    def __init__(self, total: int):
        self.id = uuid.uuid4().hex[:12]
        self.total = total
        self.done = 0
        self.status = "running"       
        self.message = "准备中…"
        self.log: list[str] = []
        self.results: list[dict] = []
        self.canceled = False

    def add_log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {text}")
        if len(self.log) > 400:
            del self.log[:100]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "total": self.total, "done": self.done,
            "status": self.status, "message": self.message,
            "log": self.log[-120:], "results": self.results,
        }





@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.post("/api/reveal")
async def api_reveal(payload: dict):
    path = Path(payload.get("path") or OUTPUT_DIR)
    if not path.exists():
        path = OUTPUT_DIR
    try:
        subprocess.Popen(["explorer", str(path)])
        return {"ok": True}
    except Exception as e:  
        return {"ok": False, "msg": str(e)}





def _load_article(url: str, biz: str | None = None) -> wechat.Article:
    url = wechat.normalize_url(url)
    if url in _ARTICLE_CACHE:
        return _ARTICLE_CACHE[url]
    session = wechat.load_sessions().get(biz) if biz else None
    art = wechat.get_article(url, session)
    if biz and not art.biz:      
        art.biz = biz
    _ARTICLE_CACHE[url] = art
    return art


@app.post("/api/parse")
async def api_parse(payload: dict):
    url = (payload.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "msg": "请先粘贴文章链接"}, status_code=400)
    try:
        art = await run_in_threadpool(_load_article, url)
    except Exception as e:  
        return JSONResponse({"ok": False, "msg": f"解析失败：{e}"}, status_code=200)
    return {
        "ok": True,
        "article": {
            "url": art.url,
            "title": art.title,
            "account": art.account,
            "author": art.author,
            "publish_text": art.publish_text,
            "biz": art.biz,
        },
    }





@app.post("/api/export")
async def api_export(payload: dict):
    kind = payload.get("type") or ""
    if kind == "article":
        return _start_article_export(payload)
    if kind == "author":
        return _start_author_export(payload)
    if kind == "moments":
        return _start_moments_export(payload)
    return JSONResponse({"ok": False, "msg": "未知的导出类型"}, status_code=400)


def _start_article_export(payload: dict) -> dict:
    url = (payload.get("url") or "").strip()
    try:
        art = _load_article(url)
    except Exception as e:  
        return {"ok": False, "msg": f"解析失败：{e}"}
    job = Job(1)
    _JOBS[job.id] = job
    threading.Thread(target=_run_single, args=(job, art), daemon=True).start()
    return {"ok": True, "job": job.id, "dir": str(OUTPUT_DIR)}


def _start_author_export(payload: dict) -> dict:
    articles = payload.get("articles") or []
    account = payload.get("account") or "公众号"
    mode = payload.get("mode") or "select"
    if not articles:
        return {"ok": False, "msg": "没有选择任何文章"}
    root = _batch_root(account, mode)
    job = Job(len(articles))
    _JOBS[job.id] = job
    threading.Thread(
        target=_run_batch,
        args=(job, articles, account, mode, payload.get("biz") or ""),
        daemon=True,
    ).start()
    return {"ok": True, "job": job.id, "dir": str(root)}


def _start_moments_export(payload: dict) -> dict:
    try:
        account_info = _MOMENTS.get("account_info")
        account_dir = _MOMENTS.get("account_dir")
        if not account_info or not account_dir:
            return {"ok": False, "msg": "请先点击「初始化解密」"}
        account_dir = Path(account_dir)

        raw_u = payload.get("usernames")
        usernames = raw_u if raw_u is not None else None
        start_raw = (payload.get("start") or "").strip()
        end_raw = (payload.get("end") or "").strip()
        start = wxmoments.parse_datetime(start_raw, end_of_day=False) if start_raw else None
        end = wxmoments.parse_datetime(end_raw, end_of_day=True) if end_raw else None
        if start and end and end < start:
            return {"ok": False, "msg": "结束日期必须晚于开始日期"}
        keep_interactions = bool(payload.get("keep_interactions"))

        
        picked = payload.get("picked") or None
        if picked:
            wanted = {
                (int(p.get("createTime") or 0), str(p.get("username") or "").strip())
                for p in picked
            }
            filtered = [
                p for p in (_MOMENTS.get("posts") or [])
                if (int(p.get("createTime") or 0), str(p.get("username") or "").strip()) in wanted
            ]
        else:
            filtered = _moments_filtered_posts(usernames, start, end)
        if not filtered:
            return {"ok": False, "msg": "所选条件下没有可导出的朋友圈，请调整筛选条件"}

        job = Job(len(filtered))
        _JOBS[job.id] = job
        threading.Thread(
            target=_run_moments,
            args=(job, account_info, account_dir, start, end, usernames,
                  keep_interactions, filtered),
            daemon=True,
        ).start()
        return {"ok": True, "job": job.id, "dir": str(OUTPUT_DIR)}
    except Exception as exc:  
        return {"ok": False, "msg": str(exc)}


def _run_single(job: Job, art: wechat.Article) -> None:
    try:
        job.message = art.title
        job.add_log(f"开始导出：{art.title}")
        res = exporter.export_article(art, OUTPUT_DIR, job.add_log)
        job.done = 1
        job.results.append({
            "title": art.title, "dir": str(res.article_dir),
            "md": str(res.md_file), "html": str(res.html_file),
            "figures": len(res.figures), "failed": res.failed_images,
        })
        job.status = "done"
        job.message = f"完成：{res.article_dir.name}"
        job.add_log(f"已保存到 {res.article_dir}")
    except Exception as e:  
        job.status = "error"
        job.message = f"导出失败：{e}"
        job.add_log(job.message)





HARVEST_TIP = (
    "微信缓存里还没有这个公众号的文章记录。\n"
    "请在微信里打开该公众号的「历史消息」页，把里面的每一篇文章都点开一遍"
    "（点开即可，可以立刻按 Ctrl+W 关掉）。\n"
    "这边会自动收录，下面列表会实时变多，点完直接勾选下载。"
)


@app.post("/api/author/articles")
async def api_author_articles(payload: dict):
    url = (payload.get("url") or "").strip()
    try:
        art = await run_in_threadpool(_load_article, url)
    except Exception as e:  
        return {"ok": False, "msg": f"解析失败：{e}"}

    biz = art.biz
    head = {"ok": True, "biz": biz, "account": art.account, "author": art.author}
    if not biz:
        return {**head, "ok": False, "msg": "没有从这篇文章里识别出公众号 ID"}

    articles = await run_in_threadpool(harvest.harvest_articles, biz)
    if not articles:
        return {**head, "need_open": True, "articles": [], "msg": HARVEST_TIP}

    return {
        **head,
        "need_open": False,
        "articles": [
            {"title": h.title, "url": h.url, "time": h.time_text,
             "date": h.date_text, "dt": h.publish_ts,
             "mid": h.mid, "idx": h.idx}
            for h in articles
        ],
    }





_AUTHOR_WATCH: dict[str, dict] = {}   


def _watch_items(biz: str) -> list[dict]:
    items = list(_AUTHOR_WATCH.get(biz, {}).get("items", {}).values())
    items.sort(key=lambda d: int(d.get("mid") or 0), reverse=True)
    out = []
    for d in items:
        h = harvest.Harvested(url=d.get("url", ""), mid=str(d.get("mid", "")),
                              idx=str(d.get("idx", "")),
                              publish_ts=int(d.get("publish_ts") or 0))
        out.append({
            "title": d.get("title", ""), "url": d.get("url", ""),
            "time": h.time_text, "date": h.date_text, "dt": h.publish_ts,
            "mid": d.get("mid", ""), "idx": d.get("idx", ""),
        })
    return out


def _author_fill_loop(biz: str) -> None:
    """后台常驻：把缺标题/时间的文章并发抓正文补齐（带 chksm 零风控）。

    补齐结果直接写进 state["items"]，watch/status 返回时前端就能看到；
    抓到的正文同时进 _ARTICLE_CACHE，之后导出不再重复下载。
    5 线程并发，31 篇 ≈ 十几秒补完（原来串行 1.5s/篇要 1-2 分钟）。
    """
    from concurrent.futures import ThreadPoolExecutor
    state = _AUTHOR_WATCH.get(biz)
    if not state:
        return
    pool = ThreadPoolExecutor(max_workers=5)
    while True:
        todo = [k for k, d in state["items"].items()
                if not d.get("title") and d.get("url")]
        if not todo:
            state["filling"] = False
            time.sleep(2.0)
            continue
        state["filling"] = True

        def work(k: str) -> None:
            d = state["items"].get(k)
            if not d or d.get("title"):
                return
            try:
                art = _load_article(d["url"], biz)
                if art and art.title:
                    d["title"] = art.title
                if art and art.publish_ts:
                    d["publish_ts"] = art.publish_ts
            except Exception:  
                pass

        list(pool.map(work, todo))   
        time.sleep(0.5)


def _author_watch_loop(biz: str) -> None:
    state = _AUTHOR_WATCH.setdefault(
        biz, {"running": False, "items": {}, "added": []})
    state.setdefault("added", [])   
    tick = 0
    while state.get("running") and biz in _AUTHOR_WATCH:
        try:
            light = harvest.harvest_articles(biz, use_pages=False)
            for h in light:
                if h.key not in state["items"]:
                    state["added"].append(h.key)   
                state["items"].setdefault(h.key, {
                    "url": h.url, "mid": h.mid, "idx": h.idx,
                    "sn": h.sn, "chksm": h.chksm,
                    "title": "", "publish_ts": 0,
                })
            
            if tick % 20 == 0:
                full = harvest.harvest_articles(biz, use_pages=True)
                for h in full:
                    slot = state["items"].setdefault(h.key, {})
                    slot.update({"url": h.url, "mid": h.mid, "idx": h.idx,
                                 "sn": h.sn, "chksm": h.chksm})
                    if h.title:
                        slot["title"] = h.title
                    if h.publish_ts:
                        slot["publish_ts"] = h.publish_ts
        except Exception:  
            pass
        tick += 1
        time.sleep(3.5)
    state["running"] = False


@app.post("/api/author/watch/start")
async def api_author_watch_start(payload: dict):
    biz = (payload.get("biz") or "").strip()
    if not biz:
        return {"ok": False, "msg": "缺少公众号 ID"}
    state = _AUTHOR_WATCH.setdefault(biz, {"running": False, "items": {}})
    if not state.get("running"):
        state["running"] = True
        threading.Thread(target=_author_watch_loop, args=(biz,), daemon=True).start()
    if not state.get("fill_thread"):
        state["fill_thread"] = True
        threading.Thread(target=_author_fill_loop, args=(biz,), daemon=True).start()
    return {"ok": True, "total": len(state["items"])}


@app.post("/api/author/watch/status")
async def api_author_watch_status(payload: dict):
    biz = (payload.get("biz") or "").strip()
    state = _AUTHOR_WATCH.get(biz)
    if not state:
        return {"ok": True, "running": False, "total": 0, "articles": []}
    
    added = state.get("added", [])
    state["added"] = []
    added_meta = []
    for k in added:
        d = state["items"].get(k, {})
        added_meta.append({
            "mid": d.get("mid", ""), "idx": d.get("idx", ""),
            "title": d.get("title", ""),
        })
    return {"ok": True, "running": state.get("running", False),
            "filling": state.get("filling", False),
            "filling_left": sum(1 for d in state["items"].values()
                                if not d.get("title") and d.get("url")),
            "total": len(state["items"]),
            "added": added_meta,
            "articles": _watch_items(biz)}


@app.post("/api/author/watch/stop")
async def api_author_watch_stop(payload: dict):
    biz = (payload.get("biz") or "").strip()
    state = _AUTHOR_WATCH.get(biz)
    if state:
        state["running"] = False
    return {"ok": True}


def _batch_root(account: str, mode: str) -> Path:
    """下载目录：全部 → output/<公众号名>-<导出日期>/ ；选中/单篇 → output/。"""
    if mode == "all":
        return OUTPUT_DIR / f"{exporter.safe_name(account, 30)}-{datetime.now():%Y%m%d}"
    return OUTPUT_DIR


def _run_batch(job: Job, articles: list[dict], account: str, mode: str,
               biz: str = "") -> None:
    """mode=all → output/<公众号名>-<导出日期>/ ；mode=select → output/。

    批量导出（不止一篇）完成后在产物根目录写一份 export_meta.json，
    记录导出时间、总数量、每篇的标题/链接/发表时间。单篇导出不写。
    """
    root = _batch_root(account, mode)
    root.mkdir(parents=True, exist_ok=True)
    job.add_log(f"共 {job.total} 篇文章，保存到 {root}")

    records: list[dict] = []   
    for i, item in enumerate(articles, 1):
        if job.canceled:
            job.status = "canceled"
            job.message = "已取消"
            return
        job.message = f"({i}/{job.total}) {item.get('title', '')}"
        job.add_log(f"[{i}/{job.total}] {item.get('title', '')}")
        try:
            art = _load_article(item["url"], biz)
            res = exporter.export_article(art, root, job.add_log)
            job.results.append({
                "title": art.title, "dir": str(res.article_dir),
                "figures": len(res.figures), "failed": res.failed_images,
            })
            records.append({
                "status": "ok",
                "title": art.title or item.get("title", "") or (f"文章{art.mid}" if art.mid else "未命名文章"),
                "url": harvest.public_url(art, art.url),
                "publish_time": art.publish_text,
            })
        except Exception as e:  
            job.add_log(f"   失败：{e}")
            records.append({
                "status": "fail", "title": item.get("title", ""),
                "url": item.get("url", ""), "publish_time": "",
            })
        job.done = i

    if job.total > 1 and not job.canceled:
        try:
            meta_path = harvest.write_export_meta(str(root), account, mode, records)
            job.add_log(f"导出记录：{meta_path}")
        except Exception as e:  
            job.add_log(f"写导出记录失败：{e}")

    job.status = "done"
    job.message = f"全部完成，共 {job.total} 篇"
    job.add_log(job.message)





_MOMENTS: dict[str, Any] = {"account_info": None, "account_dir": None}


def _moments_config() -> dict[str, Any]:
    return wxmoments.load_config(wxmoments.DEFAULT_CONFIG)


def _moments_acquire_key(account_info: wxmoments.AccountInfo) -> str:
    """网页版密钥获取：config → 本地已存 → 自动读取（微信须已登录前台）。"""
    config = _moments_config()
    key = (str(config.get("db_key") or "") or os.environ.get("WXMOMENTS_KEY", "")).strip()
    if key:
        return key
    saved = wxmoments.load_saved_db_key(account_info)
    if saved:
        return saved
    from wechat_decrypt_tool.modules.key_service import get_db_key_workflow
    result = get_db_key_workflow(db_storage_path=str(account_info.db_storage_dir))
    key = str(result.get("db_key") or "").strip()
    if not re.fullmatch(rf"[0-9a-fA-F]{{{wxmoments.DB_KEY_HEX_LENGTH}}}", key):
        raise RuntimeError(
            "未能自动获取数据库密钥，请确认电脑版微信已登录并停留在主界面后重试"
        )
    return key


@app.post("/api/moments/init")
async def api_moments_init(payload: dict):
    """初始化解密为后台任务（定位→密钥→解密→预读数据），前端轮询进度。"""
    job = Job(1)
    _JOBS[job.id] = job
    threading.Thread(target=_run_moments_init, args=(job,), daemon=True).start()
    return {"ok": True, "job": job.id}


def _run_moments_init(job: Job) -> None:
    try:
        job.message = "定位微信账号…"
        config = _moments_config()
        account_info = wxmoments.find_account(config)
        _MOMENTS["account_info"] = account_info

        
        root = str(account_info.wxid_dir.parent)
        if str(config.get("wechat_data_root") or "").strip() != root:
            config["wechat_data_root"] = root
            wxmoments.save_config(wxmoments.DEFAULT_CONFIG, config)

        job.message = "获取数据库密钥…"
        key = _moments_acquire_key(account_info)

        job.message = "解密数据库…"
        account_dir = wxmoments.decrypt_databases(account_info, key)
        wxmoments.save_db_key(account_info, account_dir, key)
        _MOMENTS["account_dir"] = account_dir

        job.message = "读取朋友圈数据…"
        posts = wxmoments.load_timeline(account_dir, None)

        
        friends = {e.username for e in wxmoments.load_contact_entries(account_dir)}
        self_set: set[str] = set()
        for cand in wxmoments.self_username_candidates(account_info, config):
            self_set.update(wxmoments.self_username_variants(cand))
        kept = [p for p in posts
                if str(p.get("username") or "").strip() in friends
                or str(p.get("username") or "").strip() in self_set]
        posts = kept
        _MOMENTS["posts"] = posts

        job.done = 1
        job.status = "done"
        job.message = f"解密完成：{account_info.account} · 朋友圈 {len(posts)} 条"
        job.results.append({
            "account": account_info.account,
            "posts": len(posts),
        })
        job.add_log(job.message)
    except Exception as exc:  
        job.status = "error"
        job.message = f"初始化解密失败：{exc}"
        job.add_log(job.message)


def _moments_ready() -> Path:
    account_dir = _MOMENTS.get("account_dir")
    if not account_dir:
        raise RuntimeError("请先点击「初始化解密」")
    return Path(account_dir)


def _moments_filtered_posts(usernames: list[str] | None,
                            start, end) -> list[dict[str, Any]]:
    """从内存缓存过滤朋友圈（好友 + 时间）。
    usernames=None → 全部；[] → 0 条（什么都没选）。"""
    posts = _MOMENTS.get("posts") or []
    if usernames is not None:
        wanted = set(usernames)
        
        ai = _MOMENTS.get("account_info")
        if ai:
            for cand in wxmoments.self_username_candidates(ai, _moments_config()):
                if cand in wanted:
                    wanted.update(wxmoments.self_username_variants(cand))
        posts = [p for p in posts if str(p.get("username") or "") in wanted]
    if start or end:
        kept = []
        for p in posts:
            created = wxmoments.post_created_datetime(p)
            if created is None:
                continue
            if start and created < start:
                continue
            if end and created > end:
                continue
            kept.append(p)
        posts = kept
    return posts


@app.get("/api/moments/status")
async def api_moments_status():
    """当前会话是否已解密封装（供前端刷新后自动恢复列表）。"""
    ai = _MOMENTS.get("account_info")
    posts = _MOMENTS.get("posts") or []
    return {
        "ok": True,
        "ready": bool(ai),
        "account": ai.account if ai else "",
        "total": len(posts),
    }


@app.post("/api/moments/contacts")
async def api_moments_contacts(payload: dict):
    """好友列表 = 本机朋友圈数据里实际出现过的用户（仅聊天/无数据者不出现）。"""
    try:
        account_dir = _moments_ready()
        contacts = await run_in_threadpool(wxmoments.load_contact_entries, account_dir)
        wxmoments.write_contact_cache(account_dir, contacts)
        posts = _MOMENTS.get("posts") or []
        ai = _MOMENTS.get("account_info")

        
        self_variants: set[str] = set()
        if ai:
            for cand in wxmoments.self_username_candidates(ai, _moments_config()):
                self_variants.update(wxmoments.self_username_variants(cand))

        
        counts: dict[str, int] = {}
        for p in posts:
            u = str(p.get("username") or "").strip()
            if not u or u in self_variants:
                continue
            counts[u] = counts.get(u, 0) + 1

        names: dict[str, tuple[str, str, str]] = {}
        for c in contacts:
            names[c.username] = (c.remark, c.nickname, c.privacy_status)

        out: list[dict[str, Any]] = []
        if ai:
            out.append({"wxid": ai.account, "nickname": "我", "remark": "我", "privacy": ""})
        for u in sorted(counts, key=lambda x: counts[x], reverse=True):
            info = names.get(u)
            if info:
                out.append({"wxid": u, "nickname": info[1], "remark": info[0], "privacy": info[2]})
            
        return {"ok": True, "contacts": out}
    except Exception as exc:  
        return {"ok": False, "msg": str(exc)}



CONTACT_COLUMNS: dict[str, str] = {
    "remark": "备注名",
    "nickname": "实际名称",
    "wechat_id": "微信号",
    "region": "地区",
    "signature": "个性签名",
    "description": "备注",
    "wxid": "用户ID",
    "common_groups": "共同群数",
    "avatar": "头像链接",
}


@app.post("/api/contacts/all")
async def api_contacts_all(payload: dict):
    """全部当前好友的详细信息（"好友列表导出"页签）。"""
    try:
        account_dir = _moments_ready()
        details = await run_in_threadpool(wxmoments.load_contact_details, account_dir)
        return {"ok": True, "contacts": details, "total": len(details)}
    except Exception as exc:  
        return {"ok": False, "msg": str(exc)}


@app.post("/api/contacts/export")
async def api_contacts_export(payload: dict):
    """按所选列导出全部好友为 CSV（UTF-8 BOM，Excel 可直接打开）。"""
    try:
        account_dir = _moments_ready()
        requested = payload.get("columns") or []
        columns = [c for c in requested if c in CONTACT_COLUMNS]
        if not columns:
            columns = ["remark", "nickname", "wechat_id", "region", "signature"]
        details = await run_in_threadpool(wxmoments.load_contact_details, account_dir)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = OUTPUT_DIR / f"好友列表-{ts}.csv"
        with open(path, "w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow([CONTACT_COLUMNS[c] for c in columns])
            for item in details:
                row = []
                for c in columns:
                    if c == "remark":
                        
                        row.append(item.get("remark") or item.get("nickname") or item.get("wxid") or "")
                    elif c == "region":
                        row.append(" ".join(x for x in (
                            item.get("country"), item.get("province"), item.get("city")) if x))
                    else:
                        row.append(str(item.get(c) or ""))
                writer.writerow(row)
        return {"ok": True, "path": str(path), "count": len(details)}
    except Exception as exc:  
        return {"ok": False, "msg": str(exc)}


@app.post("/api/moments/timeline")
async def api_moments_timeline(payload: dict):
    try:
        account_dir = _moments_ready()
        raw_u = payload.get("usernames")
        usernames = raw_u if raw_u is not None else None
        start_raw = (payload.get("start") or "").strip()
        end_raw = (payload.get("end") or "").strip()
        start = wxmoments.parse_datetime(start_raw, end_of_day=False) if start_raw else None
        end = wxmoments.parse_datetime(end_raw, end_of_day=True) if end_raw else None
        if start and end and end < start:
            return {"ok": False, "msg": "结束日期必须晚于开始日期"}

        posts = _moments_filtered_posts(usernames, start, end)

        contacts = wxmoments.load_contact_entries(account_dir)
        names = wxmoments.build_contact_display_names(contacts)
        ai = _MOMENTS.get("account_info")
        if ai:
            names.setdefault(ai.account, "我")

        out: list[dict[str, Any]] = []
        for post in posts:
            created = wxmoments.post_created_datetime(post)
            if not created:
                continue
            username = str(post.get("username") or "").strip()
            display = (names.get(username)
                       or str(post.get("displayName") or "").strip()
                       or username or "未知")
            out.append({
                "createTime": int(post.get("createTime") or 0),
                "datetime": created.strftime("%Y-%m-%d %H:%M:%S"),
                "username": username,
                "display": display,
                "text": str(post.get("contentDesc") or "").strip()[:200],
                "title": str(post.get("title") or "").strip(),
                "url": str(post.get("contentUrl") or "").strip(),
                "location": str(post.get("location") or "").strip(),
                "mediaCount": len(post.get("media") or []),
            })
        out.sort(key=lambda d: int(d["createTime"]), reverse=True)
        return {"ok": True, "posts": out, "total": len(out)}
    except Exception as exc:  
        return {"ok": False, "msg": str(exc)}


@app.post("/api/moments/post_detail")
async def api_moments_post_detail(payload: dict):
    try:
        account_dir = _moments_ready()
        ct = int(payload.get("createTime") or 0)
        uname = str(payload.get("username") or "").strip()
        if not ct:
            return {"ok": False, "msg": "缺少 createTime"}
        posts = _MOMENTS.get("posts") or []
        post = next((p for p in posts
                     if int(p.get("createTime") or 0) == ct
                     and str(p.get("username") or "").strip() == uname), None)
        if post is None:
            return {"ok": False, "msg": "未找到该朋友圈，列表可能已刷新，请重试"}
        account_info = _MOMENTS.get("account_info")
        contacts = wxmoments.load_contact_entries(account_dir)
        names = wxmoments.build_contact_display_names(contacts)
        if account_info:
            names.setdefault(account_info.account, "我")
        username = str(post.get("username") or "").strip()
        display = (names.get(username)
                   or str(post.get("displayName") or "").strip()
                   or username or "未知")
        created = wxmoments.post_created_datetime(post)
        detail: dict[str, Any] = {
            "display": display,
            "datetime": created.strftime("%Y-%m-%d %H:%M:%S") if created else "",
            "text": str(post.get("contentDesc") or "").strip(),
            "location": str(post.get("location") or "").strip(),
            "title": str(post.get("title") or "").strip(),
            "url": str(post.get("contentUrl") or "").strip(),
            "likes": [],
            "comments": [],
            "media": [],
        }
        for item in (post.get("likes") or []):
            n = wxmoments.display_interaction_name(item, names)
            if n and n not in detail["likes"]:
                detail["likes"].append(n)
        for item in (post.get("comments") or []):
            if not isinstance(item, dict):
                continue
            author = wxmoments.display_interaction_person(
                item.get("username"), item.get("nickname"), names) or "未知好友"
            content = str(item.get("content") or "").replace("\xa0", " ").strip()
            if content:
                detail["comments"].append({"author": author, "content": content})
        for media in (post.get("media") or []):
            if not isinstance(media, dict):
                continue
            entry: dict[str, Any] = {"type": str(media.get("type") or "image"), "data": None}
            if account_info:
                try:
                    payload, mime, _src = await run_in_threadpool(
                        wxmoments.read_local_image,
                        account_info, Path(account_dir), post, media)
                    if payload:
                        entry["data"] = (f"data:{mime};base64,"
                                         + base64.b64encode(payload).decode())
                except Exception:
                    pass
            detail["media"].append(entry)
        return {"ok": True, "detail": detail}
    except Exception as exc:  
        return {"ok": False, "msg": str(exc)}


@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        return JSONResponse({"ok": False, "msg": "任务不存在"}, status_code=404)
    return {"ok": True, "job": job.to_dict()}


@app.post("/api/job/{job_id}/cancel")
async def api_job_cancel(job_id: str):
    job = _JOBS.get(job_id)
    if job:
        job.canceled = True
    return {"ok": True}


def _run_moments(job: Job, account_info: wxmoments.AccountInfo,
                 account_dir: Path, start, end, usernames,
                 keep_interactions: bool,
                 posts_override: list[dict[str, Any]] | None = None) -> None:
    """朋友圈导出后台任务：图片密钥 → 逐条导出（进度）→ 生成 PDF。"""
    try:
        config = _moments_config()
        output_root = Path(
            str(config.get("output_root") or "") or (BASE_DIR / "output")
        ).expanduser()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = output_root / f"朋友圈-{ts}"
        output.mkdir(parents=True, exist_ok=True)
        job.add_log(f"输出目录：{output}")

        job.message = "准备图片密钥…"
        asyncio.run(wxmoments.save_image_keys(
            account_info.account, account_info.wxid_dir, account_info.db_storage_dir))

        contacts = wxmoments.load_contact_entries(account_dir)
        contact_names = wxmoments.build_contact_display_names(contacts) if contacts else {}

        from wechat_decrypt_tool.modules.sns_media import (
            reset_sns_download_report, sns_download_report,
        )
        reset_sns_download_report()

        def cb(idx: int, total: int, display: str, time_text: str) -> None:
            if job.canceled:
                raise RuntimeError("已取消")
            job.done = idx
            job.total = total
            job.message = f"({idx}/{total}) {time_text} {display}"

        job.message = "导出朋友圈…"
        stats, exported = asyncio.run(wxmoments.export_markdown(
            account_info, account_dir, output, start, end, usernames, config,
            keep_interactions, contact_names,
            allow_download=True, progress_cb=cb, posts_override=posts_override,
        ))
        job.done = stats["posts"]

        job.message = "生成 PDF…"
        html_path = wxmoments.write_pdf_html(output, exported)
        try:
            wxmoments.render_pdf(output, exported, output / "moments.pdf")
        except Exception as exc:  
            job.add_log(f"PDF 生成失败（HTML 仍可用）：{exc}")

        download_report = sns_download_report()
        (output / "download_report.json").write_text(
            json.dumps(download_report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        job.results.append({
            "dir": str(output),
            "posts": stats["posts"],
            "images": stats["images"],
            "missing_images": stats["missing_images"],
            "md": str(output / "moments.md"),
            "html": str(html_path),
        })
        job.status = "done"
        job.message = f"完成：{output.name}（{stats['posts']} 条）"
        job.add_log(job.message)
    except Exception as exc:  
        if job.canceled:
            job.status = "canceled"
            job.message = "已取消"
        else:
            job.status = "error"
            job.message = f"导出失败：{exc}"
        job.add_log(job.message)



def _clear_stale_proxy() -> None:
    """启动时清理上次崩溃残留的系统代理（历史抓包方案遗留的兜底，防止用户莫名断网）。"""
    import winreg

    key_path = ("Software\\Microsoft\\Windows\\CurrentVersion\\"
                "Internet Settings")
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                             winreg.KEY_READ | winreg.KEY_SET_VALUE)
    except OSError:
        return
    try:
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        except OSError:
            enable = 0
        try:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except OSError:
            server = ""
        server = (server or "").replace(" ", "")
        if enable and server in (f"127.0.0.1:{PROXY_PORT}",
                                 f"localhost:{PROXY_PORT}"):
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
            try:
                winreg.DeleteValue(key, "ProxyServer")
            except OSError:
                pass
            print(f"  [提示] 已还原上次未清理的系统代理设置（{server}）")
    finally:
        winreg.CloseKey(key)


def _open_browser() -> None:
    time.sleep(1.2)
    webbrowser.open(f"http://{HOST}:{PORT}/")


def _port_busy() -> bool:
    import socket
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main() -> None:
    import uvicorn

    if _port_busy():
        webbrowser.open(f"http://{HOST}:{PORT}/")
        return

    _clear_stale_proxy()

    print(f"\n  WeChat Data Exporter →  http://{HOST}:{PORT}/\n")
    threading.Thread(target=_open_browser, daemon=True).start()
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    finally:
        _clear_stale_proxy()


if __name__ == "__main__":
    main()
