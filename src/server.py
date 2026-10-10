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
from contextlib import asynccontextmanager
import base64
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
os.environ["WECHAT_TOOL_ENABLE_CONSOLE_LOG"] = "0"

from wechat_download import exporter, harvest, wechat
from wechat_download.config import HOST, OUTPUT_DIR, PORT, PROXY_PORT, WEB_DIR
import wxmoments
from author_watch import AuthorWatch
from web_runtime import Job, JOBS as _JOBS, start_job, diagnostics, install_browser_logging
from wechat_decrypt_tool.modules.logging_config import get_logger

logger = get_logger(__name__)
install_browser_logging()


@asynccontextmanager
async def lifespan(app):
    yield
    for watch in _AUTHOR_WATCH.values():
        watch.stop()


app = FastAPI(title="微信数据导出工具", lifespan=lifespan)


_ARTICLE_CACHE: dict[str, wechat.Article] = {}


@app.middleware("http")
async def browser_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        logger.exception("请求失败：%s %s", request.method, request.url.path)
        return JSONResponse({"ok": False, "msg": f"操作失败：{exc}"}, status_code=500)


@app.get("/api/diagnostics")
async def api_diagnostics(after: int = 0):
    return diagnostics(after)


@app.get("/api/logs")
async def api_logs():
    snapshot = _MOMENTS
    posts = snapshot.get("posts") or []
    try:
        log_file = str(wxmoments.get_log_file_path())
    except Exception:
        log_file = str(BASE_DIR / "runtime" / "wxmoments.log")
    return {**diagnostics(), "ready": bool(snapshot.get("account_dir")),
            "log_file": log_file,
            "coverage": wxmoments.build_coverage_report(posts, posts, None, None)
            if snapshot.get("account_dir") else None}


@app.post("/api/logs/open")
async def api_logs_open():
    """用系统默认程序打开日志文件，方便查看完整记录。"""
    try:
        path = wxmoments.get_log_file_path()
    except Exception:
        path = BASE_DIR / "runtime" / "wxmoments.log"
    if not path.exists():
        return {"ok": False, "msg": f"日志文件不存在：{path}"}
    subprocess.Popen(["explorer", "/select,", str(path)])
    return {"ok": True}


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
        return await run_in_threadpool(_start_article_export, payload)
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
    job = start_job(1, _run_single, art)
    return {"ok": True, "job": job.id, "dir": str(OUTPUT_DIR)}


def _start_author_export(payload: dict) -> dict:
    articles = payload.get("articles") or []
    account = payload.get("account") or "公众号"
    mode = payload.get("mode") or "select"
    if not articles:
        return {"ok": False, "msg": "没有选择任何文章"}
    root = _batch_root(account, mode)
    job = start_job(len(articles), _run_batch, articles, account, mode,
                    payload.get("biz") or "")
    return {"ok": True, "job": job.id, "dir": str(root)}


def _start_moments_export(payload: dict) -> dict:
    with _INIT_LOCK:
        return _start_moments_export_locked(payload)


def _start_moments_export_locked(payload: dict) -> dict:
    global _MOMENTS_EXPORT_JOB
    if _INIT_JOB and _INIT_JOB.status == "running":
        return {"ok": False, "msg": "正在解密，请等待完成后再导出"}
    if _MOMENTS_EXPORT_JOB and _MOMENTS_EXPORT_JOB.status == "running":
        return {"ok": False, "msg": "已有朋友圈导出任务，请等待完成或取消"}
    try:
        account_info = _MOMENTS.get("account_info")
        account_dir = _MOMENTS.get("account_dir")
        if not account_info or not account_dir:
            return {"ok": False, "msg": "请先点击「重新解密」"}
        account_dir = Path(account_dir)

        usernames, start, end = _parse_moments_filters(payload)
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
            return {"ok": False, "msg": "所选条件下没有朋友圈"}

        job = start_job(len(filtered), _run_moments, account_info, account_dir,
                        start, end, usernames, keep_interactions, filtered)
        _MOMENTS_EXPORT_JOB = job
        return {"ok": True, "job": job.id, "dir": str(OUTPUT_DIR)}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


