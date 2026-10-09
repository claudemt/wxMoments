"""导出编排：把一篇文章落成 <html / md / figures> 三样东西。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from . import harvest, render, wechat
from .config import OUTPUT_DIR

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_SPACES = re.compile(r"\s+")


def safe_name(name: str, limit: int = 70) -> str:
    name = _BAD_CHARS.sub("_", (name or "").strip())
    name = _SPACES.sub(" ", name).strip(" .")
    if len(name) > limit:
        name = name[:limit].rstrip()
    return name or "未命名"


@dataclass
class ExportResult:
    article_dir: Path
    html_file: Path
    md_file: Path
    figures: list[str]
    failed_images: int = 0


def article_dir_name(art: wechat.Article) -> str:
    account = safe_name(art.account or "公众号", 30)
    
    title = safe_name(art.title or (f"文章{art.mid}" if art.mid else "未命名文章"), 60)
    return f"{account}-{title}"


def export_article(art: wechat.Article, root: Path | None = None,
                   progress=None) -> ExportResult:
    """把文章导出到 root/<公众号名>-<文章名>/ 下。只导出正文。"""
    root = Path(root or OUTPUT_DIR)
    dir_name = article_dir_name(art)
    adir = root / dir_name
    fdir = adir / "figures"
    fdir.mkdir(parents=True, exist_ok=True)

    if progress:
        progress("保存图片…")

    soup = BeautifulSoup(art.content_html or "", "lxml")
    url_map: dict[str, str] = {}
    files: list[str] = []
    failed = 0

    def localize(src: str) -> str:
        """把一个远端图片地址下载到 figures/，返回本地相对路径。"""
        nonlocal failed
        if src in url_map:
            return url_map[src]
        try:
            data, ext = wechat.download_image(wechat.full_size_url(src))
            fname = f"{len(url_map) + 1:02d}.{ext}"
            (fdir / fname).write_bytes(data)
            rel = f"figures/{fname}"
            files.append(fname)
        except Exception:
            failed += 1
            rel = src  
        url_map[src] = rel
        return rel

    for img in soup.find_all("img"):
        src = img.get("src")
        if src:
            img["src"] = localize(src)
        img.attrs.pop("data-src", None)

    
    style_url_re = re.compile(r"url\((['\"]?)(https?://[^)'\"]+)\1\)")
    for tag in soup.find_all(style=True):
        style = tag["style"]
        if "url(" not in style:
            continue
        tag["style"] = style_url_re.sub(lambda m: f"url({localize(m.group(2))})", style)

    body_html = soup.body.decode_contents() if soup.body else (art.content_html or "")

    meta_bits = []
    if art.account:
        meta_bits.append(art.account)
    if art.author and art.author != art.account:
        meta_bits.append(art.author)
    if art.publish_text:
        meta_bits.append(art.publish_text)
    meta_line = " ｜ ".join(meta_bits)

    stem = safe_name(art.account or "公众号", 30)
    stem += "-" + safe_name(art.title or (f"文章{art.mid}" if art.mid else "未命名文章"), 60)

    html_file = adir / f"{stem}.html"
    html_file.write_text(
        render.html_document(art.title, meta_line, body_html,
                             harvest.public_url(art, art.url)),
        encoding="utf-8",
    )

    md_body = render.render_markdown(body_html)
    md_lines = [f"# {art.title}", ""]
    if meta_line:
        md_lines += [f"> {meta_line}  ",
                     f"> 原文：{harvest.public_url(art, art.url)}", ""]
    md_file = adir / f"{stem}.md"
    md_file.write_text("\n".join(md_lines) + "\n" + md_body, encoding="utf-8")

    return ExportResult(adir, html_file, md_file, files, failed)
