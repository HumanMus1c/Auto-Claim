"""记住控制台的窗口位置与尺寸。

控制台每次启动都回到 1180x800 的默认位置：用户把窗口拖到副屏、拉大看日志，
下次启动又被打回原点，每次都得重调一遍。这里把窗口矩形存进
``%LOCALAPPDATA%\\Auto-Claim\\dashboard\\window.json``，下次启动原样恢复。

几条刻意的约束：

1. **只记「普通状态」的矩形**。最大化/最小化时窗口尺寸等于屏幕尺寸或 0，
   记下来会把用户真正想要的尺寸覆盖掉。最大化单独存一个 ``maximized`` 标记，
   恢复时交给 pywebview 自己最大化。
2. **恢复后必须校验可见性**。副屏拔掉、分辨率变了、把笔记本从扩展屏抱走，
   旧坐标可能落在桌面之外——那种窗口开在看不见的地方，用户只能删配置文件
   才救得回来。所以窗口出现后用 ``MonitorFromWindow`` 校验，不在桌面内就
   复位到主屏居中（见 ``ensure_visible()``）。
3. **写盘节流**。拖动/缩放会连续触发事件，几百毫秒内的多次变化合并成一次写，
   避免一遍拖动刷出上百次磁盘写入。

坐标口径与 pywebview 保持一致：``x/y/width/height`` 都是**逻辑像素**，
物理像素的换算由后端按显示器 DPI 处理；只有本模块内部直接调 Win32 的
那几个函数（``ensure_visible`` / ``center_on_primary``）用物理像素。
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time

DEFAULT_SIZE = (1180, 800)      # 首次启动的窗口尺寸
MIN_SIZE = (940, 640)           # 与 create_window 的 min_size 保持一致
STORE_NAME = "window.json"

# 判定「窗口还在桌面上」的最小可见面积（逻辑上不严格，物理像素下也够用）：
# 只要标题栏那一小块露出来，用户就能拖回来；只剩一条边露着则视为不可用。
MIN_VISIBLE = (120, 40)

# 尺寸的合理区间：再大就当成垃圾数据丢弃，避免一个坏值让窗口开到天外
MAX_SIDE = 8192
MAX_COORD = 32768

_IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------- 存取

def store_path(folder):
    """窗口几何的落盘路径。folder 由调用方给（打包后是 %LOCALAPPDATA% 下的目录）。"""
    return os.path.join(folder, STORE_NAME)


def _as_int(value):
    """把 JSON 里的值收敛成 int；布尔/字符串/None 一律当无效。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _size(raw, min_size, default_size):
    """宽高：逐项回退到默认值，并夹在 [min_size, MAX_SIDE] 之间。"""
    sizes = []
    for key, default, minimum in (
            ("width", default_size[0], min_size[0]),
            ("height", default_size[1], min_size[1])):
        value = _as_int(raw.get(key))
        if value is None:
            value = default
        value = max(int(minimum), min(value, MAX_SIDE))
        sizes.append(value)
    return sizes[0], sizes[1]


def _coord(value):
    if value is None:
        return None
    number = _as_int(value)
    if number is None or abs(number) > MAX_COORD:
        return None
    return number