def _run_single(job: Job, art: wechat.Article) -> None:
    try:
        job.message = art.title
        job.add_log(f"开始导出：{art.title}")
        res = exporter.export_article(art, OUTPUT_DIR, job.add_log, job.check_canceled)
        job.done = 1
        job.results.append({
            "title": art.title, "dir": str(res.article_dir),
            "md": str(res.md_file), "html": str(res.html_file),
            "figures": len(res.figures), "failed": res.failed_images,
        })
        job.message = f"完成：{res.article_dir.name}"
        if res.failed_images:
            job.message += f"（{res.failed_images} 张图片下载失败）"
        job.add_log(f"已保存到 {res.article_dir}")
        job.finish("done", job.message)
    except Exception as e:
        job.finish("canceled" if job.canceled else "error",
                   "已取消" if job.canceled else f"导出失败：{e}")


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


_AUTHOR_WATCH: dict[str, AuthorWatch] = {}


@app.post("/api/author/watch/start")
async def api_author_watch_start(payload: dict):
    biz = (payload.get("biz") or "").strip()
    if not biz:
        return {"ok": False, "msg": "缺少公众号 ID"}
    watch = _AUTHOR_WATCH.setdefault(biz, AuthorWatch(biz, _load_article))
    if not watch.start():
        return {"ok": False, "msg": "上次收录正在停止，请稍后重试"}
    return watch.snapshot()


@app.post("/api/author/watch/status")
async def api_author_watch_status(payload: dict):
    watch = _AUTHOR_WATCH.get((payload.get("biz") or "").strip())
    return watch.snapshot() if watch else {"ok": True, "running": False,
                                           "total": 0, "articles": []}


@app.post("/api/author/watch/stop")
async def api_author_watch_stop(payload: dict):
    watch = _AUTHOR_WATCH.get((payload.get("biz") or "").strip())
    if watch:
        watch.stop()
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
            job.finish("canceled", "已取消")
            return
        job.message = f"({i}/{job.total}) {item.get('title', '')}"
        job.add_log(f"[{i}/{job.total}] {item.get('title', '')}")
        try:
            art = _load_article(item["url"], biz)
            res = exporter.export_article(art, root, job.add_log, job.check_canceled)
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
            if job.canceled:
                job.finish("canceled", "已取消")
                return
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

    succeeded = len(job.results)
    failed = job.total - succeeded
    job.message = f"导出结束：成功 {succeeded} 篇，失败 {failed} 篇"
    failed_images = sum(result.get("failed", 0) for result in job.results)
    if failed_images:
        job.message += f"，{failed_images} 张图片下载失败"
    job.finish("done" if succeeded else "error", job.message)


_MOMENTS: dict[str, Any] = {"account_info": None, "account_dir": None}
_INIT_LOCK = threading.Lock()
_INIT_JOB: Job | None = None
_MOMENTS_EXPORT_JOB: Job | None = None


def _parse_moments_filters(payload: dict):
    usernames = payload.get("usernames")
    if usernames is not None and (not isinstance(usernames, list)
                                 or not all(isinstance(u, str) for u in usernames)):
        raise ValueError("好友筛选必须是用户 ID 列表")
    start_raw = str(payload.get("start") or "").strip()
    end_raw = str(payload.get("end") or "").strip()
    start = wxmoments.parse_datetime(start_raw, end_of_day=False) if start_raw else None
    end = wxmoments.parse_datetime(end_raw, end_of_day=True) if end_raw else None
    if start and end and end < start:
        raise ValueError("结束日期必须晚于开始日期")
    return usernames, start, end


