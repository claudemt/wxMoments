from __future__ import annotations

import ctypes
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .image_key_resolver import (
    TemplateScanResult,
    current_v2_template,
    scan_v2_templates,
    trusted_xor_for_verified_aes_key,
)



WECHAT_EXECUTABLE_NAMES = frozenset(("weixin.exe", "wechat.exe"))

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100

MAX_MEMORY_REGION_SIZE = 50 * 1024 * 1024
MEMORY_CHUNK_SIZE = 4 * 1024 * 1024
MEMORY_CHUNK_OVERLAP = 68
MAX_USER_ADDRESS = 0x7FFF_FFFF_FFFF

AES_KEY_HEX_LENGTH = 32
AES_KEY_BYTE_LENGTH = 16
UTF16_KEY_BYTE_LENGTH = 64
PROGRESS_REPORT_INTERVAL = 20
EXPANDED_TEMPLATE_LIMIT = 100

_WRITABLE_PROTECTIONS = frozenset(
    (
        PAGE_READWRITE,
        PAGE_WRITECOPY,
        PAGE_EXECUTE_READWRITE,
        PAGE_EXECUTE_WRITECOPY,
    )
)
_ASCII_RUN_RE = re.compile(rb"[A-Za-z0-9]+")
_UTF16_RUN_RE = re.compile(rb"(?:[A-Za-z0-9]\x00)+")

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class MemoryRegion:
    base_address: int
    size: int
    state: int
    protect: int


@dataclass(frozen=True, slots=True)
class ProcessMemoryKeyMatch:
    aes_key: str
    template_path: Path
    encoding: str


@dataclass(frozen=True, slots=True)
class MemoryImageKeyResolution:
    pid: int
    xor_key: int
    aes_key: str
    verified: bool
    template_path: Path
    encoding: str

    def as_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "xor_key": self.xor_key,
            "xor_key_hex": f"0x{self.xor_key:02X}",
            "aes_key": self.aes_key,
            "verified": self.verified,
            "template_path": str(self.template_path),
            "encoding": self.encoding,
        }


class _MemoryApi(Protocol):
    def open_process(self, pid: int) -> object | None: ...

    def query_region(self, handle: object, address: int) -> MemoryRegion | None: ...

    def read_memory(self, handle: object, address: int, size: int) -> bytes: ...

    def close_handle(self, handle: object) -> None: ...


class _MemoryBasicInformation(ctypes.Structure):
    _fields_ = (
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_uint32),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_uint32),
        ("Protect", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
    )


