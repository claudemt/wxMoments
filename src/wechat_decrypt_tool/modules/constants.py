"""Named constants for wxMoments.

Centralize all magic number and string literals used across the codebase.
"""

from __future__ import annotations

# ── Media types ──
MEDIA_TYPE_IMAGE = 2
MEDIA_TYPE_VIDEO = 1
MEDIA_TYPE_LIVE_PHOTO = 4

# ── Post types ──
POST_TYPE_NORMAL = 1
POST_TYPE_ARTICLE = 3        # 公众号文章
POST_TYPE_LINK = 5           # 外部分享链接
POST_TYPE_COVER = 7          # 朋友圈封面
POST_TYPE_FINDER = 28        # 视频号
POST_TYPE_MUSIC = 42         # 音乐/链接卡片

# ── Query limits ──
DEFAULT_PAGE_LIMIT = 200
MAX_PAGE_LIMIT = 200
MAX_IMAGE_DOWNLOAD_BYTES = 25 * 1024 * 1024    # 25MB
MAX_VIDEO_DOWNLOAD_BYTES = 200 * 1024 * 1024   # 200MB
VIDEO_DECRYPT_SIZE = 131072                     # 128KB

# ── Time windows ──
IMAGE_CACHE_WINDOW_SECONDS = 72 * 3600          # 72小时
SNS_AUTO_CACHE_TTL_SECONDS = 60

# ── Database key ──
DB_KEY_HEX_LENGTH = 64
INTERNAL_DB_KEY_BYTE_LENGTH = 32

# ── Image key ──
IMAGE_AES_KEY_LENGTH = 16
IMAGE_XOR_KEY_MAX = 255

# ── PDF ──
PDF_FONT_NAME = "STSong-Light"
PDF_FONT_FALLBACK = "Helvetica"
PDF_DPI = 240
PDF_MARGIN_LEFT = 42
PDF_MARGIN_RIGHT = 42
PDF_MARGIN_TOP = 44
PDF_MARGIN_BOTTOM = 48

# ── WeChat process names ──
WECHAT_PROCESS_NAMES_WINDOWS = frozenset({"weixin.exe", "wechat.exe"})

# ── CDN domains ──
ALLOWED_CDN_DOMAINS = frozenset({
    ".qpic.cn",
    ".qlogo.cn",
    ".tc.qq.com",
    ".video.qq.com",
})