def _contact_names() -> dict[str, str]:
    return _MOMENTS.get("contact_names") or {}


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
    global _INIT_JOB
    with _INIT_LOCK:
        if _INIT_JOB and _INIT_JOB.status == "running":
            return {"ok": True, "job": _INIT_JOB.id}
        if _MOMENTS_EXPORT_JOB and _MOMENTS_EXPORT_JOB.status == "running":
            return {"ok": False, "msg": "正在导出朋友圈，请等待完成或取消"}
        _INIT_JOB = start_job(1, _run_moments_init)
        return {"ok": True, "job": _INIT_JOB.id}


def _run_moments_init(job: Job) -> None:
    global _MOMENTS
    try:
        job.message = "定位微信账号…"
        config = _moments_config()
        account_info = wxmoments.find_account(config)


        root = str(account_info.wxid_dir.parent)
        if str(config.get("wechat_data_root") or "").strip() != root:
            config["wechat_data_root"] = root
            wxmoments.save_config(wxmoments.DEFAULT_CONFIG, config)

        job.message = "获取数据库密钥…"
        key = _moments_acquire_key(account_info)

        job.message = "读取最新微信缓存…"
        account_dir = wxmoments.decrypt_databases(account_info, key)
        wxmoments.save_db_key(account_info, account_dir, key)

        job.message = "读取朋友圈数据…"
        posts = wxmoments.load_timeline(account_dir, None)


        contacts = wxmoments.load_contact_entries(account_dir)
        friends = {e.username for e in contacts}
        self_set: set[str] = set()
        for cand in wxmoments.self_username_candidates(account_info, config):
            self_set.update(wxmoments.self_username_variants(cand))
        kept = [p for p in posts
                if str(p.get("username") or "").strip() in friends
                or str(p.get("username") or "").strip() in self_set]
        posts = kept
        names = wxmoments.build_contact_display_names(contacts)
        for username in self_set:
            names.setdefault(username, "我")
        coverage = wxmoments.build_coverage_report(posts, posts, None, None)
        # Publish a complete snapshot only after every initialization step succeeds.
        _MOMENTS = {"account_info": account_info, "account_dir": account_dir,
                    "posts": posts, "contacts": contacts, "self_variants": self_set,
                    "contact_names": names, "coverage": coverage}
        for warning in coverage["warnings"]:
            job.add_log(warning)

        job.done = 1
        job.message = f"刷新完成：{account_info.account} · 朋友圈 {len(posts)} 条"
        job.results.append({
            "account": account_info.account,
            "posts": len(posts),
        })
        job.finish("done", job.message)
    except Exception as exc:
        job.finish("error", f"刷新失败：{exc}")


def _moments_ready() -> Path:
    account_dir = _MOMENTS.get("account_dir")
    if not account_dir:
        raise RuntimeError("请先点击「重新解密」")
    return Path(account_dir)


def _moments_filtered_posts(usernames: list[str] | None,
                            start, end) -> list[dict[str, Any]]:
    """从内存缓存过滤朋友圈（好友 + 时间）。
    usernames=None → 全部；[] → 0 条（什么都没选）。"""
    posts = _MOMENTS.get("posts") or []
    if usernames is not None:
        wanted = set(usernames)

        ai = _MOMENTS.get("account_info")
        if ai and ai.account in wanted:
            wanted.update(_MOMENTS.get("self_variants") or set())
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
        "ready": bool(ai and _MOMENTS.get("account_dir")),
        "init_job": _INIT_JOB.id if _INIT_JOB and _INIT_JOB.status == "running" else "",
        "coverage": _MOMENTS.get("coverage"),
        "account": ai.account if ai else "",
        "total": len(posts),
    }


@app.post("/api/moments/contacts")
async def api_moments_contacts(payload: dict):
    """好友列表 = 本机朋友圈数据里实际出现过的用户（仅聊天/无数据者不出现）。"""
    try:
        account_dir = _moments_ready()
        contacts = _MOMENTS.get("contacts") or []
        posts = _MOMENTS.get("posts") or []
        ai = _MOMENTS.get("account_info")


        self_variants = _MOMENTS.get("self_variants") or set()

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
        return {"ok": True, "contacts": details, "total": len(details),
                "columns": CONTACT_COLUMNS}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


