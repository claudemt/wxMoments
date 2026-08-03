from __future__ import annotations

import logging
import os
from pathlib import Path

_LOG_FILE: Path | None = None


def _runtime_log_file() -> Path:
    root = Path(os.environ.get("WXMOMENTS_RUNTIME_DIR", "") or "runtime")
    root.mkdir(parents=True, exist_ok=True)
    return root / "wxmoments.log"


def setup_logging(log_level: str = "INFO") -> Path:
    global _LOG_FILE

    level_name = os.environ.get("WECHAT_TOOL_LOG_LEVEL", log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = _runtime_log_file()

    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    root.setLevel(level)
    root.addHandler(file_handler)

    console_flag = os.environ.get("WECHAT_TOOL_ENABLE_CONSOLE_LOG", "").strip().lower()
    if console_flag in {"1", "true", "yes", "on"}:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        root.addHandler(console_handler)

    _LOG_FILE = log_file
    return log_file


def get_logger(name: str) -> logging.Logger:
    if _LOG_FILE is None:
        setup_logging()
    return logging.getLogger(name)


def get_log_file_path() -> Path:
    if _LOG_FILE is None:
        return setup_logging()
    return _LOG_FILE
