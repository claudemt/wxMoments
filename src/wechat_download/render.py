"""正文清洗 + Markdown 渲染 + HTML 模板。

原则：只保留核心文字与图片。
文章末尾的点赞/在看/分享/赞赏/关注引导/二维码/推荐阅读等模块一律不进入正文，
因为正文只取 #js_content，文末模块本来就在它外面；这里再做一次保险的清洗。
"""
from __future__ import annotations

import html as _html
import re

from bs4 import BeautifulSoup, NavigableString, Tag


DROP_TAGS = {
    "script", "style", "noscript", "svg", "canvas", "map", "button",
    "input", "form", "select", "textarea", "iframe", "embed", "object",
    "mp-style-type", "mpcpc", "mp-common-template",
}


DROP_KEYWORDS = (
    "js_pc_qr_code", "qr_code", "js_qr", "reward", "like", "comment",
    "share", "js_ad", "ad_area", "ad_", "advert", "js_tags", "vote",
    "js_profile", "rich_media_tool", "js_unified_extra", "js_article_bottom",
    "guess_like", "related", "js_related", "recommend", "js_sponsor",
    "js_pc_weapp", "weapp_card", "js_minipro", "js_author", "js_follow",
    "js_bottom", "js_article_comment", "js_mp_", "js_toobar", "js_temp_bottom",
    "js_underline_content", "read_more", "original_area", "appmsg_like",
)


INLINE_TAGS = {
    "a", "span", "strong", "b", "em", "i", "u", "code", "br", "img", "sub",
    "sup", "small", "label", "font", "mark", "del", "s", "abbr", "time",
    "big", "tt", "strike", "q", "cite", "dfn", "kbd", "samp", "var", "wbr",
}


BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "h1", "h2", "h3",
    "h4", "h5", "h6", "blockquote", "ul", "ol", "li", "pre", "table",
    "thead", "tbody", "tr", "td", "th", "hr", "figure", "figcaption", "dl",
    "dt", "dd", "center", "main", "aside", "nav", "address", "fieldset",
}

KEEP_ATTRS = {
    "style", "src", "href", "alt", "title", "colspan", "rowspan", "width",
    "height", "align", "data-src", "class",
}


def _is_junk(tag: Tag) -> bool:
    if not tag or tag.name in DROP_TAGS:
        return True
    attrs = tag.attrs or {}  
    cls = " ".join(attrs.get("class") or [])
    tid = attrs.get("id") or ""
    mark = f"{cls} {tid}".lower()
    if not mark.strip():
        return False
    return any(k in mark for k in DROP_KEYWORDS)


def clean_content(node: Tag) -> str:
    """清洗 #js_content，返回不可信但干净的 HTML 片段。"""
    soup = BeautifulSoup(str(node), "lxml")
    node = soup.select_one("#js_content")
    if node is None:  
        node = soup.body or node

    
    for tag in node.find_all(True):
        if _is_junk(tag):
            tag.decompose()

    
    for img in node.find_all("img"):
        real = img.get("data-src") or img.get("src") or ""
        if real:
            img["src"] = real.replace("&amp;", "&")
        if not img.get("alt"):
            img["alt"] = ""

    
    
    MEDIA_TAGS = ("mpvoice", "mp-common-videosnap", "mp-common-mpvideo",
                  "mp-common-qqmusic", "mp-common-videodata", "qqmusic",
                  "iframe")
    COVER_ATTRS = ("poster", "data-poster", "data-coverurl", "coverurl",
                   "albumurl", "cover_url")
    for tag in node.find_all(MEDIA_TAGS):
        kind = "音频" if tag.name in ("mpvoice", "mp-common-qqmusic", "qqmusic") else "视频"
        cover = next((tag.get(a) for a in COVER_ATTRS if tag.get(a)), "")
        if cover:
            fig = soup.new_tag("figure")
            fig["class"] = "media"
            img = soup.new_tag("img")
            img["src"] = cover
            img["alt"] = f"{kind}封面"
            cap = soup.new_tag("figcaption")
            cap.string = f"［{kind}，保留封面，未导出媒体本体］"
            fig.append(img)
            fig.append(cap)
            tag.replace_with(fig)
        else:
            tag.replace_with(
                BeautifulSoup(f"<p>［此处为文章内的{kind}内容，未导出］</p>", "lxml").p
            )

    
    for tag in node.find_all(True):
        attrs = tag.attrs or {}
        for attr in list(attrs):
            if attr not in KEEP_ATTRS:
                del attrs[attr]
        tag.attrs = attrs

    
    for _ in range(6):
        removed = 0
        for tag in reversed(node.find_all(BLOCK_TAGS)):
            if tag.find(["img", "table", "pre", "hr", "video", "audio"]):
                continue
            if tag.get_text(strip=True):
                continue
            tag.decompose()
            removed += 1
        if not removed:
            break

    return node.decode_contents()





