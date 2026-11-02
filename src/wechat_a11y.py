from __future__ import annotations

import ctypes
import json
import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

CORE_MARKER = b"qt.accessibility.core"

GATE_PATTERN = re.compile(
    rb"\x48\x85\xc9\x0f\x84(?P<jump>.{4})\x80\x3d(?P<disp>.{4})\x00\x0f\x84", re.S)
LEA_PATTERN = re.compile(rb"\x48\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d](?P<disp>.{4})", re.S)

IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_WRITE = 0x80000000

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008

TH32CS_SNAPMODULE = 0x00000008
MAX_CANDIDATES = 4


class _ModuleEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("th32ModuleID", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("GlblcntUsage", ctypes.c_ulong),
        ("ProccntUsage", ctypes.c_ulong),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", ctypes.c_ulong),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


@dataclass
class GateResult:
    ok: bool
    code: str
    message: str = ""
    detail: str = ""
    rva: int = 0
    candidates: int = 0


def pe_sections(data: Any) -> list[dict[str, Any]]:
    try:
        pe_off = struct.unpack_from("<I", data, 0x3C)[0]
        if bytes(data[pe_off:pe_off + 4]) != b"PE\0\0":
            return []
        coff = pe_off + 4
        count = struct.unpack_from("<H", data, coff + 2)[0]
        opt_size = struct.unpack_from("<H", data, coff + 16)[0]
        sec_off = coff + 20 + opt_size
        out: list[dict[str, Any]] = []
        for i in range(count):
            off = sec_off + i * 40
            name = bytes(data[off:off + 8]).split(b"\0", 1)[0].decode("ascii", "ignore")
            vsize, rva, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
            chars = struct.unpack_from("<I", data, off + 36)[0]
            out.append({"name": name, "rva": rva, "vsize": vsize,
                        "raw_size": raw_size, "raw_ptr": raw_ptr, "chars": chars})
        return out
    except Exception:
        return []


def _off_to_rva(sections: Sequence[dict[str, Any]], offset: int) -> int | None:
    for sec in sections:
        if sec["raw_ptr"] <= offset < sec["raw_ptr"] + sec["raw_size"]:
            return sec["rva"] + (offset - sec["raw_ptr"])
    return None


def _section_at(sections: Sequence[dict[str, Any]], rva: int) -> dict[str, Any] | None:
    for sec in sections:
        if sec["rva"] <= rva < sec["rva"] + max(sec["vsize"], sec["raw_size"]):
            return sec
    return None


def scan_gate_candidates(data: Any) -> list[int]:
    sections = pe_sections(data)
    if not sections:
        return []
    try:
        core_off = data.find(CORE_MARKER)
    except Exception:
        core_off = -1
    core_rva = _off_to_rva(sections, core_off) if core_off >= 0 else None
    xrefs: list[int] = []
    if core_rva is not None:
        for match in LEA_PATTERN.finditer(data):
            disp = struct.unpack("<i", match.group("disp"))[0]
            here = _off_to_rva(sections, match.start())
            if here is not None and here + 7 + disp == core_rva:
                xrefs.append(here)

    scored: list[tuple[int, int]] = []
    for match in GATE_PATTERN.finditer(data):
        code_rva = _off_to_rva(sections, match.start())
        if code_rva is None:
            continue
        sec = _section_at(sections, code_rva)
        if not sec or not (sec["chars"] & IMAGE_SCN_MEM_EXECUTE):
            continue
        disp_off = _off_to_rva(sections, match.start("disp"))
        if disp_off is None:
            continue
        disp = struct.unpack("<i", match.group("disp"))[0]
        target = (disp_off - 2) + 7 + disp
        target_sec = _section_at(sections, target)
        if not target_sec or not (target_sec["chars"] & IMAGE_SCN_MEM_WRITE):
            continue
        distance = min((abs(code_rva - x) for x in xrefs), default=1 << 30)
        scored.append((distance, target))
    if not scored:
        return []
    scored.sort(key=lambda item: item[0])
    near = [rva for dist, rva in scored if dist <= 0x20000]
    ordered = near or [rva for _dist, rva in scored]
    return list(dict.fromkeys(ordered))


def pid_from_hwnd(hwnd: int) -> int:
    try:
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def weixin_module(pid: int) -> tuple[int, int, str] | None:
    try:
        kernel32 = ctypes.windll.kernel32
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, int(pid))
        if not snap or int(snap) == -1:
            return None
        entry = _ModuleEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        found: tuple[int, int, str] | None = None
        try:
            if kernel32.Module32FirstW(snap, ctypes.byref(entry)):
                while True:
                    name = str(entry.szModule or "").lower()
                    if name == "weixin.dll":
                        found = (int(entry.modBaseAddr or 0), int(entry.modBaseSize or 0),
                                 str(entry.szExePath or ""))
                        break
                    if not kernel32.Module32NextW(snap, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snap)
        return found
    except Exception:
        return None