class _Win32MemoryApi:

    def __init__(self, kernel32: object) -> None:
        self._open_process = kernel32.OpenProcess
        self._open_process.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        self._open_process.restype = ctypes.c_void_p

        self._virtual_query_ex = kernel32.VirtualQueryEx
        self._virtual_query_ex.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_MemoryBasicInformation),
            ctypes.c_size_t,
        )
        self._virtual_query_ex.restype = ctypes.c_size_t

        self._read_process_memory = kernel32.ReadProcessMemory
        self._read_process_memory.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        )
        self._read_process_memory.restype = ctypes.c_int

        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = (ctypes.c_void_p,)
        self._close_handle.restype = ctypes.c_int

    def open_process(self, pid: int) -> object | None:
        return self._open_process(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
            False,
            pid,
        )

    def query_region(self, handle: object, address: int) -> MemoryRegion | None:
        info = _MemoryBasicInformation()
        result = self._virtual_query_ex(
            handle,
            ctypes.c_void_p(address),
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not result:
            return None
        return MemoryRegion(
            base_address=int(info.BaseAddress or 0),
            size=int(info.RegionSize),
            state=int(info.State),
            protect=int(info.Protect),
        )

    def read_memory(self, handle: object, address: int, size: int) -> bytes:
        if size <= 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        bytes_read = ctypes.c_size_t(0)
        success = self._read_process_memory(
            handle,
            ctypes.c_void_p(address),
            buffer,
            size,
            ctypes.byref(bytes_read),
        )
        if not success or bytes_read.value <= 0:
            return b""
        return buffer.raw[: min(bytes_read.value, size)]

    def close_handle(self, handle: object) -> None:
        self._close_handle(handle)


def _create_win32_api() -> _Win32MemoryApi | None:
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return _Win32MemoryApi(kernel32)
    except (AttributeError, OSError):
        return None


def find_wechat_pids(
    *,
    process_iter: Callable[..., Iterable[object]] | None = None,
) -> tuple[int, ...]:
    if process_iter is None:
        try:
            import psutil
        except ImportError:
            return ()
        process_iter = psutil.process_iter

    pids: set[int] = set()
    try:
        processes = process_iter(("pid", "name"))
    except Exception:
        return ()

    try:
        for process in processes:
            try:
                info = getattr(process, "info", None) or {}
                name = info.get("name")
                if name is None and hasattr(process, "name"):
                    name = process.name()
                executable_names = WECHAT_EXECUTABLE_NAMES
                if str(name or "").casefold() not in executable_names:
                    continue

                pid = info.get("pid", getattr(process, "pid", None))
                if isinstance(pid, bool):
                    continue
                pid = int(pid)
                if pid > 0:
                    pids.add(pid)
            except (AttributeError, OSError, TypeError, ValueError):
                continue
            except Exception:
                continue
    except Exception:
        pass
    return tuple(sorted(pids))


def _notify(progress: ProgressCallback | None, message: str) -> None:
    if progress is None:
        return
    try:
        progress(message)
    except Exception:
        pass


def _is_scannable_region(region: MemoryRegion) -> bool:
    if region.state != MEM_COMMIT or not (0 < region.size <= MAX_MEMORY_REGION_SIZE):
        return False
    if region.protect & (PAGE_GUARD | PAGE_NOACCESS):
        return False
    return (region.protect & 0xFF) in _WRITABLE_PROTECTIONS


def _deadline_reached(
    deadline: float | None,
    clock: Callable[[], float],
) -> bool:
    return deadline is not None and clock() >= deadline


def _enumerate_scannable_regions(
    api: _MemoryApi,
    handle: object,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[MemoryRegion, ...]:
    regions: list[MemoryRegion] = []
    address = 0
    while address < MAX_USER_ADDRESS:
        if _deadline_reached(deadline, clock):
            break
        try:
            region = api.query_region(handle, address)
        except Exception:
            break
        if region is None:
            break

        next_address = region.base_address + region.size
        if next_address <= address:
            break
        if _is_scannable_region(region):
            regions.append(region)
        address = next_address
    return tuple(regions)


def iter_memory_aes_candidates(
    data: bytes,
    *,
    allow_start_boundary: bool = True,
    allow_end_boundary: bool = True,
) -> Iterator[tuple[str, str]]:
    seen: set[str] = set()

    for match in _ASCII_RUN_RE.finditer(data):
        start, end = match.span()
        if end - start != AES_KEY_HEX_LENGTH:
            continue
        if start == 0 and not allow_start_boundary:
            continue
        if end == len(data) and not allow_end_boundary:
            continue
        aes_key = match.group()[:AES_KEY_BYTE_LENGTH].decode("ascii")
        if aes_key not in seen:
            seen.add(aes_key)
            yield aes_key, "ascii"

    for match in _UTF16_RUN_RE.finditer(data):
        start, end = match.span()
        if end - start != UTF16_KEY_BYTE_LENGTH:
            continue
        if start == 0 and not allow_start_boundary:
            continue
        if end == len(data) and not allow_end_boundary:
            continue

        raw_candidate = match.group()[::2]
        aes_key = raw_candidate[:AES_KEY_BYTE_LENGTH].decode("ascii")
        if aes_key not in seen:
            seen.add(aes_key)
            yield aes_key, "utf-16le"


def find_verified_aes_key_in_chunk(
    data: bytes,
    template_scan: TemplateScanResult,
    *,
    allow_start_boundary: bool = True,
    allow_end_boundary: bool = True,
) -> ProcessMemoryKeyMatch | None:
    template = current_v2_template(template_scan)
    if template is None:
        return None
    for aes_key, encoding in iter_memory_aes_candidates(
        data,
        allow_start_boundary=allow_start_boundary,
        allow_end_boundary=allow_end_boundary,
    ):
        if trusted_xor_for_verified_aes_key(aes_key, template_scan) is not None:
            return ProcessMemoryKeyMatch(
                aes_key=aes_key,
                template_path=template.path,
                encoding=encoding,
            )
    return None


def scan_process_for_image_key(
    pid: int,
    template_scan: TemplateScanResult,
    progress: ProgressCallback | None = None,
    *,
    memory_api: _MemoryApi | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] | None = None,
) -> ProcessMemoryKeyMatch | None:
    if not template_scan.templates:
        return None
    current_template = current_v2_template(template_scan)
    if current_template is None or not any(
        template.tail_xor_key is not None for template in template_scan.templates
    ):
        return None
    clock_fn = clock or time.monotonic
    if _deadline_reached(deadline, clock_fn):
        return None

    api = memory_api or _create_win32_api()
    if api is None:
        return None

    try:
        handle = api.open_process(int(pid))
    except (OSError, TypeError, ValueError):
        return None
    except Exception:
        return None
    if not handle:
        return None

    try:
        regions = _enumerate_scannable_regions(
            api,
            handle,
            deadline=deadline,
            clock=clock_fn,
        )
        total_size = sum(region.size for region in regions)
        _notify(
            progress,
            f"PID {pid}: scanning {len(regions)} writable regions "
            f"({total_size / 1024 / 1024:.0f} MiB)",
        )

        for region_index, region in enumerate(regions):
            if _deadline_reached(deadline, clock_fn):
                return None
            if region_index % PROGRESS_REPORT_INTERVAL == 0:
                _notify(progress, f"PID {pid}: region {region_index}/{len(regions)}")

            offset = 0
            trailing = b""
            while offset < region.size:
                if _deadline_reached(deadline, clock_fn):
                    return None
                request_size = min(MEMORY_CHUNK_SIZE, region.size - offset)
                try:
                    chunk = api.read_memory(
                        handle,
                        region.base_address + offset,
                        request_size,
                    )
                except Exception:
                    chunk = b""

                if not chunk:
                    trailing = b""
                    offset += request_size
                    continue

                chunk = bytes(chunk[:request_size])
                if _deadline_reached(deadline, clock_fn):
                    return None
                data = trailing + chunk
                full_read = len(chunk) == request_size
                match = find_verified_aes_key_in_chunk(
                    data,
                    template_scan,
                    allow_start_boundary=(offset == 0),
                    allow_end_boundary=(
                        full_read and offset + request_size >= region.size
                    ),
                )
                if match is not None:
                    return match

                if full_read:
                    trailing = data[-MEMORY_CHUNK_OVERLAP:]
                else:
                    trailing = b""
                offset += request_size
        return None
    finally:
        try:
            api.close_handle(handle)
        except Exception:
            pass


def _normalise_scanner_match(
    match: ProcessMemoryKeyMatch | MemoryImageKeyResolution | str | bytes | None,
) -> tuple[str, str] | None:
    if match is None:
        return None
    encoding = "ascii"
    value: str | bytes
    if isinstance(match, (ProcessMemoryKeyMatch, MemoryImageKeyResolution)):
        value = match.aes_key
        encoding = match.encoding
    elif isinstance(match, (str, bytes)):
        value = match
    else:
        return None

    try:
        text = value.decode("ascii") if isinstance(value, bytes) else value
        key_bytes = text.encode("ascii")
    except (AttributeError, UnicodeEncodeError):
        return None
    if len(key_bytes) < AES_KEY_BYTE_LENGTH or any(
        not (chr(byte).isascii() and chr(byte).isalnum())
        for byte in key_bytes[:AES_KEY_BYTE_LENGTH]
    ):
        return None
    return key_bytes[:AES_KEY_BYTE_LENGTH].decode("ascii"), encoding


def _normalise_pids(values: Iterable[int]) -> tuple[int, ...]:
    pids: set[int] = set()
    try:
        for value in values:
            if isinstance(value, bool):
                continue
            pid = int(value)
            if pid > 0:
                pids.add(pid)
    except (TypeError, ValueError):
        return ()
    return tuple(sorted(pids))


def scan_image_key_from_memory(
    account_dir: str | os.PathLike[str],
    timeout: float = 60,
    interval: float = 5,
    progress: ProgressCallback | None = None,
    *,
    pid_provider: Callable[[], Iterable[int]] | None = None,
    process_scanner: Callable[
        [int, TemplateScanResult, ProgressCallback | None],
        ProcessMemoryKeyMatch | MemoryImageKeyResolution | str | bytes | None,
    ]
    | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> MemoryImageKeyResolution | None:
    try:
        timeout_seconds = max(0.0, float(timeout))
        interval_seconds = float(interval)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout and interval must be numeric") from exc
    if interval_seconds <= 0:
        raise ValueError("interval must be greater than zero")

    clock_fn = clock or time.monotonic
    deadline = clock_fn() + timeout_seconds
    if _deadline_reached(deadline, clock_fn):
        return None

    template_scan = scan_v2_templates(account_dir)
    if _deadline_reached(deadline, clock_fn):
        return None
    try:
        expanded_scan = scan_v2_templates(account_dir, limit=EXPANDED_TEMPLATE_LIMIT)
    except TypeError:
        expanded_scan = template_scan
    if _deadline_reached(deadline, clock_fn):
        return None
    if len(expanded_scan.templates) >= len(template_scan.templates):
        template_scan = expanded_scan
    if not template_scan.templates:
        _notify(progress, "No V2 image template was found")
        return None
    current_template = current_v2_template(template_scan)
    if current_template is None or not any(
        template.tail_xor_key is not None for template in template_scan.templates
    ):
        _notify(progress, "V2 templates did not provide JPEG XOR evidence")
        return None
    provide_pids = pid_provider or find_wechat_pids
    sleep_fn = sleep or time.sleep

    while clock_fn() < deadline:
        try:
            pids = _normalise_pids(provide_pids())
        except Exception:
            pids = ()
        if pids:
            _notify(progress, f"Scanning WeChat PIDs: {', '.join(map(str, pids))}")
        else:
            process_label = "Weixin.exe or WeChat.exe"
            _notify(progress, f"Waiting for {process_label}")

        for pid in pids:
            if clock_fn() >= deadline:
                return None
            try:
                if process_scanner is None:
                    raw_match = scan_process_for_image_key(
                        pid,
                        template_scan,
                        progress,
                        deadline=deadline,
                        clock=clock_fn,
                    )
                else:
                    raw_match = process_scanner(pid, template_scan, progress)
            except Exception:
                continue
            if clock_fn() >= deadline:
                return None
            normalised = _normalise_scanner_match(raw_match)
            if normalised is None:
                continue
            aes_key, encoding = normalised

            template = current_v2_template(template_scan)
            xor_key = trusted_xor_for_verified_aes_key(aes_key, template_scan)
            if template is None or xor_key is None:
                continue
            return MemoryImageKeyResolution(
                pid=pid,
                xor_key=xor_key,
                aes_key=aes_key,
                verified=True,
                template_path=template.path,
                encoding=encoding,
            )

        remaining = deadline - clock_fn()
        if remaining <= 0:
            return None
        sleep_fn(min(interval_seconds, remaining))
    return None


__all__ = [
    "MEMORY_CHUNK_OVERLAP",
    "MEMORY_CHUNK_SIZE",
    "MAX_MEMORY_REGION_SIZE",
    "MemoryImageKeyResolution",
    "MemoryRegion",
    "ProcessMemoryKeyMatch",
    "find_verified_aes_key_in_chunk",
    "find_wechat_pids",
    "iter_memory_aes_candidates",
    "scan_image_key_from_memory",
    "scan_process_for_image_key",
]
