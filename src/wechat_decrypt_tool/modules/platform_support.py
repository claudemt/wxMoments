from __future__ import annotations

import sys
from typing import Any


def current_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return str(sys.platform or "unknown")


def is_windows() -> bool:
    return current_platform() == "windows"


def runtime_capabilities() -> dict[str, Any]:
    system = current_platform()
    return {
        "platform": system,
        "database_key_extraction": True,
        "database_key_manual_input": True,
        "database_decryption": True,
        "image_key_memory_scan": system == "windows",
        "realtime_wcdb": system == "windows",
        "account_archive_export": True,
        "account_archive_import": True,
        "account_archive_cross_platform": True,
    }


__all__ = [
    "current_platform",
    "is_windows",
    "runtime_capabilities",
]
