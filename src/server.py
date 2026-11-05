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
import uuid
from contextlib import asynccontextmanager
import base64
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
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
import wechat_labels
import wxchat
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
        response = await call_next(request)
    except Exception as exc:
        logger.exception("请求失败：%s %s", request.method, request.url.path)
        return JSONResponse({"ok": False, "msg": f"操作失败：{exc}"}, status_code=500)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


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
    """在资源管理器里打开导出目录；只允许 output 下的路径。"""
    root = OUTPUT_DIR.resolve()
    raw = str((payload or {}).get("path") or "").strip()
    path = Path(raw) if raw else root
    if not path.is_absolute():
        # 兼容前端传 "output/xxx" 这种相对项目根的写法
        candidate = root / path
        if not candidate.exists() and (BASE_DIR / path).exists():
            candidate = BASE_DIR / path
        path = candidate
    try:
        path = path.resolve()
    except Exception:
        path = root
    if path != root and root not in path.parents:
        path = root
    if not path.exists():
        return {"ok": False, "msg": f"目录不存在：{path}"}
    try:
        subprocess.Popen(["explorer", str(path)])
        return {"ok": True, "path": str(path)}
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
    if kind == "moments_group":
        return _start_moments_group_export(payload)
    if kind == "chat":
        return wxchat.start_export(payload, start_job=start_job, output_root=_output_root())
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
            return {"ok": False, "msg": "请先点击「刷新」"}
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


def _start_moments_group_export(payload: dict) -> dict:
    with _INIT_LOCK:
        return _start_moments_group_export_locked(payload)


def _start_moments_group_export_locked(payload: dict) -> dict:
    global _MOMENTS_EXPORT_JOB
    if _INIT_JOB and _INIT_JOB.status == "running":
        return {"ok": False, "msg": "正在解密，请等待完成后再导出"}
    if _MOMENTS_EXPORT_JOB and _MOMENTS_EXPORT_JOB.status == "running":
        return {"ok": False, "msg": "已有朋友圈导出任务，请等待完成或取消"}
    try:
        account_info = _MOMENTS.get("account_info")
        account_dir = _MOMENTS.get("account_dir")
        if not account_info or not account_dir:
            return {"ok": False, "msg": "请先点击「刷新」"}

        gid = str(payload.get("id") or "").strip()
        contact_names = _contact_names()
        label_record: dict[str, Any] | None = None
        if wechat_labels.is_label_group_id(gid):
            store = wechat_labels.load_store(_labels_path())
            records = wechat_labels.label_records(store, _current_account())
            label_id = wechat_labels.label_id_from_group(gid)
            label_record = next((r for r in records if str(r.get("id")) == label_id), None)
            if label_record is None:
                return {"ok": False, "msg": "标签不存在"}
            group = wechat_labels.export_group(label_record, records=records,
                                               contact_names=contact_names)
            group["split"] = bool(payload.get("split"))
            group["keep_interactions"] = bool(payload.get("keep_interactions"))
            if not group.get("members"):
                return {"ok": False, "msg": "标签里没有好友"}
            if group.get("status") not in ("ok", "empty") and not payload.get("allow_partial"):
                return {
                    "ok": False,
                    "need_confirm": True,
                    "status": group.get("status"),
                    "msg": f"标签「{group['name']}」只读到 {len(group['members'])} 人，仍要导出？",
                    "matched": len(group["members"]),
                }
        else:
            group = next((g for g in _group_records() if g.get("id") == gid), None)
            if group is None:
                return {"ok": False, "msg": "分组不存在，请重新保存"}
        if not group.get("members"):
            return {"ok": False, "msg": "该分组没有好友"}

        folder = str(group.get("folder") or "") or (
            "朋友圈-" + wxmoments.sanitize_folder_name(str(group.get("name") or "")))
        job = start_job(1, _run_moments_group, account_info, Path(account_dir), group, folder)
        _MOMENTS_EXPORT_JOB = job
        return {"ok": True, "job": job.id, "dir": str(_output_root() / folder)}
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
    "微信缓存里还没有这个公众号的文章。\n"
    "请在微信里打开该公众号的「历史消息」，逐篇点开。"
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
        return {"ok": False, "msg": "收录正在停止，稍后再试"}
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
    if mode == "all":
        return OUTPUT_DIR / f"{exporter.safe_name(account, 30)}-{datetime.now():%Y%m%d}"
    return OUTPUT_DIR


def _run_batch(job: Job, articles: list[dict], account: str, mode: str,
               biz: str = "") -> None:
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
_LABEL_LOCK = threading.Lock()
_LABEL_JOB: Job | None = None
_LABEL_SYNC_LOCK = threading.Lock()


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


def _output_root() -> Path:
    config = _moments_config()
    return Path(
        str(config.get("output_root") or "") or (BASE_DIR / "output")
    ).expanduser()


