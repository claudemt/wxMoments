"""Named constants for wxMoments.

Centralize all magic number and string literals used across the codebase.
"""

from __future__ import annotations


MEDIA_TYPE_IMAGE = 2
MEDIA_TYPE_VIDEO = 1
MEDIA_TYPE_LIVE_PHOTO = 4


POST_TYPE_NORMAL = 1
POST_TYPE_ARTICLE = 3        
POST_TYPE_LINK = 5           
POST_TYPE_COVER = 7          
POST_TYPE_FINDER = 28        
POST_TYPE_MUSIC = 42         


DEFAULT_PAGE_LIMIT = 200
MAX_PAGE_LIMIT = 200
MAX_IMAGE_DOWNLOAD_BYTES = 25 * 1024 * 1024    
MAX_VIDEO_DOWNLOAD_BYTES = 200 * 1024 * 1024   
VIDEO_DECRYPT_SIZE = 131072                     


IMAGE_CACHE_WINDOW_SECONDS = 72 * 3600          
SNS_AUTO_CACHE_TTL_SECONDS = 60


DB_KEY_HEX_LENGTH = 64
INTERNAL_DB_KEY_BYTE_LENGTH = 32


IMAGE_AES_KEY_LENGTH = 16
IMAGE_XOR_KEY_MAX = 255


PDF_FONT_NAME = "STSong-Light"
PDF_FONT_FALLBACK = "Helvetica"
PDF_DPI = 240
PDF_MARGIN_LEFT = 42
PDF_MARGIN_RIGHT = 42
PDF_MARGIN_TOP = 44
PDF_MARGIN_BOTTOM = 48


WECHAT_PROCESS_NAMES_WINDOWS = frozenset({"weixin.exe", "wechat.exe"})


ALLOWED_CDN_DOMAINS = frozenset({
    ".qpic.cn",
    ".qlogo.cn",
    ".tc.qq.com",
    ".video.qq.com",
})
