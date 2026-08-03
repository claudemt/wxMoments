from __future__ import annotations

import multiprocessing
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any


PATTERN = re.compile(
    b"^\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\x85\xC0",
    re.DOTALL,
)

CODE_SECTION_CHARACTERISTIC = 0x20000000
CHUNK_SIZE = 2 * 1024 * 1024
OVERLAP_SIZE = 100


def _format_spaced_hex(key_bytes: bytes) -> str:
    key_hex = key_bytes.hex().upper()
    return " ".join(key_hex[i : i + 2] for i in range(0, len(key_hex), 2))


def worker_search(task: tuple[str, int, int, int, int]) -> list[dict[str, Any]]:
    file_path, file_offset, chunk_size, overlap, base_va = task
    results: list[dict[str, Any]] = []

    with open(file_path, "rb") as f:
        f.seek(file_offset)
        chunk_data = f.read(chunk_size + overlap)

    offset = 0
    while True:
        idx = chunk_data.find(b"\x48\xBA", offset)
        if idx == -1 or idx >= chunk_size:
            break

        match = PATTERN.match(chunk_data[idx : idx + 85])
        if match:
            key_bytes = match.group(1) + match.group(2) + match.group(3) + match.group(4)
            results.append(
                {
                    "va": f"0x{base_va + idx:X}",
                    "file_offset": f"0x{file_offset + idx:X}",
                    "key": _format_spaced_hex(key_bytes),
                    "key_hex": key_bytes.hex(),
                }
            )
            offset = idx + len(match.group(0))
        else:
            offset = idx + 1

    return results


def extract_xor_keys_from_dll(
    dll_path: str | Path,
    *,
    max_workers: int | None = None,
) -> list[dict[str, Any]]:
    try:
        import pefile
    except Exception as e:
        raise RuntimeError("pefile dependency is required to scan Weixin.dll for the DB key") from e

    path = Path(dll_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Weixin.dll not found: {path}")

    try:
        pe = pefile.PE(str(path), fast_load=True)
    except Exception as e:
        raise RuntimeError(f"PE parse failed: {e}") from e

    tasks: list[tuple[str, int, int, int, int]] = []
    try:
        image_base = int(pe.OPTIONAL_HEADER.ImageBase)
        for section in pe.sections:
            if not (int(section.Characteristics) & CODE_SECTION_CHARACTERISTIC):
                continue

            sec_size = int(section.SizeOfRawData)
            sec_file_offset = int(section.PointerToRawData)
            sec_va = image_base + int(section.VirtualAddress)

            for i in range(0, sec_size, CHUNK_SIZE):
                current_chunk_size = min(CHUNK_SIZE, sec_size - i)
                tasks.append(
                    (
                        str(path),
                        sec_file_offset + i,
                        current_chunk_size,
                        OVERLAP_SIZE,
                        sec_va + i,
                    )
                )
    finally:
        try:
            pe.close()
        except Exception:
            pass

    if not tasks:
        return []

    worker_count = max(1, int(max_workers or multiprocessing.cpu_count()))
    worker_count = min(worker_count, len(tasks))
    found_matches: list[dict[str, Any]] = []

    if worker_count == 1:
        for task in tasks:
            found_matches.extend(worker_search(task))
    else:
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(worker_search, task) for task in tasks]
            for future in as_completed(futures):
                found_matches.extend(future.result())

    found_matches.sort(key=lambda item: int(str(item.get("va") or "0"), 16))
    return found_matches