def _groups_path() -> Path:
    return _output_root() / "moments_groups.json"


def _current_account() -> str:
    ai = _MOMENTS.get("account_info")
    return ai.account if ai else ""


def _group_records() -> list[dict[str, Any]]:
    data = wxmoments.load_groups(_groups_path())
    account = _current_account()
    return [g for g in data["groups"]
            if isinstance(g, dict) and str(g.get("account") or "") == account]


def _save_group_records(records: list[dict[str, Any]]) -> None:
    data = wxmoments.load_groups(_groups_path())
    account = _current_account()
    others = [g for g in data["groups"]
              if not (isinstance(g, dict) and str(g.get("account") or "") == account)]
    data["groups"] = others + records
    wxmoments.save_groups(_groups_path(), data)


def _touch_group_export(group_id: str) -> None:
    records = _group_records()
    for g in records:
        if g.get("id") == group_id:
            g["last_export"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            g["last_seen_posts"] = _group_total_posts(g)
            break
    else:
        return
    _save_group_records(records)


def _mark_label_export(label_id: str, total_posts: int) -> None:
    with _LABEL_LOCK:
        store = wechat_labels.load_store(_labels_path())
        record = next((r for r in wechat_labels.label_records(store, _current_account())
                       if str(r.get("id")) == str(label_id)), None)
        if record is None:
            return
        wechat_labels.mark_exported(record, total_posts)
        wechat_labels.save_store(_labels_path(), store)


def _group_total_posts(group: dict) -> int:
    total = 0
    for m in group.get("members") or []:
        if not isinstance(m, dict):
            continue
        wxid = str(m.get("wxid") or "").strip()
        if wxid:
            total += len(_moments_filtered_posts([wxid], None, None))
    return total


def _labels_path() -> Path:
    return _output_root() / "moments_labels.json"


def _label_records() -> list[dict[str, Any]]:
    return wechat_labels.label_records(wechat_labels.load_store(_labels_path()), _current_account())


def _post_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    self_variants = _MOMENTS.get("self_variants") or set()
    for post in _MOMENTS.get("posts") or []:
        username = str(post.get("username") or "").strip()
        if not username or username in self_variants:
            continue
        counts[username] = counts.get(username, 0) + 1
    return counts


def _label_views() -> dict[str, Any]:
    account = _current_account()
    if not account:
        return {"labels": [], "label_state": {"available": False, "reason": "请先点击「刷新」"}}
    store = wechat_labels.load_store(_labels_path())
    records = wechat_labels.label_records(store, account)
    meta = wechat_labels.account_record(store, account).get("definitions") or {}
    names = _contact_names()
    counts = _post_counts()
    views = [wechat_labels.label_view(record, records=records, contact_names=names,
                                     post_counts=counts) for record in records]
    notices = []
    for view in views:
        if view.get("notice"):
            notices.append({"id": view["id"], "kind": "pending", "name": view["name"],
                            "text": view["notice"], "added": view["pending"]["added"],
                            "removed": view["pending"]["removed"],
                            "edited": view.get("edited_locally")})
        elif view.get("deleted_notice"):
            notices.append({"id": view["id"], "kind": "deleted", "name": view["name"],
                            "text": view["deleted_notice"]})
    return {
        "labels": views,
        "label_notices": notices,
        "label_state": {
            "available": bool(views),
            "member_source": meta.get("membership") or "none",
            "updated": str(meta.get("read_at") or ""),
            "reason": "" if views else "还没有读到微信标签，点击「刷新」后再试",
        },
    }


def _refresh_label_definitions(account: str, account_dir: Path,
                               contacts: list[Any],
                               definitions: Any = None) -> dict[str, Any]:
    try:
        if definitions is None:
            definitions = wechat_labels.read_label_definitions(Path(account_dir))
    except Exception as exc:
        logger.warning("读取微信标签定义失败：%s", exc)
        return {"ok": False, "reason": str(exc), "count": 0}
    if not definitions.available:
        logger.info("未读取到微信标签定义：%s", definitions.reason)
        return {"ok": False, "reason": definitions.reason, "count": 0}
    with _LABEL_LOCK:
        store = wechat_labels.load_store(_labels_path())
        merged = wechat_labels.merge_definitions(store, account, definitions, contacts)
        db = wechat_labels.apply_db_membership(store, account, definitions, contacts)
        wechat_labels.save_store(_labels_path(), store)
    return {
        "ok": bool(merged.get("ok")),
        "count": int(merged.get("count") or 0),
        "membership": definitions.membership,
        "imported": int(db.get("imported") or 0),
        "imported_names": merged.get("imported") or [],
        "removed_names": merged.get("removed") or [],
        "renamed": merged.get("renamed") or [],
        "pending": db.get("pending") or [],
        "reason": "" if merged.get("ok") else str(merged.get("reason") or ""),
    }


def _moments_acquire_key(account_info: wxmoments.AccountInfo) -> str:
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
        account_dir, decrypt_stale = wxmoments.decrypt_databases(account_info, key)
        wxmoments.save_db_key(account_info, account_dir, key)
        if decrypt_stale:
            job.add_log(decrypt_stale)

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
        _MOMENTS = {"account_info": account_info, "account_dir": account_dir,
                    "posts": posts, "contacts": contacts, "self_variants": self_set,
                    "contact_names": names, "coverage": coverage}
        for warning in coverage["warnings"]:
            job.add_log(warning)

        label_info = _refresh_label_definitions(account_info.account, account_dir, contacts)
        if label_info.get("ok"):
            job.add_log(f"微信标签：{label_info.get('count')} 个")
            for name in label_info.get("imported_names") or []:
                job.add_log(f"新标签「{name}」已导入")
            for name in label_info.get("removed_names") or []:
                job.add_log(f"标签「{name}」已在微信删除")
            for item in label_info.get("pending") or []:
                detail = []
                if item.get("added"):
                    detail.append(f"增添了 {'、'.join(item['added'])}")
                if item.get("removed"):
                    detail.append(f"删除了 {'、'.join(item['removed'])}")
                if detail:
                    job.add_log(f"「{item.get('name')}」{'，'.join(detail)}")
            if label_info.get("membership") != "db":
                _start_label_autosync(job, account_info.account, Path(account_dir))
        elif label_info.get("reason"):
            job.add_log(f"标签未读取：{label_info['reason']}")

        job.done = 1
        job.message = f"刷新完成：{account_info.account} · 朋友圈 {len(posts)} 条"
        job.results.append({
            "account": account_info.account,
            "posts": len(posts),
            "labels": int(label_info.get("count") or 0),
        })
        job.finish("done", job.message)
    except Exception as exc:
        job.finish("error", f"刷新失败：{exc}")


def _moments_ready() -> Path:
    account_dir = _MOMENTS.get("account_dir")
    if not account_dir:
        raise RuntimeError("请先点击「刷新」")
    return Path(account_dir)


def _moments_filtered_posts(usernames: list[str] | None,
                            start, end) -> list[dict[str, Any]]:
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
    ai = _MOMENTS.get("account_info")
    posts = _MOMENTS.get("posts") or []
    return {
        "ok": True,
        "ready": bool(ai and _MOMENTS.get("account_dir")),
        "init_job": _INIT_JOB.id if _INIT_JOB and _INIT_JOB.status == "running" else "",
        "label_job": _LABEL_JOB.id if _LABEL_JOB and _LABEL_JOB.status == "running" else "",
        "coverage": _MOMENTS.get("coverage"),
        "account": ai.account if ai else "",
        "total": len(posts),
    }


@app.post("/api/moments/contacts")
async def api_moments_contacts(payload: dict):
    try:
        _moments_ready()
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


def _group_view(record: dict[str, Any]) -> dict[str, Any]:
    names = _contact_names()
    members = []
    for m in record.get("members") or []:
        if not isinstance(m, dict):
            continue
        wxid = str(m.get("wxid") or "").strip()
        if not wxid:
            continue
        members.append({"wxid": wxid, "name": names.get(wxid) or m.get("name") or wxid})
    total = _group_total_posts(record)
    return {
        "id": str(record.get("id") or ""),
        "name": str(record.get("name") or ""),
        "folder": str(record.get("folder") or ""),
        "keep_interactions": bool(record.get("keep_interactions")),
        "split": bool(record.get("split", False)),
        "members": members,
        "total_posts": total,
        "new_posts": max(0, total - int(record.get("last_seen_posts") or 0)),
        "last_export": str(record.get("last_export") or ""),
    }


@app.post("/api/moments/groups")
async def api_moments_groups(payload: dict):
    try:
        return {"ok": True, "groups": [_group_view(g) for g in _group_records()],
                **_label_views()}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


def _labels_sync_enabled() -> bool:
    return _moments_config().get("wechat_label_uia", True) is not False


def _start_label_autosync(job: Job, account: str, account_dir: Path) -> Job | None:
    global _LABEL_JOB
    if not _labels_sync_enabled():
        return None
    if _LABEL_JOB and _LABEL_JOB.status == "running":
        return _LABEL_JOB
    try:
        records = wechat_labels.label_records(wechat_labels.load_store(_labels_path()), account)
        pending = [str(r.get("id")) for r in records if wechat_labels.needs_sync(r, auto=True)]
    except Exception as exc:
        job.add_log(f"标签成员自动同步跳过：{exc}")
        return None
    if not pending:
        return None
    job.add_log(f"正在自动读取 {len(pending)} 个微信标签的成员…")
    _LABEL_JOB = start_job(len(pending), _run_label_sync, account, Path(account_dir), pending)
    return _LABEL_JOB


def _label_total_posts(record: dict[str, Any]) -> int:
    counts = _post_counts()
    return sum(counts.get(str(m.get("wxid") or ""), 0) for m in record.get("members") or [])


def _refresh_contact_snapshot(account_info: Any, account_dir: Path) -> tuple[Path, str]:
    if account_info is None:
        return account_dir, ""
    try:
        key = _moments_acquire_key(account_info)
        fresh, stale = wxmoments.decrypt_contact_db(account_info, key)
    except Exception as exc:
        logger.warning("重新解密通讯录失败：%s", exc)
        return account_dir, str(exc)
    try:
        contacts = wxmoments.load_contact_entries(fresh)
    except Exception as exc:
        logger.warning("重新读取好友名单失败：%s", exc)
        contacts = []
    if contacts:
        names = wxmoments.build_contact_display_names(contacts)
        for username in _MOMENTS.get("self_variants") or set():
            names.setdefault(username, "我")
        _MOMENTS["contacts"] = contacts
        _MOMENTS["contact_names"] = names
    _MOMENTS["account_dir"] = fresh
    return fresh, stale


@app.post("/api/moments/labels/sync")
async def api_moments_labels_sync(payload: dict):
    global _LABEL_JOB
    try:
        account = _current_account()
        account_dir = _MOMENTS.get("account_dir")
        if not account or not account_dir:
            return {"ok": False, "msg": "请先点击「刷新」"}
        if not _labels_sync_enabled():
            return {"ok": False, "msg": "配置中已关闭标签同步"}
        if _LABEL_JOB and _LABEL_JOB.status == "running":
            return {"ok": True, "job": _LABEL_JOB.id}
        if _INIT_JOB and _INIT_JOB.status == "running":
            return {"ok": False, "msg": "正在刷新，稍后再试"}
        if _MOMENTS_EXPORT_JOB and _MOMENTS_EXPORT_JOB.status == "running":
            return {"ok": False, "msg": "正在导出朋友圈，请等待完成或取消"}

        records = [r for r in _label_records() if r.get("present", True)]
        target = str(payload.get("id") or "").strip()
        if wechat_labels.is_label_group_id(target):
            only = [wechat_labels.label_id_from_group(target)]
        elif payload.get("all"):
            only = [str(r.get("id")) for r in records]
        else:
            only = [str(r.get("id")) for r in records
                    if r.get("status") in (wechat_labels.STATUS_NEVER, wechat_labels.STATUS_ERROR)]

        fresh_dir, stale_reason = await run_in_threadpool(
            _refresh_contact_snapshot, _MOMENTS.get("account_info"), Path(account_dir))
        definitions = wechat_labels.read_label_definitions(fresh_dir)
        if not definitions.available:
            return {"ok": False, "msg": definitions.reason or "没能读取微信标签"}
        if definitions.membership == "db":
            info = _refresh_label_definitions(account, fresh_dir,
                                              _MOMENTS.get("contacts") or [],
                                              definitions=definitions)
            pending = info.get("pending") or []
            return {"ok": True, "mode": "db", "labels": info.get("count"),
                    "imported": info.get("imported"),
                    "pending": len(pending), "changed": len(pending),
                    "imported_names": info.get("imported_names") or [],
                    "removed_names": info.get("removed_names") or [],
                    "renamed": info.get("renamed") or [],
                    "stale": stale_reason}
        if not only:
            return {"ok": True, "mode": "uia", "job": "", "count": 0,
                    "msg": "微信标签没有变化"}
        _LABEL_JOB = start_job(len(only), _run_label_sync, account, fresh_dir, only)
        return {"ok": True, "job": _LABEL_JOB.id, "count": len(only)}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


def _run_label_sync(job: Job, account: str, account_dir: Path, only_ids: list[str]) -> None:
    from wechat_uia import UiaUnavailable, WechatLabelReader

    if not _LABEL_SYNC_LOCK.acquire(blocking=False):
        job.finish("error", "已有标签同步正在进行，请稍候")
        return
    try:
        contacts = _MOMENTS.get("contacts") or []
        job.message = "正在连接微信窗口…"
        with WechatLabelReader(log=job.add_log, cancel=lambda: job.canceled,
                               cache_dir=_labels_path().parent) as reader:
            store = wechat_labels.load_store(_labels_path())
            result = wechat_labels.sync_members(
                store, account, contacts, reader, only_ids=only_ids,
                log=job.add_log, progress=lambda done: setattr(job, "done", done),
                canceled=lambda: job.canceled)
            with _LABEL_LOCK:
                wechat_labels.save_store(_labels_path(), store)
        if job.canceled:
            job.finish("canceled", "已取消标签同步")
            return
        if not result.get("ok") and not (result.get("synced") or result.get("partial")):
            job.finish("error", str(result.get("msg") or "标签同步失败"))
            return
        job.done = job.total
        job.results.append({"synced": result.get("synced", 0),
                            "partial": result.get("partial", 0),
                            "failed": result.get("failed", 0)})
        job.finish("done", f"标签同步完成：完整 {result.get('synced', 0)} 个 · "
                           f"不完整 {result.get('partial', 0)} 个 · "
                           f"失败 {result.get('failed', 0)} 个")
    except UiaUnavailable as exc:
        job.add_log(f"标签同步不可用：{exc}")
        job.finish("error", str(exc))
    except Exception as exc:
        job.finish("error", f"标签同步失败：{exc}")
    finally:
        _LABEL_SYNC_LOCK.release()


@app.post("/api/moments/labels/seen")
async def api_moments_labels_seen(payload: dict):
    try:
        label_id = wechat_labels.label_id_from_group(payload.get("id"))
        if not label_id:
            return {"ok": False, "msg": "缺少标签 ID"}
        with _LABEL_LOCK:
            store = wechat_labels.load_store(_labels_path())
            record = next((r for r in wechat_labels.label_records(store, _current_account())
                           if str(r.get("id")) == label_id), None)
            if record is None:
                return {"ok": False, "msg": "标签不存在"}
            wechat_labels.mark_seen(record, _label_total_posts(record))
            wechat_labels.save_store(_labels_path(), store)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


def _find_label(store: dict[str, Any], label_id: str) -> dict[str, Any] | None:
    for record in wechat_labels.label_records(store, _current_account()):
        if str(record.get("id")) == str(label_id):
            return record
    return None


@app.post("/api/moments/labels/save")
async def api_moments_labels_save(payload: dict):
    try:
        if not _current_account():
            return {"ok": False, "msg": "请先点击「刷新」"}
        label_id = wechat_labels.label_id_from_group(payload.get("id"))
        if not label_id:
            return {"ok": False, "msg": "缺少标签 ID"}
        usernames = payload.get("usernames")
        if not isinstance(usernames, list) or not all(isinstance(u, str) for u in usernames):
            return {"ok": False, "msg": "好友选择无效"}
        usernames = [u.strip() for u in usernames if str(u).strip()]
        if not usernames:
            return {"ok": False, "msg": "请至少选择一位好友"}
        contacts = _MOMENTS.get("contacts") or []
        with _LABEL_LOCK:
            store = wechat_labels.load_store(_labels_path())
            record = _find_label(store, label_id)
            if record is None:
                return {"ok": False, "msg": "标签不存在"}
            name = str(payload.get("name") or "").strip()
            if name:
                record["name"] = name
            count = wechat_labels.save_local_members(record, usernames, contacts)
            store["accounts"][_current_account()]["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            wechat_labels.save_store(_labels_path(), store)
        return {"ok": True, "count": count}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


@app.post("/api/moments/labels/delete")
async def api_moments_labels_delete(payload: dict):
    try:
        if not _current_account():
            return {"ok": False, "msg": "请先点击「刷新」"}
        label_id = wechat_labels.label_id_from_group(payload.get("id"))
        if not label_id:
            return {"ok": False, "msg": "缺少标签 ID"}
        with _LABEL_LOCK:
            store = wechat_labels.load_store(_labels_path())
            account = _current_account()
            before = len(wechat_labels.label_records(store, account))
            wechat_labels.account_record(store, account)["labels"] = [
                item for item in wechat_labels.label_records(store, account)
                if str(item.get("id")) != str(label_id)]
            after = len(wechat_labels.label_records(store, account))
            if after == before:
                return {"ok": False, "msg": "标签不存在"}
            wechat_labels.save_store(_labels_path(), store)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


@app.post("/api/moments/labels/apply")
async def api_moments_labels_apply(payload: dict):
    try:
        if not _current_account():
            return {"ok": False, "msg": "请先点击「刷新」"}
        label_id = wechat_labels.label_id_from_group(payload.get("id"))
        if not label_id:
            return {"ok": False, "msg": "缺少标签 ID"}
        mode = "ignore" if str(payload.get("mode") or "").lower() == "ignore" else "sync"
        with _LABEL_LOCK:
            store = wechat_labels.load_store(_labels_path())
            record = _find_label(store, label_id)
            if record is None:
                return {"ok": False, "msg": "标签不存在"}
            pending = record.get("pending") or {}
            if not pending.get("added") and not pending.get("removed"):
                return {"ok": False, "msg": "这个标签在微信里没有新变化"}
            wechat_labels.apply_pending(record, mode=mode)
            wechat_labels.save_store(_labels_path(), store)
        return {"ok": True, "mode": mode}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


@app.post("/api/moments/labels/convert")
async def api_moments_labels_convert(payload: dict):
    try:
        if not _current_account():
            return {"ok": False, "msg": "请先点击「刷新」"}
        label_id = wechat_labels.label_id_from_group(payload.get("id"))
        if not label_id:
            return {"ok": False, "msg": "缺少标签 ID"}
        with _LABEL_LOCK:
            store = wechat_labels.load_store(_labels_path())
            record = _find_label(store, label_id)
            if record is None:
                return {"ok": False, "msg": "标签不存在"}
            name = str(record.get("name") or "标签").strip() or "标签"
            usernames = [str(m.get("wxid") or "") for m in record.get("members") or []
                         if str(m.get("wxid") or "").strip()]
            account = _current_account()
            wechat_labels.account_record(store, account)["labels"] = [
                item for item in wechat_labels.label_records(store, account)
                if str(item.get("id")) != str(label_id)]
            wechat_labels.save_store(_labels_path(), store)
        if not usernames:
            return {"ok": False, "msg": "这个标签没有成员，已从列表移除"}
        return _save_group_payload({"name": name, "usernames": usernames})
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


@app.post("/api/moments/groups/save")
async def api_moments_groups_save(payload: dict):
    return _save_group_payload(payload)


def _save_group_payload(payload: dict) -> dict:
    try:
        if not _current_account():
            return {"ok": False, "msg": "请先点击「刷新」"}
        name = str(payload.get("name") or "").strip()
        if not name:
            return {"ok": False, "msg": "请填写分组名称"}
        usernames = payload.get("usernames") or []
        if not isinstance(usernames, list) or not all(isinstance(u, str) for u in usernames):
            return {"ok": False, "msg": "好友选择无效"}
        usernames = [u.strip() for u in usernames if str(u).strip()]
        if not usernames:
            return {"ok": False, "msg": "请至少选择一位好友"}
        names = _contact_names()

        records = _group_records()
        gid = str(payload.get("id") or "").strip()
        target = next((g for g in records if g.get("id") == gid), None) if gid else None
        if gid and target is None:
            return {"ok": False, "msg": "分组不存在，可能已被删除"}
        if target is None:
            target = {
                "id": uuid.uuid4().hex[:12],
                "account": _current_account(),
                "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "last_export": "",
            }
            records.append(target)
        target["name"] = name
        folder = "朋友圈-" + wxmoments.sanitize_folder_name(name)
        clash = next((g for g in records if g is not target and g.get("folder") == folder), None)
        if clash:
            return {"ok": False,
                    "msg": f"已有分组「{clash.get('name')}」用同一个导出文件夹「{folder}」，请换个名字"}
        target["folder"] = folder
        target["keep_interactions"] = bool(payload.get("keep_interactions"))
        split = payload.get("split")
        target["split"] = False if split is None else bool(split)
        target["members"] = [{"wxid": u, "name": names.get(u) or u} for u in usernames]
        target["last_seen_posts"] = _group_total_posts(target)
        _save_group_records(records)
        return {"ok": True, "group": _group_view(target)}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


@app.post("/api/moments/groups/seen")
async def api_moments_groups_seen(payload: dict):
    try:
        gid = str(payload.get("id") or "").strip()
        if not gid:
            return {"ok": False, "msg": "缺少分组 ID"}
        records = _group_records()
        for g in records:
            if g.get("id") == gid:
                g["last_seen_posts"] = _group_total_posts(g)
                break
        else:
            return {"ok": False, "msg": "分组不存在"}
        _save_group_records(records)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


@app.post("/api/moments/groups/delete")
async def api_moments_groups_delete(payload: dict):
    try:
        gid = str(payload.get("id") or "").strip()
        records = _group_records()
        kept = [g for g in records if g.get("id") != gid]
        if len(kept) == len(records):
            return {"ok": False, "msg": "分组不存在"}
        _save_group_records(kept)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


CHATLIST_CONTACT_COLUMNS: dict[str, str] = {
    "remark": "备注名",
    "nickname": "实际名称",
    "wechat_id": "微信号",
    "region": "地区",
    "signature": "个性签名",
    "description": "备注",
    "wxid": "用户ID",
    "common_groups": "共同群数",
    "pinned": "置顶",
    "muted": "免打扰",
    "avatar": "头像链接",
}

CHATLIST_GROUP_COLUMNS: dict[str, str] = {
    "name": "群名称",
    "remark": "备注名",
    "pinned": "置顶",
    "folded": "折叠",
    "owner": "群主",
    "member_count": "成员数",
    "members": "群成员",
    "announcement": "群公告",
    "muted": "免打扰",
    "in_room": "在群内",
    "room_id": "群ID",
    "avatar": "群头像",
}


def _chatlist_cell(item: dict[str, Any], column: str) -> str:
    value = item.get(column)
    if column == "remark":
        if value:
            return str(value)
        if "room_id" in item:
            return ""      # 群备注多为空，留白比重复群名更清楚
        return str(item.get("nickname") or item.get("wxid") or "")
    if column == "region":
        return " ".join(x for x in (item.get("country"), item.get("province"),
                                    item.get("city")) if x)
    if column == "members":
        return "、".join(value or [])
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value or "")


@app.post("/api/chatlist/all")
async def api_chatlist_all(payload: dict):
    try:
        account_dir = _moments_ready()
        contacts = await run_in_threadpool(wxmoments.load_contact_details, account_dir)
        groups = await run_in_threadpool(wxmoments.load_group_details, account_dir)
        return {"ok": True, "contacts": contacts, "groups": groups,
                "contact_columns": CHATLIST_CONTACT_COLUMNS,
                "group_columns": CHATLIST_GROUP_COLUMNS}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


@app.post("/api/chatlist/export")
async def api_chatlist_export(payload: dict):
    try:
        account_dir = _moments_ready()
        kind = str(payload.get("kind") or "contacts").strip()
        if kind not in ("contacts", "groups"):
            return {"ok": False, "msg": "未知的导出类型"}
        columns_def = (CHATLIST_CONTACT_COLUMNS if kind == "contacts"
                       else CHATLIST_GROUP_COLUMNS)
        requested = payload.get("columns") or []
        columns = list(dict.fromkeys(c for c in requested if c in columns_def))
        if not columns:
            return {"ok": False, "msg": "请至少选择一个有效的导出列"}
        if kind == "contacts":
            items = await run_in_threadpool(wxmoments.load_contact_details, account_dir)
            prefix = "好友列表"
        else:
            items = await run_in_threadpool(wxmoments.load_group_details, account_dir)
            prefix = "群聊列表"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUTPUT_DIR / f"{prefix}-{ts}.csv"
        with open(path, "w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow([columns_def[c] for c in columns])
            for item in items:
                writer.writerow([_chatlist_cell(item, c) for c in columns])
        return {"ok": True, "path": str(path), "count": len(items)}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


@app.post("/api/moments/timeline")
async def api_moments_timeline(payload: dict):
    try:
        _moments_ready()
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


@app.get("/api/chat/status")
async def api_chat_status():
    return wxchat.status()


@app.post("/api/chat/init")
async def api_chat_init(payload: dict):
    return wxchat.start_refresh(payload, start_job=start_job, acquire_key=_moments_acquire_key)


@app.post("/api/chat/sessions")
async def api_chat_sessions(payload: dict):
    try:
        return await run_in_threadpool(wxchat.sessions, payload or {})
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


@app.post("/api/chat/messages")
async def api_chat_messages(payload: dict):
    try:
        return await run_in_threadpool(wxchat.messages, payload or {})
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


@app.get("/api/chat/media")
async def api_chat_media(kind: str = "image_full", session: str = "", create_time: int = 0,
                         server_id: str = "", filename: str = "", size: int = 0,
                         md5: str = "", url: str = "", thumb: str = ""):
    try:
        data, mime = await run_in_threadpool(
            wxchat.media_blob,
            {"kind": kind, "session": session, "create_time": create_time,
             "server_id": server_id, "filename": filename, "size": size,
             "md5": md5, "url": url, "thumb": thumb},
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=404)
    if not data:
        return JSONResponse({"ok": False, "msg": "没有找到本地媒体缓存"}, status_code=404)
    return Response(content=data, media_type=mime or "application/octet-stream",
                    headers={"Cache-Control": "private, max-age=600"})


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
    try:
        config = _moments_config()
        output_root = _output_root()
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


def _unique_member_dir(group_dir: Path, display: str, wxid: str,
                       used: dict[str, str]) -> Path:
    base = wxmoments.sanitize_folder_name(display or wxid, fallback=wxid or "好友")
    name = base
    index = 2
    while name in used:
        name = f"{base}_{index:02d}"
        index += 1
    used[name] = wxid
    return group_dir / name


def _export_group_dir(job: Job, account_info: wxmoments.AccountInfo, account_dir: Path,
                      config: dict[str, Any], contact_names: dict[str, str], out_dir: Path,
                      posts: list[dict[str, Any]], usernames: list[str],
                      keep_interactions: bool, label: str,
                      base_done: int, grand_total: int) -> dict[str, Any]:
    from wechat_decrypt_tool.modules.sns_media import (
        reset_sns_download_report, sns_download_report,
    )
    reset_sns_download_report()

    def cb(idx: int, _total: int, who: str, time_text: str) -> None:
        if job.canceled:
            raise RuntimeError("已取消")
        job.done = base_done + idx
        job.message = f"({job.done}/{grand_total}) {time_text} {who}"

    out_dir.mkdir(parents=True, exist_ok=True)
    stats, exported = asyncio.run(wxmoments.export_markdown(
        account_info, account_dir, out_dir, None, None, usernames, config,
        keep_interactions, contact_names,
        allow_download=True, progress_cb=cb, posts_override=posts,
        coverage_posts=_MOMENTS.get("posts") or [], reuse_existing=True,
    ))
    job.done = base_done + len(posts)

    job.check_canceled()
    job.message = f"生成 {label} 的 PDF…"
    try:
        wxmoments.write_pdf_html(out_dir, exported)
        wxmoments.render_pdf(out_dir, exported, out_dir / "moments.pdf")
    except Exception as exc:
        job.add_log(f"{label}：PDF 生成失败（HTML 仍可用）：{exc}")

    report = sns_download_report()
    (out_dir / "download_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    job.add_log(
        f"{label}：{stats['posts']} 条，图片 {stats['images']} "
        f"（复用 {stats.get('reused_images', 0)}），缺失 {stats['missing_images']}"
    )
    return stats


def _run_moments_group(job: Job, account_info: wxmoments.AccountInfo,
                       account_dir: Path, group: dict[str, Any],
                       folder: str) -> None:
    try:
        config = _moments_config()
        group_dir = _output_root() / folder
        group_dir.mkdir(parents=True, exist_ok=True)
        job.add_log(f"输出目录：{group_dir}")

        job.check_canceled()
        job.message = "准备图片密钥…"
        asyncio.run(wxmoments.save_image_keys(
            account_info.account, account_info.wxid_dir, account_info.db_storage_dir))

        contact_names = _contact_names()
        keep_interactions = bool(group.get("keep_interactions"))
        split = bool(group.get("split", False))
        is_label = wechat_labels.is_label_group_id(group.get("id"))
        if is_label:
            job.add_log(f"微信标签「{group.get('name')}」：导出 {len(group.get('members') or [])} 位已匹配好友"
                        + ("（成员未完整读取，未匹配的好友不会出现）" if not group.get("complete") else ""))

        plans: list[tuple[str, str, list[dict[str, Any]]]] = []
        for member in group.get("members") or []:
            wxid = str(member.get("wxid") or "").strip() if isinstance(member, dict) else ""
            if not wxid:
                continue
            display = contact_names.get(wxid) or wxid
            posts = _moments_filtered_posts([wxid], None, None)
            if not posts:
                job.add_log(f"{display}：没有可导出的朋友圈，跳过")
                continue
            plans.append((wxid, display, posts))
        if not plans:
            job.finish("error", "该分组的好友都没有可导出的朋友圈")
            return

        total = sum(len(posts) for _, _, posts in plans)
        job.total = total

        if split:
            used_dirs: dict[str, str] = {}
            done = 0
            for wxid, display, posts in plans:
                job.check_canceled()
                member_dir = _unique_member_dir(group_dir, display, wxid, used_dirs)
                _export_group_dir(
                    job, account_info, account_dir, config, contact_names, member_dir,
                    posts, [wxid], keep_interactions, display, done, total)
                done += len(posts)
        else:
            merged = [post for _, _, posts in plans for post in posts]
            merged.sort(key=lambda item: int(item.get("createTime") or 0), reverse=True)
            _export_group_dir(
                job, account_info, account_dir, config, contact_names, group_dir,
                merged, [wxid for wxid, _, _ in plans], keep_interactions,
                "全部好友", 0, total)

        exported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        (group_dir / "group.json").write_text(json.dumps({
            "name": str(group.get("name") or ""),
            "folder": folder,
            "source": str(group.get("source") or "custom"),
            "label_status": str(group.get("status") or ""),
            "label_complete": bool(group.get("complete")) if is_label else None,
            "split": split,
            "keep_interactions": keep_interactions,
            "exported_at": exported_at,
            "members": [
                {"wxid": wxid, "name": display, "posts": len(posts)}
                for wxid, display, posts in plans
            ],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        if is_label:
            _mark_label_export(str(group.get("label_id") or ""), total)
        else:
            _touch_group_export(str(group.get("id") or ""))

        job.results.append({
            "dir": str(group_dir),
            "posts": total,
            "members": len(plans),
        })
        mode = "拆分" if split else "组合"
        job.message = f"完成：{folder}（{len(plans)} 位好友 · {mode} · {total} 条）"
        job.finish("done", job.message)
    except Exception as exc:
        job.finish("canceled" if job.canceled else "error",
                   "已取消" if job.canceled else f"导出失败：{exc}")


def _clear_stale_proxy() -> None:
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
