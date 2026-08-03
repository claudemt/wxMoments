from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import sys
from io import BytesIO
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

from wechat_decrypt_tool.modules.constants import (
    MEDIA_TYPE_IMAGE, MEDIA_TYPE_VIDEO, MEDIA_TYPE_LIVE_PHOTO,
    POST_TYPE_NORMAL, POST_TYPE_ARTICLE, POST_TYPE_LINK, POST_TYPE_COVER,
    POST_TYPE_FINDER, POST_TYPE_MUSIC,
    DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT,
    DB_KEY_HEX_LENGTH,
    IMAGE_AES_KEY_LENGTH, IMAGE_XOR_KEY_MAX,
    PDF_FONT_NAME, PDF_FONT_FALLBACK, PDF_DPI,
    PDF_MARGIN_LEFT, PDF_MARGIN_RIGHT, PDF_MARGIN_TOP, PDF_MARGIN_BOTTOM,
)
from wechat_decrypt_tool.modules.wechat_emoji import emojify_wechat_shortcodes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_CONFIG = CONFIG_DIR / "config.json"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
OUTPUT_RUNTIME_DIR = RUNTIME_DIR / "output"

os.environ.setdefault("WECHAT_TOOL_DATA_DIR", str(RUNTIME_DIR))
os.environ.setdefault("WECHAT_TOOL_OUTPUT_DIR", str(OUTPUT_RUNTIME_DIR))
os.environ.setdefault("WECHAT_TOOL_BUILD_SESSION_LAST_MESSAGE", "0")


@dataclass(frozen=True)
class AccountInfo:
    account: str
    wxid_dir: Path
    db_storage_dir: Path


@dataclass
class ExportedPost:
    time_text: str
    display: str
    location: str
    body: str
    images: list[str]
    interactions: str = ""


