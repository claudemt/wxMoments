from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import wechat_a11y

MAIN_TITLES = ("微信", "Weixin")
MAIN_CLASS_HINT = "QWindowIcon"
MAIN_CLASS_MMUI = "mmui::MainWindow"
MGR_TITLE = "通讯录管理"
MANAGER_ENTRY = "通讯录管理"
MGR_CLASS_HINT = "ContactsManagerWindow"
CONTACTS_TAB = "通讯录"
TAB_ITEM_CLASS = "XTabBarItem"
MANAGER_ROW_CLASS = "ContactsCellMangerBtnView"
LABEL_FOLDER = "标签"
FOLDER_CELL_CLASS = "ContactsManagerControlFolderCell"
LABEL_CELL_CLASS = "ContactsManagerControlLabelCell"
CREATE_CELL_CLASS = "ContactsManagerControlCreateLabelCell"
DETAIL_CELL_CLASS = "ContactsManagerDetailCell"
DETAIL_LIST_CLASS = "ContactsManagerDetailView"

_LABEL_ROW_RE = re.compile(r"^(?P<name>.*?)\s*[（(]\s*(?P<count>\d+)\s*[)）]\s*$")

SCROLL_UP, SCROLL_DOWN = 120, -120


class UiaUnavailable(RuntimeError):

    def __init__(self, code: str, message: str, *, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.detail = detail


class LabelSyncBusy(RuntimeError):
    pass


class Desktop(Protocol):

    def find_window(self, title: str, class_hint: str = "") -> Any | None: ...
    def wait_window(self, title: str, class_hint: str = "", *, timeout: float) -> Any | None: ...
    def close_window(self, node: Any) -> None: ...
    def restore_window(self, node: Any) -> None: ...
    def children(self, node: Any) -> list[Any]: ...
    def name(self, node: Any) -> str: ...
    def class_name(self, node: Any) -> str: ...
    def rect(self, node: Any) -> tuple[int, int, int, int] | None: ...
    def activate(self, node: Any) -> bool: ...
    def click(self, node: Any) -> bool: ...
    def wheel(self, node: Any, delta: int) -> bool: ...
    def sleep(self, seconds: float) -> None: ...


def ensure_dpi_aware() -> bool:
    try:
        import ctypes

        user32 = ctypes.windll.user32
    except Exception:
        return False
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return True
    except Exception:
        pass
    try:
        return bool(user32.SetProcessDPIAware())
    except Exception:
        return False


def wechat_process_running() -> bool:
    try:
        import psutil

        return any(proc.info["name"] and proc.info["name"].lower().startswith("weixin")
                   for proc in psutil.process_iter(["name"]))
    except Exception:
        return False


class UiaDesktop:

    def __init__(self) -> None:
        self._auto: Any = None
        ensure_dpi_aware()

    def _uia(self) -> Any:
        if self._auto is None:
            try:
                import uiautomation as auto
            except Exception as exc:
                raise UiaUnavailable(
                    "no_backend",
                    "缺少 UI Automation 组件，无法读取微信标签成员",
                    detail=f"pip install uiautomation 失败或缺依赖：{exc}",
                ) from exc
            self._auto = auto
        return self._auto

    def _anchor(self, hwnd: int) -> Any | None:
        try:
            return self._uia().ControlFromHandle(hwnd)
        except Exception:
            return None

    def _find_hwnd(self, title: str, class_hint: str) -> int:
        import win32gui

        found: list[tuple[int, int]] = []

        def visit(hwnd: int, _param: Any) -> bool:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                name = win32gui.GetWindowText(hwnd)
                cls = win32gui.GetClassName(hwnd) or ""
            except Exception:
                return True
            if title and name != title:
                return True
            if class_hint and class_hint.lower() not in cls.lower():
                return True
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            except Exception:
                return True
            found.append(((right - left) * (bottom - top), hwnd))
            return True

        try:
            win32gui.EnumWindows(visit, None)
        except Exception as exc:
            raise UiaUnavailable("no_window", "无法枚举窗口", detail=str(exc)) from exc
        if not found:
            return 0
        found.sort(reverse=True)
        return found[0][1]

    def find_window(self, title: str, class_hint: str = "") -> Any | None:
        hwnd = self._find_hwnd(title, class_hint)
        return self._anchor(hwnd) if hwnd else None

    def wait_window(self, title: str, class_hint: str = "", *, timeout: float) -> Any | None:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            node = self.find_window(title, class_hint)
            if node is not None:
                return node
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)

    def close_window(self, node: Any) -> None:
        hwnd = int(getattr(node, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            return
        try:
            import win32con
            import win32gui

            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass
        time.sleep(0.4)

    def restore_window(self, node: Any) -> None:
        hwnd = int(getattr(node, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            return
        try:
            import win32con
            import win32gui

            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.3)
        except Exception:
            pass

    def children(self, node: Any) -> list[Any]:
        try:
            return list(node.GetChildren())
        except Exception:
            return []

    def name(self, node: Any) -> str:
        try:
            return str(node.Name or "")
        except Exception:
            return ""

    def class_name(self, node: Any) -> str:
        try:
            return str(node.ClassName or "")
        except Exception:
            return ""

    def rect(self, node: Any) -> tuple[int, int, int, int] | None:
        try:
            box = node.BoundingRectangle
        except Exception:
            return None
        try:
            left, top, right, bottom = int(box.left), int(box.top), int(box.right), int(box.bottom)
        except Exception:
            return None
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom

    def activate(self, node: Any) -> bool:
        for getter, method in (("GetSelectionItemPattern", "Select"),
                               ("GetInvokePattern", "Invoke")):
            try:
                pattern = getattr(node, getter)()
                getattr(pattern, method)()
                time.sleep(0.2)
                return True
            except Exception:
                continue
        return False

    def click(self, node: Any) -> bool:
        box = self.rect(node)
        if box is None:
            return False
        x = (box[0] + box[2]) // 2
        y = (box[1] + box[3]) // 2
        try:
            import ctypes
            import win32gui

            hwnd = _window_at(x, y)
            root = win32gui.GetAncestor(int(hwnd), 2) if hwnd else 0
            if root:
                _focus_hwnd(root)
            cleared = _clear_transparent(x, y)
            try:
                ctypes.windll.user32.SetCursorPos(int(x), int(y))
                time.sleep(0.08)
                ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
                time.sleep(0.05)
                ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
            finally:
                _restore_transparent(cleared)
        except Exception:
            return False
        time.sleep(0.2)
        return True

    def wheel(self, node: Any, delta: int) -> bool:
        box = self.rect(node)
        if box is None:
            return False
        x = (box[0] + box[2]) // 2
        y = (box[1] + box[3]) // 2
        try:
            import ctypes

            ctypes.windll.user32.SetCursorPos(int(x), int(y))
            time.sleep(0.08)
            cleared = _clear_transparent(x, y)
            try:
                ctypes.windll.user32.mouse_event(0x0800, 0, 0, int(delta), 0)
            finally:
                _restore_transparent(cleared)
        except Exception:
            return False
        return True

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))

    def redraw(self, node: Any) -> None:
        hwnd = int(getattr(node, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            return
        try:
            import ctypes

            ctypes.windll.user32.RedrawWindow(ctypes.c_void_p(hwnd), None, None, 0x0001 | 0x0080 | 0x0100)
        except Exception:
            pass

    def focus_window(self, node: Any) -> bool:
        hwnd = int(getattr(node, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            return False
        return _focus_hwnd(hwnd)


def _focus_hwnd(hwnd: int) -> bool:
    try:
        import ctypes
        import win32con
        import win32gui

        user32 = ctypes.windll.user32
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        mine = ctypes.windll.kernel32.GetCurrentThreadId()
        fore = win32gui.GetForegroundWindow()
        other = int(user32.GetWindowThreadProcessId(int(fore), None) or 0) if fore else 0
        attached = False
        if other and other != mine:
            attached = bool(user32.AttachThreadInput(mine, other, True))
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(mine, other, False)
        time.sleep(0.2)
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _window_at(x: int, y: int) -> int:
    try:
        import ctypes
        from ctypes import wintypes

        return int(ctypes.windll.user32.WindowFromPoint(wintypes.POINT(int(x), int(y))) or 0)
    except Exception:
        return 0


def _clear_transparent(x: int, y: int) -> list[tuple[int, int]]:
    saved: list[tuple[int, int]] = []
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = _window_at(x, y)
        for _ in range(6):
            if not hwnd:
                break
            ex = user32.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
            if ex & 0x20:
                user32.SetWindowLongW(hwnd, -20, ex & ~0x20)
                saved.append((int(hwnd), int(ex)))
            hwnd = user32.GetParent(hwnd)
    except Exception:
        pass
    return saved


def _restore_transparent(saved: Iterable[tuple[int, int]]) -> None:
    try:
        import ctypes

        for hwnd, ex in saved:
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex)
    except Exception:
        pass


@dataclass
class LabelRow:
    name: str
    count: int = 0
    raw: str = ""
    has_count: bool = False


@dataclass
class MemberRead:
    label: str
    displayed_total: int
    rows: list[str] = field(default_factory=list)
    complete: bool = False
    reason: str = ""
    scrolls: int = 0
    rewinds: int = 0
    gapped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "displayed_total": self.displayed_total,
            "rows": list(self.rows),
            "complete": self.complete,
            "reason": self.reason,
            "scrolls": self.scrolls,
            "rewinds": self.rewinds,
            "gapped": self.gapped,
        }


def parse_label_row(raw: str) -> LabelRow:
    text = str(raw or "").strip()
    match = _LABEL_ROW_RE.match(text)
    if match:
        return LabelRow(name=match.group("name").strip(), count=int(match.group("count")),
                        raw=text, has_count=True)
    return LabelRow(name=text, count=0, raw=text)


def stitch_rows(collected: list[str], page: list[str]) -> tuple[list[str], int]:
    if not page:
        return list(collected), 0
    if not collected:
        return list(page), len(page)
    for overlap in range(min(len(collected), len(page)), 0, -1):
        if collected[-overlap:] == page[:overlap]:
            return list(collected) + page[overlap:], overlap
    return list(collected) + list(page), 0


class WechatLabelReader:

    def __init__(self, desktop: Desktop | None = None, *,
                 timeout: float = 60.0,
                 click_wait: float = 0.7,
                 wheel_wait: float = 0.7,
                 max_scrolls: int = 80,
                 cache_dir: Path | None = None,
                 log: Callable[[str], None] | None = None,
                 cancel: Callable[[], bool] | None = None) -> None:
        self.desktop = desktop or UiaDesktop()
        self.timeout = timeout
        self.click_wait = click_wait
        self.wheel_wait = wheel_wait
        self.max_scrolls = max_scrolls
        self.cache_dir = cache_dir
        self._log = log or (lambda _msg: None)
        self._cancel = cancel or (lambda: False)
        self._opened_manager = False
        self._deadline = 0.0
        self._screen_reader_previous: int | None = None

    def _check(self) -> None:
        if self._cancel():
            raise UiaUnavailable("canceled", "已取消标签同步")
        if self._deadline and time.monotonic() > self._deadline:
            raise UiaUnavailable("timeout", "读取微信标签超时，请重试")

    def _find(self, root: Any, class_hint: str = "", *, name: str = "",
              contains: str = "", max_depth: int = 40) -> Any | None:
        queue: list[tuple[Any, int]] = [(root, 0)]
        while queue:
            node, depth = queue.pop(0)
            cls = self.desktop.class_name(node)
            text = self.desktop.name(node)
            class_ok = class_hint.lower() in cls.lower() if class_hint else False
            name_ok = (text == name) if name else (contains in text if contains else False)
            if class_hint and name:
                hit = class_ok and name_ok
            elif class_hint:
                hit = class_ok
            elif name or contains:
                hit = name_ok
            else:
                hit = False
            if hit:
                return node
            if depth >= max_depth:
                continue
            for child in self.desktop.children(node):
                queue.append((child, depth + 1))
        return None

    def _all(self, root: Any, class_hint: str, *, max_depth: int = 40) -> list[Any]:
        out: list[Any] = []
        queue: list[tuple[Any, int]] = [(root, 0)]
        while queue:
            node, depth = queue.pop(0)
            if class_hint.lower() in self.desktop.class_name(node).lower():
                out.append(node)
            if depth >= max_depth:
                continue
            for child in self.desktop.children(node):
                queue.append((child, depth + 1))
        return out

    def _click(self, node: Any, label: str, verify: Any = None) -> bool:
        self._check()
        if self.desktop.activate(node):
            if verify is None or self._settle(verify):
                self.desktop.sleep(self.click_wait)
                return True
            self._log(f"「{label}」的无障碍调用没有生效，改用鼠标点击")
        if not self.desktop.click(node):
            raise UiaUnavailable("click_failed", f"无法点击「{label}」",
                                 detail="控件矩形不可用或被遮挡")
        self.desktop.sleep(self.click_wait)
        if verify is not None and not self._settle(verify):
            return False
        return True

    def _settle(self, verify: Any, *, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if verify():
                    return True
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return False
            self.desktop.sleep(0.2)

    def main_window(self) -> Any:
        for title in MAIN_TITLES:
            for hint in (MAIN_CLASS_HINT, MAIN_CLASS_MMUI):
                window = self.desktop.find_window(title, hint)
                if window is not None:
                    return window
            window = self.desktop.find_window(title)
            if window is not None:
                return window
        if wechat_process_running():
            raise UiaUnavailable(
                "no_window",
                "微信主窗口不可用，请打开微信主界面后重试",
            )
        raise UiaUnavailable(
            "no_window",
            "微信未运行，请先登录电脑版微信",
        )

    def _tree_ready(self, window: Any) -> bool:
        queue: list[tuple[Any, int]] = [(window, 0)]
        while queue:
            node, depth = queue.pop(0)
            if self.desktop.class_name(node).startswith("mmui::"):
                return True
            if depth >= 3:
                continue
            children = self.desktop.children(node)
            if depth >= 1 and children:
                return True
            for child in children:
                queue.append((child, depth + 1))
        return False

    def _content_ready(self, window: Any) -> bool:
        if self._find(window, TAB_ITEM_CLASS, name=CONTACTS_TAB, max_depth=8) is not None:
            return True
        return self._find(window, "", name=CONTACTS_TAB, max_depth=8) is not None

    @staticmethod
    def _screen_reader_flag() -> int:
        import ctypes
        from ctypes import wintypes

        value = wintypes.BOOL()
        ok = ctypes.windll.user32.SystemParametersInfoW(0x0046, 0, ctypes.byref(value), 0)
        return int(value.value) if ok else 0

    @staticmethod
    def _set_screen_reader_flag(value: int) -> None:
        import ctypes

        ctypes.windll.user32.SystemParametersInfoW(0x0047, int(value), None, 0x0002)

    def wake_accessibility(self, main: Any) -> bool:
        if self._tree_ready(main):
            return True
        try:
            previous = self._screen_reader_flag()
            if previous:
                return False
            self._set_screen_reader_flag(1)
            self._screen_reader_previous = previous
            for _ in range(8):
                self.desktop.sleep(0.25)
                if self._tree_ready(main):
                    self._log("已临时开启系统读屏标志，微信控件树可用（结束时会还原）")
                    return True
            self._restore_accessibility()
            return False
        except Exception:
            self._restore_accessibility()
            return False

    def _restore_accessibility(self) -> None:
        if self._screen_reader_previous is None:
            return
        try:
            self._set_screen_reader_flag(self._screen_reader_previous)
        except Exception:
            pass
        self._screen_reader_previous = None

    def _focus(self, window: Any) -> bool:
        fn = getattr(self.desktop, "focus_window", None)
        if fn is None:
            return False
        try:
            return bool(fn(window))
        except Exception:
            return False

    def _wait_content(self, window: Any, *, timeout: float = 4.0) -> bool:
        redraw = getattr(self.desktop, "redraw", None)
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            if self._tree_ready(window) and self._content_ready(window):
                return True
            if redraw is not None and attempt < 3:
                redraw(window)
                attempt += 1
            if time.monotonic() >= deadline:
                return False
            self.desktop.sleep(0.3)

    def _ensure_tree(self, main: Any) -> None:
        self._focus(main)
        if self._tree_ready(main) and self._content_ready(main):
            return
        hwnd = int(getattr(main, "NativeWindowHandle", 0) or 0)
        result = None
        if hwnd:
            self._log("正在打开微信的控件树…")
            try:
                result = wechat_a11y.ensure_accessibility(
                    hwnd, cache_dir=self.cache_dir, log=self._log)
            except Exception as exc:
                self._log(f"打开控件树失败：{exc}")
                result = None
            if result is not None and result.ok:
                self.desktop.sleep(0.3)
                relisted = self.main_window()
                if self._wait_content(relisted):
                    return
        if self.wake_accessibility(main):
            relisted = self.main_window()
            if self._content_ready(relisted):
                return
        locked = wechat_a11y.desktop_locked()
        if not self._tree_ready(main):
            if result is not None and not result.ok:
                message, advice = wechat_a11y.describe_failure(result)
                raise UiaUnavailable("tree_cold", message, detail=advice)
            if locked:
                raise UiaUnavailable(
                    "desktop_locked",
                    "电脑处于锁屏状态，读不到标签成员",
                    detail="请先解锁屏幕，再同步标签",
                )
            raise UiaUnavailable(
                "tree_cold",
                "微信没有开放控件树，读不到标签成员",
                detail="可在 config.json 设置 \"wechat_label_uia\": false 关闭标签成员同步。",
            )
        if locked:
            raise UiaUnavailable(
                "desktop_locked",
                "电脑处于锁屏状态，读不到标签成员",
                detail="请先解锁屏幕，再同步标签",
            )
        raise UiaUnavailable(
            "content_hidden",
            "微信界面内容不可读，请确认微信主窗口已正常显示",
        )

    def manager_window(self) -> Any | None:
        window = self.desktop.find_window(MGR_TITLE, MGR_CLASS_HINT)
        if window is None:
            window = self.desktop.find_window(MGR_TITLE)
        return window

    def open_manager(self) -> Any:
        self._deadline = self._deadline or (time.monotonic() + self.timeout)
        window = self.manager_window()
        if window is not None:
            self._wait_children(window, timeout=3.0)
            return window

        main = self.main_window()
        self._ensure_tree(main)
        main = self.main_window()
        self._focus(main)
        self.desktop.restore_window(main)
        tab = self._find(main, TAB_ITEM_CLASS, name=CONTACTS_TAB)
        if tab is None:
            tab = self._find(main, "", name=CONTACTS_TAB)
        if tab is None:
            raise UiaUnavailable("manager_failed", "微信主窗口里找不到「通讯录」入口")
        if not self._click(tab, CONTACTS_TAB,
                           verify=lambda: self._contacts_page_ready(main)):
            raise UiaUnavailable("manager_failed", "微信主窗口没有切到「通讯录」页面",
                                 detail="微信窗口可能被别的窗口挡住或没有焦点。")

        entry = self._wait_class(main, MANAGER_ROW_CLASS, timeout=5.0)
        if entry is None:
            entry = self._wait_name(main, MANAGER_ENTRY, timeout=2.0)
        if entry is None:
            raise UiaUnavailable("manager_failed", "通讯录页面没有加载出「通讯录管理」入口")
        window = None
        for attempt in range(3):
            if attempt:
                self._log(f"「通讯录管理」没有打开，重试第 {attempt} 次")
                self._focus(self.main_window())
            if not self.desktop.click(entry):
                raise UiaUnavailable("click_failed", "无法点击「通讯录管理」",
                                     detail="控件矩形不可用或被遮挡")
            self.desktop.sleep(self.click_wait)
            window = self.desktop.wait_window(MGR_TITLE, MGR_CLASS_HINT, timeout=4.0)
            if window is None:
                window = self.desktop.wait_window(MGR_TITLE, "", timeout=2.0)
            if window is not None:
                break
        if window is None:
            raise UiaUnavailable("manager_failed", "「通讯录管理」窗口没有打开")
        self._opened_manager = True
        self._wait_children(window, timeout=6.0)
        return window

    def _contacts_page_ready(self, main: Any) -> bool:
        if self._find(main, MANAGER_ROW_CLASS, max_depth=14) is not None:
            return True
        return self._find(main, "", name=MANAGER_ENTRY, max_depth=14) is not None

    def _wait_class(self, root: Any, class_hint: str, *, timeout: float) -> Any | None:
        deadline = time.monotonic() + timeout
        while True:
            node = self._find(root, class_hint)
            if node is not None:
                return node
            if time.monotonic() >= deadline:
                return None
            self.desktop.sleep(0.3)

    def _wait_name(self, root: Any, name: str, *, timeout: float) -> Any | None:
        deadline = time.monotonic() + timeout
        while True:
            node = self._find(root, "", name=name)
            if node is not None:
                return node
            if time.monotonic() >= deadline:
                return None
            self.desktop.sleep(0.3)

    def _wait_children(self, root: Any, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.desktop.children(root):
                return
            self.desktop.sleep(0.25)

    def _labels_visible(self, window: Any) -> bool:
        return bool(self._all(window, LABEL_CELL_CLASS) or self._find(window, CREATE_CELL_CLASS))

    def ensure_labels_open(self, window: Any) -> None:
        if self._labels_visible(window):
            return
        folder = self._find(window, FOLDER_CELL_CLASS, name=LABEL_FOLDER)
        if folder is None:
            folder = self._find(window, "", name=LABEL_FOLDER)
        if folder is None:
            raise UiaUnavailable("labels_failed", "「通讯录管理」左栏找不到「标签」分组")
        self._click(folder, LABEL_FOLDER, verify=lambda: self._labels_visible(window))
        self._wait_children(window, timeout=3.0)
        if not self._labels_visible(window):
            raise UiaUnavailable("labels_failed", "「标签」分组展开后仍没有读到标签列表")

    def list_labels(self) -> list[LabelRow]:
        window = self.open_manager()
        self.ensure_labels_open(window)
        rows = [parse_label_row(self.desktop.name(node)) for node in self._all(window, LABEL_CELL_CLASS)]
        return [row for row in rows if row.name]

    def select_label(self, window: Any, label: str, expected_total: int = 0) -> None:
        cells = [node for node in self._all(window, LABEL_CELL_CLASS)]
        exact = [node for node in cells if parse_label_row(self.desktop.name(node)).name == label]
        if len(exact) > 1:
            raise UiaUnavailable(
                "duplicate_label",
                f"微信里有 {len(exact)} 个同名标签「{label}」，无法确定成员归属",
            )
        if not exact:
            raise UiaUnavailable("no_label", f"微信里找不到标签「{label}」，可能在别处已删除")
        verify = (lambda: bool(self._detail_page(window))) if expected_total else None
        if not self._click(exact[0], label, verify):
            raise UiaUnavailable("not_selected", f"右侧没有切到标签「{label}」的成员列表")

    def _detail_page(self, window: Any) -> list[str]:
        cells: list[tuple[int, int, str]] = []
        for node in self._all(window, DETAIL_CELL_CLASS):
            text = self.desktop.name(node).strip()
            if not text:
                continue
            box = self.desktop.rect(node) or (0, 0, 0, 0)
            cells.append((int(box[1]), int(box[0]), text))
        cells.sort(key=lambda item: (item[0], item[1]))
        return [text for _top, _left, text in cells]

    def _scroll(self, window: Any, delta: int, anchor: Any | None) -> bool:
        target = anchor if anchor is not None else self._find(window, DETAIL_LIST_CLASS)
        if target is None:
            nodes = self._all(window, DETAIL_CELL_CLASS)
            target = nodes[0] if nodes else None
        if target is None:
            return False
        self._focus(window)
        if not self.desktop.wheel(target, delta):
            return False
        self.desktop.sleep(self.wheel_wait)
        return True

    def _rewind(self, window: Any, anchor: Any | None) -> int:
        rounds = 0
        previous: list[str] | None = None
        for _ in range(20):
            self._check()
            page = self._detail_page(window)
            if previous is not None and page == previous:
                break
            previous = page
            if not page:
                break
            if not self._scroll(window, SCROLL_UP, anchor):
                break
            rounds += 1
        return rounds

    def read_members(self, label: str, expected_total: int = 0, *,
                     count_known: bool = True) -> MemberRead:
        window = self.open_manager()
        self.ensure_labels_open(window)
        self.select_label(window, label, expected_total)
        attempts = 2 if (expected_total == 0 and count_known) else 12
        first_page: list[str] = []
        for _ in range(attempts):
            self.desktop.sleep(0.25)
            first_page = self._detail_page(window)
            if first_page:
                break

        list_node = self._find(window, DETAIL_LIST_CLASS)
        anchor = list_node
        if anchor is None:
            cells = self._all(window, DETAIL_CELL_CLASS)
            anchor = cells[0] if cells else None

        if not first_page:
            if expected_total == 0 and count_known:
                return MemberRead(label=label, displayed_total=0, rows=[], complete=True,
                                  reason="empty")
            if not count_known:
                return MemberRead(label=label, displayed_total=0, rows=[], complete=False,
                                  reason="empty_list")
            raise UiaUnavailable("not_selected", f"右侧没有切到标签「{label}」的成员列表")

        rewinds = self._rewind(window, anchor)
        collected: list[str] = []
        scrolls = 0
        idle = 0
        grown = -1
        gapped = False
        result = MemberRead(label=label, displayed_total=expected_total, rows=[], complete=False,
                            reason="bottom", rewinds=rewinds)

        while True:
            self._check()
            page = self._detail_page(window)
            if page:
                before = len(collected)
                collected, overlap = stitch_rows(collected, page)
                if scrolls and overlap == 0 and len(collected) > before:
                    gapped = True
            if len(collected) > grown:
                grown = len(collected)
                idle = 0
            else:
                idle += 1
            if idle >= 2:
                break
            if not page and not collected:
                break
            if not self._scroll(window, SCROLL_DOWN, anchor):
                result.reason = "scroll_stuck" if collected else "no_list"
                break
            scrolls += 1
            if scrolls >= self.max_scrolls:
                result.reason = "scroll_cap"
                break

        result.rows = collected
        result.scrolls = scrolls
        result.gapped = gapped
        if expected_total:
            if len(collected) < expected_total:
                result.complete = False
                result.reason = "incomplete"
            elif len(collected) > expected_total:
                result.rows = collected[:expected_total]
                result.complete = True
                result.reason = "truncated"
            else:
                result.complete = True
        else:
            result.complete = bool(collected)
            result.reason = "no_count" if collected else "empty_list"
        return result

    def close(self) -> None:
        try:
            if self._opened_manager:
                window = self.manager_window()
                if window is not None:
                    self.desktop.close_window(window)
                self._opened_manager = False
        finally:
            self._restore_accessibility()

    def __enter__(self) -> "WechatLabelReader":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def probe() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="只读检查微信标签可用性")
    parser.add_argument("--members", default="", help="额外读取指定标签的成员")
    args = parser.parse_args()

    reader = WechatLabelReader(timeout=90.0, log=print)
    try:
        labels = reader.list_labels()
        print(json.dumps({"ok": True, "labels": [row.__dict__ for row in labels]},
                         ensure_ascii=False, indent=2))
        if args.members:
            result = reader.read_members(args.members)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except UiaUnavailable as exc:
        print(json.dumps({"ok": False, "code": exc.code, "msg": str(exc), "detail": exc.detail},
                         ensure_ascii=False, indent=2))
        return 2
    finally:
        reader.close()


if __name__ == "__main__":
    raise SystemExit(probe())
