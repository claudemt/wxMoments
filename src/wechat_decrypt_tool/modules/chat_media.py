"""聊天媒体解析：把消息里的图片 / 文件 / 语音 / 视频还原成本地可用的字节。

微信 4.x 的图片按 `msg/attach/<md5(会话)>/<YYYY-MM>/Img/<md5>.dat` 存放，
文件名与消息 XML 里的 md5 并不一致，因此这里用「会话目录 + 修改时间最接近」
做一对一匹配；文件与视频本身是明文，直接按名称定位。
"""

from __future__ import annotations

import mimetypes
import re
import sqlite3
from bisect import bisect_left
from pathlib import Path
from typing import Any, Iterable

from .logging_config import get_logger
from .media_decrypt import _read_and_maybe_decrypt_media

logger = get_logger(__name__)

from .chat_reader import _decode_bytes

IMAGE_SUFFIXES = {".dat", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".wxgf"}
IMAGE_TOLERANCE = 900          # 消息时间与文件时间允许的最大偏差（秒）
_DERIVED_IMAGE = re.compile(r"_(?:t|h)(?:_w)?\.(?:dat|jpe?g|png|gif|webp|bmp|wxgf)$", re.I)
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}


def _session_hash(session: str) -> str:
    import hashlib

    return hashlib.md5(session.encode("utf-8")).hexdigest()