@app.post("/api/contacts/export")
async def api_contacts_export(payload: dict):
    """按所选列导出全部好友为 CSV（UTF-8 BOM，Excel 可直接打开）。"""
    try:
        account_dir = _moments_ready()
        requested = payload.get("columns") or []
        columns = list(dict.fromkeys(c for c in requested if c in CONTACT_COLUMNS))
        if not columns:
            return {"ok": False, "msg": "请至少选择一个有效的导出列"}
        details = await run_in_threadpool(wxmoments.load_contact_details, account_dir)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
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
        usernames, start, end = _parse_moments_filters(payload)
        posts = _moments_filtered_posts(usernames, start, end)
        names = _contact_names()
        coverage = wxmoments.build_coverage_report(_MOMENTS.get("posts") or [], posts, start, end)

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
        return {"ok": True, "posts": out, "total": len(out), "coverage": coverage}
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
            return {"ok": False, "msg": "未找到该朋友圈，请重试"}
        account_info = _MOMENTS.get("account_info")
        names = _contact_names()
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
    if not job:
        return JSONResponse({"ok": False, "msg": "任务不存在"}, status_code=404)
    if job.status == "running":
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

        job.check_canceled()
        job.message = "准备图片密钥…"
        asyncio.run(wxmoments.save_image_keys(
            account_info.account, account_info.wxid_dir, account_info.db_storage_dir))

        contact_names = _contact_names()

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
            coverage_posts=_MOMENTS.get("posts") or [],
        ))
        job.done = stats["posts"]

        job.check_canceled()
        job.message = "生成 PDF…"
        html_path = wxmoments.write_pdf_html(output, exported)
        try:
            wxmoments.render_pdf(output, exported, output / "moments.pdf")
        except Exception as exc:
            job.add_log(f"PDF 生成失败（HTML 仍可用）：{exc}")

        job.check_canceled()
        download_report = sns_download_report()
        job.add_log(wxmoments.summarize_download_report(download_report))
        if stats["missing_images"]:
            job.add_log(f"有 {stats['missing_images']} 张图片未能获取，详见下载报告")
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
        job.message = f"完成：{output.name}（{stats['posts']} 条）"
        if stats["missing_images"]:
            job.message += f"，{stats['missing_images']} 张图片缺失"
        job.finish("done", job.message)
    except Exception as exc:
        job.finish("canceled" if job.canceled else "error",
                   "已取消" if job.canceled else f"导出失败：{exc}")


def _clear_stale_proxy() -> None:
    """启动时清理上次崩溃残留的系统代理（历史抓包方案遗留的兜底，防止用户莫名断网）。"""
    if sys.platform != "win32":
        return
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
            logger.warning("已清理旧版抓包遗留的系统代理：%s", server)
    finally:
        winreg.CloseKey(key)


def _open_browser() -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _running_service():
            webbrowser.open(f"http://{HOST}:{PORT}/")
            return
        time.sleep(0.2)
    logger.warning("浏览器未自动打开，请访问 http://%s:%s/", HOST, PORT)


def _running_service() -> bool:
    from urllib.request import urlopen
    try:
        with urlopen(f"http://{HOST}:{PORT}/api/moments/status", timeout=1) as response:
            status = json.load(response)
        return status.get("ok") is True and isinstance(status.get("ready"), bool)
    except Exception:
        return False


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
        if _running_service():
            webbrowser.open(f"http://{HOST}:{PORT}/")
            return
        raise RuntimeError(f"端口 {PORT} 被其他程序占用，请关闭占用程序后重试。")

    _clear_stale_proxy()

    threading.Thread(target=_open_browser, daemon=True).start()
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning",
                    log_config=None, access_log=False)
    finally:
        _clear_stale_proxy()


if __name__ == "__main__":
    main()
