from __future__ import annotations

import os
import contextlib
import re
import shutil
import sqlite3
import tempfile


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
    try:
        fd, tmp = tempfile.mkstemp(prefix="wxmoments-cache-", suffix=".db")
        os.close(fd)
        shutil.copyfile(path, tmp)
        return tmp
    except OSError:
        if "tmp" in locals():
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return ""


def _sqlite_rows(path: str) -> list[tuple[str, int, str]]:
    src = _snapshot(path)
    if not src:
        return []
    name = os.path.basename(path)
    out: list[tuple[str, int, str]] = []
    try:
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    except sqlite3.Error:
        with contextlib.suppress(OSError):
            os.unlink(src)
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
        with contextlib.suppress(OSError):
            os.unlink(src)
    return out


def _text_urls(path: str) -> list[tuple[str, int, str]]:
    src = _snapshot(path)
    if not src:
        return []
    try:
        with open(src, "rb") as fh:
            blob = fh.read()
    except OSError:
        return []
    finally:
        with contextlib.suppress(OSError):
            os.unlink(src)
    name = os.path.basename(path)

    return [(name, len(blob) - m.start(), m.group(0).decode("latin-1"))
            for m in URL_RE.finditer(blob)]


def iter_cached_urls():
    for profile in find_profiles():
        for name in DATA_FILES:
            path = os.path.join(profile, name)
            if os.path.isfile(path):
                reader = _sqlite_rows if name in SQL_TABLES else _text_urls
                yield from reader(path)