class ChatMedia:
    """按会话解析媒体文件；所有方法在缺文件时返回 None，不抛异常。"""

    def __init__(self, wxid_dir: str | Path | None, account_dir: str | Path | None = None):
        self.wxid_dir = Path(wxid_dir).expanduser() if wxid_dir else None
        self.account_dir = Path(account_dir).expanduser() if account_dir else self.wxid_dir
        self._images: dict[str, list[tuple[float, Path]]] = {}
        self._videos: list[tuple[float, Path]] | None = None
        self._files: dict[str, list[Path]] | None = None
        self._used: set[Path] = set()

    # -- 基础 ---------------------------------------------------------------
    def _msg_dir(self) -> Path | None:
        if self.wxid_dir is None:
            return None
        direct = self.wxid_dir / "msg"
        return direct if direct.is_dir() else None

    def available(self) -> bool:
        return self._msg_dir() is not None

    # -- 图片 ---------------------------------------------------------------
    def _image_entries(self, session: str) -> list[tuple[float, Path]]:
        if session in self._images:
            return self._images[session]
        entries: list[tuple[float, Path]] = []
        msg_dir = self._msg_dir()
        if msg_dir is not None:
            root = msg_dir / "attach" / _session_hash(session)
            if root.is_dir():
                for path in root.rglob("*"):
                    if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                        continue
                    try:
                        entries.append((path.stat().st_mtime, path))
                    except OSError:
                        continue
        entries.sort(key=lambda item: item[0])
        self._images[session] = entries
        return entries

    @staticmethod
    def _pick(entries: list[tuple[float, Path]], stamp: float,
              used: set[Path]) -> Path | None:
        if not entries:
            return None
        times = [item[0] for item in entries]
        pos = bisect_left(times, stamp)
        best: tuple[float, Path] | None = None
        for offset in range(-6, 7):
            index = pos + offset
            if index < 0 or index >= len(entries):
                continue
            when, path = entries[index]
            if path in used:
                continue
            gap = abs(when - stamp)
            if gap > IMAGE_TOLERANCE:
                continue
            if best is None or gap < best[0]:
                best = (gap, path)
        return best[1] if best else None

    def image_path(self, session: str, create_time: int, *, prefer_thumb: bool = True,
                   used: set[Path] | None = None) -> Path | None:
        entries = self._image_entries(session)
        if not entries:
            return None
        # 只按原图匹配；缩略图（_t / _h / _t_W）和原图共用同一组 mtime，
        # 若混在一起排序，取「原图」时反而会命中缩略图，预览就会一直很糊。
        pool = [item for item in entries if not _DERIVED_IMAGE.search(item[1].name)]
        used = used if used is not None else self._used
        path = self._pick(pool, float(create_time), used)
        if path is None:
            return None
        if prefer_thumb:
            for suffix in ("_t", "_t_W", "_h"):
                candidate = path.with_name(f"{path.stem}{suffix}{path.suffix}")
                if candidate.is_file():
                    return candidate
        return path

    def image_bytes(self, session: str, create_time: int, *,
                    prefer_thumb: bool = True) -> tuple[bytes, str]:
        path = self.image_path(session, create_time, prefer_thumb=prefer_thumb)
        if path is None:
            return b"", ""
        return self.read_media(path)

    def read_media(self, path: Path) -> tuple[bytes, str]:
        try:
            data, mime = _read_and_maybe_decrypt_media(path, self.account_dir, self.wxid_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取媒体失败 %s: %s", path.name, exc)
            return b"", ""
        if not data:
            return b"", ""
        if mime in ("", "application/octet-stream"):
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return data, mime

    # -- 文件 ---------------------------------------------------------------
    def _file_index(self) -> dict[str, list[Path]]:
        if self._files is not None:
            return self._files
        index: dict[str, list[Path]] = {}
        msg_dir = self._msg_dir()
        if msg_dir is not None:
            root = msg_dir / "file"
            if root.is_dir():
                for path in root.rglob("*"):
                    if path.is_file():
                        index.setdefault(path.name.casefold(), []).append(path)
        self._files = index
        return index

    def file_path(self, filename: str, size: int = 0) -> Path | None:
        name = (filename or "").strip()
        if not name:
            return None
        candidates = self._file_index().get(Path(name).name.casefold()) or []
        if not candidates:
            return None
        if size:
            for path in candidates:
                try:
                    if abs(path.stat().st_size - int(size)) <= max(2, int(size) // 100):
                        return path
                except (OSError, ValueError):
                    continue
        return candidates[0]

    # -- 视频 ---------------------------------------------------------------
    def _video_entries(self) -> list[tuple[float, Path]]:
        if self._videos is not None:
            return self._videos
        entries: list[tuple[float, Path]] = []
        msg_dir = self._msg_dir()
        if msg_dir is not None:
            root = msg_dir / "video"
            if root.is_dir():
                for path in root.rglob("*"):
                    if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
                        continue
                    try:
                        entries.append((path.stat().st_mtime, path))
                    except OSError:
                        continue
        entries.sort(key=lambda item: item[0])
        self._videos = entries
        return entries

    def video_path(self, create_time: int, md5: str = "") -> Path | None:
        entries = self._video_entries()
        if not entries:
            return None
        if md5:
            for _when, path in entries:
                if md5 in path.stem:
                    return path
        return self._pick(entries, float(create_time), set())

    def video_thumb(self, create_time: int, md5: str = "") -> Path | None:
        video = self.video_path(create_time, md5)
        if video is None:
            return None
        thumb = video.with_name(video.stem + "_thumb.jpg")
        return thumb if thumb.is_file() else None

    # -- 语音 ---------------------------------------------------------------
    def voice_bytes(self, media_dbs: Iterable[Path], session: str, server_id: str
                    ) -> tuple[bytes, str, str]:
        from .media_decrypt import _convert_silk_to_browser_audio

        if not server_id:
            return b"", "", ""
        for path in media_dbs:
            if not path.is_file():
                continue
            conn: sqlite3.Connection | None = None
            try:
                conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                conn.text_factory = bytes
                names = {_decode_bytes(r[0]) for r in conn.execute(
                    "select name from sqlite_master where type='table'")}
                if "VoiceInfo" not in names:
                    continue
                row = conn.execute(
                    "select voice_data from VoiceInfo where svr_id=? limit 1",
                    (int(server_id),)).fetchone()
                if row is None or not row[0]:
                    continue
                blob = bytes(row[0])
                data, ext, mime = _convert_silk_to_browser_audio(blob)
                if data:
                    return data, ext, mime
                return blob, "silk", "audio/silk"
            except (sqlite3.Error, ValueError) as exc:
                logger.warning("%s", exc)
                continue
            finally:
                if conn is not None:
                    conn.close()
        return b"", "", ""


def human_size(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.1f} {units[index]}"


def safe_filename(name: str, fallback: str = "file") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip()).strip(" .")
    return (cleaned or fallback)[:120]