def load(folder, min_size=MIN_SIZE, default_size=DEFAULT_SIZE):
    """读回上次的窗口几何。

    返回的 dict 一定含 width / height / maximized；只有 x、y 都有效时才会带上
    位置（首次启动或位置数据损坏时交给 pywebview 自己居中）。
    任何读盘/解析失败都退回默认值——窗口位置不值得让程序起不来。
    """
    raw = {}
    try:
        with open(store_path(folder), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    width, height = _size(raw, min_size, default_size)
    # 只认真正的 True：文件被手工改过、或写了 "yes" / 1 时，宁可不最大化，
    # 也不要一启动就顶着全屏把人吓一跳。
    rect = {"width": width, "height": height,
            "maximized": raw.get("maximized") is True}

    x, y = _coord(raw.get("x")), _coord(raw.get("y"))
    if x is not None and y is not None:
        rect["x"] = x
        rect["y"] = y
    return rect


def save(folder, rect):
    """原子写入窗口几何。失败只返回 False，不抛给调用方。"""
    payload = {
        "width": _as_int(rect.get("width")) or DEFAULT_SIZE[0],
        "height": _as_int(rect.get("height")) or DEFAULT_SIZE[1],
        "maximized": bool(rect.get("maximized")),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    x, y = _coord(rect.get("x")), _coord(rect.get("y"))
    if x is not None and y is not None:
        payload["x"] = x
        payload["y"] = y

    path = store_path(folder)
    temp = path + ".tmp"
    try:
        os.makedirs(folder, exist_ok=True)
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(temp, path)       # 原子替换：写到一半崩了也不会留下半个文件
        return True
    except OSError:
        try:
            os.remove(temp)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------- 变更跟踪

class Tracker:
    """把 moved / resized / maximized / restored 收敛成节流的落盘。

    pywebview 的事件回调跑在独立线程，拖动时每秒能来上百次，所以这里
    只更新内存里的矩形、延迟合并写盘；窗口关闭时 ``flush()`` 补最后一次。
    """

    def __init__(self, folder, rect=None, delay=0.4):
        self._folder = folder
        self._delay = delay
        self._lock = threading.Lock()
        self._timer = None
        self._rect = dict(rect or {})
        self._maximized = bool(self._rect.get("maximized"))
        self._minimized = False
        self._restored_position = ("x" in self._rect and "y" in self._rect)

    # -- 事件回调 --------------------------------------------------

    def bind(self, window):
        """把窗口事件接到本跟踪器上。

        事件名集中在这里（而不是散在 app.py 里），换后端或升级 pywebview 时
        只需要改这一处；任何一步失败都由调用方兜住。
        """
        window.events.moved += self.on_moved
        window.events.resized += self.on_resized
        window.events.maximized += self.on_maximized
        window.events.restored += self.on_restored
        window.events.minimized += self.on_minimized
        # 关闭前补最后一次落盘：拖动后 400ms 内关窗，节流计时器可能还没触发
        window.events.closing += self.flush

    def on_moved(self, x, y):
        """窗口被拖动。最大化/最小化时的坐标不是用户想要的位置，忽略。"""
        if self._maximized or self._minimized:
            return
        with self._lock:
            self._rect["x"] = int(x)
            self._rect["y"] = int(y)
        self._schedule()

    def on_resized(self, width, height):
        """窗口被缩放。最大化的尺寸等于屏幕尺寸，记下来会覆盖还原后的大小。"""
        if self._maximized or self._minimized:
            return
        if width < MIN_SIZE[0] or height < MIN_SIZE[1]:
            return
        with self._lock:
            self._rect["width"] = int(width)
            self._rect["height"] = int(height)
        self._schedule()

    def on_maximized(self):
        """最大化：只记标记，不动矩形（矩形是用户还原后要回到的位置）。"""
        self._maximized = True
        with self._lock:
            self._rect["maximized"] = True
        self._schedule()

    def on_minimized(self):
        """最小化：尺寸变成 0 附近，什么都不记。"""
        self._minimized = True

    def on_restored(self):
        """从最大化/最小化还原：解除标记，随后到来的 resized 才是真实尺寸。"""
        self._maximized = False
        self._minimized = False
        with self._lock:
            self._rect["maximized"] = False
        self._schedule()

    # -- 读写 ------------------------------------------------------

    def sync_from(self, window):
        """从活窗口读回真实矩形。

        用于「位置被判定为不可见、程序自己把窗口挪回来」之后：此时事件回调
        里记下的还是旧坐标，必须用窗口实际值覆盖，否则下次启动又回到桌面外。
        """
        values = {}
        for name in ("x", "y", "width", "height"):
            try:
                values[name] = int(getattr(window, name))
            except Exception:
                values[name] = None
        with self._lock:
            if values.get("width") and values.get("height"):
                self._rect["width"] = values["width"]
                self._rect["height"] = values["height"]
            if None not in (values.get("x"), values.get("y")):
                self._rect["x"] = values["x"]
                self._rect["y"] = values["y"]
                self._restored_position = True
        self.flush()

    @property
    def restored_position(self):
        """启动时是否有可用的历史位置（没有就该让系统居中）。"""
        return self._restored_position

    def snapshot(self):
        with self._lock:
            return dict(self._rect)

    def flush(self):
        """立刻落盘（关闭窗口前调用）。"""
        with self._lock:
            timer, self._timer = self._timer, None
            rect = dict(self._rect)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        return save(self._folder, rect)

    def _schedule(self):
        with self._lock:
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
            timer = threading.Timer(self._delay, self.flush)
            timer.daemon = True
            self._timer = timer
            timer.start()


# ---------------------------------------------------------------- Win32 校验

if _IS_WINDOWS:
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    class _RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                    ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]

    MONITOR_DEFAULTTONEAREST = 2
    SPI_GETWORKAREA = 0x0030
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010

    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.MonitorFromWindow.restype = wintypes.HMONITOR
    _user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR,
                                        ctypes.POINTER(_MONITORINFO)]
    _user32.GetMonitorInfoW.restype = wintypes.BOOL
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                     ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int,
                                     wintypes.UINT]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.IsZoomed.argtypes = [wintypes.HWND]
    _user32.IsZoomed.restype = wintypes.BOOL
    _user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                              ctypes.c_void_p,
                                              wintypes.UINT]
    _user32.SystemParametersInfoW.restype = wintypes.BOOL


