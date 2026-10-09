from __future__ import annotations

from ..exceptions import MediaError

from .constants import (
    MAX_IMAGE_DOWNLOAD_BYTES, MAX_VIDEO_DOWNLOAD_BYTES,
    VIDEO_DECRYPT_SIZE, ALLOWED_CDN_DOMAINS,
)

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit
import asyncio
import atexit
import base64
import hashlib
import html
import json
import os
import queue
import re
import subprocess
import threading
import time

import httpx

from .logging_config import get_logger

logger = get_logger(__name__)
_MODULE_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _MODULE_DIR.parent if _MODULE_DIR.name == "modules" else _MODULE_DIR
_NATIVE_DIR = _PACKAGE_DIR / "native"
_WEFLOW_WASM_DIR = _NATIVE_DIR / "weflow_wasm"






_REPORT_LOCK = threading.Lock()
_REPORT_COUNTS: dict[str, int] = {}
_REPORT_SAMPLES: dict[str, list[dict[str, Any]]] = {}
_REPORT_SAMPLE_LIMIT = 5




_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_BACKOFF_SECONDS = 0.6


def reset_sns_download_report() -> None:
    with _REPORT_LOCK:
        _REPORT_COUNTS.clear()
        _REPORT_SAMPLES.clear()


def record_sns_download_event(
    category: str,
    *,
    url: str = "",
    status: int = 0,
    detail: str = "",
) -> None:
    cat = str(category or "unknown").strip() or "unknown"
    with _REPORT_LOCK:
        _REPORT_COUNTS[cat] = _REPORT_COUNTS.get(cat, 0) + 1
        bucket = _REPORT_SAMPLES.setdefault(cat, [])
        if len(bucket) < _REPORT_SAMPLE_LIMIT:
            bucket.append(
                {
                    "url": str(url or "")[:400],
                    "status": int(status or 0),
                    "detail": str(detail or "")[:200],
                }
            )


def sns_download_report() -> dict[str, Any]:
    with _REPORT_LOCK:
        return {
            "counts": dict(sorted(_REPORT_COUNTS.items())),
            "samples": {k: [dict(item) for item in v] for k, v in _REPORT_SAMPLES.items()},
        }


def _classify_failure(exc: BaseException) -> tuple[str, int]:
    """Map an exception to a (report category, http status) pair."""
    if isinstance(exc, SnsMediaError):
        return "rejected", 0
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            status = int(exc.response.status_code)
        except Exception:
            status = 0
        return f"http_{status}" if status else "http_error", status
    return "network_error", 0


def _describe_response(resp: httpx.Response) -> str:
    try:
        headers = resp.headers
    except Exception:
        return ""
    parts = []
    for name in ("x-enc", "content-type", "content-length", "server"):
        value = str(headers.get(name) or "").strip()
        if value:
            parts.append(f"{name}={value}")
    return " ".join(parts)


