"""Win32 单实例锁与 UI 线程看门狗。

这两个能力都只依赖 ctypes，不引入新的第三方依赖：

  * acquire_single_instance()
      进程级命名互斥量。第一次启动创建成功并持有句柄；第二次启动
      发现同名对象已存在，激活已有窗口后退出，避免再起一套 WebView2。

  * UIWatchdog
      后台线程定期检查主窗口是否还在响应。判定同时看三路信号：

        1. IsHungAppWindow()：Windows 自己的“无响应”结论（5 秒无
           PeekMessage）。本次排障中正是它触发了 AppHangB1，而只看
           WM_NULL 的旧实现完全没察觉。
        2. SendMessageTimeoutW(WM_NULL)：消息循环是否还在派发消息。
        3. JS 心跳：前端每 5 秒调用一次 Api.heartbeat()，要经过
           WebMessageReceived（UI 线程）和 evaluate_js（UI 线程）整条
           链路。心跳停滞说明 UI 线程或 WebView2 已被卡住。

      连续多次判定为卡死后，写入 Python 线程栈和一份原生 minidump，
      再 os._exit()。dump 是唯一能在事后还原 UI 线程原生调用栈的线索。

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

# 卡死转储：只需要线程栈，不要整份内存
GENERIC_WRITE = 0x40000000
CREATE_ALWAYS = 2
FILE_ATTRIBUTE_NORMAL = 0x80
MINIDUMP_WITH_THREAD_INFO = 0x00001000

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


def find_own_windows():
    """返回本进程所有可见顶层窗口：[(hwnd, area, iconic), ...]。

    按面积从大到小排序。看门狗每一轮都重新枚举，而不是只在启动时记
    一个 HWND——窗口句柄可能被销毁后复用，旧实现会一直探测一个已经
    不属于本进程的窗口，从而把真正的卡死漏掉。
    """
    if not _IS_WINDOWS:
        return []

    windows = []
    own_pid = os.getpid()
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != own_pid or not _user32.IsWindowVisible(hwnd):
            return True
        windows.append((hwnd, _window_area(hwnd), bool(_user32.IsIconic(hwnd))))
        return True

    _user32.EnumWindows(callback_type(callback), 0)
    windows.sort(key=lambda item: item[1], reverse=True)
    return windows


def find_own_window():
    """找本进程面积最大的可见顶层窗口；没有则返回 None。"""
    windows = find_own_windows()
    return windows[0][0] if windows else None


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


def window_state(hwnd, timeout_ms=1000):
    """返回 (alive, owned, hung, responsive, iconic)。

    hung 用的是 Windows 自己的判定（IsHungAppWindow），和任务管理器
    画“未响应”、WER 报 AppHangB1 是同一套标准：线程 5 秒内没有调用
    PeekMessage。它比 WM_NULL 更早、也更接近用户看到的现象。
    """
    if not _IS_WINDOWS or not hwnd or not _user32.IsWindow(hwnd):
        return False, False, False, False, False

    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value != os.getpid():
        # 句柄已被销毁并复用，不能再当成自己的窗口
        return True, False, False, False, False

    return (
        True,
        True,
        bool(_user32.IsHungAppWindow(hwnd)),
        is_window_responsive(hwnd, timeout_ms),
        bool(_user32.IsIconic(hwnd)),
    )


def write_minidump(path):
    """把当前进程的线程栈写成 minidump；失败返回 False。

    只在看门狗判定卡死时调用。MiniDumpWriteDump 会挂起其它线程，
    在已经卡死的进程里有再次阻塞的风险，所以调用方必须放到独立线程
    并设置超时，不能让它拖住退出流程。
    """
    if not _IS_WINDOWS:
        return False

    try:
        dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return False

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    dbghelp.MiniDumpWriteDump.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    dbghelp.MiniDumpWriteDump.restype = wintypes.BOOL

    handle = kernel32.CreateFileW(
        path, GENERIC_WRITE, 0, None, CREATE_ALWAYS,
        FILE_ATTRIBUTE_NORMAL, None,
    )
    if not handle or handle == ctypes.c_void_p(-1).value:
        return False

    try:
        return bool(dbghelp.MiniDumpWriteDump(
            kernel32.GetCurrentProcess(),
            os.getpid(),
            handle,
            MINIDUMP_WITH_THREAD_INFO,
            None,
            None,
            None,
        ))
    finally:
        kernel32.CloseHandle(handle)


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

    连续 max_misses 次判定为卡死后写线程栈、抓一份原生 minidump，再退出。
    退出是刻意选择：卡死时任何窗口操作都可能继续阻塞，os._exit 才能可靠
    释放 WebView2 子进程和 DirectComposition 表面。

    只看 WM_NULL 是不够的。2026-10-20 18:38 那次 AppHangB1 里，Windows
    已经把窗口判成 “Top level window is idle”，但旧的 WM_NULL 探测没有
    记录到任何一次失败，看门狗形同虚设。现在同时看 IsHungAppWindow、
    WM_NULL 和前端心跳，并且周期性把“探测正常”也写进日志——否则事后
    无法区分“看门狗判定正常”和“看门狗根本没在跑”。
    """

    def __init__(
        self,
        log_path,
        timeout_ms=1000,
        interval_s=1.0,
        max_misses=2,
        startup_grace_s=8.0,
        window_wait_s=30.0,
        heartbeat_stale_s=20.0,
        probe_log_every_s=120.0,
        dump_keep=3,
    ):
        super().__init__(name="WorkBuddyUIWatchdog", daemon=True)
        self.log_path = os.path.abspath(log_path)
        self.timeout_ms = max(100, int(timeout_ms))
        self.interval_s = max(0.1, float(interval_s))
        self.max_misses = max(1, int(max_misses))
        self.startup_grace_s = max(0.0, float(startup_grace_s))
        self.window_wait_s = max(0.0, float(window_wait_s))
        self.heartbeat_stale_s = max(5.0, float(heartbeat_stale_s))
        self.probe_log_every_s = max(10.0, float(probe_log_every_s))
        self.dump_keep = max(1, int(dump_keep))
        self._stop_event = threading.Event()
        self._last_beat = 0.0
        self._beat_count = 0
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        except OSError:
            pass

    def stop(self):
        self._stop_event.set()

    def beat(self):
        """前端心跳。由 Api.heartbeat() 在后台线程里调用。"""
        self._last_beat = time.monotonic()
        self._beat_count += 1
        if self._beat_count == 1:
            # 只记一次：证明 JS → WebMessageReceived → evaluate_js 整条链路通了
            self._log("收到前端心跳：整条 UI 链路可用")

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

    def _write_hang_dump(self):
        """抓一份原生 minidump，失败也继续退出。"""
        if os.environ.get("WORKBUDDY_HANG_DUMP", "1") == "0":
            return

        folder = os.path.join(os.path.dirname(self.log_path), "dumps")
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            folder = os.path.dirname(self.log_path)

        path = os.path.join(
            folder, "hang-%s.dmp" % time.strftime("%Y%m%d-%H%M%S"))
        outcome = []

        def _run():
            outcome.append(write_minidump(path))

        thread = threading.Thread(
            target=_run, name="WorkBuddyHangDump", daemon=True)
        thread.start()
        thread.join(8.0)

        if outcome and outcome[0]:
            self._log("已写入卡死转储：%s" % path)
        elif thread.is_alive():
            self._log("卡死转储超时（写入线程仍阻塞）：%s" % path)
        else:
            self._log("卡死转储失败：%s" % path)

        self._prune_dumps(folder)

    def _prune_dumps(self, folder):
        try:
            dumps = [
                os.path.join(folder, name)
                for name in os.listdir(folder)
                if name.startswith("hang-") and name.endswith(".dmp")
            ]
            dumps.sort(key=os.path.getmtime, reverse=True)
            for old in dumps[self.dump_keep:]:
                os.remove(old)
        except OSError:
            pass

    def _start_hard_exit(self, delay_s=20.0):
        """转储/faulthandler 本身也可能卡住，最后留一个硬退出的兜底。"""
        def _run():
            time.sleep(max(1.0, delay_s))
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.GetCurrentProcess.restype = wintypes.HANDLE
                kernel32.TerminateProcess.argtypes = [
                    wintypes.HANDLE, wintypes.UINT]
                kernel32.TerminateProcess(
                    kernel32.GetCurrentProcess(), 3)
            except Exception:
                pass
            os._exit(3)

        threading.Thread(
            target=_run, name="WorkBuddyHardExit", daemon=True).start()

    def _wait_for_window(self, seconds):
        deadline = time.monotonic() + max(0.0, seconds)
        while (not self._stop_event.is_set()
               and time.monotonic() < deadline):
            hwnd = find_own_window()
            if hwnd:
                return hwnd
            self._stop_event.wait(0.5)
        return None

    def _heartbeat_stale(self, iconic):
        if self._last_beat <= 0 or iconic:
            return False
        gap = time.monotonic() - self._last_beat
        if gap <= self.heartbeat_stale_s:
            return False
        # 睡眠/休眠期间 JS 定时器也会停。超过阈值很多倍的空档更可能是
        # 系统挂起，而不是 UI 卡死；重新计时，避免唤醒后立刻误杀。
        if gap > self.heartbeat_stale_s * 8:
            self._last_beat = time.monotonic()
            self._log("心跳中断 %.0fs，按系统休眠/时钟跳变处理，重新计时"
                      % gap)
            return False
        return True

    def run(self):
        if not _IS_WINDOWS:
            return

        hwnd = self._wait_for_window(self.window_wait_s)
        if not hwnd:
            self._log("未找到主窗口，看门狗退出")
            return

        self._log(
            "启动：hwnd=0x%X，探测=%dms×%d，心跳超时=%.0fs"
            % (hwnd, self.timeout_ms, self.max_misses,
               self.heartbeat_stale_s)
        )
        if self._stop_event.wait(self.startup_grace_s):
            return

        misses = 0
        last_probe_log = 0.0
        while not self._stop_event.is_set():
            windows = find_own_windows()
            if not windows:
                hwnd = self._wait_for_window(5.0)
                if not hwnd:
                    self._log("主窗口已关闭，看门狗退出")
                    return
                misses = 0
                self._log("主窗口重新出现：hwnd=0x%X" % hwnd)
            else:
                # 每轮都用当前最大的窗口，避免句柄销毁后被复用却还在探测
                hwnd = windows[0][0]

            alive, owned, hung, responsive, iconic = window_state(
                hwnd, self.timeout_ms)
            if not alive or not owned:
                # 句柄刚好在枚举后失效：下一轮重新枚举即可
                self._stop_event.wait(self.interval_s)
                continue

            stale = self._heartbeat_stale(iconic)
            if hung or not responsive or stale:
                misses += 1
                reasons = []
                if hung:
                    reasons.append("IsHungAppWindow=1")
                if not responsive:
                    reasons.append("WM_NULL 超时")
                if stale:
                    reasons.append("心跳停滞 %.0fs（共 %d 次）"
                                   % (time.monotonic() - self._last_beat,
                                      self._beat_count))
                self._log(
                    "UI 无响应 %d/%d：hwnd=0x%X，%s"
                    % (misses, self.max_misses, hwnd,
                       "，".join(reasons) or "未知")
                )
                if misses >= self.max_misses:
                    self._log("连续无响应，强制退出以释放 WebView2/DWM")
                    self._start_hard_exit()
                    self._dump_thread_stacks()
                    self._write_hang_dump()
                    self._log("退出码=3")
                    os._exit(3)
            else:
                if misses:
                    self._log("UI 已恢复响应")
                misses = 0
                now = time.monotonic()
                if now - last_probe_log >= self.probe_log_every_s:
                    last_probe_log = now
                    beat_age = ("未收到" if self._last_beat <= 0
                                else "%.0fs 前" % (now - self._last_beat))
                    self._log(
                        "探测正常：hwnd=0x%X，可见=1，最小化=%d，"
                        "心跳=%s（累计 %d 次）"
                        % (hwnd, 1 if iconic else 0, beat_age,
                           self._beat_count)
                    )

            self._stop_event.wait(self.interval_s)