_WS_RE = re.compile(r"[\s ​‌‍﻿]+")


def _text(s: str) -> str:
    return _WS_RE.sub(" ", s)


def _img_md(tag: Tag) -> str:
    src = tag.get("src") or ""
    if not src:
        return ""
    alt = (tag.get("alt") or "").strip()
    return f"![{alt}]({src})"


def _inline(node) -> str:
    
    if isinstance(node, Tag) and node.name == "img":
        return _img_md(node)
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            parts.append(_text(str(child)))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name in DROP_TAGS or _is_junk(child):
            continue
        if name == "img":
            parts.append(_img_md(child))
        elif name == "br":
            parts.append("\n")
        elif name == "a":
            text = _inline(child).strip()
            href = (child.get("href") or "").replace("&amp;", "&").strip()
            if text and href and not href.lower().startswith(("javascript:", "#")):
                parts.append(f"[{text}]({href})")
            elif text:
                parts.append(text)
            elif href:
                parts.append(f"<{href}>")
        elif name in ("strong", "b"):
            t = _inline(child).strip()
            parts.append(f"**{t}**" if t else "")
        elif name in ("em", "i"):
            t = _inline(child).strip()
            parts.append(f"*{t}*" if t else "")
        elif name in ("del", "s", "strike"):
            t = _inline(child).strip()
            parts.append(f"~~{t}~~" if t else "")
        elif name == "code":
            t = child.get_text()
            parts.append(f"`{t}`" if t else "")
        elif name in BLOCK_TAGS:
            parts.append("\n\n" + _block(child) + "\n\n")
        else:
            parts.append(_inline(child))
    return "".join(parts)


def _has_block_child(node: Tag) -> bool:
    return any(
        isinstance(c, Tag) and c.name in BLOCK_TAGS and c.name not in ("br",)
        for c in node.children
    )


def _block(node: Tag) -> str:
    name = node.name

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        t = _inline(node).strip()
        return f"{'#' * level} {t}" if t else ""

    if name == "hr":
        return "---"

    if name == "pre":
        code = node.get_text()
        lang = ""
        m = re.search(r"language-([\w+-]+)", " ".join(node.get("class") or []))
        if m:
            lang = m.group(1)
        else:
            lang = node.get("data-lang") or ""
        return f"```{lang}\n{code.rstrip()}\n```"

    if name == "blockquote":
        inner = _children(node).strip()
        return "\n".join("> " + ln if ln.strip() else ">" for ln in inner.split("\n"))

    if name in ("ul", "ol"):
        lines = []
        for i, li in enumerate(node.find_all("li", recursive=False), 1):
            head = "- "
            if name == "ol":
                head = f"{i}. "
            body = _children(li).strip()
            body = body.replace("\n", "\n" + " " * len(head))
            lines.append(head + body)
        return "\n".join(lines)

    if name == "table":
        return _table(node)

    if name == "li":
        body = _children(node).strip()
        return f"- {body}"

    
    if _has_block_child(node):
        return _children(node)
    content = _inline(node).strip()
    return content