async def _download_with_retries(attempt_fn, *, url: str):
    """Run an async download attempt, retrying only transient failures.

    Deterministic 4xx responses are surfaced immediately: the caller's fallback
    (thumbnail rendition, alternative host) is the thing that can actually
    rescue those, and re-requesting the same URL would just waste the budget.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            return await attempt_fn()
        except Exception as exc:
            last_exc = exc
            status = 0
            if isinstance(exc, httpx.HTTPStatusError):
                try:
                    status = int(exc.response.status_code)
                except Exception:
                    status = 0
            transient = status in _RETRYABLE_STATUS or (
                status == 0 and not isinstance(exc, SnsMediaError)
            )
            if not transient or attempt == _DOWNLOAD_ATTEMPTS:
                raise
            logger.info(
                "[sns_media] transient failure (attempt %d/%d) on %s: %s",
                attempt,
                _DOWNLOAD_ATTEMPTS,
                url,
                exc,
            )
            await asyncio.sleep(_DOWNLOAD_BACKOFF_SECONDS * attempt)

    if last_exc is not None:  
        raise last_exc
    raise RuntimeError("download attempts exhausted without a result")


class SnsMediaError(MediaError):
    def __init__(self, status_code: int = 500, detail: str = ""):
        super().__init__(detail or str(status_code))
        self.status_code = status_code
        self.detail = detail or str(status_code)


def is_allowed_sns_media_host(host: str) -> bool:
    h = str(host or "").strip().lower()
    if not h:
        return False
    return any(h.endswith(d) for d in ALLOWED_CDN_DOMAINS)


def normalize_sns_cache_url(url: str) -> str:
    raw = html.unescape(str(url or "")).strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if key.lower() not in {"token", "idx"}
            ],
            doseq=True,
        )
        base = f"{parsed.netloc}{parsed.path}"
        return f"{base}?{query}" if query else base
    except Exception:
        base, separator, query = raw.partition("?")
        stable_base = re.sub(r"^https?://", "", base, flags=re.I)
        if not separator:
            return stable_base
        params = [
            item
            for item in query.split("&")
            if item.partition("=")[0].strip().lower() not in {"token", "idx"}
        ]
        return f"{stable_base}?{'&'.join(params)}" if params else stable_base


def fix_sns_cdn_url(
    url: str,
    *,
    token: str = "",
    is_video: bool = False,
    keep_source_size: bool = False,
) -> str:
    u = html.unescape(str(url or "")).strip()
    if not u:
        return ""

    try:
        p = urlparse(u)
        host = str(p.hostname or "").lower()
        if not is_allowed_sns_media_host(host):
            return u
    except Exception:
        return u

    u = re.sub(r"^http://", "https://", u, flags=re.I)

    
    
    
    
    if not is_video and not keep_source_size:
        u = re.sub(r"/(?:150|200|480)(?=($|\?))", "/0", u)

    tok = str(token or "").strip()
    if tok:
        base, separator, query = u.partition("?")
        params = []
        if separator:
            params = [
                item
                for item in query.split("&")
                if item.partition("=")[0].strip().lower() not in {"token", "idx"}
            ]
        u = f"{base}?{'&'.join(params)}" if params else base
        if is_video:
            base, separator, query = u.partition("?")
            u = f"{base}?token={tok}&idx=1"
            if separator and query:
                u = f"{u}&{query}"
        else:
            connector = "&" if "?" in u else "?"
            u = f"{u}{connector}token={tok}&idx=1"

    return u


def _detect_mp4_ftyp(head: bytes) -> bool:
    return bool(head) and len(head) >= 8 and head[4:8] == b"ftyp"


@lru_cache(maxsize=1)
def _weflow_wxisaac64_script_path() -> str:
    bundled = _WEFLOW_WASM_DIR / "weflow_wasm_keystream.js"
    if bundled.exists() and bundled.is_file():
        return str(bundled)
    return ""


class _WeflowWasmProcess:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen[str]] = None
        self._responses: queue.Queue[Optional[dict[str, object]]] = queue.Queue()
        self._request_id = 0

    def _start_locked(self, script: str) -> subprocess.Popen[str]:
        process = self._process
        if process is not None and process.poll() is None:
            return process

        responses: queue.Queue[Optional[dict[str, object]]] = queue.Queue()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            ["node", script, "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        if process.stdin is None or process.stdout is None:
            process.kill()
            raise RuntimeError("Failed to open WeFlow WASM stdio pipes")

        def read_responses() -> None:
            try:
                for line in process.stdout:
                    try:
                        value = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(value, dict):
                        responses.put(value)
            finally:
                responses.put(None)

        threading.Thread(
            target=read_responses,
            name="sns-wasm-response-reader",
            daemon=True,
        ).start()
        self._responses = responses
        self._process = process
        return process

    def generate(self, script: str, key: str, size: int) -> bytes:
        with self._lock:
            process = self._start_locked(script)
            assert process.stdin is not None
            self._request_id += 1
            request_id = self._request_id
            request = json.dumps(
                {"id": request_id, "key": str(key), "size": int(size)},
                ensure_ascii=True,
                separators=(",", ":"),
            )
            try:
                process.stdin.write(request + "\n")
                process.stdin.flush()
                response = self._responses.get(timeout=30.0)
            except Exception:
                self._stop_locked()
                raise

            if response is None or int(response.get("id") or 0) != request_id:
                self._stop_locked()
                raise RuntimeError("WeFlow WASM process returned an invalid response")
            error = str(response.get("error") or "").strip()
            if error:
                raise RuntimeError(error)
            payload = str(response.get("data") or "").strip()
            if not payload:
                raise RuntimeError("WeFlow WASM process returned an empty keystream")
            return base64.b64decode(payload, validate=False)

    def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        try:
            if process.stdout is not None:
                process.stdout.close()
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            self._stop_locked()


_WEFLOW_WASM_PROCESS = _WeflowWasmProcess()
atexit.register(_WEFLOW_WASM_PROCESS.close)


@lru_cache(maxsize=64)
def weflow_wxisaac64_keystream(key: str, size: int) -> bytes:
    key_text = str(key or "").strip()
    if not key_text or size <= 0:
        return b""

    script = _weflow_wxisaac64_script_path()
    if script:
        try:
            return _WEFLOW_WASM_PROCESS.generate(script, key_text, int(size))
        except Exception:
            pass

    from .isaac64 import Isaac64  

    want = int(size)
    size8 = ((want + 7) // 8) * 8
    return Isaac64(key_text).generate_keystream(size8)[:want]


_SNS_REMOTE_VIDEO_CACHE_EXTS = [
    ".mp4",
]


def _sns_remote_video_cache_dir_and_stem(account_dir: Path, *, url: str, key: str) -> tuple[Path, str]:
    del key
    digest = hashlib.md5(normalize_sns_cache_url(url).encode("utf-8", errors="ignore")).hexdigest()
    cache_dir = account_dir / "sns_remote_video_cache" / digest[:2]
    return cache_dir, digest


def _sns_remote_video_cache_existing_path(cache_dir: Path, stem: str) -> Optional[Path]:
    for ext in _SNS_REMOTE_VIDEO_CACHE_EXTS:
        p = cache_dir / f"{stem}{ext}"
        try:
            if p.exists() and p.is_file():
                return p
        except Exception:
            continue
    return None


def get_cached_sns_remote_video(
    *,
    account_dir: Path,
    url: str,
    key: str,
    token: str,
) -> Optional[Path]:
    fixed_url = fix_sns_cdn_url(str(url or ""), token=str(token or ""), is_video=True)
    if not fixed_url:
        return None

    try:
        host = str(urlparse(fixed_url).hostname or "").strip().lower()
    except Exception:
        return None
    if not is_allowed_sns_media_host(host):
        return None

    cache_dir, cache_stem = _sns_remote_video_cache_dir_and_stem(
        account_dir,
        url=fixed_url,
        key=str(key or ""),
    )
    existing = _sns_remote_video_cache_existing_path(cache_dir, cache_stem)
    if existing is None:
        return None

    return existing


async def _download_sns_remote_to_file(
    url: str,
    dest_path: Path,
    *,
    max_bytes: int,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[str, str]:
    u = str(url or "").strip()
    if not u:
        return "", ""

    try:
        p = urlparse(u)
        host = str(p.hostname or "").lower()
        if not is_allowed_sns_media_host(host):
            raise SnsMediaError(status_code=400, detail="SNS media host not allowed.")
    except SnsMediaError:
        raise
    except Exception:
        raise SnsMediaError(status_code=400, detail="Invalid SNS media URL.")

    base_headers = {
        "User-Agent": "MicroMessenger Client",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }

    async def download(http_client: httpx.AsyncClient) -> tuple[str, str]:
        dest_path.unlink(missing_ok=True)
        total = 0
        async with http_client.stream("GET", u, headers=base_headers, timeout=15.0) as resp:
            if resp.status_code not in {200, 206}:
                raise httpx.HTTPStatusError(
                    f"Unexpected SNS status {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            content_type = str(resp.headers.get("Content-Type") or "").strip()
            x_enc = str(resp.headers.get("x-enc") or "").strip()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with dest_path.open("wb") as f:
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise SnsMediaError(status_code=400, detail="SNS video too large.")
                    f.write(chunk)
        return content_type, x_enc

    if client is not None:
        return await _download_with_retries(lambda: download(client), url=u)
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as owned_client:
        return await _download_with_retries(lambda: download(owned_client), url=u)


def maybe_decrypt_sns_video_file(path: Path, key: str) -> bool:
    key_text = str(key or "").strip()
    if not key_text:
        return False

    try:
        size = int(path.stat().st_size)
    except Exception:
        return False

    if size <= 8:
        return False

    decrypt_size = min(VIDEO_DECRYPT_SIZE, size)
    if decrypt_size <= 0:
        return False

    try:
        with path.open("r+b") as f:
            head = f.read(8)
            if _detect_mp4_ftyp(head):
                return False

            f.seek(0)
            buf = bytearray(f.read(decrypt_size))
            if not buf:
                return False

            ks = weflow_wxisaac64_keystream(key_text, decrypt_size)
            n = min(len(buf), len(ks))
            for i in range(n):
                buf[i] ^= ks[i]

            f.seek(0)
            f.write(buf)
            f.flush()

            f.seek(0)
            head2 = f.read(8)
            if _detect_mp4_ftyp(head2):
                return True
            return True
    except Exception:
        return False


async def materialize_sns_remote_video(
    *,
    account_dir: Path,
    url: str,
    key: str,
    token: str,
    use_cache: bool,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Path]:
    fixed_url = fix_sns_cdn_url(str(url or ""), token=str(token or ""), is_video=True)
    if not fixed_url:
        return None

    cache_dir, cache_stem = _sns_remote_video_cache_dir_and_stem(account_dir, url=fixed_url, key=str(key or ""))

    if use_cache:
        existing = get_cached_sns_remote_video(
            account_dir=account_dir,
            url=fixed_url,
            key=key,
            token=token,
        )
        if existing is not None:
            return existing

    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_dir / f"{cache_stem}.mp4.{time.time_ns()}.tmp"
    try:
        await _download_sns_remote_to_file(
            fixed_url,
            tmp_path,
            max_bytes=MAX_VIDEO_DOWNLOAD_BYTES,
            client=client,
        )
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None

    await asyncio.to_thread(maybe_decrypt_sns_video_file, tmp_path, str(key or ""))

    ok_mp4 = False
    try:
        with tmp_path.open("rb") as f:
            head = f.read(8)
        ok_mp4 = _detect_mp4_ftyp(head)
    except Exception:
        ok_mp4 = False

    if not ok_mp4:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None

    final_path = cache_dir / f"{cache_stem}.mp4"
    try:
        os.replace(str(tmp_path), str(final_path))
    except Exception:
        final_path = tmp_path

    for other_ext in _SNS_REMOTE_VIDEO_CACHE_EXTS:
        if other_ext.lower() == ".mp4":
            continue
        other = cache_dir / f"{cache_stem}{other_ext}"
        try:
            if other.exists() and other.is_file():
                other.unlink(missing_ok=True)
        except Exception:
            continue
    return final_path


def best_effort_unlink(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def detect_image_mime(data: bytes) -> str:
    if not data:
        return ""

    if data.startswith(b"\xFF\xD8\xFF"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand == b"avif":
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx"):
            return "image/heic"
        if brand in (b"heif", b"mif1", b"msf1"):
            return "image/heif"
    if data.startswith(b"BM"):
        return "image/bmp"

    return ""


def weflow_decrypt_sns_image_bytes(payload: bytes, key: str) -> bytes:
    raw = bytes(payload or b"")
    key_text = str(key or "").strip()
    if not raw or not key_text:
        return raw

    ks = weflow_wxisaac64_keystream(key_text, len(raw))
    if not ks:
        return raw

    out = bytearray(raw)
    n = min(len(out), len(ks))
    for i in range(n):
        out[i] ^= ks[i]
    return bytes(out)


_SNS_REMOTE_CACHE_EXTS = [
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".avif",
    ".heic",
    ".heif",
]


def _mime_to_ext(mt: str) -> str:
    m = str(mt or "").split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/avif": ".avif",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }.get(m, ".bin")


def mime_to_ext(media_type: str) -> str:
    ext = _mime_to_ext(media_type)
    return ext if ext != ".bin" else ".jpg"


def _ext_to_mime(ext: str) -> str:
    e = str(ext or "").strip().lower().lstrip(".")
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "bmp": "image/bmp",
        "avif": "image/avif",
        "heic": "image/heic",
        "heif": "image/heif",
    }.get(e, "")


def _sns_remote_cache_dir_and_stem(account_dir: Path, *, url: str, key: str) -> tuple[Path, str]:
    del key
    digest = hashlib.md5(normalize_sns_cache_url(url).encode("utf-8", errors="ignore")).hexdigest()
    cache_dir = account_dir / "sns_remote_cache" / digest[:2]
    return cache_dir, digest


def _sns_remote_cache_existing_path(cache_dir: Path, stem: str) -> Optional[Path]:
    for ext in _SNS_REMOTE_CACHE_EXTS:
        p = cache_dir / f"{stem}{ext}"
        try:
            if p.exists() and p.is_file():
                return p
        except Exception:
            continue
    return None


def _sniff_image_mime_from_file(path: Path) -> str:
    try:
        with path.open("rb") as f:
            head = f.read(64)
        return detect_image_mime(head)
    except Exception:
        return ""


async def _download_sns_remote_bytes(
    url: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[bytes, str, str]:
    u = str(url or "").strip()
    if not u:
        return b"", "", ""

    max_bytes = MAX_IMAGE_DOWNLOAD_BYTES

    base_headers = {
        "User-Agent": "MicroMessenger Client",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
    }

    async def download(http_client: httpx.AsyncClient) -> tuple[bytes, str, str]:
        resp = await http_client.get(u, headers=base_headers, timeout=15.0)
        if resp.status_code not in {200, 206}:
            raise httpx.HTTPStatusError(
                f"Unexpected SNS status {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        payload = bytes(resp.content or b"")
        if len(payload) > max_bytes:
            raise SnsMediaError(status_code=400, detail="SNS media too large (>25MB).")
        content_type = str(resp.headers.get("Content-Type") or "").strip()
        x_enc = str(resp.headers.get("x-enc") or "").strip()
        return payload, content_type, x_enc

    try:
        if client is not None:
            result = await _download_with_retries(lambda: download(client), url=u)
        else:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as owned_client:
                result = await _download_with_retries(lambda: download(owned_client), url=u)
    except httpx.HTTPStatusError as exc:
        status = int(exc.response.status_code)
        detail = _describe_response(exc.response)
        record_sns_download_event(f"http_{status}", url=u, status=status, detail=detail)
        logger.warning("[sns_media] GET %s -> HTTP %s %s", u, status, detail)
        raise
    except Exception as exc:
        category, status = _classify_failure(exc)
        record_sns_download_event(category, url=u, status=status, detail=str(exc))
        logger.warning("[sns_media] GET %s failed (%s): %s", u, category, exc)
        raise

    record_sns_download_event("downloaded", url=u, status=200)
    return result


@dataclass(frozen=True)
class SnsRemoteImageResult:
    payload: bytes
    media_type: str
    source: str
    x_enc: str = ""
    cache_path: Optional[Path] = None


def get_cached_sns_remote_image(
    *,
    account_dir: Path,
    url: str,
    key: str,
    token: str,
    keep_source_size: bool = False,
) -> Optional[SnsRemoteImageResult]:
    u_fixed = fix_sns_cdn_url(
        url, token=token, is_video=False, keep_source_size=keep_source_size
    )
    if not u_fixed:
        return None

    try:
        host = str(urlparse(u_fixed).hostname or "").strip().lower()
    except Exception:
        return None
    if not is_allowed_sns_media_host(host):
        return None

    cache_dir, cache_stem = _sns_remote_cache_dir_and_stem(account_dir, url=u_fixed, key=str(key or ""))
    try:
        existing = _sns_remote_cache_existing_path(cache_dir, cache_stem)
        if existing is None:
            return None

        mt = _ext_to_mime(existing.suffix)
        if not mt:
            mt = _sniff_image_mime_from_file(existing)
            if not mt:
                try:
                    existing.unlink(missing_ok=True)
                except Exception:
                    pass
                return None

            ext = _mime_to_ext(mt)
            if ext != ".bin":
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    desired = cache_dir / f"{cache_stem}{ext}"
                    if desired.exists():
                        existing.unlink(missing_ok=True)
                        existing = desired
                    else:
                        os.replace(str(existing), str(desired))
                        existing = desired
                except Exception:
                    pass

        payload = existing.read_bytes()
        if not payload:
            return None
        return SnsRemoteImageResult(
            payload=payload,
            media_type=mt,
            source="remote-cache",
            x_enc="",
            cache_path=existing,
        )
    except Exception:
        return None


async def try_fetch_and_decrypt_sns_image_remote(
    *,
    account_dir: Path,
    url: str,
    key: str,
    token: str,
    use_cache: bool,
    client: Optional[httpx.AsyncClient] = None,
    keep_source_size: bool = False,
) -> Optional[SnsRemoteImageResult]:
    u_fixed = fix_sns_cdn_url(
        url, token=token, is_video=False, keep_source_size=keep_source_size
    )
    if not u_fixed:
        return None

    try:
        p = urlparse(u_fixed)
        host = str(p.hostname or "").strip().lower()
    except Exception:
        return None
    if not is_allowed_sns_media_host(host):
        return None

    cache_dir, cache_stem = _sns_remote_cache_dir_and_stem(account_dir, url=u_fixed, key=str(key or ""))

    if use_cache:
        cached = get_cached_sns_remote_image(
            account_dir=account_dir,
            url=u_fixed,
            key=key,
            token=token,
            keep_source_size=keep_source_size,
        )
        if cached is not None:
            record_sns_download_event("served_from_cache", url=u_fixed, status=0)
            return cached

    cache_path: Optional[Path] = None

    try:
        raw, content_type, x_enc = await _download_sns_remote_bytes(u_fixed, client=client)
    except Exception as e:
        
        logger.debug("[sns_media] remote download failed: %s", e)
        return None

    if not raw:
        record_sns_download_event(
            "empty_payload", url=u_fixed, status=200, detail=content_type
        )
        return None

    mt_raw = detect_image_mime(raw)

    decoded = raw
    mt = mt_raw
    decrypted = False
    k = str(key or "").strip()

    need_decrypt = bool(k) and (not mt_raw) and bool(raw)
    if k and x_enc and str(x_enc).strip() not in ("0", "false", "False"):
        need_decrypt = True

    if need_decrypt:
        try:
            decoded2 = await asyncio.to_thread(weflow_decrypt_sns_image_bytes, raw, k)
            mt2 = detect_image_mime(decoded2)
            if mt2:
                decoded = decoded2
                mt = mt2
                decrypted = decoded2 != raw
            else:
                if mt_raw:
                    decoded = raw
                    mt = mt_raw
                    decrypted = False
                else:
                    record_sns_download_event(
                        "decrypt_failed",
                        url=u_fixed,
                        status=200,
                        detail=f"x-enc={x_enc or '-'} content-type={content_type or '-'} bytes={len(raw)}",
                    )
                    logger.warning(
                        "[sns_media] %s downloaded but could not be decoded "
                        "(x-enc=%s content-type=%s bytes=%d)",
                        u_fixed,
                        x_enc or "-",
                        content_type or "-",
                        len(raw),
                    )
                    return None
        except Exception as e:
            logger.warning("[sns_media] remote decrypt failed on %s: %s", u_fixed, e)
            if not mt_raw:
                record_sns_download_event(
                    "decrypt_error", url=u_fixed, status=200, detail=str(e)
                )
                return None
            decoded = raw
            mt = mt_raw
            decrypted = False

    if not mt:
        record_sns_download_event(
            "not_an_image",
            url=u_fixed,
            status=200,
            detail=f"x-enc={x_enc or '-'} content-type={content_type or '-'} bytes={len(raw)}",
        )
        logger.warning(
            "[sns_media] %s returned no recognisable image (%d bytes, content-type=%s)",
            u_fixed,
            len(raw),
            content_type or "-",
        )
        return None

    record_sns_download_event(
        "ok" if not decrypted else "ok_decrypted", url=u_fixed, status=200
    )

    try:
        ext = _mime_to_ext(mt)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{cache_stem}{ext}"

        tmp = cache_path.with_suffix(cache_path.suffix + f".{time.time_ns()}.tmp")
        tmp.write_bytes(decoded)
        os.replace(str(tmp), str(cache_path))

        for other_ext in _SNS_REMOTE_CACHE_EXTS:
            if other_ext.lower() == ext.lower():
                continue
            other = cache_dir / f"{cache_stem}{other_ext}"
            try:
                if other.exists() and other.is_file():
                    other.unlink(missing_ok=True)
            except Exception:
                continue
    except Exception:
        cache_path = None

    return SnsRemoteImageResult(
        payload=decoded,
        media_type=mt,
        source="remote-decrypt" if decrypted else "remote",
        x_enc=str(x_enc or "").strip(),
        cache_path=cache_path,
    )

