from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional

_CDATA_BLOCK_RE = re.compile(r"<!\[CDATA\[[\s\S]*?\]\]>", flags=re.IGNORECASE)
_BARE_AMP_RE = re.compile(r"&(?!(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);)")
_INVALID_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_STRAY_LT_RE = re.compile(r"<(?![A-Za-z/!?])")
_TEXT_FIELD_RE = re.compile(
    r"(<(contentDesc|description|title|nickname|descr|displayname|nickName|summary)\b[^>]*>)([\s\S]*?)(</\2\s*>)",
    flags=re.IGNORECASE,
)
_FIELD_INLINE_TAGS = r"br|p|div|span|a|b|i|u|s|em|strong|font|img|li|ul|ol|h[1-6]"
_FIELD_TAG_LT_RE = re.compile(rf"<(?!/?(?:{_FIELD_INLINE_TAGS})\b)", flags=re.IGNORECASE)
_FRAGMENT_TAG = "wechat-fragment"


def looks_like_xml(text: str) -> bool:
    value = str(text or "").lstrip()
    if value.startswith('"') and value.endswith('"'):
        value = value.strip('"').lstrip()
    return value.startswith("<")


def _apply_outside_cdata(xml_text: str, fn) -> str:
    parts: list[str] = []
    last = 0
    for match in _CDATA_BLOCK_RE.finditer(xml_text):
        parts.append(fn(xml_text[last:match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(fn(xml_text[last:]))
    return "".join(parts)


def sanitize_xml_text(xml_text: str) -> str:
    text = str(xml_text or "").replace("\ufeff", "")
    if not text:
        return ""
    return _INVALID_CHARS_RE.sub("", text).strip()


def escape_stray_markup(xml_text: str) -> str:
    def fix(chunk: str) -> str:
        return _BARE_AMP_RE.sub("&amp;", _STRAY_LT_RE.sub("&lt;", chunk))

    return _apply_outside_cdata(str(xml_text or ""), fix)


def _escape_field_text(match: re.Match[str]) -> str:
    inner = match.group(3)
    if "<![CDATA[" in inner:
        return match.group(0)
    escaped = _FIELD_TAG_LT_RE.sub("&lt;", inner)
    escaped = _BARE_AMP_RE.sub("&amp;", escaped)
    if escaped == inner:
        return match.group(0)
    return f"{match.group(1)}{escaped}{match.group(4)}"


def escape_text_fields(xml_text: str) -> str:
    return _TEXT_FIELD_RE.sub(_escape_field_text, str(xml_text or ""))


def _recover_with_lxml(xml_text: str) -> Optional[ET.Element]:
    try:
        from lxml import etree as lxml_etree
    except Exception:
        return None
    try:
        parser = lxml_etree.XMLParser(recover=True, huge_tree=True)
        recovered = lxml_etree.fromstring(xml_text.encode("utf-8"), parser)
    except Exception:
        return None
    if recovered is None:
        return None
    try:
        return ET.fromstring(lxml_etree.tostring(recovered, encoding="utf-8"))
    except Exception:
        return None


def _parse_fragment(xml_text: str) -> Optional[ET.Element]:
    try:
        root = ET.fromstring(f"<{_FRAGMENT_TAG}>{xml_text}</{_FRAGMENT_TAG}>")
    except Exception:
        return None
    children = list(root)
    return children[0] if len(children) == 1 else root


def _repair_candidates(xml_text: str) -> list[str]:
    field_escaped = escape_text_fields(xml_text)
    if field_escaped != xml_text:
        repaired = escape_stray_markup(field_escaped)
        return [field_escaped, repaired] if repaired != field_escaped else [field_escaped]
    repaired = escape_stray_markup(xml_text)
    return [xml_text, repaired] if repaired != xml_text else [xml_text]


def parse_xml(xml_text: str) -> Optional[ET.Element]:
    sanitized = sanitize_xml_text(xml_text)
    if not sanitized:
        return None

    candidates = _repair_candidates(sanitized)
    for candidate in candidates:
        try:
            return ET.fromstring(candidate)
        except Exception:
            continue

    for candidate in candidates:
        root = _parse_fragment(candidate)
        if root is not None:
            return root

    for candidate in candidates:
        recovered = _recover_with_lxml(candidate)
        if recovered is not None:
            return recovered
    return None
