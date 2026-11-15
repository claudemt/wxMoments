from __future__ import annotations

from pathlib import Path


_IGNORED_SOURCE_DATABASE_NAMES = frozenset({"key_info.db"})
_INDEX_DATABASE_NAMES = frozenset(
    {
        "chat_search_index.db",
        "chat_search_index.tmp.db",
    }
)
_INDEX_DATABASE_SUFFIXES = ("_fts.db",)
_INTERNAL_OUTPUT_DATABASE_NAMES = frozenset(
    {
        "chat_search_index.db",
        "chat_search_index.tmp.db",
        "session_last_message.db",
    }
)


def normalize_database_file_name(file_name: str | Path) -> str:
    return Path(str(file_name or "")).name.strip().lower()


def is_index_database_file(file_name: str | Path) -> bool:
    lower_name = normalize_database_file_name(file_name)
    if not lower_name:
        return False
    if lower_name in _INDEX_DATABASE_NAMES:
        return True
    return lower_name.endswith(_INDEX_DATABASE_SUFFIXES)


def should_skip_source_database(file_name: str | Path) -> bool:
    lower_name = normalize_database_file_name(file_name)
    if not lower_name:
        return True
    if lower_name in _IGNORED_SOURCE_DATABASE_NAMES:
        return True
    return is_index_database_file(lower_name)


def should_include_in_database_count(file_name: str | Path) -> bool:
    lower_name = normalize_database_file_name(file_name)
    if not lower_name.endswith(".db"):
        return False
    if should_skip_source_database(lower_name):
        return False
    if lower_name in _INTERNAL_OUTPUT_DATABASE_NAMES:
        return False
    return True
