"""Win32 单实例锁与 UI 线程看门狗。

这两个能力都只依赖 ctypes，不引入新的第三方依赖：

  * acquire_single_instance()
      进程级命名互斥量。第一次启动创建成功并持有句柄；第二次启动
      发现同名对象已存在，激活已有窗口后退出，避免再起一套 WebView2。

  * UIWatchdog
      后台线程定期向主窗口发送 WM_NULL，用 SendMessageTimeoutW 的
      超时判断 UI 消息循环是否还在泵消息。连续多次无响应时，先把
      所有 Python 线程栈写进 watchdog.log，再 os._exit()。

为什么看门狗要直接退出，而不是 destroy 窗口：
    窗口卡死时，DestroyWindow / SendMessage 本身也可能阻塞；
    os._exit() 不经过 Python 清理，能立刻释放 WebView2 子进程和
    DirectComposition 表面，避免整个 DWM/任务栏被拖住。

日志默认由调用方传入，通常为：
    %LOCALAPPDATA%\\WorkBuddyDashboard\\watchdog.log
"""

from __future__ import annotations

import ctypes
import faulthandler
import os
import sys
import threading
import time
from ctypes import wintypes

MUTEX_NAME = r"Local\WorkBuddyDashboard.SingleInstance"
WINDOW_TITLE = "WorkBuddy 控制台"

ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183

WM_NULL = 0x0000
SMTO_ABORTIFHUNG = 0x0002
SW_RESTORE = 9
SW_SHOW = 5

MB_ICONINFORMATION = 0x00000040
MB_ICONWARNING = 0x00000030
MB_SETFOREGROUND = 0x00010000

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR
    ]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
    ]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    _user32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        ),
        wintypes.LPARAM,
    ]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowRect.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.RECT)
    ]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.IsHungAppWindow.argtypes = [wintypes.HWND]
    _user32.IsHungAppWindow.restype = wintypes.BOOL
    _user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindowAsync.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.FlashWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    _user32.FlashWindow.restype = wintypes.BOOL
    _user32.MessageBoxW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT
    ]
    _user32.MessageBoxW.restype = ctypes.c_int
    _user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    # LRESULT 是 LONG_PTR；不能用默认的 c_int，否则 64 位下会被截断。
    _user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t


def _process_image_name(pid):
    """返回 pid 对应的 exe 文件名（不含目录）；失败返回空串。"""
    if not _IS_WINDOWS or not pid:
        return ""
    handle = _kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not _kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value)
    finally:
        _kernel32.CloseHandle(handle)


def _window_area(hwnd):
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0
    return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)


def find_own_window():
    """找本进程面积最大的可见顶层窗口。

    用进程 PID 过滤，不读取窗口标题——窗口卡死时 GetWindowText 也可能
    阻塞，而 EnumWindows + GetWindowThreadProcessId + GetWindowRect
    不会给目标窗口发消息。
    """
    if not _IS_WINDOWS:
        return None

    best = [None, 0]
    own_pid = os.getpid()
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != own_pid or not _user32.IsWindowVisible(hwnd):
            return True
        area = _window_area(hwnd)
        if area > best[1]:
            best[0] = hwnd
            best[1] = area
        return True

    _user32.EnumWindows(callback_type(callback), 0)
    return best[0]


def find_window_by_process(process_name, exclude_pid=None):
    """按 exe 文件名找另一个进程的主窗口。

    不读窗口标题，因此即使已有实例已经卡死，这里也不会因为
    FindWindow/GetWindowText 而阻塞。
    """
    if not _IS_WINDOWS or not process_name:
        return None

    wanted = process_name.lower()
    best = [None, 0]
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or pid.value == exclude_pid:
            return True
        if _process_image_name(pid.value).lower() != wanted:
            return True
        area = _window_area(hwnd)
        if area > best[1]:
            best[0] = hwnd
            best[1] = area
        return True

    _user32.EnumWindows(callback_type(callback), 0)
    return best[0]


def acquire_single_instance(name=MUTEX_NAME):
    """创建/打开单实例互斥量。

    返回 (handle, already_running)：

      (handle, False)  第一次启动，调用方持有 handle 直到进程退出
      (None, True)     已有实例
      (None, False)    互斥量创建失败；调用方应继续启动（fail-open）
    """
    if not _IS_WINDOWS:
        return None, False

    handle = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        error = ctypes.get_last_error()
        # 权限不允许打开已有互斥量时，按“已有实例”处理更安全，
        # 至少不会在用户不知情的情况下再起一套 WebView2。
        return None, error == ERROR_ACCESS_DENIED

    # 必须紧接着 CreateMutexW 读取 LastError，中间不能插其他 ctypes 调用。
    error = ctypes.get_last_error()
    if error == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return None, True
    return handle, False


def release_single_instance(handle):
    """显式释放互斥量句柄；进程退出时系统也会自动释放。"""
    if handle and _IS_WINDOWS:
        _kernel32.CloseHandle(handle)