class ProcessOps:

    def __init__(self, pid: int):
        self.handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION,
            False, int(pid))
        self.ok = bool(self.handle)

    def read(self, address: int) -> int | None:
        if not self.ok:
            return None
        try:
            buf = (ctypes.c_ubyte * 1)()
            got = ctypes.c_size_t(0)
            if not ctypes.windll.kernel32.ReadProcessMemory(
                    self.handle, ctypes.c_void_p(address), buf, 1, ctypes.byref(got)):
                return None
            return int(buf[0]) if got.value == 1 else None
        except Exception:
            return None

    def write(self, address: int, value: int) -> bool:
        if not self.ok:
            return False
        try:
            buf = (ctypes.c_ubyte * 1)(int(value) & 0xFF)
            wrote = ctypes.c_size_t(0)
            ok = ctypes.windll.kernel32.WriteProcessMemory(
                self.handle, ctypes.c_void_p(address), buf, 1, ctypes.byref(wrote))
            return bool(ok and wrote.value == 1)
        except Exception:
            return False

    def close(self) -> None:
        if self.ok:
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.ok = False

    def __enter__(self) -> "ProcessOps":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def tree_state(hwnd: int, *, max_depth: int = 5, max_nodes: int = 400) -> dict[str, Any]:
    try:
        import uiautomation as auto
    except Exception:
        return {"ok": False, "mmui": False, "content": False, "reason": "no_uia"}
    try:
        root = auto.ControlFromHandle(int(hwnd))
    except Exception:
        return {"ok": False, "mmui": False, "content": False, "reason": "no_window"}
    mmui = False
    content = False
    nodes = 0
    stack: list[tuple[Any, int]] = [(root, 0)]
    while stack and nodes < max_nodes:
        node, depth = stack.pop()
        nodes += 1
        try:
            cls = str(node.ClassName or "")
        except Exception:
            continue
        if cls.startswith("mmui::"):
            mmui = True
            if cls in ("mmui::MainView", "mmui::MainTabBar",
                       "mmui::ContactsManagerWindow", "mmui::ContactsManagerView"):
                content = True
        if depth >= max_depth:
            continue
        try:
            for child in node.GetChildren():
                stack.append((child, depth + 1))
        except Exception:
            continue
    return {"ok": True, "mmui": mmui, "content": content, "nodes": nodes}


def desktop_locked() -> bool:
    try:
        user32 = ctypes.windll.user32
        handle = user32.OpenInputDesktop(0, False, 0x0100)
        if handle:
            user32.CloseDesktop(handle)
            return False
        return True
    except Exception:
        return False


def _identity(dll_path: str) -> str:
    try:
        stat = Path(dll_path).stat()
        return f"{dll_path}|{stat.st_size}|{int(stat.st_mtime)}"
    except Exception:
        return str(dll_path)


def _cache_file(cache_dir: Path | None) -> Path | None:
    if cache_dir is None:
        return None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return cache_dir / "a11y_gate.json"