def _children(node: Tag) -> str:
    return "".join(_block(c) + "\n\n" if isinstance(c, Tag) and c.name in BLOCK_TAGS
                   else _inline(c) if isinstance(c, Tag)
                   else _text(str(c))
                   for c in node.children)


def _table(node: Tag) -> str:
    rows = []
    for tr in node.find_all("tr"):
        cells = [re.sub(r"\s*\n\s*", " ", _inline(td).strip()).replace("|", "\\|")
                 for td in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join(["---"] * width) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def render_markdown(content_html: str) -> str:
    soup = BeautifulSoup(content_html, "lxml")
    root = soup.body or soup
    md = _children(root)
    return _tidy(md)


def _tidy(md: str) -> str:
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    md = re.sub(r"[ \t]+\n", "\n", md)
    lines: list[str] = []
    for raw in md.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        
        if stripped in ("", "****", "**", "[]()", "![]()", "![](  )", "> ", "-", "--- "):
            if stripped in ("",) and lines and lines[-1] == "":
                continue
            if stripped == "":
                lines.append("")
                continue
            continue
        
        if lines and lines[-1] == line and line.startswith("!["):
            continue
        lines.append(line)
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip() + "\n"





HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0; padding: 24px 16px 64px;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 17px; line-height: 1.75; color: #222; background: #fff;
    -webkit-text-size-adjust: 100%;
  }}
  .wrap {{ max-width: 677px; margin: 0 auto; }}
  h1.title {{ font-size: 22px; line-height: 1.4; margin: 0 0 12px; font-weight: 600; }}
  .meta {{ font-size: 14px; color: #8a8a8a; margin-bottom: 6px; }}
  .meta a {{ color: #8a8a8a; }}
  .src {{ font-size: 12px; color: #b0b0b0; margin-bottom: 24px; word-break: break-all; }}
  hr.sep {{ border: 0; border-top: 1px solid #eee; margin: 16px 0 24px; }}
  #content {{ word-break: break-word; }}
  #content img {{ max-width: 100%; height: auto; display: block; margin: 12px auto; }}
  #content figure.media {{ margin: 24px 0; text-align: center; }}
  #content figure.media img {{ margin-bottom: 8px; border-radius: 6px; }}
  #content figure.media figcaption {{ font-size: 13px; color: #999; }}
  #content p {{ margin: 0 0 16px; }}
  #content section {{ margin: 0; }}
  #content blockquote {{ margin: 16px 0; padding: 4px 14px; border-left: 3px solid #ddd; color: #666; }}
  #content pre {{ background: #f6f8fa; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 14px; }}
  #content code {{ font-family: Consolas, Monaco, monospace; }}
  #content table {{ border-collapse: collapse; margin: 16px 0; }}
  #content td, #content th {{ border: 1px solid #ddd; padding: 6px 10px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #1b1b1b; color: #d8d8d8; }}
    .meta, .meta a {{ color: #8f8f8f; }}
    #content blockquote {{ border-color: #444; color: #aaa; }}
    #content pre {{ background: #262626; }}
    #content td, #content th {{ border-color: #444; }}
    hr.sep {{ border-color: #333; }}
    #content figure.media figcaption {{ color: #777; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <h1 class="title">{title}</h1>
  <div class="meta">{meta}</div>
  <div class="src">原文：<a href="{url}">{url}</a></div>
  <hr class="sep">
  <div id="content">
{body}
  </div>
</div>
</body>
</html>
"""


def html_document(title: str, meta: str, body_html: str, url: str) -> str:
    body = "\n".join("    " + ln if ln.strip() else "" for ln in body_html.split("\n"))
    return HTML_TEMPLATE.format(
        title=_html.escape(title),
        meta=meta,
        url=_html.escape(url),
        body=body,
    )