def _window_rect(hwnd):
    """窗口当前矩形（物理像素）：(left, top, right, bottom)。"""
    if not _IS_WINDOWS or not hwnd:
        return None
    rect = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _work_area(hwnd):
    """窗口所在显示器的工作区（物理像素，已排除任务栏）。"""
    if not _IS_WINDOWS or not hwnd:
        return None
    monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not monitor:
        return None
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    work = info.rcWork
    return work.left, work.top, work.right, work.bottom


def _primary_work_area():
    if not _IS_WINDOWS:
        return None
    rect = _RECT()
    if not _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0,
                                         ctypes.byref(rect), 0):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def center_on_primary(hwnd):
    """把窗口移回主屏居中，并在超出工作区时缩小。返回是否真的动过。"""
    if not _IS_WINDOWS or not hwnd:
        return False
    # 最大化时不要碰位置：最大化窗口的矩形本来就是屏幕矩形，挪动只会让它
    # 变成「中间那个大小」的怪窗口。
    if _user32.IsZoomed(hwnd):
        return False
    current = _window_rect(hwnd)
    work = _primary_work_area()
    if not current or not work:
        return False

    wl, wt, wr, wb = work
    width = min(current[2] - current[0], wr - wl)
    height = min(current[3] - current[1], wb - wt)
    x = wl + max(0, (wr - wl - width) // 2)
    y = wt + max(0, (wb - wt - height) // 2)
    ok = _user32.SetWindowPos(hwnd, None, x, y, width, height,
                              SWP_NOZORDER | SWP_NOACTIVATE)
    return bool(ok)


def ensure_visible(hwnd, min_visible=MIN_VISIBLE):
    """确认窗口还落在可用的桌面范围内。

    返回 ``"ok"``（位置可用）、``"recentered"``（已复位到主屏居中）或
    ``"skipped"``（拿不到句柄/非 Windows/最大化，不处理）。
    """
    if not _IS_WINDOWS or not hwnd:
        return "skipped"
    if _user32.IsZoomed(hwnd):
        return "skipped"

    current = _window_rect(hwnd)
    work = _work_area(hwnd)
    if not current or not work:
        return "skipped"

    left, top, right, bottom = current
    wl, wt, wr, wb = work
    overlap_w = min(right, wr) - max(left, wl)
    overlap_h = min(bottom, wb) - max(top, wt)
    fits = ((right - left) <= (wr - wl) and (bottom - top) <= (wb - wt))

    if overlap_w >= min_visible[0] and overlap_h >= min_visible[1] and fits:
        return "ok"
    return "recentered" if center_on_primary(hwnd) else "skipped"
