"""让原生图标跟随 Windows 亮/暗主题。

Windows 不会替桌面程序自动反色：透明底的黑色线条图标放在深色标题栏、
深色任务栏或深色通知区域里几乎看不见。pywebview 也只在 ``start(icon=...)``
时设置一次窗口图标，系统主题切换后不会重新加载。

本模块只依赖 ctypes，做三件事：

  1. 按当前主题选择 ``icon_light.ico`` / ``icon_dark.ico``，通过
     ``WM_SETICON`` 同时更新标题栏图标和任务栏图标；
  2. 创建通知区域（托盘）图标，主题变化时用 ``NIM_MODIFY`` 换图；
  3. 后台线程轮询 ``AppsUseLightTheme``，主题一变就同步，窗口关闭时
     删除托盘图标并释放 HICON。

图标属于“锦上添花”，任何一步失败都只返回 False / 跳过，不抛到调用方，
避免因为图标问题把整个控制台拖垮。
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

_IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------- Win32 常量

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_CANCELMODE = 0x001F
WM_COMMAND = 0x0111
WM_SETICON = 0x0080
WM_APP = 0x8000

WM_TRAY = WM_APP + 1
WM_ICON_THEME = WM_APP + 2

ICON_SMALL = 0
ICON_BIG = 1
ICON_SMALL2 = 2

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

SM_CXICON = 11
SM_CYICON = 12
SM_CXSMICON = 49
SM_CYSMICON = 50

SW_RESTORE = 9

SMTO_ABORTIFHUNG = 0x0002

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100

TRAY_CMD_SHOW = 1
TRAY_CMD_EXIT = 2

ERROR_CLASS_ALREADY_EXISTS = 1410

# 运行期图标文件名：浅色主题用深色线条，深色主题用浅色线条。
ICON_LIGHT = "icon_light.ico"
ICON_DARK = "icon_dark.ico"
ICON_FALLBACK = "icon.ico"

TRAY_TOOLTIP = "Auto-Claim 控制台"


if _IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class _NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", _GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]

    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HANDLE

    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int

    _user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE,
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.DestroyIcon.argtypes = [wintypes.HICON]
    _user32.DestroyIcon.restype = wintypes.BOOL

    _user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t

    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.restype = wintypes.BOOL
    _user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindowAsync.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.PostQuitMessage.restype = None
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t

    _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        ctypes.c_void_p,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterWindowMessageW.restype = wintypes.UINT

    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
    ]
    _user32.GetMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t

    _user32.CreatePopupMenu.argtypes = []
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR
    ]
    _user32.AppendMenuW.restype = wintypes.BOOL
    _user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        ctypes.c_void_p,
    ]
    _user32.TrackPopupMenu.restype = ctypes.c_int
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.DestroyMenu.restype = wintypes.BOOL
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.GetCursorPos.restype = wintypes.BOOL

    _shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)
    ]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL


# ---------------------------------------------------------------- 图标选择


def icon_path(assets, dark):
    """返回当前主题应使用的 ICO 路径；缺文件时退回通用 icon.ico。"""
    name = ICON_DARK if dark else ICON_LIGHT
    path = os.path.join(assets, name)
    if os.path.isfile(path):
        return path
    path = os.path.join(assets, ICON_FALLBACK)
    return path if os.path.isfile(path) else None


def current_dark():
    """读取当前 Windows 应用主题；失败返回 None，让调用方保持现状。"""
    if not _IS_WINDOWS:
        return None
    try:
        import win_theme
        return bool(win_theme.is_dark())
    except Exception:
        return None


def load_icon(path, small=True):
    """从 ICO 加载 HICON；失败返回 None。调用方负责 DestroyIcon。"""
    if not _IS_WINDOWS or not path:
        return None
    try:
        if small:
            cx = _user32.GetSystemMetrics(SM_CXSMICON) or 16
            cy = _user32.GetSystemMetrics(SM_CYSMICON) or 16
        else:
            cx = _user32.GetSystemMetrics(SM_CXICON) or 32
            cy = _user32.GetSystemMetrics(SM_CYICON) or 32
        handle = _user32.LoadImageW(
            None, path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
        if not handle:
            handle = _user32.LoadImageW(
                None, path, IMAGE_ICON, 0, 0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE)
        return handle or None
    except Exception:
        return None


def _destroy_icon(handle):
    if handle and _IS_WINDOWS:
        try:
            _user32.DestroyIcon(handle)
        except Exception:
            pass


def _is_window(hwnd):
    if not _IS_WINDOWS or not hwnd:
        return False
    try:
        return bool(_user32.IsWindow(hwnd))
    except Exception:
        return False


class _IconSet:
    """一个主题对应的小/大两枚 HICON，在同一窗口上重复使用。"""

    def __init__(self, path):
        self.path = path
        self.small = load_icon(path, small=True)
        self.big = load_icon(path, small=False) or self.small

    def apply(self, hwnd):
        if not _IS_WINDOWS or not hwnd:
            return False
        # 只设 SMALL / BIG。实测在这台 Win11 上再设 ICON_SMALL2 会把 BIG
        # 槽覆盖成小图标（Alt+Tab 变成 16px），任务栏本身读 SMALL/BIG 已足够。
        ok = False
        for kind, handle in (
            (ICON_SMALL, self.small),
            (ICON_BIG, self.big),
        ):
            if not handle:
                continue
            result = ctypes.c_size_t()
            try:
                sent = _user32.SendMessageTimeoutW(
                    hwnd, WM_SETICON, kind, handle,
                    SMTO_ABORTIFHUNG, 1000, ctypes.byref(result))
            except Exception:
                sent = 0
            ok = bool(sent) or ok
        return ok

    def close(self):
        _destroy_icon(self.small)
        if self.big != self.small:
            _destroy_icon(self.big)
        self.small = self.big = None


class IconManager:
    """把窗口图标、任务栏图标和托盘图标绑定到系统主题。"""

    def __init__(self, hwnd, assets, dark=False, interval=2.0,
                 tray=True, on_theme=None):
        self.hwnd = int(hwnd or 0)
        self.assets = assets
        self.interval = max(0.5, float(interval))
        self._dark = bool(dark)
        self._tray_enabled = bool(tray) and _IS_WINDOWS
        self._on_theme = on_theme
        self._tray = None
        self._icons = {}
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not _IS_WINDOWS or not self.hwnd:
            return False
        self._apply(self._dark)
        if self._tray_enabled:
            path = icon_path(self.assets, self._dark)
            if path:
                self._tray = TrayIcon(self.hwnd, path, TRAY_TOOLTIP)
                self._tray.start()
        self._thread = threading.Thread(
            target=self._watch, name="wb-native-icons", daemon=True)
        self._thread.start()
        return True

    def _apply(self, dark):
        path = icon_path(self.assets, dark)
        if not path:
            return False
        icons = self._icons.get(path)
        if icons is None:
            icons = _IconSet(path)
            self._icons[path] = icons
        return icons.apply(self.hwnd)

    def _watch(self):
        while not self._stop.wait(self.interval):
            if not _is_window(self.hwnd):
                return
            dark = current_dark()
            if dark is None or dark == self._dark:
                continue
            self._dark = dark
            self._apply(dark)
            if self._tray is not None:
                path = icon_path(self.assets, dark)
                if path:
                    self._tray.set_icon(path)
            if self._on_theme is not None:
                try:
                    self._on_theme(dark)
                except Exception:
                    pass

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        for icons in self._icons.values():
            icons.close()
        self._icons.clear()


class TrayIcon:
    """通知区域图标。

    隐藏窗口必须在独立线程里跑消息循环；Shell_NotifyIcon 的回调、右键
    菜单和 TaskbarCreated 都在这条线程上处理。所有公开方法都可以从其他
    线程调用，内部通过 PostMessage 切回托盘线程。
    """

    def __init__(self, main_hwnd, icon_path_value, tooltip=TRAY_TOOLTIP):
        self._main_hwnd = int(main_hwnd or 0)
        self._icon_path = icon_path_value
        self._tooltip = (tooltip or TRAY_TOOLTIP)[:127]
        self._hwnd = None
        self._hicon = None
        self._thread = None
        self._ready = threading.Event()
        self._done = threading.Event()
        self._failed = False
        self._taskbar_created = 0
        self._wndproc = None
        self._lock = threading.Lock()
        self._class_name = "WorkBuddyTray.%d" % os.getpid()

    # ---------- 对外接口 ----------

    def start(self):
        if not _IS_WINDOWS:
            return False
        self._thread = threading.Thread(
            target=self._run, name="wb-tray-icon", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return not self._failed

    def set_icon(self, path):
        """切换图标；不要求托盘线程已经建好窗口。"""
        with self._lock:
            self._icon_path = path
        hwnd = self._hwnd
        if hwnd:
            try:
                _user32.PostMessageW(hwnd, WM_ICON_THEME, 0, 0)
            except Exception:
                pass

    def stop(self):
        hwnd = self._hwnd
        if hwnd:
            try:
                # 右键菜单可能正阻塞托盘线程，先取消菜单再关窗口。
                _user32.PostMessageW(hwnd, WM_CANCELMODE, 0, 0)
                _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._done.wait(timeout=1.0)
            self._thread.join(timeout=1.0)
        self._thread = None

    # ---------- 托盘线程 ----------

    def _run(self):
        try:
            if not self._create_window():
                self._failed = True
                return
            self._taskbar_created = _user32.RegisterWindowMessageW(
                "TaskbarCreated")
            self._add_icon()
            self._ready.set()

            msg = wintypes.MSG()
            while True:
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self._failed = True
        finally:
            self._ready.set()
            self._remove_icon()
            self._done.set()

    def _create_window(self):
        self._wndproc = _WNDPROC(self._wnd_proc)
        wc = _WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = _kernel32.GetModuleHandleW(None)
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = self._class_name

        atom = _user32.RegisterClassW(ctypes.byref(wc))
        if not atom and ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
            return False

        hwnd = _user32.CreateWindowExW(
            0, self._class_name, "WorkBuddyTray", 0,
            0, 0, 0, 0, None, None, wc.hInstance, None)
        if not hwnd:
            return False
        self._hwnd = int(hwnd)
        return True

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = int(lparam) & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._activate()
                    return 0
                if event == WM_RBUTTONUP:
                    self._show_menu()
                    return 0
            elif msg == WM_ICON_THEME:
                self._reload_icon()
                return 0
            elif msg == WM_COMMAND:
                command = int(wparam) & 0xFFFF
                if command == TRAY_CMD_SHOW:
                    self._activate()
                elif command == TRAY_CMD_EXIT:
                    self._exit()
                return 0
            elif msg == WM_CLOSE:
                _user32.DestroyWindow(hwnd)
                return 0
            elif msg == WM_DESTROY:
                self._remove_icon()
                _user32.PostQuitMessage(0)
                return 0
            elif self._taskbar_created and msg == self._taskbar_created:
                self._add_icon()
                return 0
        except Exception:
            pass
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---------- 菜单与动作 ----------

    def _show_menu(self):
        menu = _user32.CreatePopupMenu()
        if not menu:
            return
        try:
            _user32.AppendMenuW(menu, MF_STRING, TRAY_CMD_SHOW,
                                "打开控制台")
            _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            _user32.AppendMenuW(menu, MF_STRING, TRAY_CMD_EXIT, "退出")

            point = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(point))
            owner = self._main_hwnd if _is_window(self._main_hwnd) else self._hwnd
            _user32.SetForegroundWindow(owner)
            command = _user32.TrackPopupMenu(
                menu,
                TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
                point.x, point.y, 0, self._hwnd, None)
            _user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
            if command == TRAY_CMD_SHOW:
                self._activate()
            elif command == TRAY_CMD_EXIT:
                self._exit()
        finally:
            _user32.DestroyMenu(menu)

    def _activate(self):
        hwnd = self._main_hwnd
        if not _is_window(hwnd):
            return
        _user32.ShowWindowAsync(hwnd, SW_RESTORE)
        _user32.SetForegroundWindow(hwnd)

    def _exit(self):
        hwnd = self._main_hwnd
        if _is_window(hwnd):
            _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    # ---------- 图标增删改 ----------

    def _current_icon(self):
        with self._lock:
            path = self._icon_path
        return load_icon(path, small=True)

    def _base_data(self):
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        return data

    def _add_icon(self):
        if not self._hwnd:
            return False
        self._remove_icon()
        icon = self._current_icon()
        if not icon:
            return False
        data = self._base_data()
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = icon
        data.szTip = self._tooltip
        if _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            self._hicon = icon
            return True
        _destroy_icon(icon)
        return False

    def _reload_icon(self):
        if not self._hwnd:
            return False
        icon = self._current_icon()
        if not icon:
            return False
        if not self._hicon:
            _destroy_icon(icon)
            return self._add_icon()

        data = self._base_data()
        data.uFlags = NIF_ICON
        data.hIcon = icon
        if _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)):
            old = self._hicon
            self._hicon = icon
            _destroy_icon(old)
            return True
        _destroy_icon(icon)
        return False

    def _remove_icon(self):
        if self._hwnd:
            data = self._base_data()
            try:
                _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            except Exception:
                pass
        if self._hicon:
            _destroy_icon(self._hicon)
            self._hicon = None