def _load_cache(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(path: Path | None, data: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def candidate_rvas(dll_path: str, *, cache_dir: Path | None = None) -> list[int]:
    cache_path = _cache_file(cache_dir)
    cache = _load_cache(cache_path)
    ident = _identity(dll_path)
    entry = cache.get(ident) or {}
    out: list[int] = []
    verified = entry.get("verified")
    if isinstance(verified, int):
        out.append(int(verified))
    cached = entry.get("candidates")
    if isinstance(cached, list):
        for rva in cached:
            if isinstance(rva, int) and rva not in out:
                out.append(rva)
        return out

    try:
        raw = Path(dll_path).read_bytes()
    except Exception:
        return out
    scanned = scan_gate_candidates(raw)
    for rva in scanned:
        if rva not in out:
            out.append(rva)
    cache[ident] = {**entry, "candidates": scanned}
    _save_cache(cache_path, cache)
    return out


def _remember_verified(dll_path: str, rva: int, cache_dir: Path | None) -> None:
    cache_path = _cache_file(cache_dir)
    cache = _load_cache(cache_path)
    ident = _identity(dll_path)
    entry = cache.get(ident) or {}
    entry["verified"] = int(rva)
    cache[ident] = entry
    _save_cache(cache_path, cache)


def ensure_accessibility(hwnd: int, *, cache_dir: Path | None = None,
                         log: Callable[[str], None] | None = None,
                         probe: Callable[[int], dict[str, Any]] | None = None,
                         ops_factory: Callable[[int], Any] | None = None,
                         candidates: Iterable[int] | None = None,
                         timeout: float = 3.0) -> GateResult:
    say = log or (lambda _msg: None)
    check = probe or tree_state

    state = check(int(hwnd))
    if state.get("mmui"):
        return GateResult(True, "ready", "控件树已可用")

    pid = pid_from_hwnd(int(hwnd))
    if not pid:
        return GateResult(False, "no_process", "拿不到微信进程号")

    module = weixin_module(pid)
    if module is None:
        return GateResult(False, "no_module", "微信进程里没有找到 Weixin.dll")

    base, _size, dll_path = module
    rvas = list(candidates) if candidates is not None else candidate_rvas(dll_path, cache_dir=cache_dir)
    if not rvas:
        return GateResult(False, "no_candidates",
                          "这个微信版本的 Weixin.dll 里没有找到无障碍开关")

    factory = ops_factory or ProcessOps
    ops = factory(pid)
    try:
        if not getattr(ops, "ok", True):
            return GateResult(False, "access_denied",
                              "没有权限读取微信进程（可尝试以管理员身份运行 wxMoments）")
        tried = 0
        for rva in rvas[:MAX_CANDIDATES]:
            address = int(base) + int(rva)
            current = ops.read(address)
            if current is None:
                continue
            tried += 1
            wrote = False
            if current != 1:
                if not ops.write(address, 1):
                    continue
                wrote = True
            deadline = time.monotonic() + max(0.6, timeout)
            while True:
                if check(int(hwnd)).get("mmui"):
                    _remember_verified(dll_path, int(rva), cache_dir)
                    say(f"已打开微信无障碍接口（Weixin.dll+0x{int(rva):x}）")
                    return GateResult(True, "activated", "控件树已打开",
                                      rva=int(rva), candidates=len(rvas))
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
            if wrote:
                ops.write(address, int(current))
        if not tried:
            return GateResult(False, "read_failed", "读不到微信进程里的无障碍开关")
        return GateResult(False, "gate_failed",
                          "找到了候选开关但微信没有响应，可能被安全策略拦截",
                          candidates=len(rvas))
    finally:
        try:
            ops.close()
        except Exception:
            pass


def describe_failure(result: GateResult) -> tuple[str, str]:
    if result.code == "no_module":
        return ("没找到微信主程序，读不到标签成员",
                "请确认微信已登录并完全启动")
    if result.code == "access_denied":
        return ("没有权限访问微信进程",
                "请以管理员身份运行，或关闭以管理员权限启动的微信")
    if result.code == "no_candidates":
        return ("当前微信版本不支持读取标签成员",
                "只能导入标签名称，成员请改用分组")
    if result.code == "gate_failed":
        return ("微信没有开放控件树，读不到标签成员",
                "重启微信后再试，或改用分组")
    return ("读不到微信标签成员", result.detail or result.message)


if __name__ == "__main__":
    import win32gui

    hwnd = win32gui.FindWindow("Qt51514QWindowIcon", "微信") or win32gui.FindWindow("mmui::MainWindow", "微信")
    print("hwnd", hwnd, "desktop_locked", desktop_locked())
    if hwnd:
        print("before", tree_state(hwnd))
        print("gate", ensure_accessibility(hwnd, log=print))
        print("after", tree_state(hwnd))