def is_window_responsive(hwnd, timeout_ms=2000):
    """用 WM_NULL + SendMessageTimeoutW 探测窗口消息循环。"""
    if not _IS_WINDOWS or not hwnd:
        return False
    result = ctypes.c_size_t()
    ok = _user32.SendMessageTimeoutW(
        hwnd,
        WM_NULL,
        0,
        0,
        SMTO_ABORTIFHUNG,
        max(1, int(timeout_ms)),
        ctypes.byref(result),
    )
    return bool(ok)


def activate_existing_window(
        process_name=None, retry_s=3.0, timeout_ms=1200):
    """激活另一个已运行实例的窗口。

    返回：
      "activated"  找到并已恢复/置前
      "hung"       找到窗口但消息循环无响应
      "missing"    超时仍未找到窗口（可能还在启动，或权限不同）
    """
    if not _IS_WINDOWS:
        return "missing"

    if not process_name:
        process_name = os.path.basename(sys.executable)

    deadline = time.monotonic() + max(0.0, retry_s)
    hwnd = None
    while True:
        hwnd = find_window_by_process(process_name, exclude_pid=os.getpid())
        if hwnd:
            break
        if time.monotonic() >= deadline:
            return "missing"
        time.sleep(0.2)

    if (_user32.IsHungAppWindow(hwnd)
            or not is_window_responsive(hwnd, timeout_ms)):
        return "hung"

    if _user32.IsIconic(hwnd):
        _user32.ShowWindowAsync(hwnd, SW_RESTORE)
    else:
        _user32.ShowWindowAsync(hwnd, SW_SHOW)

    if not _user32.SetForegroundWindow(hwnd):
        # 前台锁定可能让 SetForegroundWindow 失败；闪一下任务栏按钮，
        # 至少让用户知道窗口在哪。
        _user32.FlashWindow(hwnd, True)
    return "activated"


def show_message(text, title=WINDOW_TITLE, warning=False):
    """在没有 WebView2 的第二实例里显示一条原生提示。"""
    if not _IS_WINDOWS:
        return
    icon = MB_ICONWARNING if warning else MB_ICONINFORMATION
    _user32.MessageBoxW(None, text, title, MB_SETFOREGROUND | icon)


class UIWatchdog(threading.Thread):
    """后台 UI 线程看门狗。

    连续 max_misses 次探测失败后写线程栈并退出进程。退出是刻意选择：
    卡死时任何窗口操作都可能继续阻塞，os._exit 才能可靠释放 DWM。
    """

    def __init__(
        self,
        log_path,
        timeout_ms=2000,
        interval_s=1.0,
        max_misses=3,
        startup_grace_s=8.0,
        window_wait_s=30.0,
    ):
        super().__init__(name="WorkBuddyUIWatchdog", daemon=True)
        self.log_path = os.path.abspath(log_path)
        self.timeout_ms = max(100, int(timeout_ms))
        self.interval_s = max(0.1, float(interval_s))
        self.max_misses = max(1, int(max_misses))
        self.startup_grace_s = max(0.0, float(startup_grace_s))
        self.window_wait_s = max(0.0, float(window_wait_s))
        self._stop_event = threading.Event()
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        except OSError:
            pass

    def stop(self):
        self._stop_event.set()

    def _log(self, message):
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write("[%s] [watchdog] %s\n"
                         % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
                fh.flush()
        except Exception:
            pass

    def _dump_thread_stacks(self):
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write("[%s] [watchdog] UI 无响应，线程栈如下：\n"
                         % time.strftime("%Y-%m-%d %H:%M:%S"))
                faulthandler.dump_traceback(file=fh, all_threads=True)
                fh.flush()
        except Exception:
            pass

    def _wait_for_window(self, seconds):
        deadline = time.monotonic() + max(0.0, seconds)
        while (not self._stop_event.is_set()
               and time.monotonic() < deadline):
            hwnd = find_own_window()
            if hwnd:
                return hwnd
            self._stop_event.wait(0.5)
        return None

    def run(self):
        if not _IS_WINDOWS:
            return

        hwnd = self._wait_for_window(self.window_wait_s)
        if not hwnd:
            self._log("未找到主窗口，看门狗退出")
            return

        self._log("启动：hwnd=0x%X" % hwnd)
        if self._stop_event.wait(self.startup_grace_s):
            return

        misses = 0
        while not self._stop_event.is_set():
            if not _user32.IsWindow(hwnd):
                hwnd = self._wait_for_window(5.0)
                if not hwnd:
                    self._log("主窗口已关闭，看门狗退出")
                    return
                misses = 0
                self._log("主窗口重新出现：hwnd=0x%X" % hwnd)

            if is_window_responsive(hwnd, self.timeout_ms):
                if misses:
                    self._log("UI 已恢复响应")
                misses = 0
            else:
                misses += 1
                self._log(
                    "UI 无响应 %d/%d：hwnd=0x%X，超时=%dms"
                    % (misses, self.max_misses, hwnd, self.timeout_ms)
                )
                if misses >= self.max_misses:
                    self._log("连续无响应，强制退出以释放 WebView2/DWM")
                    self._dump_thread_stacks()
                    self._log("退出码=3")
                    os._exit(3)

            self._stop_event.wait(self.interval_s)