@dataclass(frozen=True)
class ContactEntry:
    username: str
    remark: str
    nickname: str
    other_info: str = ""
    privacy_status: str = ""
    source_table: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出微信朋友圈为 Markdown、HTML 和 PDF")
    parser.add_argument("--key", default="", help=argparse.SUPPRESS)
    parser.add_argument("--start", default="", help="起始时间，例如 20260606；留空表示不限制")
    parser.add_argument("--end", default="", help="终止时间，例如 20260606；留空表示不限制")
    parser.add_argument("--only-self", default="", help="是否只导出自己的朋友圈: y/n，默认 y")
    parser.add_argument("--keep-interactions", default="", help="是否保留点赞评论: y/n，默认 n")
    parser.add_argument("--export-contacts", default="", help="是否导出好友列表: y/n，默认 n")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help=argparse.SUPPRESS)
    parser.add_argument("--output-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--no-download", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"配置文件读取失败: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    return data


def ensure_imports() -> None:
    if (SRC_ROOT / "wechat_decrypt_tool").exists():
        sys.path.insert(0, str(SRC_ROOT))
        return
    raise FileNotFoundError("缺少 src/wechat_decrypt_tool，请确认项目文件完整")


def ask_input(value: str, label: str, default: str = "") -> str:
    if value:
        return value.strip()
    suffix = f" [{default}]" if default else ""
    return input(f"{label}{suffix}: ").strip() or default


def print_input_rules() -> None:
    print(
        "\n输入规则：\n"
        "- 时间格式示例：20260606 或 2026-06-06；直接回车表示不限制。\n"
        "- 是否只导出自己的朋友圈：请输入 y 或 n；直接回车表示 y。\n"
        "- 是否保留点赞评论：请输入 y 或 n；直接回车表示 n。\n"
        "- 是否导出好友列表：请输入 y 或 n；直接回车表示 n。\n"
        "- 如果选择 n，可以继续按好友备注或昵称筛选。\n"
        "- 好友筛选：第1位直接回车表示导出所有人；输入 /me 表示自己；输入 /enough 表示结束选择。\n",
        flush=True,
    )


def parse_datetime(value: str, *, end_of_day: bool) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt in {"%Y%m%d", "%Y-%m-%d"} and end_of_day:
                return datetime.combine(parsed.date(), time.max)
            return parsed
        except ValueError:
            continue
    raise ValueError(f"时间格式不正确: {raw}。示例: 20260606 或 2026-06-06")


def parse_yes_no(value: str) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return True
    if raw in {"y", "yes", "1", "true", "on"}:
        return True
    if raw in {"n", "no", "0", "false", "off"}:
        return False
    raise ValueError("只导出自己的朋友圈请输入 y 或 n")


def parse_yes_no_default_no(value: str) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return False
    if raw in {"y", "yes", "1", "true", "on"}:
        return True
    if raw in {"n", "no", "0", "false", "off"}:
        return False
    raise ValueError("请输入 y 或 n")


def clean_account_name(path_name: str) -> str:
    name = str(path_name or "").strip()
    match = re.match(r"^(wxid_[^_]+)(?:_[0-9a-f]{4})?$", name, flags=re.IGNORECASE)
    return match.group(1) if match else name


def default_wechat_roots() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("WXMOMENTS_WECHAT_DATA_ROOT", "")
    if configured:
        for item in re.split(r"[;\n]", configured):
            if item.strip():
                roots.append(Path(item.strip()).expanduser())

    try:
        from wechat_decrypt_tool.modules.wechat_detection import auto_detect_wechat_data_dirs

        roots.extend(Path(item).expanduser() for item in auto_detect_wechat_data_dirs())
    except Exception as exc:
        print(f"[warning] {exc}", file=sys.stderr)

    for raw in (
        Path.home() / "Documents" / "xwechat_files",
        Path.home() / "Documents" / "WeChat Files",
        Path.home() / "Documents" / "Weixin Files",
    ):
        text = str(raw or "").strip()
        if text:
            roots.append(Path(text).expanduser())
    uniq: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(root)
    return uniq


_ACCOUNT_SCAN_SKIP_NAMES = {
    "$recycle.bin",
    "$winreagent",
    ".git",
    ".venv",
    "appdata",
    "config.msi",
    "documents and settings",
    "intel",
    "node_modules",
    "onedrivetemp",
    "perflogs",
    "program files",
    "program files (x86)",
    "programdata",
    "recovery",
    "runtime",
    "system volume information",
    "windows",
    "windows.old",
    "windows.old(1)",
}


def _iter_sns_db_paths(root: Path, *, max_depth: int = 6) -> list[Path]:
    root = root.expanduser()
    found: list[Path] = []
    if not root.exists():
        return found

    stack: list[tuple[Path, int]] = [(root, 0)]
    seen: set[str] = set()
    while stack:
        current, depth = stack.pop()
        try:
            key = os.path.normcase(str(current.resolve()))
        except OSError:
            key = os.path.normcase(str(current))
        if key in seen:
            continue
        seen.add(key)

        sns_direct = current / "db_storage" / "sns" / "sns.db"
        if sns_direct.exists():
            found.append(sns_direct)
        sns_flat = current / "db_storage" / "sns.db"
        if sns_flat.exists():
            found.append(sns_flat)

        if current.name.lower() == "db_storage":
            sns_child = current / "sns" / "sns.db"
            if sns_child.exists():
                found.append(sns_child)
            sns_child = current / "sns.db"
            if sns_child.exists():
                found.append(sns_child)

        if depth >= max_depth:
            continue

        try:
            with os.scandir(current) as entries:
                children = [
                    Path(entry.path)
                    for entry in entries
                    if entry.is_dir()
                    and entry.name.strip().lower() not in _ACCOUNT_SCAN_SKIP_NAMES
                ]
        except (PermissionError, OSError):
            continue
        stack.extend((child, depth + 1) for child in reversed(children))

    uniq: list[Path] = []
    seen_paths: set[str] = set()
    for item in found:
        try:
            key = os.path.normcase(str(item.resolve()))
        except OSError:
            key = os.path.normcase(str(item))
        if key not in seen_paths:
            seen_paths.add(key)
            uniq.append(item)
    return uniq


def _account_from_sns_db_path(sns_path: Path, account_hint: str = "") -> AccountInfo | None:
    if sns_path.name.lower() != "sns.db":
        return None
    db_dir = sns_path.parent.parent if sns_path.parent.name.lower() == "sns" else sns_path.parent
    if db_dir.name.lower() != "db_storage":
        return None
    wxid_dir = db_dir.parent
    account = clean_account_name(wxid_dir.name)
    if account_hint and account_hint.lower() not in {account.lower(), wxid_dir.name.lower()}:
        return None
    return AccountInfo(account=account, wxid_dir=wxid_dir, db_storage_dir=db_dir)


def iter_account_candidates(root: Path, account_hint: str = "") -> list[AccountInfo]:
    candidates: list[AccountInfo] = []
    if not root.exists():
        return candidates
    roots = [root]
    try:
        roots.extend([p for p in root.iterdir() if p.is_dir() and p.name.lower().startswith("wxid_")])
    except Exception as exc:
        print(f"[warning] {exc}", file=sys.stderr)
    for wxid_dir in roots:
        db_dir = wxid_dir / "db_storage"
        sns = db_dir / "sns" / "sns.db"
        if not sns.exists():
            sns = db_dir / "sns.db"
        if not sns.exists():
            continue
        account = clean_account_name(wxid_dir.name)
        if account_hint and account_hint.lower() not in {account.lower(), wxid_dir.name.lower()}:
            continue
        candidates.append(AccountInfo(account=account, wxid_dir=wxid_dir, db_storage_dir=db_dir))

    for sns_path in _iter_sns_db_paths(root):
        item = _account_from_sns_db_path(sns_path, account_hint)
        if item is not None:
            candidates.append(item)

    uniq: list[AccountInfo] = []
    seen: set[str] = set()
    for item in candidates:
        try:
            key = os.path.normcase(str(item.db_storage_dir.resolve()))
        except OSError:
            key = os.path.normcase(str(item.db_storage_dir))
        if key not in seen:
            seen.add(key)
            uniq.append(item)
    return uniq


def find_account(config: dict[str, Any]) -> AccountInfo:
    account_hint = str(config.get("account") or "").strip()
    configured_root = str(config.get("wechat_data_root") or "").strip()
    roots = [
        Path(item.strip()).expanduser()
        for item in re.split(r"[;\n]", configured_root)
        if item.strip()
    ] if configured_root else default_wechat_roots()
    candidates: list[tuple[int, AccountInfo]] = []
    for root in roots:
        for item in iter_account_candidates(root, account_hint):
            sns_path = item.db_storage_dir / "sns" / "sns.db"
            if not sns_path.exists():
                sns_path = item.db_storage_dir / "sns.db"
            try:
                size = int(sns_path.stat().st_size)
            except OSError:
                size = 0
            candidates.append((size, item))
    if not candidates:
        searched = "、".join(str(p) for p in roots)
        raise FileNotFoundError(
            "没有找到微信朋友圈数据库。\n"
            f"已查找: {searched}\n"
            "请确认这台电脑登录过微信并已产生朋友圈缓存；如果微信文件保存位置不是默认值，"
            "请在 config/config.json 的 wechat_data_root 中填写微信数据目录，例如 "
            r"C:\Users\你的用户名\Documents\WeChat Files 或 D:\微信文件。"
        )
    return max(candidates, key=lambda item: item[0])[1]


def is_wechat_running() -> bool:
    try:
        import psutil
    except Exception:
        return False
    targets = {"weixin.exe", "wechat.exe"}
    for proc in psutil.process_iter(["name"]):
        try:
            if str(proc.info.get("name") or "").strip().lower() in targets:
                return True
        except Exception:
            continue
    return False


def load_saved_db_key(account_info: AccountInfo) -> str:
    key_store = OUTPUT_RUNTIME_DIR / "account_keys.json"
    if not key_store.exists():
        return ""
    try:
        data = json.loads(key_store.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[warning] {exc}", file=sys.stderr)
        return ""
    if not isinstance(data, dict):
        return ""
    candidates = [account_info.account, account_info.wxid_dir.name]
    for candidate in candidates:
        item = data.get(candidate)
        if not isinstance(item, dict):
            continue
        key = str(item.get("db_key") or "").strip()
        if re.fullmatch(rf"[0-9a-fA-F]{{{DB_KEY_HEX_LENGTH}}}", key):
            return key
    return ""


def acquire_db_key(account_info: AccountInfo, config: dict[str, Any], args: argparse.Namespace) -> str:
    key = (args.key or str(config.get("db_key") or "") or os.environ.get("WXMOMENTS_KEY", "")).strip()
    if key:
        return key

    saved = load_saved_db_key(account_info)
    if saved:
        print("已使用本地保存的数据库密钥。")
        return saved

    if not is_wechat_running():
        input("请先登录电脑版微信，确认已进入微信主界面后按回车继续...")
    else:
        input("请确认电脑版微信已登录并停留在主界面，然后按回车自动获取密钥...")

    from wechat_decrypt_tool.modules.key_service import get_db_key_workflow

    try:
        result = get_db_key_workflow(db_storage_path=str(account_info.db_storage_dir))
        return str(result.get("db_key") or "").strip()
    except Exception as exc:
        print(f"自动获取密钥失败: {exc}")
        return input("请手动输入数据库密钥（64位十六进制）: ").strip()


async def save_image_keys(account: str, wxid_dir: Path, db_storage_dir: Path) -> None:
    from wechat_decrypt_tool.modules.key_service import get_image_key_integrated_workflow

    account_dir = OUTPUT_RUNTIME_DIR / "databases" / account
    account_dir.mkdir(parents=True, exist_ok=True)
    data = await get_image_key_integrated_workflow(
        account,
        db_storage_path=str(db_storage_dir),
        wxid_dir=str(wxid_dir),
    )
    if data.get("verified") is not True:
        raise RuntimeError("无法取得已验证的图片密钥，请保持微信已登录后重试")
    xor_raw = data.get("xor_key")
    try:
        xor_key = int(str(xor_raw), 16)
    except (TypeError, ValueError):
        xor_key = int(xor_raw)
    aes_key = str(data.get("aes_key") or "")[:16]
    if not (0 <= xor_key <= IMAGE_XOR_KEY_MAX) or len(aes_key) != IMAGE_AES_KEY_LENGTH:
        raise RuntimeError("图片密钥无效")
    (account_dir / "_media_keys.json").write_text(
        json.dumps({"xor": xor_key, "aes": aes_key}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def decrypt_databases(account_info: AccountInfo, key: str) -> Path:
    from wechat_decrypt_tool.modules.wechat_decrypt import decrypt_wechat_databases

    result = decrypt_wechat_databases(db_storage_path=str(account_info.db_storage_dir), key=key)
    if str(result.get("status") or "").lower() not in {"success", "ok"}:
        raise RuntimeError(str(result.get("message") or "数据库解密失败"))
    account_results = result.get("account_results") if isinstance(result.get("account_results"), dict) else {}
    for account_name, detail in account_results.items():
        if account_info.account.lower() == str(account_name or "").lower() and isinstance(detail, dict):
            out = Path(str(detail.get("output_dir") or ""))
            if (out / "sns.db").exists():
                return out
    fallback = OUTPUT_RUNTIME_DIR / "databases" / account_info.account
    if (fallback / "sns.db").exists():
        return fallback
    for candidate in (OUTPUT_RUNTIME_DIR / "databases").glob("*/sns.db"):
        return candidate.parent
    raise FileNotFoundError("解密完成后没有找到 sns.db")


def image_size(payload: bytes, media_type: str) -> tuple[int, int]:
    from wechat_decrypt_tool.modules.sns_reader import _image_size_from_bytes

    try:
        return _image_size_from_bytes(payload, media_type)
    except Exception:
        return (0, 0)


def expected_image_size(media: dict[str, Any]) -> tuple[int, int]:
    size = media.get("size") if isinstance(media.get("size"), dict) else {}
    for w_raw, h_raw in (
        (size.get("width"), size.get("height")),
        (media.get("width"), media.get("height")),
        (media.get("w"), media.get("h")),
    ):
        try:
            w = int(float(w_raw or 0))
            h = int(float(h_raw or 0))
        except Exception:
            continue
        if w > 0 and h > 0:
            return w, h
    return (0, 0)


def meets_expected_size(payload: bytes, media_type: str, expected: tuple[int, int]) -> bool:
    exp_w, exp_h = expected
    if exp_w <= 0 or exp_h <= 0:
        return True
    width, height = image_size(payload, media_type)
    if width <= 0 or height <= 0:
        return True
    return width * 4 >= exp_w * 3 and height * 4 >= exp_h * 3


def timestamp_folder_name(created: datetime) -> str:
    return created.strftime("%Y%m%d_%H%M%S")


def unique_post_dir(figure: Path, created: datetime) -> Path:
    base = timestamp_folder_name(created)
    candidate = figure / base
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = figure / f"{base}_{index:02d}"
        if not candidate.exists():
            return candidate
        index += 1


def normalize_hex32(value: Any) -> str:
    from wechat_decrypt_tool.modules.sns_reader import normalize_hex32 as _impl
    return _impl(value)


def generate_sns_cache_key(tid: str, media_id: str, media_type: int = MEDIA_TYPE_IMAGE) -> str:
    from wechat_decrypt_tool.modules.sns_reader import generate_sns_cache_key as _impl
    return _impl(tid, media_id, media_type)


def resolve_sns_cached_image_path_by_cache_key(wxid_dir: Path, cache_key: str, create_time: int = 0) -> Path | None:
    from wechat_decrypt_tool.modules.sns_reader import resolve_sns_cached_image_path_by_cache_key as _impl
    return _impl(wxid_dir, cache_key, create_time)


def pick_sns_media_str(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def sns_media_md5_value(media: dict[str, Any], raw_url: str) -> str:
    url_attrs = media.get("urlAttrs") if isinstance(media.get("urlAttrs"), dict) else {}
    thumb_attrs = media.get("thumbAttrs") if isinstance(media.get("thumbAttrs"), dict) else {}
    md5_raw = pick_sns_media_str(
        url_attrs.get("md5"),
        thumb_attrs.get("md5"),
        url_attrs.get("MD5"),
        thumb_attrs.get("MD5"),
    )
    if not md5_raw:
        match = re.search(r"[?&]md5=([0-9a-fA-F]{16,32})", str(raw_url or ""))
        if match:
            md5_raw = match.group(1)
    return normalize_hex32(md5_raw)


def sns_image_source(media: dict[str, Any], *, prefer_thumb: bool) -> tuple[str, str, str, bool]:
    url_attrs = media.get("urlAttrs") if isinstance(media.get("urlAttrs"), dict) else {}
    thumb_attrs = media.get("thumbAttrs") if isinstance(media.get("thumbAttrs"), dict) else {}
    original_url = pick_sns_media_str(
        media.get("url"),
        media.get("originUrl"),
        media.get("originalUrl"),
        media.get("origin_url"),
        media.get("original_url"),
    )
    thumb_url = pick_sns_media_str(media.get("thumb"), media.get("thumbUrl"), media.get("thumb_url"))
    use_thumb = bool(prefer_thumb and thumb_url) or not original_url
    if use_thumb:
        return (
            thumb_url or original_url,
            pick_sns_media_str(media.get("thumbKey"), media.get("thumb_key"), thumb_attrs.get("key"), media.get("key"), url_attrs.get("key")),
            pick_sns_media_str(
                media.get("thumbToken"),
                media.get("thumbUrlToken"),
                media.get("thumb_url_token"),
                thumb_attrs.get("token"),
                media.get("token"),
                url_attrs.get("token"),
            ),
            False,
        )
    return (
        original_url,
        pick_sns_media_str(media.get("key"), url_attrs.get("key"), media.get("thumbKey"), thumb_attrs.get("key")),
        pick_sns_media_str(
            media.get("token"),
            media.get("urlToken"),
            media.get("url_token"),
            url_attrs.get("token"),
            media.get("thumbToken"),
            thumb_attrs.get("token"),
        ),
        True,
    )


def resolve_sns_exact_cached_image_path(wxid_dir: Path, post: dict[str, Any], media: dict[str, Any], raw_url: str) -> Path | None:
    post_id = str(post.get("id") or post.get("tid") or "").strip()
    media_id = str(media.get("id") or "").strip()
    try:
        post_type = int(post.get("type") or POST_TYPE_NORMAL)
    except Exception:
        post_type = POST_TYPE_NORMAL
    try:
        media_type = int(media.get("type") or MEDIA_TYPE_IMAGE)
    except Exception:
        media_type = MEDIA_TYPE_IMAGE
    try:
        create_time = int(post.get("createTime") or 0)
    except Exception:
        create_time = 0

    if post_id and media_id and post_type == POST_TYPE_COVER:
        bkg_md5 = hashlib.md5(f"{post_id}_{media_id}_4".encode("utf-8", errors="ignore")).hexdigest()
        bkg_path = wxid_dir / "business" / "sns" / "bkg" / bkg_md5[:2] / bkg_md5
        if bkg_path.is_file():
            return bkg_path

    if post_id and media_id:
        for candidate_type in (media_type, MEDIA_TYPE_IMAGE, MEDIA_TYPE_LIVE_PHOTO, MEDIA_TYPE_VIDEO, 0):
            cache_key = generate_sns_cache_key(post_id, media_id, candidate_type)
            found = resolve_sns_cached_image_path_by_cache_key(wxid_dir, cache_key, create_time)
            if found is not None:
                return found

    md5_32 = sns_media_md5_value(media, raw_url)
    if md5_32:
        return resolve_sns_cached_image_path_by_cache_key(wxid_dir, md5_32, create_time)
    return None


def load_timeline(account_dir: Path, usernames: list[str] | None = None, *, source: str = "auto") -> list[dict[str, Any]]:
    from wechat_decrypt_tool.modules.sns_reader import list_sns_timeline

    posts: list[dict[str, Any]] = []
    offset = 0
    limit = DEFAULT_PAGE_LIMIT
    usernames_text = ",".join(usernames or []) or None
    while True:
        response = list_sns_timeline(
            account=account_dir.name,
            limit=limit,
            offset=offset,
            usernames=usernames_text,
            keyword=None,
            source=source,
        )
        page = response.get("items") or response.get("timeline") or []
        posts.extend([item for item in page if isinstance(item, dict)])
        if not response.get("hasMore") or not page:
            break
        offset += len(page)
    return posts


def post_created_datetime(post: dict[str, Any]) -> datetime | None:
    try:
        created_ts = int(post.get("createTime") or 0)
    except Exception:
        return None
    if created_ts <= 0:
        return None
    return datetime.fromtimestamp(created_ts)


def build_coverage_report(
    posts: list[dict[str, Any]],
    filtered: list[dict[str, Any]],
    start: datetime | None,
    end: datetime | None,
) -> dict[str, Any]:
    dated = sorted(dt for post in posts if (dt := post_created_datetime(post)) is not None)
    filtered_dated = sorted(dt for post in filtered if (dt := post_created_datetime(post)) is not None)
    gaps: list[dict[str, Any]] = []
    for prev, curr in zip(dated, dated[1:]):
        gap_days = (curr.date() - prev.date()).days - 1
        if gap_days >= 21:
            gaps.append(
                {
                    "from": prev.strftime("%Y-%m-%d %H:%M:%S"),
                    "to": curr.strftime("%Y-%m-%d %H:%M:%S"),
                    "gap_days": gap_days,
                }
            )
    gaps.sort(key=lambda item: int(item.get("gap_days") or 0), reverse=True)
    report = {
        "local_posts_with_time": len(dated),
        "exported_posts_with_time": len(filtered_dated),
        "local_earliest": dated[0].strftime("%Y-%m-%d %H:%M:%S") if dated else "",
        "local_latest": dated[-1].strftime("%Y-%m-%d %H:%M:%S") if dated else "",
        "export_earliest": filtered_dated[0].strftime("%Y-%m-%d %H:%M:%S") if filtered_dated else "",
        "export_latest": filtered_dated[-1].strftime("%Y-%m-%d %H:%M:%S") if filtered_dated else "",
        "requested_start": start.strftime("%Y-%m-%d %H:%M:%S") if start else "",
        "requested_end": end.strftime("%Y-%m-%d %H:%M:%S") if end else "",
        "large_gaps": gaps[:20],
        "large_gap_threshold_days": 21,
    }
    warnings: list[str] = []
    if start and dated and start < dated[0]:
        warnings.append(
            f"请求起始时间早于本地缓存最早记录：{start:%Y-%m-%d %H:%M:%S} < {dated[0]:%Y-%m-%d %H:%M:%S}"
        )
    if end and dated and end > dated[-1]:
        warnings.append(
            f"请求结束时间晚于本地缓存最新记录：{end:%Y-%m-%d %H:%M:%S} > {dated[-1]:%Y-%m-%d %H:%M:%S}"
        )
    if gaps:
        largest = gaps[0]
        warnings.append(
            f"本地缓存存在明显断档：{largest['from']} 至 {largest['to']} 之间约 {largest['gap_days']} 天没有记录"
        )
    report["warnings"] = warnings
    return report


def write_coverage_report(output: Path, report: dict[str, Any]) -> None:
    (output / "coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    earliest = str(report.get("local_earliest") or "")
    latest = str(report.get("local_latest") or "")
    if earliest or latest:
        print(f"  本地缓存范围: {earliest or '未知'} ~ {latest or '未知'}", flush=True)
    for warning in report.get("warnings") or []:
        print(f"  [coverage] {warning}", flush=True)


def save_db_key(account_info: AccountInfo, account_dir: Path, key: str) -> None:
    try:
        from wechat_decrypt_tool.modules.key_store import upsert_account_keys_in_store

        aliases = [account_info.wxid_dir.name, account_dir.name]
        upsert_account_keys_in_store(
            account_dir.name,
            db_key=key,
            aliases=aliases,
            db_key_source_wxid_dir=str(account_info.wxid_dir),
            db_key_source_db_storage_path=str(account_info.db_storage_dir),
        )
    except Exception as exc:
        print(f"[warning] {exc}", file=sys.stderr)


def self_username_candidates(account_info: AccountInfo, config: dict[str, Any]) -> list[str]:
    values: list[str] = []
    raw_one = str(config.get("self_username") or "").strip()
    if raw_one:
        values.append(raw_one)
    values.extend([account_info.account, account_info.wxid_dir.name])
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def normalize_contact_lookup_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip().casefold()


def decode_contact_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        for encoding in ("utf-8", "utf-16-le", "utf-16"):
            try:
                text = raw.decode(encoding, errors="ignore")
                text = text.replace("\x00", "").replace("\xa0", " ").strip()
                if text:
                    return text
            except Exception:
                continue
        return ""
    return str(value).replace("\xa0", " ").strip()


def sqlite_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return []
    out: list[str] = []
    for row in rows:
        try:
            name = str(row[1] or "").strip()
        except Exception:
            name = ""
        if name:
            out.append(name)
    return out


def row_value(row: sqlite3.Row, *names: str) -> Any:
    lower_to_name = {str(key).lower(): str(key) for key in row.keys()}
    for name in names:
        actual = lower_to_name.get(str(name).lower())
        if actual is not None:
            return row[actual]
    return None


def int_row_value(row: sqlite3.Row, *names: str) -> int:
    value = row_value(row, *names)
    try:
        return int(value or 0)
    except Exception:
        return 0


def infer_contact_privacy_status(flag: int, local_type: int, delete_flag: int) -> str:
    statuses: list[str] = []
    if flag & 0x100:
        statuses.append("不让ta看我的朋友圈")
    if flag & 0x10000:
        statuses.append("不看ta的朋友圈")
    if flag & 0x8:
        statuses.append("黑名单")
    if delete_flag:
        statuses.append("已删除或非当前好友")
    if local_type and not (local_type & 0x1):
        statuses.append("可能不是通讯录好友")
    return "；".join(dict.fromkeys(statuses))


def contact_is_current_friend(row: sqlite3.Row, table: str, self_usernames: set[str]) -> bool:
    """Return whether a contact row represents a current address-book friend.

    WeChat's contact.db is a broad profile cache, not a pure friend list. In
    recent PC WeChat builds, many non-friend cached profiles appear in
    contact.local_type=3 with flag=4, which previously made exports balloon
    into thousands of unrelated-looking rows. Current address-book contacts
    are the rows in contact.local_type=1 that are not deleted and not official
    accounts.
    """
    if str(table or "").lower() != "contact":
        return False
    username = decode_contact_text(row_value(row, "username"))
    if username in self_usernames:
        return False
    if not should_export_contact(username):
        return False
    if int_row_value(row, "delete_flag"):
        return False
    if int_row_value(row, "verify_flag"):
        return False
    return int_row_value(row, "local_type") == 1


def contact_other_info(row: sqlite3.Row) -> str:
    fields = [
        ("微信号", ("alias",)),
        ("额外备注", ("description", "desc", "remark_desc", "remark_description")),
        ("手机号", ("phone", "phone_number", "mobile", "mobile_phone", "telephone")),
        ("标签", ("label_id_list", "label_ids", "labels", "tag", "tag_ids")),
        ("地区", ("region", "country", "province", "city")),
        ("个性签名", ("signature", "sign")),
    ]
    parts: list[str] = []
    for label, names in fields:
        values: list[str] = []
        if label == "地区":
            values = [decode_contact_text(row_value(row, name)) for name in names]
            values = [item for item in values if item]
            value = " ".join(dict.fromkeys(values))
        else:
            value = ""
            for name in names:
                value = decode_contact_text(row_value(row, name))
                if value:
                    break
        if value:
            parts.append(f"{label}: {value}")
    return "；".join(parts)


def should_export_contact(username: str) -> bool:
    lowered = str(username or "").strip().lower()
    if not lowered:
        return False
    if (
        lowered.endswith("@chatroom")
        or lowered.endswith("@openim")
        or lowered.endswith("@kefu.openim")
        or lowered.startswith("gh_")
    ):
        return False
    system_accounts = {
        "filehelper",
        "newsapp",
        "fmessage",
        "weibo",
        "qqmail",
        "tmessage",
        "qmessage",
        "medianote",
        "floatbottle",
        "lbsapp",
        "shakeapp",
        "feedsapp",
        "qqsync",
        "weixin",
        "weixinreminder",
        "officialaccounts",
        "brandsessionholder",
        "masssendapp",
    }
    return lowered not in system_accounts


def load_contact_entries(account_dir: Path) -> list[ContactEntry]:
    contact_db = account_dir / "contact.db"
    if not contact_db.exists():
        return []

    entries_by_username: dict[str, ContactEntry] = {}
    self_usernames = {clean_account_name(account_dir.name), account_dir.name}
    conn = sqlite3.connect(str(contact_db))
    conn.row_factory = sqlite3.Row
    try:
        for table in ("contact", "stranger"):
            columns = sqlite_table_columns(conn, table)
            if not columns:
                continue
            try:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            except Exception:
                rows = []
            for row in rows:
                username = decode_contact_text(row_value(row, "username"))
                if not contact_is_current_friend(row, table, self_usernames):
                    continue
                remark = decode_contact_text(row_value(row, "remark"))
                nickname = decode_contact_text(row_value(row, "nick_name", "nickname"))
                if not remark and not nickname:
                    continue
                flag = int_row_value(row, "flag")
                local_type = int_row_value(row, "local_type")
                delete_flag = int_row_value(row, "delete_flag")
                entries_by_username.setdefault(
                    username,
                    ContactEntry(
                        username=username,
                        remark=remark,
                        nickname=nickname,
                        other_info=contact_other_info(row),
                        privacy_status=infer_contact_privacy_status(flag, local_type, delete_flag),
                        source_table=table,
                    ),
                )
    finally:
        conn.close()

    return sorted(entries_by_username.values(), key=lambda item: (item.remark or item.nickname or item.username).casefold())


def write_contact_cache(account_dir: Path, contacts: list[ContactEntry]) -> Path:
    cache_path = account_dir / "_contacts_cache.json"
    payload = [
        {
            "wxid": item.username,
            "nickname": item.nickname,
            "remark": item.remark,
        }
        for item in contacts
    ]
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return cache_path


def export_contact_list(output: Path, contacts: list[ContactEntry]) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    fieldnames = ["wxid", "昵称", "备注名"]
    has_other = any(c.other_info for c in contacts)
    has_privacy = any(c.privacy_status for c in contacts)
    if has_other:
        fieldnames.append("其他信息")
    if has_privacy:
        fieldnames.append("隐私状态")
    rows = [
        {
            "wxid": item.username,
            "昵称": item.nickname,
            "备注名": item.remark,
            **({"其他信息": item.other_info} if has_other else {}),
            **({"隐私状态": item.privacy_status} if has_privacy else {}),
        }
        for item in contacts
    ]
    json_path = output / "contacts.json"
    csv_path = output / "contacts.csv"
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    exported = len(rows)
    print(f"  共导出 {exported} 位好友", flush=True)
    return {"contacts_exported": len(rows), "contacts_json": str(json_path), "contacts_csv": str(csv_path)}


def build_contact_lookup(contacts: list[ContactEntry]) -> dict[str, list[ContactEntry]]:
    lookup: dict[str, list[ContactEntry]] = {}
    for contact in contacts:
        for value in (contact.remark, contact.nickname):
            key = normalize_contact_lookup_text(value)
            if key:
                lookup.setdefault(key, []).append(contact)
    return lookup


def build_contact_display_names(contacts: list[ContactEntry]) -> dict[str, str]:
    out: dict[str, str] = {}
    for contact in contacts:
        display = contact.remark or contact.nickname or contact.username
        if contact.username and display:
            out[contact.username] = display
    return out


def display_interaction_name(value: Any, contact_names: dict[str, str]) -> str:
    if isinstance(value, dict):
        username = str(value.get("username") or value.get("userName") or "").replace("\xa0", " ").strip()
        if username and username in contact_names:
            return contact_names[username]
        nickname = str(value.get("nickname") or value.get("nickName") or value.get("displayName") or "").replace("\xa0", " ").strip()
        return nickname or username
    raw = str(value or "").replace("\xa0", " ").strip()
    if not raw:
        return ""
    return contact_names.get(raw, raw)


def display_interaction_person(username: Any, nickname: Any, contact_names: dict[str, str]) -> str:
    user = str(username or "").replace("\xa0", " ").strip()
    if user and user in contact_names:
        return contact_names[user]
    nick = str(nickname or "").replace("\xa0", " ").strip()
    if nick:
        return nick
    return user


def build_self_display_lookup(
    account_info: AccountInfo,
    config: dict[str, Any],
    posts: list[dict[str, Any]],
    contact_names: dict[str, str],
) -> dict[str, str]:
    candidates = self_username_candidates(account_info, config)
    candidate_set = set(candidates)
    display = str(config.get("self_display_name") or config.get("self_nickname") or "").strip()
    if not display:
        for post in posts:
            username = str(post.get("username") or "").strip()
            if username not in candidate_set:
                continue
            display = str(post.get("displayName") or "").replace("\xa0", " ").strip()
            if display:
                break
    if not display:
        display = contact_names.get(account_info.account, "") or account_info.account
    return {candidate: display for candidate in candidates if candidate and display}


async def materialize_comment_stickers(
    account_info: AccountInfo,
    account_dir: Path,
    output: Path,
    post_dir: Path,
    post: dict[str, Any],
    comment: dict[str, Any],
    comment_index: int,
    stats: dict[str, int],
    *,
    allow_download: bool,
) -> list[str]:
    rels: list[str] = []
    images = comment.get("images") if isinstance(comment.get("images"), list) else []
    for image_index, raw in enumerate(images, 1):
        media = raw if isinstance(raw, dict) else {}
        if not media:
            continue
        payload, mt, _source = await choose_image(account_info, account_dir, post, media, allow_download=allow_download)
        if not payload:
            stats["missing_stickers"] = stats.get("missing_stickers", 0) + 1
            continue
        post_dir.mkdir(parents=True, exist_ok=True)
        name = f"emoji_{comment_index:02d}_{image_index:02d}{mime_to_ext(mt)}"
        target = post_dir / name
        target.write_bytes(payload)
        rel = target.relative_to(output).as_posix()
        rels.append(rel)
        stats["stickers"] = stats.get("stickers", 0) + 1
    return rels


async def format_post_interactions(
    account_info: AccountInfo,
    account_dir: Path,
    output: Path,
    post_dir: Path,
    post: dict[str, Any],
    contact_names: dict[str, str],
    stats: dict[str, int],
    *,
    allow_download: bool,
) -> str:
    lines: list[str] = []

    raw_likes = post.get("likes") if isinstance(post.get("likes"), list) else []
    likes = []
    for item in raw_likes:
        name = display_interaction_name(item, contact_names)
        if name and name not in likes:
            likes.append(name)
    if likes:
        lines.append(f"**❤️ 点赞**：{'、'.join(likes)}")

    raw_comments = post.get("comments") if isinstance(post.get("comments"), list) else []
    comment_lines: list[str] = []
    for comment_index, item in enumerate(raw_comments, 1):
        if not isinstance(item, dict):
            continue
        author = display_interaction_person(item.get("username"), item.get("nickname"), contact_names) or "未知好友"
        reply_to = display_interaction_person(item.get("refUsername"), item.get("refNickname"), contact_names)
        content = emojify_wechat_shortcodes(str(item.get("content") or "").replace("\xa0", " ").strip())
        sticker_rels = await materialize_comment_stickers(
            account_info,
            account_dir,
            output,
            post_dir,
            post,
            item,
            comment_index,
            stats,
            allow_download=allow_download,
        )
        sticker_md = " ".join(f"![表情包]({rel})" for rel in sticker_rels)
        if sticker_md:
            content = f"{content} {sticker_md}".strip()
        if not content:
            continue
        if reply_to:
            comment_lines.append(f"- {author} 回复 {reply_to}：{content}")
        else:
            comment_lines.append(f"- {author}：{content}")
    if comment_lines:
        if lines:
            lines.append("")
        lines.append("**💬 评论**")
        lines.extend(comment_lines)

    return "\n".join(lines).strip()


def resolve_contact_input(
    raw: str,
    lookup: dict[str, list[ContactEntry]],
    account_info: AccountInfo,
    config: dict[str, Any],
) -> list[str]:
    value = str(raw or "").strip()
    if value == "/me":
        return self_username_candidates(account_info, config)

    key = normalize_contact_lookup_text(value)
    matches = lookup.get(key) or []
    if not matches:
        raise ValueError(f"没有找到好友：{value}")

    usernames = sorted({item.username for item in matches if item.username})
    if len(usernames) > 1:
        names = []
        for item in matches[:8]:
            label = item.remark or item.nickname or item.username
            names.append(f"{label}({item.username})")
        raise ValueError(f"匹配到多个好友：{value}。请给目标好友设置更唯一的备注后重试。匹配项：{', '.join(names)}")
    return usernames


def ask_friend_filters(
    account_info: AccountInfo,
    account_dir: Path,
    config: dict[str, Any],
    contacts: list[ContactEntry] | None = None,
) -> tuple[list[str] | None, list[dict[str, str]]]:
    contacts = contacts if contacts is not None else load_contact_entries(account_dir)
    write_contact_cache(account_dir, contacts)
    lookup = build_contact_lookup(contacts)

    selected_usernames: list[str] = []
    selected_contacts: list[dict[str, str]] = []
    index = 1
    while True:
        if index == 1:
            raw = input("请输入第1位好友的昵称或备注（回车=所有人）: ").strip()
            if not raw:
                return None, []
        else:
            raw = input(f"请输入第{index}位好友的昵称或备注（/enough=结束）: ").strip()
            if not raw:
                continue
        if raw == "/enough":
            if not selected_usernames:
                raise ValueError("还没有选择任何好友")
            return selected_usernames, selected_contacts

        resolved = resolve_contact_input(raw, lookup, account_info, config)
        for username in resolved:
            if username not in selected_usernames:
                selected_usernames.append(username)
        selected_contacts.append({"input": raw, "usernames": ",".join(resolved)})
        index += 1


async def fetch_remote_image(account_dir: Path, media: dict[str, Any], prefer_thumb: bool) -> tuple[bytes, str, str]:
    from wechat_decrypt_tool.modules.sns_media import fix_sns_cdn_url, get_cached_sns_remote_image, try_fetch_and_decrypt_sns_image_remote

    raw_url, key, token, is_original = sns_image_source(media, prefer_thumb=prefer_thumb)
    if not raw_url:
        return b"", "", ""
    fixed = fix_sns_cdn_url(raw_url, token=token, is_video=False)
    expected = expected_image_size(media) if is_original else (0, 0)
    cached = get_cached_sns_remote_image(account_dir=account_dir, url=fixed, key=str(key or ""), token=str(token or ""))
    if cached is not None and meets_expected_size(bytes(cached.payload or b""), str(cached.media_type or ""), expected):
        return bytes(cached.payload or b""), str(cached.media_type or ""), "remote-cache-original" if is_original else "remote-cache-thumb"
    fetched = await try_fetch_and_decrypt_sns_image_remote(
        account_dir=account_dir,
        url=fixed,
        key=str(key or ""),
        token=str(token or ""),
        use_cache=True,
    )
    if fetched is not None and meets_expected_size(bytes(fetched.payload or b""), str(fetched.media_type or ""), expected):
        return bytes(fetched.payload or b""), str(fetched.media_type or ""), "remote-original" if is_original else "remote-thumb"
    return b"", "", ""


def read_local_image(account_info: AccountInfo, account_dir: Path, post: dict[str, Any], media: dict[str, Any]) -> tuple[bytes, str, str]:
    from wechat_decrypt_tool.modules.media_decrypt import _detect_image_media_type, _read_and_maybe_decrypt_media

    local = resolve_sns_exact_cached_image_path(account_info.wxid_dir, post, media, str(media.get("url") or media.get("thumb") or ""))
    if local is None:
        post_id = str(post.get("id") or post.get("tid") or "").strip()
        media_id = str(media.get("id") or "").strip()
        for candidate_type in (MEDIA_TYPE_IMAGE, MEDIA_TYPE_LIVE_PHOTO, MEDIA_TYPE_VIDEO, 0):
            key = generate_sns_cache_key(post_id, media_id, candidate_type)
            found = resolve_sns_cached_image_path_by_cache_key(account_info.wxid_dir, key)
            if found:
                local = found
                break
    if local is None:
        return b"", "", ""
    try:
        payload, media_type = _read_and_maybe_decrypt_media(Path(local), account_dir)
    except Exception as exc:
        print(f"[warning] {exc}", file=sys.stderr)
        return b"", "", ""
    mt = str(media_type or "").split(";", 1)[0].strip() or _detect_image_media_type(payload[:32])
    if not payload or not mt.startswith("image/"):
        return b"", "", ""
    return payload, mt, "local-cache"


async def choose_image(
    account_info: AccountInfo,
    account_dir: Path,
    post: dict[str, Any],
    media: dict[str, Any],
    *,
    allow_download: bool,
) -> tuple[bytes, str, str]:
    if allow_download:
        payload, mt, source = await fetch_remote_image(account_dir, media, prefer_thumb=False)
        if payload:
            return payload, mt, source
    local_payload, local_mt, local_source = read_local_image(account_info, account_dir, post, media)
    expected = expected_image_size(media)
    if local_payload and meets_expected_size(local_payload, local_mt, expected):
        return local_payload, local_mt, local_source
    if allow_download:
        payload, mt, source = await fetch_remote_image(account_dir, media, prefer_thumb=True)
        if payload:
            return payload, mt, source
    if local_payload:
        return local_payload, local_mt, "local-cache-small"
    return b"", "", ""


def mime_to_ext(media_type: str) -> str:
    from wechat_decrypt_tool.modules.sns_media import mime_to_ext as _impl
    return _impl(media_type)


async def export_markdown(
    account_info: AccountInfo,
    account_dir: Path,
    output: Path,
    start: datetime | None,
    end: datetime | None,
    usernames: list[str] | None,
    config: dict[str, Any],
    keep_interactions: bool,
    contact_names: dict[str, str],
    *,
    allow_download: bool,
) -> tuple[dict[str, Any], list[ExportedPost]]:
    figure = output / "figure"
    figure.mkdir(parents=True, exist_ok=True)
    posts = load_timeline(account_dir, usernames, source="auto")
    filtered: list[dict[str, Any]] = []
    for post in posts:
        created_ts = int(post.get("createTime") or 0)
        if created_ts <= 0:
            continue
        created = datetime.fromtimestamp(created_ts)
        if start and created < start:
            continue
        if end and created > end:
            continue
        filtered.append(post)
    filtered.sort(key=lambda item: int(item.get("createTime") or 0), reverse=True)
    interaction_contact_names = dict(contact_names)
    interaction_contact_names.update(build_self_display_lookup(account_info, config, filtered, contact_names))
    coverage = build_coverage_report(posts, filtered, start, end)
    write_coverage_report(output, coverage)

    lines = ["# 微信朋友圈备份", ""]
    stats = {
        "posts": len(filtered),
        "images": 0,
        "original_images": 0,
        "fallback_images": 0,
        "missing_images": 0,
        "stickers": 0,
        "missing_stickers": 0,
        "local_earliest": coverage.get("local_earliest", ""),
        "local_latest": coverage.get("local_latest", ""),
        "coverage_gap_count": len(coverage.get("large_gaps") or []),
    }
    exported_posts: list[ExportedPost] = []
    total = len(filtered)
    for idx, post in enumerate(filtered, 1):
        created = datetime.fromtimestamp(int(post.get("createTime") or 0))
        time_text = created.strftime("%Y-%m-%d %H:%M:%S")
        username = str(post.get("username") or "").strip()
        fallback_display = str(post.get("displayName") or username or account_info.account).strip()
        display = interaction_contact_names.get(username, fallback_display)
        lines.extend([f"## {format_post_heading(time_text, display)}", ""])
        location = str(post.get("location") or "").strip()
        content = emojify_wechat_shortcodes(str(post.get("contentDesc") or "").strip())
        title = emojify_wechat_shortcodes(str(post.get("title") or "").strip())
        content_url = str(post.get("contentUrl") or "").strip()
        body = content or "（无文字）"
        if title:
            body = f"{body}\n\n🔗 **链接标题**：{title}" if body else f"🔗 **链接标题**：{title}"
        if content_url:
            body = f"{body}\n\n🔗 **链接**：{content_url}" if body else f"🔗 **链接**：{content_url}"
        lines.extend([body, ""])

        post_dir = unique_post_dir(figure, created)
        image_index = 0
        post_images: list[str] = []
        for media_raw in post.get("media") or []:
            media = media_raw if isinstance(media_raw, dict) else {}
            try:
                media_type = int(media.get("type") or 0)
            except Exception as exc:
                print(f"[warning] {exc}", file=sys.stderr)
                media_type = 0
            if media_type != MEDIA_TYPE_IMAGE:
                continue
            payload, mt, source = await choose_image(account_info, account_dir, post, media, allow_download=allow_download)
            if not payload:
                stats["missing_images"] += 1
                continue
            image_index += 1
            post_dir.mkdir(parents=True, exist_ok=True)
            name = f"{image_index:02d}{mime_to_ext(mt)}"
            target = post_dir / name
            target.write_bytes(payload)
            rel = target.relative_to(output).as_posix()
            post_images.append(rel)
            lines.extend([f"![{time_text} image {image_index}]({rel})", ""])
            stats["images"] += 1
            if source.startswith("remote") and "thumb" not in source:
                stats["original_images"] += 1
            else:
                stats["fallback_images"] += 1
        interactions = ""
        if keep_interactions:
            interactions = await format_post_interactions(
                account_info,
                account_dir,
                output,
                post_dir,
                post,
                interaction_contact_names,
                stats,
                allow_download=allow_download,
            )
            if interactions:
                lines.extend([interactions, ""])
        lines.extend(["---", ""])
        exported_posts.append(
            ExportedPost(
                time_text=time_text,
                display=display,
                location=location,
                body=body,
                images=post_images,
                interactions=interactions,
            )
        )
        print(f"  ({idx}/{total}) {time_text} {display}", flush=True)
    (output / "moments.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return stats, exported_posts


def image_grid_class(count: int) -> str:
    if count <= 1:
        return "grid-one"
    if count in (2, 3):
        return f"grid-{count}"
    if count == 4:
        return "grid-four"
    return "grid-three"


def markdown_text_to_html(text: str, output: Path | None = None, *, file_uris: bool = False) -> str:
    def inline_markup(raw: str) -> str:
        def image_repl(match: re.Match[str]) -> str:
            alt = match.group(1)
            rel = match.group(2)
            src = Path(output / rel).resolve().as_uri() if output is not None and file_uris else rel
            return f'<img class="inline-sticker" src="{html.escape(src, quote=True)}" alt="{html.escape(alt, quote=True)}">'

        escaped = html.escape(raw)
        escaped = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", image_repl, escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        return escaped.replace("\n", "<br>")

    normalized = emojify_wechat_shortcodes(str(text or ""))
    paragraphs = [inline_markup(p) for p in re.split(r"\n{2,}", normalized) if p.strip()]
    return "".join(f"<p>{p}</p>" for p in paragraphs) or "<p>（无文字）</p>"


def interactions_to_html(text: str, output: Path | None = None, *, file_uris: bool = False) -> str:
    if not str(text or "").strip():
        return ""
    return f'<div class="interactions">{markdown_text_to_html(text, output, file_uris=file_uris)}</div>'


def format_post_heading(time_text: str, display: str) -> str:
    display = str(display or "").strip()
    if display and display != "未知":
        return f"{time_text} · {display}"
    return time_text


def post_heading_text(post: ExportedPost) -> str:
    return format_post_heading(post.time_text, post.display)


def optimized_pdf_image_uri(output: Path, rel: str, max_side: int = 640, quality: int = 60) -> str:
    source = output / rel
    if not source.exists():
        return source.resolve().as_uri()
    safe_rel = Path(rel)
    target = output / "_pdf_assets" / safe_rel.with_suffix(".jpg")
    try:
        from PIL import Image, ImageOps

        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, "white")
                alpha = image.getchannel("A")
                background.paste(image.convert("RGBA"), mask=alpha)
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            image.save(target, format="JPEG", quality=quality, optimize=True, progressive=True)
        return target.resolve().as_uri()
    except Exception as exc:
        print(f"[warning] PDF 图片压缩失败，使用原图: {source} ({exc})", file=sys.stderr)
        return source.resolve().as_uri()


def write_pdf_html(
    output: Path,
    posts: list[ExportedPost],
    filename: str = "moments.html",
    optimize_images: bool = False,
) -> Path:
    css = """
@page { size: A4; margin: 10mm 11mm 11mm; }
* { box-sizing: border-box; }
body { margin: 0; color: #1f2328; background: #fff; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI Emoji", "Segoe UI", "Microsoft YaHei", Arial, sans-serif; font-size: 13.5px; line-height: 1.48; }
.page { max-width: 760px; margin: 0 auto; }
.moment { padding: 0 0 10px; margin: 0 0 10px; border-bottom: 1px solid #e5e7eb; }
.moment:last-child { border-bottom: 0; }
.time { font-size: 18px; font-weight: 700; margin: 0 0 7px; letter-spacing: .01em; }
.body { font-size: 14px; }
.body p { margin: 0 0 6px; white-space: normal; overflow-wrap: anywhere; }
.interactions { margin-top: 7px; padding: 7px 9px; background: #f6f8fa; border-left: 3px solid #8ace9b; border-radius: 8px; color: #3f4b4f; font-size: 12px; }
.interactions p { margin: 0 0 3px; }
.inline-sticker { display: inline-block; width: 34px; height: 34px; object-fit: contain; vertical-align: middle; margin: 0 2px; border-radius: 6px; }
.images { display: grid; gap: 5px; margin-top: 7px; width: 100%; max-width: 410px; }
.grid-one { display: block; max-width: 300px; }
.grid-2 { grid-template-columns: repeat(2, 1fr); }
.grid-3, .grid-three { grid-template-columns: repeat(3, 1fr); }
.grid-four { grid-template-columns: repeat(2, 1fr); max-width: 272px; }
.image-cell { width: 100%; aspect-ratio: 1 / 1; overflow: hidden; background: #f3f4f6; border-radius: 6px; }
.grid-one .image-cell { aspect-ratio: auto; }
.image-cell img { display: block; width: 100%; height: 100%; object-fit: cover; }
.grid-one .image-cell img { width: auto; max-width: 100%; height: auto; max-height: 330px; object-fit: contain; }
"""
    parts = [
        "<!doctype html>",
        "<html>",
        "<head>",
        '<meta charset="utf-8">',
        "<title>微信朋友圈备份</title>",
        f"<style>{css}</style>",
        "</head>",
        "<body>",
        '<main class="page">',
    ]
    for post in posts:
        parts.append('<article class="moment">')
        parts.append(f'<h2 class="time">{html.escape(post_heading_text(post))}</h2>')
        parts.append(f'<div class="body">{markdown_text_to_html(post.body, output, file_uris=optimize_images)}</div>')
        if post.images:
            parts.append(f'<div class="images {image_grid_class(len(post.images))}">')
            for rel in post.images:
                src = optimized_pdf_image_uri(output, rel) if optimize_images else rel
                parts.append(f'<div class="image-cell"><img src="{html.escape(src, quote=True)}"></div>')
            parts.append("</div>")
        if post.interactions:
            parts.append(interactions_to_html(post.interactions, output, file_uris=optimize_images))
        parts.append("</article>")
    parts.extend(["</main>", "</body>", "</html>", ""])
    html_path = output / filename
    html_path.write_text("\n".join(parts), encoding="utf-8")
    return html_path


def pdf_image_rows(count: int) -> list[int]:
    if count <= 0:
        return []
    if count <= 3:
        return [count]
    if count == 4:
        return [2, 2]
    if count == 5:
        return [3, 2]
    if count == 6:
        return [3, 3]
    rows: list[int] = []
    remaining = count
    while remaining > 0:
        row_count = min(3, remaining)
        rows.append(row_count)
        remaining -= row_count
    return rows


def pdf_image_columns(count: int) -> int:
    if count <= 1:
        return 1
    if count in (2, 3):
        return count
    if count == 4:
        return 2
    return 3


def render_pdf_direct(output: Path, posts: list[ExportedPost], pdf_path: Path) -> None:
    from PIL import Image, ImageOps
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    font_name = PDF_FONT_NAME
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    except Exception:
        font_name = PDF_FONT_FALLBACK
        print("警告: PDF 中文字体 STSong-Light 不可用，中文可能显示为方块。"
              "安装 Adobe Acrobat 或配置中文字体可解决此问题。")

    image_dpi = PDF_DPI

    page_width, page_height = A4
    margin_left = PDF_MARGIN_LEFT
    margin_right = PDF_MARGIN_RIGHT
    margin_top = PDF_MARGIN_TOP
    margin_bottom = PDF_MARGIN_BOTTOM
    content_width = page_width - margin_left - margin_right
    y = page_height - margin_top
    page_no = 1
    canv = canvas.Canvas(str(pdf_path), pagesize=A4)

    def draw_footer() -> None:
        canv.setFont(PDF_FONT_FALLBACK, 8)
        canv.setFillColor(colors.HexColor("#777777"))
        canv.drawCentredString(page_width / 2, 22, str(page_no))
        canv.setFillColor(colors.black)

    def new_page() -> None:
        nonlocal y, page_no
        draw_footer()
        canv.showPage()
        page_no += 1
        y = page_height - margin_top

    def ensure_space(height: float) -> None:
        if y - height < margin_bottom:
            new_page()

    def text_width(text: str, size: float) -> float:
        return pdfmetrics.stringWidth(text, font_name, size)

    def wrap_line(text: str, size: float, max_width: float) -> list[str]:
        raw = str(text or "")
        if not raw:
            return [""]
        lines: list[str] = []
        current = ""
        for char in raw:
            candidate = current + char
            if current and text_width(candidate, size) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines or [""]

    def draw_wrapped(text: str, size: float, leading: float, color: str = "#1f2328") -> None:
        nonlocal y
        canv.setFont(font_name, size)
        canv.setFillColor(colors.HexColor(color))
        paragraphs = re.split(r"\n{2,}", str(text or "").strip() or "（无文字）")
        for paragraph in paragraphs:
            for source_line in paragraph.splitlines() or [""]:
                for line in wrap_line(source_line, size, content_width):
                    ensure_space(leading + 2)
                    canv.drawString(margin_left, y, line)
                    y -= leading
            y -= 3
        canv.setFillColor(colors.black)

    def load_pdf_image(path: Path, max_width_pt: float, max_height_pt: float) -> tuple[ImageReader, float, float] | None:
        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                width_px, height_px = image.size
                if width_px <= 0 or height_px <= 0:
                    return None
                scale = min(max_width_pt / width_px, max_height_pt / height_px)
                draw_width = max(1.0, width_px * scale)
                draw_height = max(1.0, height_px * scale)
                target_width_px = max(1, int(draw_width / 72 * image_dpi))
                target_height_px = max(1, int(draw_height / 72 * image_dpi))
                resize_scale = min(1.0, target_width_px / width_px, target_height_px / height_px)
                if resize_scale < 1.0:
                    image = image.resize(
                        (max(1, int(width_px * resize_scale)), max(1, int(height_px * resize_scale))),
                        Image.Resampling.LANCZOS,
                    )
                if image.mode in {"RGBA", "LA"}:
                    background = Image.new("RGB", image.size, "white")
                    alpha = image.getchannel("A") if image.mode == "RGBA" else image.getchannel("A")
                    background.paste(image.convert("RGBA"), mask=alpha)
                    image = background
                elif image.mode != "RGB":
                    image = image.convert("RGB")
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=92, optimize=True)
                buffer.seek(0)
                return ImageReader(buffer), draw_width, draw_height
        except Exception as exc:
            print(f"[warning] {exc}", file=sys.stderr)
            return None

    def draw_single_image(path: Path) -> None:
        nonlocal y
        max_width = min(content_width, 320)
        max_height = 360
        item = load_pdf_image(path, max_width, max_height)
        if item is None:
            return
        reader, width, height = item
        ensure_space(height + 12)
        canv.drawImage(reader, margin_left, y - height, width=width, height=height, preserveAspectRatio=True, mask="auto")
        y -= height + 10

    def draw_image_grid(paths: list[Path]) -> None:
        nonlocal y
        gap = 5
        grid_width = min(content_width, 365)
        columns = pdf_image_columns(len(paths))
        cell = (grid_width - gap * (columns - 1)) / columns
        index = 0
        for row_count in pdf_image_rows(len(paths)):
            ensure_space(cell + 9)
            x = margin_left
            for path in paths[index:index + row_count]:
                item = load_pdf_image(path, cell, cell)
                if item is not None:
                    reader, width, height = item
                    img_x = x + (cell - width) / 2
                    img_y = y - cell + (cell - height) / 2
                    canv.drawImage(reader, img_x, img_y, width=width, height=height, preserveAspectRatio=True, mask="auto")
                canv.setStrokeColor(colors.HexColor("#eeeeee"))
                canv.rect(x, y - cell, cell, cell, stroke=1, fill=0)
                x += cell + gap
            y -= cell + gap
            index += row_count
        y -= 5

    for post in posts:
        ensure_space(55)
        canv.setFont(font_name, 14)
        canv.setFillColor(colors.HexColor("#1f2328"))
        canv.drawString(margin_left, y, post_heading_text(post))
        y -= 17
        draw_wrapped(post.body, 11, 14.5)
        image_paths = [output / rel for rel in post.images]
        if len(image_paths) == 1:
            draw_single_image(image_paths[0])
        elif image_paths:
            draw_image_grid(image_paths)
        if post.interactions:
            draw_wrapped(post.interactions, 9.5, 12, "#3f4b4f")
        ensure_space(8)
        canv.setStrokeColor(colors.HexColor("#e5e7eb"))
        canv.line(margin_left, y, page_width - margin_right, y)
        y -= 10

    draw_footer()
    canv.save()
    if not pdf_path.read_bytes().startswith(b"%PDF"):
        raise RuntimeError("直接生成的 PDF 数据无效")


def find_chromium_executable() -> str | None:
    env_value = os.environ.get("WX_MOMENTS_CHROME") or os.environ.get("CHROME_PATH")
    candidates: list[Path] = []
    if env_value:
        candidates.append(Path(env_value).expanduser())
    for command in ("chrome", "chrome.exe", "msedge", "msedge.exe", "chromium", "chromium-browser"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    for root in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(root)
        if not base:
            continue
        candidates.extend(
            [
                Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return None


def render_pdf_chromium(html_path: Path, pdf_path: Path) -> None:
    import subprocess
    import tempfile

    browser = find_chromium_executable()
    if not browser:
        raise RuntimeError("未找到 Chrome/Edge/Chromium")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chrome-pdf-", dir=str(RUNTIME_DIR)) as user_data_dir:
        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--allow-file-access-from-files",
            "--no-pdf-header-footer",
            f"--user-data-dir={user_data_dir}",
            f"--print-to-pdf={pdf_path.resolve()}",
            html_path.resolve().as_uri(),
        ]
        timeout = int(os.environ.get("WX_MOMENTS_CHROME_TIMEOUT", "600"))
        proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError("浏览器 PDF 生成失败: " + " / ".join(message.splitlines()[-3:]))
    if not pdf_path.exists() or not pdf_path.read_bytes().startswith(b"%PDF"):
        raise RuntimeError("浏览器生成的 PDF 数据无效")


def render_pdf(output: Path, posts: list[ExportedPost], pdf_path: Path) -> None:
    if os.environ.get("WX_MOMENTS_PDF_ENGINE", "").lower() != "reportlab":
        html_path = output / "_moments_pdf.html"
        try:
            write_pdf_html(output, posts, filename=html_path.name, optimize_images=True)
            render_pdf_chromium(html_path, pdf_path)
            with contextlib.suppress(OSError):
                html_path.unlink()
            with contextlib.suppress(OSError):
                shutil.rmtree(output / "_pdf_assets")
            return
        except Exception as exc:
            print(f"[warning] 浏览器 PDF 生成失败，回退到直接渲染: {exc}", file=sys.stderr)
            with contextlib.suppress(OSError):
                html_path.unlink()
            with contextlib.suppress(OSError):
                shutil.rmtree(output / "_pdf_assets")
    render_pdf_direct(output, posts, pdf_path)


_step_counter = 0


def progress(label: str) -> None:
    global _step_counter
    _step_counter += 1
    print(f"第{_step_counter}步：{label}", flush=True)



async def main() -> int:
    args = parse_args()
    try:
        if args.quiet:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            (RUNTIME_DIR / "last_run.log").write_text("", encoding="utf-8")
            log_path = RUNTIME_DIR / "last_run.log"
            with log_path.open("a", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                return await _main_impl(args)
        else:
            return await _main_impl(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"\n运行失败：{exc}", file=sys.stderr, flush=True)
        if os.environ.get("WXMOMENTS_DEBUG_TRACEBACK", "").strip().lower() in {"1", "true", "yes", "on"}:
            raise
        return 1


async def _main_impl(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    ensure_imports()
    print("欢迎使用 wxMoments 微信朋友圈导出备份工具！\n", flush=True)
    print_input_rules()

    progress("定位微信并获取密钥")
    account_info = find_account(config)
    key = acquire_db_key(account_info, config, args)
    if not re.fullmatch(rf"[0-9a-fA-F]{{{DB_KEY_HEX_LENGTH}}}", key):
        raise ValueError("数据库密钥必须是 64 位十六进制字符串")

    start_raw = ask_input(args.start, "起始时间（格式: 20260606 或 2026-06-06，回车=不限制）", "")
    end_raw = ask_input(args.end, "终止时间（格式: 20260606 或 2026-06-06，回车=不限制）", "")
    only_raw = ask_input(args.only_self, "是否只导出自己的朋友圈? y/n", "y")
    interactions_raw = ask_input(args.keep_interactions, "是否保留点赞评论? y/n", "n")
    export_contacts_raw = ask_input(args.export_contacts, "是否导出好友列表? y/n", "n")
    start = parse_datetime(start_raw, end_of_day=False)
    end = parse_datetime(end_raw, end_of_day=True)
    only_self = parse_yes_no(only_raw)
    keep_interactions = parse_yes_no_default_no(interactions_raw)
    export_contacts_enabled = parse_yes_no_default_no(export_contacts_raw)
    usernames: list[str] | None = self_username_candidates(account_info, config) if only_self else None
    friend_inputs: list[dict[str, str]] = []
    contact_names: dict[str, str] = {}
    output_root = Path(args.output_root or str(config.get("output_root") or "") or PROJECT_ROOT / "output").expanduser()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = output_root / timestamp
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    progress("解密数据库")
    account_dir = decrypt_databases(account_info, key)
    save_db_key(account_info, account_dir, key)

    contacts: list[ContactEntry] = []
    if (not only_self) or keep_interactions or export_contacts_enabled:
        contacts = load_contact_entries(account_dir)
        write_contact_cache(account_dir, contacts)
        contact_names = build_contact_display_names(contacts)

    contact_export_meta: dict[str, Any] = {}
    if export_contacts_enabled:
        progress("导出好友列表")
        contact_export_meta = export_contact_list(output, contacts)

    if not only_self:
        progress("准备好友列表")
        usernames, friend_inputs = ask_friend_filters(account_info, account_dir, config, contacts)

    params = {
        "start_time": start_raw,
        "end_time": end_raw,
        "only_self": only_self,
        "keep_interactions": keep_interactions,
        "export_contacts": export_contacts_enabled,
        "friend_inputs": friend_inputs,
    }
    (output / "params.json").write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")

    progress("准备图片")
    await save_image_keys(account_info.account, account_info.wxid_dir, account_info.db_storage_dir)
    progress("导出朋友圈")
    stats, exported_posts = await export_markdown(
        account_info,
        account_dir,
        output,
        start,
        end,
        usernames,
        config,
        keep_interactions,
        contact_names,
        allow_download=not args.no_download,
    )
    progress("生成 PDF")
    html_path = write_pdf_html(output, exported_posts)
    pdf_path = output / "moments.pdf"
    try:
        render_pdf(output, exported_posts, pdf_path)
    except Exception as exc:
        raise RuntimeError(f"PDF 生成失败: {exc}。HTML 已生成: {html_path}") from exc
    result = {"output": str(output), **params, **contact_export_meta, **stats}
    print(f"\n导出完成：{output}", flush=True)
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
