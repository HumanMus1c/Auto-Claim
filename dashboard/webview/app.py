"""pywebview 后端 API。

JS 通过 window.pywebview.api.* 调用这里的函数。

设计约束：
  - 只暴露非 silent 命令（status / auto / growth / claim / all），
    写操作由前端二次确认
  - 命令统一走进程内 _run()（不用 subprocess，避免打包后递归启动）
  - 每个耗时调用都走后台线程 + 完成事件，界面不会被长超时请求卡住
  - 路径与超时集中在这里，前端只管渲染
  - 计划任务通过 --service / --qoder-service / --trae-service 三个无窗口
    入口回调本 exe，三条链路都不依赖系统 Python
"""

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time

# _locate_signin() 走过的候选路径，失败时供界面展示排查线索
SEARCHED = []


def _base_dir():
    """资源根目录。

    PyInstaller 单文件模式下，sys.frozen 为真、__file__ 指向解包后的临时目录
    （sys._MEIPASS），资源被放在那里；源码运行时则用脚本所在目录。
    打包与直跑必须走不同分支，否则 exe 起来就是白屏。
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


HERE = _base_dir()
ASSETS = os.path.join(HERE, "assets")
DASHBOARD = os.path.dirname(HERE)

# win_theme / win_title 在上一级目录（dashboard/），本方案继续复用
if DASHBOARD not in sys.path:
    sys.path.insert(0, DASHBOARD)


def _locate_signin():
    """定位 signin.py。

    搜索起点分两种情况，这里踩过坑：
      - 源码运行：从脚本所在目录（dashboard/webview）上溯
      - 打包运行：sys._MEIPASS 是 %TEMP% 下的临时解包目录，从那里上溯只会
        一路搜到 C:\\Users\\，**永远碰不到 exe 所在目录**——而 signin.py 正是
        跟 exe 放在一起的。所以冻结时必须优先从 dirname(sys.executable) 找。

    不写死上溯层数，目录挪动后仍能命中；环境变量优先级最高。
    """
    override = os.environ.get("WORKBUDDY_DEMO_SIGNIN")
    if override:
        return override

    roots = []
    if getattr(sys, "frozen", False):
        # exe 同级 -> 用户的预期用法（两个文件放一起）
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
        roots.append(HERE)                 # 解包目录，仅作兜底
    roots.append(HERE)
    roots.append(os.getcwd())              # 从命令行在别处启动时的最后兜底

    global SEARCHED
    SEARCHED = []
    seen = set()
    for start in roots:
        path = os.path.abspath(start)
        for _ in range(6):
            if path in seen:
                break
            seen.add(path)
            candidate = os.path.join(path, "signin.py")
            SEARCHED.append(candidate)
            if os.path.isfile(candidate):
                return candidate
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    # 兜底：明确报缺失，不静默。SIGNIN_SEARCHED 供界面把搜索路径显示出来，
    # 否则用户只看到"未找到"却不知道程序到底去哪儿找过。
    return os.path.join(HERE, "signin.py")


SIGNIN = _locate_signin()

# 仓库根目录也放进 sys.path：tasks() 要复用 signin.py 的认证链路
# （凭据解密、请求头、重试），必须能 import 到它。这里直接由
# _locate_signin() 的结果推导，免得另写一套上溯层数又走偏。
_REPO = os.path.dirname(SIGNIN)
if os.path.isdir(_REPO) and _REPO not in sys.path:
    sys.path.insert(0, _REPO)

QODER_SEARCHED = []


def _locate_qoder():
    """定位 qoder_signin.py，规则与 _locate_signin() 保持一致。

    Qoder 是上游新增的独立脚本，和控制台 exe / signin.py 一样放在仓库根。
    打包后优先找 exe 同级目录，源码运行则从 dashboard/webview 上溯。
    """
    override = (os.environ.get("WORKBUDDY_QODER_SIGNIN")
                or os.environ.get("QODER_SIGNIN"))
    if override:
        return override

    roots = []
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    roots.extend([HERE, os.getcwd()])

    global QODER_SEARCHED
    QODER_SEARCHED = []
    seen = set()
    for start in roots:
        path = os.path.abspath(start)
        for _ in range(6):
            if path in seen:
                break
            seen.add(path)
            candidate = os.path.join(path, "qoder_signin.py")
            QODER_SEARCHED.append(candidate)
            if os.path.isfile(candidate):
                return candidate
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    return os.path.join(HERE, "qoder_signin.py")


QODER = _locate_qoder()

TRAE_SEARCHED = []


def _locate_trae():
    """定位 trae_signin.py，规则与 Qoder 保持一致。"""
    override = (os.environ.get("WORKBUDDY_TRAE_SIGNIN")
                or os.environ.get("TRAE_SIGNIN"))
    if override:
        return override

    roots = []
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    roots.extend([HERE, os.getcwd()])

    global TRAE_SEARCHED
    TRAE_SEARCHED = []
    seen = set()
    for start in roots:
        path = os.path.abspath(start)
        for _ in range(6):
            if path in seen:
                break
            seen.add(path)
            candidate = os.path.join(path, "trae_signin.py")
            TRAE_SEARCHED.append(candidate)
            if os.path.isfile(candidate):
                return candidate
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    return os.path.join(HERE, "trae_signin.py")


TRAE = _locate_trae()


# ---------- 桌面端安装 / 运行探测 ----------
#
# 侧边栏的状态点回答的是「这台机器现在能不能连上这个模块」。只看登录态
# 不够：Qoder 卸载后本地可能还留着能解密的凭据，doctor 仍返回 OK，于是
# 一个已经卸载的模块被画成绿点「已连接」。这里补一层本机探测，把
# 「已安装 / 正在运行」一起交给前端，状态点才能说实话。
#
# installed / running 为 None 表示无法判断（非 Windows 或探测异常）；
# 前端遇到 None 会退回只看登录态，不把「未知」当成「未安装」。

_DESKTOP_TTL = 30.0
_DESKTOP_CACHE = {}
_DESKTOP_LOCK = threading.Lock()

# 进程列表缓存：bootstrap 一次要查三个模块，逐次枚举进程没有必要
_PROC_TTL = 5.0
_PROC_CACHE = (0.0, set())

# 卸载表缓存：扫注册表比枚举进程慢一个量级
_REG_TTL = 60.0
_REG_CACHE = (0.0, [])

_DESKTOP_HINTS = {
    "workbuddy": {
        "keywords": ("workbuddy",),
        "dirs": (r"%ProgramFiles%\WorkBuddy", r"%ProgramFiles(x86)%\WorkBuddy"),
        "launcher": None,
    },
    "qoder": {
        "keywords": ("qoder",),
        "dirs": (r"%LOCALAPPDATA%\Programs\Qoder CN",
                 r"%ProgramFiles%\Qoder CN",
                 r"%ProgramFiles(x86)%\Qoder CN"),
        # 自定义安装目录（例如 C:\Programs\Qoder CN）只有启动器记着
        "launcher": r"%LOCALAPPDATA%\Qoder CN\Qoder CN Launcher\state.ini",
    },
    "trae": {
        "keywords": ("trae",),
        "dirs": (r"%LOCALAPPDATA%\Programs\Trae CN",
                 r"%LOCALAPPDATA%\Programs\Trae",
                 r"%ProgramFiles%\Trae CN"),
        "launcher": None,
    },
}


# 卸载表根键：以字符串保存，函数里再 getattr(winreg, name)，
# 避免在模块顶层 import winreg（非 Windows 没有这个模块）
_UNINSTALL_ROOTS = (
    ("HKEY_CURRENT_USER",
     r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKEY_LOCAL_MACHINE",
     r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKEY_LOCAL_MACHINE",
     r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
)


def _process_images():
    """当前进程的 exe 文件名集合（小写）。非 Windows / 读取失败返回空集合。"""
    global _PROC_CACHE
    now = time.time()
    if _PROC_CACHE[0] and now - _PROC_CACHE[0] < _PROC_TTL:
        return _PROC_CACHE[1]

    names = set()
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD)]
            kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            block = (wintypes.DWORD * 4096)()
            used = wintypes.DWORD()
            if psapi.EnumProcesses(ctypes.byref(block), ctypes.sizeof(block),
                                   ctypes.byref(used)):
                total = used.value // ctypes.sizeof(wintypes.DWORD)
                for index in range(total):
                    pid = block[index]
                    if not pid:
                        continue
                    handle = kernel32.OpenProcess(0x1000, False, pid)
                    if not handle:
                        continue
                    try:
                        buf = ctypes.create_unicode_buffer(1024)
                        size = wintypes.DWORD(len(buf))
                        if kernel32.QueryFullProcessImageNameW(
                                handle, 0, buf, ctypes.byref(size)):
                            names.add(os.path.basename(buf.value).lower())
                    finally:
                        kernel32.CloseHandle(handle)
        except Exception as exc:
            _trace("进程探测失败: %r" % (exc,))

    _PROC_CACHE = (now, names)
    return names


def _reg_value(winreg, item, field):
    """读一个注册表值；不存在时返回空串。"""
    try:
        return str(winreg.QueryValueEx(item, field)[0])
    except OSError:
        return ""


def _read_uninstall_entry(winreg, key, name):
    """读一个卸载条目，返回 (显示名, 安装目录, 图标路径)。"""
    row = None
    values = ["", "", ""]
    try:
        with winreg.OpenKey(key, name) as item:
            values = [_reg_value(winreg, item, field)
                      for field in ("DisplayName", "InstallLocation",
                                    "DisplayIcon")]
        if values[0]:
            row = (values[0], values[1], values[2])
    except OSError as exc:
        _trace("卸载条目不可读(%s): %r" % (name, exc))
    return row


def _subkey_names(winreg, key):
    """列出一个注册表键的子键；中途失败时返回已读到的部分。"""
    try:
        total = winreg.QueryInfoKey(key)[0]
    except OSError:
        return []

    names = []
    index = 0
    while index < total:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError as exc:
            _trace("卸载表枚举中断: %r" % (exc,))
            break
        index += 1
    return names


def _read_uninstall_key(winreg, hive_name, sub):
    """读取一个卸载表根键下的全部条目。"""
    hive = getattr(winreg, hive_name, None)
    rows = []
    key = None
    if hive:
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError as exc:
            _trace("卸载表根键不可读(%s): %r" % (sub, exc))
    if key:
        try:
            for name in _subkey_names(winreg, key):
                row = _read_uninstall_entry(winreg, key, name)
                if row:
                    rows.append(row)
        finally:
            winreg.CloseKey(key)
    return rows


def _registry_installs():
    """卸载注册表里的 (显示名, 安装目录, 图标路径)；读取失败返回 []。"""
    global _REG_CACHE
    now = time.time()
    if _REG_CACHE[0] and now - _REG_CACHE[0] < _REG_TTL:
        return _REG_CACHE[1]

    rows = []
    if os.name == "nt":
        try:
            import winreg

            for hive_name, sub in _UNINSTALL_ROOTS:
                rows.extend(_read_uninstall_key(winreg, hive_name, sub))
        except Exception as exc:
            _trace("卸载表读取失败: %r" % (exc,))

    _REG_CACHE = (now, rows)
    return rows


def _first_exe(folder, keywords):
    """目录里名字含关键字的 exe；只扫一层，避免遍历整棵安装目录。

    「Uninstall X.exe」也含关键字，必须排除；恰好等于关键字的
    （WorkBuddy.exe）优先级最高，避免把卸载器当成主程序。
    """
    try:
        names = os.listdir(folder)
    except OSError:
        return None

    best = None
    best_score = 0
    for name in names:
        low = name.lower()
        if not low.endswith(".exe") or low.startswith("uninstall"):
            continue
        if not any(word in low for word in keywords):
            continue
        score = 2 if low[:-4] in keywords else 1
        if score > best_score:
            best = os.path.join(folder, name)
            best_score = score
    return best


def _install_from_launcher(path, keywords):
    """Qoder 启动器的 state.ini 记着真实的安装目录（可以是自定义路径）。"""
    text = ""
    try:
        with io.open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        pass

    folder = ""
    exe = ""
    for line in text.splitlines():
        item = line.strip()
        low = item.lower()
        if low.startswith("installdir="):
            folder = item.split("=", 1)[1].strip()
        elif low.startswith("appexecutable="):
            exe = item.split("=", 1)[1].strip()
    if not folder:
        return None

    target = os.path.join(folder, exe) if exe else ""
    if target and os.path.isfile(target):
        return target
    return _first_exe(folder, keywords)


def _registry_folder(value):
    """把 InstallLocation / DisplayIcon 统一成目录。"""
    text = (value or "").strip().strip('"')
    if not text:
        return ""
    head = text.split(",")[0].strip().strip('"')
    if head.lower().endswith(".exe"):
        return os.path.dirname(head)
    return head


def _install_candidates(kind):
    """按优先级给出候选安装目录：(目录, 来源)。"""
    spec = _DESKTOP_HINTS[kind]
    for pattern in spec["dirs"]:
        yield os.path.expandvars(pattern), "目录"

    launcher = spec["launcher"]
    if launcher:
        exe = _install_from_launcher(os.path.expandvars(launcher),
                                     spec["keywords"])
        if exe:
            yield os.path.dirname(exe), "启动器"

    for display, location, icon in _registry_installs():
        if not any(word in display.lower() for word in spec["keywords"]):
            continue
        for raw in (location, icon):
            folder = _registry_folder(raw)
            if folder:
                yield folder, "注册表"


def _find_install(kind):
    """返回 (exe 路径, 探测来源)；找不到返回 (None, None)。"""
    keywords = _DESKTOP_HINTS[kind]["keywords"]
    for folder, source in _install_candidates(kind):
        exe = _first_exe(folder, keywords)
        if exe:
            return exe, source
    return None, None


def _desktop_cached(kind, now):
    with _DESKTOP_LOCK:
        hit = _DESKTOP_CACHE.get(kind)
    if hit and now - hit[0] < _DESKTOP_TTL:
        return dict(hit[1])
    return None


def _desktop_store(kind, now, state):
    with _DESKTOP_LOCK:
        _DESKTOP_CACHE[kind] = (now, dict(state))


def _desktop_state(kind):
    """探测桌面端：{"installed", "running", "path", "source"}。"""
    if kind not in _DESKTOP_HINTS:
        return {"installed": None, "running": None, "path": None,
                "source": None}

    now = time.time()
    hit = _desktop_cached(kind, now)
    if hit is not None:
        return hit

    state = {"installed": None, "running": None, "path": None, "source": None}
    if os.name == "nt":
        try:
            names = _process_images()
            state["running"] = any(
                any(word in name for word in _DESKTOP_HINTS[kind]["keywords"])
                for name in names)
        except Exception as exc:
            _trace("运行状态探测失败(%s): %r" % (kind, exc))
        try:
            exe, source = _find_install(kind)
            state.update(installed=bool(exe), path=exe, source=source)
        except Exception as exc:
            _trace("安装状态探测失败(%s): %r" % (kind, exc))

    _desktop_store(kind, now, state)
    return state


def _signin_log_path():
    """signin.py 的默认日志位置；环境变量优先，规则与 emit() 一致。"""
    override = os.environ.get("WORKBUDDY_SIGNIN_LOG")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(SIGNIN)), "signin.log")


def _qoder_log_path():
    """qoder_signin.py 的默认日志位置；环境变量优先。"""
    override = os.environ.get("QODER_SIGNIN_LOG")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(QODER)), "qoder_signin.log")


def _trae_log_path():
    """trae_signin.py 的默认日志位置；环境变量优先。"""
    override = os.environ.get("TRAE_SIGNIN_LOG")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(TRAE)), "trae_signin.log")


SERVICE_ENTRY = {
    "workbuddy": "--service",
    "qoder": "--qoder-service",
    "trae": "--trae-service",
}


def _service_runner(kind="workbuddy"):
    """计划任务要启动的命令：可执行文件 + 固定前置参数。

    打包后直接运行本 exe 的对应子命令（``--service`` / ``--qoder-service`` /
    ``--trae-service``），任务完全不依赖系统 Python；源码运行时则用
    pythonw.exe 跑 app.py，避免弹出控制台窗口。

    Qoder / Trae 早先只认系统 pythonw，纯 exe 机器上安装必然失败；现在它们
    也走这里，只是入口参数不同。
    """
    flag = SERVICE_ENTRY.get(kind, "--service")
    if getattr(sys, "frozen", False):
        return sys.executable, [flag]

    exe = os.path.abspath(sys.executable)
    folder, name = os.path.split(exe)
    if name.lower() == "python.exe":
        pythonw = os.path.join(folder, "pythonw.exe")
        if os.path.isfile(pythonw):
            exe = pythonw
    return exe, [os.path.abspath(__file__), flag]


def _service_error(exc):
    """把安装/操作失败整理成前端可直接渲染的结构。

    带 code / hint / detail：卡片上要给用户一句能读懂的话和下一步动作，
    光有「失败」既解释不了原因也给不出办法。
    """
    try:
        import win_service
        return win_service.describe_error(exc)
    except Exception:
        return {"ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "code": "unknown",
                "hint": "请展开「详情」复制报错后重试；仍失败可到 GitHub 提交 issue。"}


def _missing_script(name, label):
    """脚本缺失时的统一失败结构。"""
    return {"ok": False,
            "error": "未找到 %s，无法安装 %s 计划任务" % (name, label),
            "code": "file-missing",
            "hint": "请把 %s 与控制台 exe 放在同一目录后重试。" % name,
            "detail": name}


def _service_status_failure(exc):
    """读状态失败时返回的完整状态结构。

    前端三张卡片按同一份字段渲染，失败时也必须给出 code / hint，
    否则卡片只剩「读取失败：ServiceError: ...」这种没用的话。
    """
    payload = _service_error(exc)
    return {
        "supported": os.name == "nt",
        "ok": False,
        "error": payload["error"],
        "code": payload["code"],
        "hint": payload["hint"],
        "detail": payload.get("detail"),
        "tasks": [],
        "installed": False,
        "installed_count": 0,
        "enabled": False,
        "running": False,
        "next_run": None,
        "last_run": None,
        "log": None,
    }


def _system_theme():
    """在创建窗口前读取系统主题与强调色。

    窗口背景色、首帧图标和首屏配色都依赖这份结果：等 WebView2 加载完、
    JS bridge 就绪后再探测，深色桌面上会先闪一下浅色窗口。读不到时按
    浅色处理，不影响启动；运行期的变化由 win_icon 轮询后推送。
    """
    try:
        import win_theme
        dark, accent = win_theme.detect()
        return {"dark": bool(dark), "accent": [int(c) for c in accent]}
    except Exception:
        return {"dark": False, "accent": [0, 120, 212]}


def _system_dark():
    """当前 Windows 应用主题；读不到时按浅色处理，不影响启动。"""
    return _system_theme()["dark"]


def icon_path(dark=None):
    """启动时的窗口图标。

    浅色主题用深色线条，深色主题用浅色线条；两者都缺失时退回 exe
    里那张深底白线的静态图标。运行期的主题切换由 win_icon 负责。
    """
    if dark is None:
        dark = _system_dark()
    name = "icon_dark.ico" if dark else "icon_light.ico"
    path = os.path.join(ASSETS, name)
    if os.path.isfile(path):
        return path
    path = os.path.join(ASSETS, "icon.ico")
    return path if os.path.isfile(path) else None


def _load_geometry(module, folder):
    """读回上次的窗口矩形。模块缺失/文件损坏都退回空字典（交给系统居中）。"""
    saved = {}
    if module is not None:
        try:
            saved = module.load(folder) or {}
            _trace("窗口几何: %r" % (saved,))
        except Exception as exc:
            _trace("读取窗口几何失败: %r" % (exc,))
    return saved


def _track_geometry(module, window, folder, saved):
    """把窗口几何事件接到跟踪器上；模块缺失时返回 None。异常交给调用方兜。"""
    if module is None:
        return None
    tracker = module.Tracker(folder, saved)
    tracker.bind(window)
    return tracker


def _native_handle(native):
    """把 .NET IntPtr / WinForms Handle 转成整数 HWND。"""
    hwnd = None
    try:
        handle = native.Handle
        if hasattr(handle, "ToInt64"):
            handle = handle.ToInt64()
        hwnd = int(handle) or None
    except (AttributeError, TypeError, ValueError, OverflowError):
        pass
    return hwnd


def _window_handle(window):
    """从 pywebview 窗口对象取 HWND；拿不到时按进程找主窗口。"""
    hwnd = _native_handle(getattr(window, "native", None))
    if hwnd:
        return hwnd
    try:
        import win_guard
        return win_guard.find_own_window()
    except Exception:
        return None

# 可在控制台里执行的命令。
#
# 只列非 silent 的：signin.py 的 emit() 对 silent* 前缀走「写日志文件」分支，
# stdout 是空的——把 silent-poll 接到控制台上会一条输出都收不到。
#
# write=True 的会真实改动服务端状态（领积分、抽奖、消耗补登卡），
# 前端对这类按钮强制二次确认。
ACTIONS = {
    "status":  dict(label="刷新监控", write=False, timeout=120,
                    desc="只读查询签到状态与本月记录，不改动任何状态"),
    "claim":   dict(label="签到", write=True, timeout=120,
                    desc="领取今日签到积分；接口幂等，已签时重复点击不会重复发积分；"
                         "完成后自动补一次状态查询，刷新卡片与本月日历"),
    "growth":  dict(label="成长中心", write=True, timeout=660,
                    desc="不碰签到接口，只跑成长中心：领旅行礼物与派 Buddy、"
                         "接任务并领任务奖、补登卡补当月断签（每轮最多 1 张）、"
                         "连登奖励兑换、开盲盒、能量开 Buddy"),
    # auto / all 仍保留给命令行调用，但操作栏只放常用的三个动作。
    "auto":    dict(label="签到 + 成长中心", write=True, timeout=660,
                    desc="先查签到状态，未签才领取今日积分，再跑完成长中心："
                         "Buddy 旅行（到达收礼、空闲派发）、接任务与领任务奖、"
                         "补登卡补当月断签、连登 7/14/28 天奖励兑换、开盲盒、能量开 Buddy",
                    hidden=True),
    "all":     dict(label="查询 + 领取", write=True, timeout=180,
                    desc="调试用：先查一次签到状态，再无条件调一次领取接口，"
                         "两步的原始返回都打进日志", hidden=True),
    # doctor 只保留后端能力，启动时自动跑一次填充「凭据格式」卡片，
    # 不在界面上暴露按钮——它对使用者没有可操作性。
    "doctor":  dict(label="离线体检", write=False, timeout=120,
                    desc="本地诊断凭据格式与运行时能力", hidden=True),
}

READONLY_ACTIONS = tuple(k for k, v in ACTIONS.items() if not v["write"])

# signin.py 用模块级全局变量记录时间预算（_started_at / _budget_seconds），
# 并发进入会互相干扰。所有调用路径共用这一把锁。
_SIGNIN_LOCK = threading.RLock()

# 只加载一次的 signin 模块，见 _load_signin()
_SIGNIN_MODULE = None
_SIGNIN_LOAD_LOCK = threading.Lock()


def _load_signin():
    """按文件路径加载 exe 旁边的 signin.py，且只加载一次。

    不能用普通 import，两个坑都真踩过：

      1) 并发半成品：tasks() 和 run() 同时 import 时，后到的线程会从
         sys.modules 直接拿到尚未执行完的模块对象，报
         "module 'signin' has no attribute '_run'" —— 而真实原因
         可能是它内部某个依赖缺失，错误信息指错了方向。
      2) 同名顶替：PyInstaller 的 FrozenImporter 在 sys.meta_path 里
         排在路径查找器之前，PYZ 里若有同名模块，sys.path.insert 也
         救不回来。

    显式按路径加载可以同时避开这两点。
    """
    global _SIGNIN_MODULE
    if _SIGNIN_MODULE is not None:
        return _SIGNIN_MODULE

    with _SIGNIN_LOAD_LOCK:
        if _SIGNIN_MODULE is not None:
            return _SIGNIN_MODULE

        import importlib.util

        alias = "_workbuddy_signin"
        spec = importlib.util.spec_from_file_location(alias, SIGNIN)
        if spec is None or spec.loader is None:
            raise ImportError("无法读取 %s" % SIGNIN)

        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            # 失败必须把半成品摘掉，否则下次会拿到不完整的模块
            sys.modules.pop(alias, None)
            raise

        _SIGNIN_MODULE = module
        return module


def _reset_signin_budget(signin):
    """复用 signin 的认证链路前，先重置它的进程级预算时钟。

    signin.py 把 _started_at / _budget_seconds 记在模块全局，而本程序是长驻
    进程：上一次刷新留下的时钟超过预算后（默认 420 秒），_budget_left() 就是
    负数，_run_auth_helper() 会拒绝启动解密子进程并直接抛 HELPER_TIMEOUT。
    这个状态还会粘住——重置预算的调用原本写在读凭据之后，永远走不到，直到
    用户跑一条命令或重启程序才恢复。必须在 resolve_session() 之前调用。
    """
    signin._start_budget()


# Qoder 同样按路径加载，避免与 exe 内可能存在的同名模块冲突。
_QODER_MODULE = None
_QODER_LOAD_LOCK = threading.Lock()
_QODER_LOCK = threading.RLock()


def _load_qoder():
    """按路径加载 qoder_signin.py，且只加载一次。"""
    global _QODER_MODULE
    if _QODER_MODULE is not None:
        return _QODER_MODULE

    with _QODER_LOAD_LOCK:
        if _QODER_MODULE is not None:
            return _QODER_MODULE

        import importlib.util

        alias = "_workbuddy_qoder"
        spec = importlib.util.spec_from_file_location(alias, QODER)
        if spec is None or spec.loader is None:
            raise ImportError("无法读取 %s" % QODER)

        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(alias, None)
            raise

        _QODER_MODULE = module
        return module


# Trae 同样按路径加载；它只依赖标准库，不需要在 spec 里声明 hiddenimports。
_TRAE_MODULE = None
_TRAE_LOAD_LOCK = threading.Lock()
_TRAE_LOCK = threading.RLock()


def _load_trae():
    """按路径加载 trae_signin.py，且只加载一次。"""
    global _TRAE_MODULE
    if _TRAE_MODULE is not None:
        return _TRAE_MODULE

    with _TRAE_LOAD_LOCK:
        if _TRAE_MODULE is not None:
            return _TRAE_MODULE

        import importlib.util

        alias = "_workbuddy_trae"
        spec = importlib.util.spec_from_file_location(alias, TRAE)
        if spec is None or spec.loader is None:
            raise ImportError("无法读取 %s" % TRAE)

        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(alias, None)
            raise

        _TRAE_MODULE = module
        return module


def _invoke_qoder(action, dry_run=False, once=False):
    """在进程内运行 qoder_signin.py，并通过临时替换 emit() 捕获结果。

    不用重定向 sys.stdout：控制台允许 WorkBuddy 与 Qoder 同时执行，全局
    替换 stdout 会让两个线程互相截走输出。qoder_signin 只会通过 emit()
    输出，临时换掉这个函数既能拿到 JSON，也不会影响其他线程。

    ALREADY / INACTIVE 原本会走 quiet 分支不打印，这里也一并捕获，页面
    才能显示“今日已领”而不是空白。
    """
    qoder = _load_qoder()
    # 每次调用都重置模块的时间预算：_started_at 是模块加载时记下的，
    # 控制台长期运行后再领取会误判“预算耗尽”。
    qoder._started_at = time.monotonic()

    captured = []
    original_emit = qoder.emit

    def capture(out, action, quiet=False):
        text = json.dumps(out, ensure_ascii=False, default=str)
        redact = getattr(qoder, "_redact", None)
        captured.append(redact(text) if redact else text)

    qoder.emit = capture
    try:
        code = qoder.run(action, dry_run=dry_run, once=once)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        code = 1
        captured.append("异常：%s: %s" % (type(exc).__name__, exc))
    finally:
        qoder.emit = original_emit
    return code, "\n".join(captured)


# 运行期数据目录：与 Trae 的 %APPDATA%\Auto-Claim\trae 对称，
# 控制台自己的日志、缓存和窗口状态统一放在 LocalAppData 下。
DATA_DIR_NAME = "Auto-Claim"
DATA_DIR_SUBDIR = "dashboard"
LEGACY_DATA_DIR_NAME = "WorkBuddyDashboard"

_diag_lock = threading.Lock()


def _local_appdata_dir():
    return os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")


def _move_or_copy_dir(legacy, folder):
    """优先原子移动旧目录；失败时复制到暂存目录再改名。

    返回 ``"move"`` / ``"copy"``，都失败时返回 ``None``。复制走
    ``.migrating.<pid>`` 暂存目录，避免被杀毒软件或旧进程打断后留下半套数据。
    """
    try:
        os.replace(legacy, folder)
        return "move"
    except OSError:
        pass

    staging = "%s.migrating.%d" % (folder, os.getpid())
    copied = True
    try:
        shutil.copytree(legacy, staging)
        os.replace(staging, folder)
    except Exception:
        copied = False
    # 成功时 staging 已被改名，失败时清掉半成品；两种情况都安全。
    shutil.rmtree(staging, ignore_errors=True)
    return "copy" if copied else None


def _migrate_legacy_dir(legacy, folder):
    """把旧版 %LOCALAPPDATA%\\WorkBuddyDashboard 搬到新目录。

    旧进程还开着 watchdog.log 时 rename 会失败，这时退回复制。
    返回是否迁移成功。
    """
    try:
        os.makedirs(os.path.dirname(folder), exist_ok=True)
    except OSError:
        return False

    outcome = _move_or_copy_dir(legacy, folder)
    if outcome == "copy":
        # 复制成功后旧目录只是副本；删不掉也不影响使用。
        shutil.rmtree(legacy, ignore_errors=True)
    return outcome is not None


def _diag_dir():
    """诊断、缓存与窗口状态的落盘目录。

    不能用 ASSETS：打包后它是 %TEMP% 下的解包目录，进程一退整个目录
    就被删掉，写进去的崩溃日志会跟着一起消失——于是 exe 里的异常
    彻底查无实据。必须落在用户目录这种真正持久的地方。

    旧版目录（WorkBuddyDashboard）在首次启动时自动迁移；迁移失败就继续
    用旧目录，下一次启动再试，避免日志和窗口位置在新旧两份里分叉。
    """
    base = _local_appdata_dir()
    folder = os.path.join(base, DATA_DIR_NAME, DATA_DIR_SUBDIR)
    legacy = os.path.join(base, LEGACY_DATA_DIR_NAME)

    with _diag_lock:
        ready = True
        if not os.path.isdir(folder) and os.path.isdir(legacy):
            ready = _migrate_legacy_dir(legacy, folder)
        if ready:
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError:
                ready = False
    if ready and os.path.isdir(folder):
        return folder
    return legacy if os.path.isdir(legacy) else HERE


def _log_path():
    return os.path.join(_diag_dir(), "run.log")


def _crash_path():
    return os.path.join(_diag_dir(), "crash.log")


def _trace(msg):
    """按需落盘的运行期追踪。

    只在 WORKBUDDY_DEBUG=1 时生效，正常使用不产生任何文件。
    存在的理由：窗口能打开、进程也正常，但界面就是不干活——这种情况下
    唯一的线索是「页面到底加载了没有、JS 到底跑了没有」，只能主动去问。
    """
    if os.environ.get("WORKBUDDY_DEBUG") != "1":
        return
    try:
        with open(os.path.join(_diag_dir(), "trace.log"),
                  "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _probe(window):
    """窗口起来后，把页面的真实状态写进 trace.log。

    document.readyState / 标题 / body 长度 / bridge 是否就绪，
    这几项足以区分「页面没加载」「页面加载了但 JS 报错」「bridge 没通」。
    """
    if os.environ.get("WORKBUDDY_DEBUG") != "1":
        return
    js = ("JSON.stringify({url:location.href,ready:document.readyState,"
          "title:document.title,"
          "badge:(document.getElementById('badge')||{}).textContent||'',"
          "env:(document.getElementById('env')||{}).textContent||'',"
          "envSignin:(document.getElementById('envSignin')||{}).textContent||'',"
          "credit:(document.getElementById('vCredit')||{}).textContent||'',"
          "streak:(document.getElementById('vStreak')||{}).textContent||'',"
          "total:(document.getElementById('vTotal')||{}).textContent||'',"
          "credType:(document.getElementById('vCred')||{}).textContent||'',"
          "todo:(document.getElementById('cntTodo')||{}).textContent||'',"
          "done:(document.getElementById('cntDone')||{}).textContent||'',"
          "growth:(document.getElementById('growthUpdated')||{}).textContent||'',"
          "growthCards:(document.getElementById('growthOverview')||{}).childElementCount||0,"
          "svc:(document.getElementById('svcState')||{}).textContent||'',"
          "svcInstalled:(document.getElementById('svcSummary')||{}).textContent||'',"
          "tasks:(document.getElementById('tasklist')||{}).childElementCount||0,"
          "calSigned:document.querySelectorAll('#cal .on').length,"
          "logLines:(document.getElementById('log')||{}).childElementCount||0,"
          "jsErrors:window.__errs||[]})")
    for i in range(3):
        time.sleep(5)
        try:
            raw = window.evaluate_js(js)
            _trace("probe#%d %s" % (i + 1, raw))
            if raw and '"ready":"complete"' in raw and '"api":true' in raw:
                break
        except Exception as exc:
            _trace("probe#%d 失败: %r" % (i + 1, exc))


def _store_path():
    """上次查询结果的持久化位置。目录见 _diag_dir()。"""
    return os.path.join(_diag_dir(), "last_result.json")


def _load_store():
    try:
        with open(_store_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_store(data):
    try:
        with open(_store_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _read_local_log(limit=200):
    """读取最近一次历史日志，供界面初始化时回填。"""
    path = _log_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-limit:]
    except OSError:
        return []


def _append_log(line):
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _qoder_log_snapshot(limit=12):
    """读取 qoder_signin.log 末尾几行，供 Qoder 监控页展示。"""
    path = _qoder_log_path()
    try:
        stat = os.stat(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = [line.rstrip("\r\n") for line in fh]
    except OSError:
        return {"path": path, "at": None, "lines": []}
    return {
        "path": path,
        "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        "lines": [line for line in lines if line.strip()][-limit:],
    }


def _trae_log_snapshot(limit=40):
    """读取 trae_signin.log 末尾几行，供 Trae 监控页展示。"""
    path = _trae_log_path()
    try:
        stat = os.stat(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = [line.rstrip("\r\n") for line in fh]
    except OSError:
        return {"path": path, "at": None, "lines": []}
    return {
        "path": path,
        "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        "lines": [line for line in lines if line.strip()][-limit:],
    }


def _append_trae_log(payload, action="console"):
    """把控制台触发的结果写进 trae_signin.log。

    trae_signin.append_log() 会用模块自己收集的敏感字段做替换；控制台里
    只传公开结果（不含 Token / Session），不存在额外泄漏面。
    """
    try:
        trae = _load_trae()
        trae.append_log(payload, action)
    except Exception:
        pass


def _append_qoder_log(payload):
    """把手动领取结果写进 Qoder 日志，刷新页面后仍能看到。

    qoder_signin.py 在非静默模式下只把 JSON 打到 stdout，不写日志；
    计划任务（silent）才会写。控制台不重定向 stdout，因此这里补一行。
    """
    if not isinstance(payload, dict):
        return
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
        with open(_qoder_log_path(), "a", encoding="utf-8") as fh:
            fh.write(line)
    except (OSError, TypeError, ValueError):
        pass


# 成长中心状态文案：后端写“数据变化”日志时使用。
_TASK_STATUS_TEXT = {
    "not_accepted": "待接单",
    "accepted": "进行中",
    "in_progress": "进行中",
    "completed": "可领奖",
    "claimed": "已领取",
    "unknown": "未知",
}
_TRAVEL_STATE_TEXT = {
    "idle": "待命",
    "traveling": "旅行中",
    "arrived": "已到达",
    "unknown": "未知",
}
_REDEEM_STATUS_TEXT = {
    "available": "可兑换",
    "claimed": "已兑换",
    "locked": "未解锁",
    "unknown": "未知",
}


class _GrowthAuthError(Exception):
    """成长中心只读接口明确返回 401/403。"""


def _growth_store_path():
    """成长中心上次只读快照，用于比较数据变化。"""
    return os.path.join(_diag_dir(), "growth.json")


def _load_growth_store():
    try:
        with open(_growth_store_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_growth_store(data):
    try:
        with open(_growth_store_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _task_index(tasks):
    result = {}
    for task in tasks or []:
        if isinstance(task, dict) and task.get("code"):
            result[str(task["code"])] = task
    return result


def _tier_index(tiers):
    result = {}
    for tier in tiers or []:
        if isinstance(tier, dict) and tier.get("tier"):
            result[tier["tier"]] = tier
    return result


def _fmt_num(value):
    return "—" if value is None else str(value)


def _fmt_progress(task):
    current = task.get("current")
    target = task.get("target")
    if current is None or target is None:
        return ""
    return "%s / %s" % (current, target)


def _growth_changes(prev_data, new_data):
    """把两次成长中心快照的差异转成人话，供运行日志逐条记录。

    只比较会影响用户判断的字段：任务状态/进度、Buddy 旅行、补登卡与可补
    日期、连登兑换、抽奖机会、能量、Buddy 盲盒是否可开。首次没有基线时
    返回空列表，避免把整份任务清单误报成“新增”。
    """
    if not isinstance(prev_data, dict) or not prev_data:
        return []
    if not isinstance(new_data, dict) or not new_data:
        return []

    changes = []

    old_tasks = _task_index(prev_data.get("tasks"))
    new_tasks = _task_index(new_data.get("tasks"))
    for code in sorted(set(old_tasks) | set(new_tasks)):
        old_task = old_tasks.get(code)
        new_task = new_tasks.get(code)
        title = (new_task or old_task or {}).get("title") or code
        if old_task is None:
            status = _TASK_STATUS_TEXT.get((new_task or {}).get("status"), "未知")
            changes.append("新增任务「%s」（%s）" % (title, status))
            continue
        if new_task is None:
            changes.append("任务「%s」已从列表移除" % title)
            continue

        bits = []
        old_status = _TASK_STATUS_TEXT.get(old_task.get("status"), "未知")
        new_status = _TASK_STATUS_TEXT.get(new_task.get("status"), "未知")
        if old_status != new_status:
            bits.append("%s → %s" % (old_status, new_status))
        old_progress = _fmt_progress(old_task)
        new_progress = _fmt_progress(new_task)
        if old_progress and new_progress and old_progress != new_progress:
            bits.append("进度 %s → %s" % (old_progress, new_progress))
        if bits:
            changes.append("任务「%s」：%s" % (title, "；".join(bits)))

    old_travel = prev_data.get("travel") or {}
    new_travel = new_data.get("travel") or {}
    old_state = old_travel.get("state")
    new_state = new_travel.get("state")
    if old_state != new_state:
        tail = ""
        if new_state == "traveling" and new_travel.get("location"):
            tail = "（%s）" % new_travel["location"]
        changes.append("Buddy 旅行：%s → %s%s" % (
            _TRAVEL_STATE_TEXT.get(old_state, old_state or "未知"),
            _TRAVEL_STATE_TEXT.get(new_state, new_state or "未知"), tail))
    elif new_state == "traveling" and old_travel.get("location") != new_travel.get("location"):
        changes.append("Buddy 旅行地点：%s → %s" % (
            old_travel.get("location") or "—", new_travel.get("location") or "—"))
    if old_travel.get("daily_limit_reached") != new_travel.get("daily_limit_reached"):
        changes.append("今日旅行名额：%s → %s" % (
            "已用完" if old_travel.get("daily_limit_reached") else "可用",
            "已用完" if new_travel.get("daily_limit_reached") else "可用"))

    old_streak = prev_data.get("streak") or {}
    new_streak = new_data.get("streak") or {}
    old_days = old_streak.get("days")
    new_days = new_streak.get("days")
    if old_days != new_days:
        changes.append("连签天数：%s → %s 天" % (_fmt_num(old_days), _fmt_num(new_days)))
    old_cards = (old_streak.get("makeup_cards") or {}).get("balance")
    new_cards = (new_streak.get("makeup_cards") or {}).get("balance")
    if old_cards != new_cards:
        changes.append("补登卡：%s → %s 张" % (_fmt_num(old_cards), _fmt_num(new_cards)))
    old_dates = old_streak.get("makeup_candidates")
    new_dates = new_streak.get("makeup_candidates")
    if isinstance(old_dates, list) and isinstance(new_dates, list) and old_dates != new_dates:
        extra = "（最近 %s）" % new_dates[0] if new_dates else ""
        changes.append("可补断签：%s → %s 天%s" % (len(old_dates), len(new_dates), extra))

    old_redeem = _tier_index((prev_data.get("redeem") or {}).get("tiers"))
    new_redeem = _tier_index((new_data.get("redeem") or {}).get("tiers"))
    for tier in ("7d", "14d", "28d"):
        old_tier = old_redeem.get(tier) or {}
        new_tier = new_redeem.get(tier) or {}
        old_text = _REDEEM_STATUS_TEXT.get(old_tier.get("status"),
                                          old_tier.get("status") or "未知")
        new_text = _REDEEM_STATUS_TEXT.get(new_tier.get("status"),
                                           new_tier.get("status") or "未知")
        if old_text != new_text:
            label = new_tier.get("label") or old_tier.get("label") or tier
            changes.append("连登兑换 %s：%s → %s" % (label, old_text, new_text))

    old_lottery = (prev_data.get("lottery") or {}).get("balance")
    new_lottery = (new_data.get("lottery") or {}).get("balance")
    if old_lottery != new_lottery:
        changes.append("抽奖机会：%s → %s 次" % (_fmt_num(old_lottery), _fmt_num(new_lottery)))

    old_energy = (prev_data.get("energy") or {}).get("balance")
    new_energy = (new_data.get("energy") or {}).get("balance")
    if old_energy != new_energy:
        changes.append("能量：%s → %s" % (_fmt_num(old_energy), _fmt_num(new_energy)))

    old_buddy = prev_data.get("buddy") or {}
    new_buddy = new_data.get("buddy") or {}
    old_affordable = old_buddy.get("affordable")
    new_affordable = new_buddy.get("affordable")
    if old_affordable != new_affordable:
        changes.append("Buddy 盲盒可开：%s → %s 次" % (
            _fmt_num(old_affordable), _fmt_num(new_affordable)))
    old_cost = old_buddy.get("cost_per_open")
    new_cost = new_buddy.get("cost_per_open")
    if old_cost != new_cost:
        changes.append("Buddy 盲盒单价：%s → %s 能量" % (_fmt_num(old_cost), _fmt_num(new_cost)))

    return changes


def _growth_snapshot(signin, endpoint, headers):
    """读取成长中心全部只读数据，返回 (data, errors)。

    单个接口失败不影响其他区块；401/403 直接抛 _GrowthAuthError，由调用方
    统一转成认证错误。这里只有 GET，不会改动任何服务端状态。
    """
    base = endpoint + "/v2/activity/growth"
    errors = []

    def fetch(path):
        code, body = signin.get(base + path, headers)
        if code in (401, 403):
            raise _GrowthAuthError("HTTP %s" % code)
        if not signin._api_success(code, body):
            message = ""
            if isinstance(body, dict):
                message = text(body.get("msg") or body.get("message") or "")
            errors.append("%s（HTTP %s%s）" % (
                path, code, ("：" + message[:80]) if message else ""))
            return None
        return body

    def mapping(value):
        return value if isinstance(value, dict) else {}

    def text(value):
        return value if isinstance(value, str) else ("" if value is None else str(value))

    def optional_num(value):
        return None if value is None else signin.as_int(value)

    tasks_body = mapping(fetch("/tasks"))
    raw_tasks = signin.dig(tasks_body, "tasks") or []
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    tasks = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        progress = mapping(item.get("progress"))
        task = {
            "code": text(item.get("task_code")),
            "title": text(item.get("title")) or text(item.get("task_code")) or "未命名任务",
            "desc": text(item.get("task_desc")) or text(item.get("description")),
            "status": text(item.get("accept_status")) or "unknown",
            "locked": bool(item.get("locked")),
            "credit": optional_num(item.get("reward_credit")) or 0,
            "energy": optional_num(item.get("reward_energy")) or 0,
            "buddy": bool(item.get("reward_buddy")),
            "current": optional_num(progress.get("current")),
            "target": optional_num(progress.get("target")),
            "badge": text(item.get("badge_name")),
            "new": bool(item.get("is_new")),
        }
        tasks.append(task)

    travel_body = mapping(fetch("/buddy/travel/status"))
    location = signin.dig(travel_body, "location")
    travel = {
        "state": text(signin.dig(travel_body, "state")) or "unknown",
        "location": text(location.get("name")) if isinstance(location, dict) else None,
        "arrive_at": optional_num(signin.dig(travel_body, "arrive_at")),
        "server_now": optional_num(signin.dig(travel_body, "server_now")),
        "daily_limit_reached": bool(signin.dig(travel_body, "daily_limit_reached")),
        "duration_hours": optional_num(signin.dig(travel_body, "duration_hours")),
        "reward_credit": optional_num(signin.dig(travel_body, "reward_credit")) or 0,
    }

    streak_body = mapping(fetch("/streak"))
    streak_obj = signin.dig(streak_body, "streak")
    if not isinstance(streak_obj, dict):
        streak_obj = {}
    cards_obj = signin.dig(streak_body, "makeup_cards")
    card_max = 4
    if isinstance(cards_obj, dict):
        card_balance = optional_num(cards_obj.get("balance"))
        card_max = optional_num(cards_obj.get("max")) or card_max
    elif cards_obj is not None:
        card_balance = optional_num(cards_obj)
    else:
        card_balance = None
    streak = {
        "days": optional_num(streak_obj.get("days")),
        "next_tier": text(streak_obj.get("next_tier")),
        "next_tier_remaining": optional_num(streak_obj.get("next_tier_remaining")),
        "month_total_days": optional_num(streak_obj.get("month_total_days")),
        "makeup_cards": {"balance": card_balance, "max": card_max},
        "makeup_candidates": [],
        "makeup_checked": False,
        "makeup_error": None,
    }
    if card_balance and card_balance > 0:
        heatmap_body = mapping(fetch("/heatmap"))
        if heatmap_body:
            try:
                streak["makeup_candidates"] = signin._makeup_candidates(streak_body, heatmap_body)
            except Exception as exc:
                streak["makeup_error"] = "%s: %s" % (type(exc).__name__, exc)
                errors.append("补登日历：%s" % streak["makeup_error"])
            else:
                streak["makeup_checked"] = True
        else:
            streak["makeup_error"] = "活跃日历读取失败"

    redeem_body = mapping(fetch("/redeem/summary"))
    redemption = signin.dig(streak_body, "redemption_status")
    if not isinstance(redemption, dict):
        redemption = {}
    tier_rewards = {}
    for entry in redemption.get("tiers") or []:
        if isinstance(entry, dict) and entry.get("tier"):
            tier_rewards[entry["tier"]] = entry
    tiers = []
    for tier, status_key, label, days in signin._REDEEM_TIERS:
        entry = tier_rewards.get(tier) or {}
        status = text(signin.dig(redeem_body, status_key + "_status"))
        if not status:
            status = text(signin.dig(redemption, "tier_%s_status" % tier))
        tier_item = {
            "tier": tier,
            "label": "%s 天" % days,
            "status": status or "unknown",
            "credit": optional_num(entry.get("credit")) or 0,
            "energy": optional_num(entry.get("energy")) or 0,
            "cards": optional_num(entry.get("cards")) or 0,
            "chances": optional_num(entry.get("chances")) or 0,
            "count": optional_num(signin.dig(redeem_body, status_key + "_count")) or 0,
        }
        tiers.append(tier_item)

    lottery_body = mapping(fetch("/lottery/chances"))
    lottery = {"balance": optional_num(signin.dig(lottery_body, "balance"))}

    energy_body = mapping(fetch("/energy"))
    energy = {
        "balance": optional_num(signin.dig(energy_body, "balance")),
        "total_earned": optional_num(signin.dig(energy_body, "total_earned")),
        "total_consumed": optional_num(signin.dig(energy_body, "total_consumed")),
    }

    quota_body = mapping(fetch("/buddy/quota"))
    buddy = {
        "energy": optional_num(signin.dig(quota_body, "balance")),
        "affordable": optional_num(signin.dig(quota_body, "affordable")),
        "cost_per_open": optional_num(signin.dig(quota_body, "cost_per_open")),
        "max_open_count": optional_num(signin.dig(quota_body, "max_open_count")),
    }

    data = {
        "tasks": tasks,
        "travel": travel,
        "streak": streak,
        "redeem": {
            "tiers": tiers,
            "remaining_days": optional_num(signin.dig(redeem_body, "remaining_days")),
        },
        "lottery": lottery,
        "energy": energy,
        "buddy": buddy,
        "errors": errors,
    }
    return data


class Api:
    """暴露给 JS 的接口集合。"""

    def __init__(self, initial_theme=None):
        self._window = None
        self._watchdog = None
        self._lock = threading.Lock()
        self._busy = False
        # 计划任务失败提示的恢复流程：同一时刻只允许一个在跑，
        # 否则两次「重新导入并重试」会同时改同一份记录。
        self._alert_lock = threading.Lock()
        self._alert_retry = None
        # Qoder 领取与 WorkBuddy 命令互不阻塞；各自维护忙碌状态。
        self._qoder_busy = False
        self._qoder_state_lock = threading.Lock()
        # Trae 的刷新/签到/用量会串行访问同一个 SQLite 与账号文件，
        # 用一把独立状态锁挡住重复点击；最后一次 monitor 结果用于忙碌时回显。
        self._trae_busy = False
        self._trae_state_lock = threading.Lock()
        self._trae_last = None
        # 创建窗口之前探测到的主题，随 bootstrap 首批数据交给前端。
        self._initial_theme = dict(initial_theme or _system_theme())

    # ---------- 生命周期 ----------

    def heartbeat(self):
        """前端每 5 秒调用一次，作为整条 UI 链路的存活信号。

        这个调用要依次经过 WebMessageReceived（UI 线程）和 evaluate_js
        （UI 线程）才能把 Promise 还给 JS；UI 线程一旦卡住，下一次心跳
        就不会到达，看门狗据此判定界面已死。返回体保持极简，避免心跳
        本身成为负担。
        """
        watchdog = self._watchdog
        if watchdog is not None:
            watchdog.beat()
        return {"ok": True}

    def bootstrap(self):
        """界面加载完成后调用：环境信息 + 上次查询的缓存结果。

        cached 让界面在网络查询返回前就能先把卡片填上，
        避免每次启动都是一片「—」。
        """
        _trace("bootstrap 被调用")
        return {
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "platform": sys.platform,
            "dark": bool(self._initial_theme.get("dark")),
            "accent": list(self._initial_theme.get("accent") or (0, 120, 212)),
            "signin": SIGNIN,
            "signin_exists": os.path.exists(SIGNIN),
            "signin_searched": list(SEARCHED),
            "qoder": QODER,
            "qoder_exists": os.path.exists(QODER),
            "qoder_searched": list(QODER_SEARCHED),
            "trae": TRAE,
            "trae_exists": os.path.exists(TRAE),
            "trae_searched": list(TRAE_SEARCHED),
            # 本机桌面端安装 / 运行状态：侧边栏状态点用它区分
            # 「未安装」「已安装未登录」「已连接」，而不是只看登录态
            "desktop": {kind: _desktop_state(kind)
                        for kind in ("workbuddy", "qoder", "trae")},
            "readonly": list(READONLY_ACTIONS),
            "actions": [
                {"key": k, "label": v["label"], "write": v["write"],
                 "desc": v["desc"], "hidden": v.get("hidden", False)}
                for k, v in ACTIONS.items() if not v.get("hidden")
            ],
            "history": _read_local_log(),
            "cached": _load_store(),
            "growth": _load_growth_store(),
            "alerts": _read_service_alerts(),
        }

    def attach(self, window_id):
        """pywebview 的 on_loaded 回调里保存窗口句柄。"""
        self._window = window_id

    @staticmethod
    def _invoke(action):
        """在当前进程里跑 signin.py 的 _run()，捕获它打印的 JSON。

        signin.py 的输出约定是「向 stdout 打一行 JSON」，emit() 内部就是
        print(payload)。这里把 stdout 临时换成 StringIO 即可拿到，
        不必改上游一行代码。
        """
        try:
            signin = _load_signin()
        except Exception as exc:
            _trace("_invoke: 加载 signin.py 失败: %r" % (exc,))
            return 1, "", "无法加载 signin.py（%s）：%s" % (SIGNIN, exc)

        # import 成功不等于模块可用：冻结后 sys.path 的顺序和源码运行时
        # 不同，可能命中别处的同名模块。出错时把来源一起带上，
        # 否则只报 "no attribute '_run'" 会完全无从判断。
        where = getattr(signin, "__file__", "<无 __file__>")
        _trace("_invoke: signin=%r has_run=%s sys.path[:4]=%r"
               % (where, hasattr(signin, "_run"), sys.path[:4]))
        if not hasattr(signin, "_run"):
            return 1, "", ("signin 模块缺少 _run()，来源：%s；"
                           "sys.path 前 3 项：%s" % (where, sys.path[:3]))

        buf = io.StringIO()
        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = buf
        try:
            with _SIGNIN_LOCK:
                code = signin._run(action)
        except SystemExit as exc:                 # _run 不该 exit，兜一下
            code = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:
            code, _ = 1, buf.write("\n异常：%s: %s" % (type(exc).__name__, exc))
        finally:
            sys.stdout, sys.stderr = stdout, stderr
        return code, buf.getvalue(), ""

    # ---------- 命令执行 ----------

    def run(self, action):
        """执行一个 signin.py 命令。立即返回，结果通过事件异步推给前端。"""
        spec = ACTIONS.get(action)
        if spec is None:
            return {"ok": False, "error": "未知命令：%s" % action}
        _trace("run 被调用: %s" % action)
        with self._lock:
            if self._busy:
                return {"ok": False, "error": "有命令正在执行"}
            self._busy = True

        threading.Thread(target=self._safe_worker, args=(action, spec),
                         daemon=True).start()
        return {"ok": True, "action": action}

    def _safe_worker(self, action, spec):
        """_worker 的包装：任何异常都要推给前端。

        之前是裸起线程，线程里的 TypeError 只进 stderr，而打包后
        没有控制台——异常彻底消失，界面永远停在"执行中…"，
        _busy 也永远是 True，后续所有命令都被挡掉。
        """
        try:
            self._worker(action, spec)
        except Exception as exc:
            import traceback
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self._push("log", "内部错误：%s" % detail)
            try:
                _append_log("内部错误: %s" % detail)
            except Exception:
                pass
            with self._lock:
                self._busy = False
            self._push("done", {"action": action, "exit": 2, "payload": None})

    def _worker(self, action, spec):
        stamp = time.strftime("%H:%M:%S")
        self._push("log", "[%s] %s" % (stamp, spec["desc"]))
        if spec["write"]:
            self._push("log", "  写操作，正在执行……")

        # 在本进程内调用 signin.py，而不是 subprocess 起子进程。
        #
        # 原因有二：
        #   1) 打包后 sys.executable 就是 exe 自己，用它跑 signin.py 等于
        #      又启动一份完整程序——而新实例启动后又会自动跑 status，
        #      于是形成 fork bomb（实测每 3 秒生成一个进程，十几秒堆到 10+）。
        #   2) 进程内调用省掉每次启动解释器的开销，status 从秒级降到毫秒级。
        code, stdout, stderr = self._invoke(action)

        parsed = self._parse_payload(stdout)

        # 只推摘要，不推原始 JSON：那坨东西含整月签到日期数组，直接刷屏没法看
        if parsed is not None:
            for line in self._summarize(parsed):
                self._push("log", line)
                _append_log(line)
        else:
            for line in stdout.splitlines():
                self._push("log", line)
                _append_log(line)
        if stderr:
            for line in stderr.splitlines():
                self._push("log", "[stderr] " + line)
                # stderr 也要落盘：_invoke 的失败信息只走这里，
                # 不写文件的话退出后完全查不到原因
                _append_log("[stderr] " + line)
        self._push("log", "[%s] exit=%s" % (time.strftime("%H:%M:%S"), code))

        # 持久化：下次启动先渲染缓存，再刷新，避免开场就是一片「—」
        if parsed is not None and code == 0:
            store = _load_store()
            store[action] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "exit": code, "payload": parsed}
            _save_store(store)

        with self._lock:
            self._busy = False
        self._push("done", {"action": action, "exit": code, "payload": parsed})

    # ---------- 事件推送 ----------

    @staticmethod
    def _parse_payload(stdout):
        """从 stdout 里挑出那行 JSON 结果。"""
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _summarize(payload):
        """把套娃响应拍平成一行一个字段的摘要。

        status 的结构是 status -> body -> data 三层，且 checkin_dates 里塞了
        整月日期数组，直接 dumps 会刷屏，所以只挑有信息量的字段。
        """
        data = payload.get("body", payload)
        while isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]

        lines = []
        for key in ("result", "report", "growth", "growth_result",
                    "credits_gained", "energy", "failures", "idle",
                    "credential_format", "runtime",
                    "electron_version", "active", "today_checked_in",
                    "today_credit", "daily_credit", "credit", "streak_days",
                    "total_credits", "is_streak_day", "next_streak_day",
                    "week_checkin_days", "season", "activity_name",
                    "theme_name", "start_time", "end_time", "http",
                    "needs_attention"):
            if key not in data:
                continue
            value = data[key]
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                shown = "是" if value else "否"
            elif isinstance(value, (list, dict)):
                shown = "（%d 项）" % len(value)
            else:
                shown = str(value)
            # report / growth 是长句，单独处理
            if key in ("report", "growth"):
                lines.append("  %s" % shown)
            else:
                lines.append("  %-18s %s" % (key, shown))
        return lines

    # ---------- 主题 ----------

    def tasks(self):
        """拉取云端任务列表。

        直接复用 signin.py 自己的认证链路（凭据解密 / 请求头 / 重试），
        不另写一份，也就不用改动上游脚本。
        """
        try:
            signin = _load_signin()
        except Exception as exc:
            return {"ok": False, "error": "无法加载 signin.py：%s" % exc}

        # 宁可快速失败也不要阻塞：auto 可能跑几分钟，
        # 拿任务列表时干等着会让界面像卡死
        if not _SIGNIN_LOCK.acquire(blocking=False):
            return {"ok": False, "error": "有命令正在执行，稍后再刷新任务"}
        try:
            _reset_signin_budget(signin)
            auth_file, _ = signin.find_auth_file()
            if not auth_file or not os.path.exists(auth_file):
                return {"ok": False, "error": "未找到登录凭据"}
            session = signin.resolve_session(signin.load_session_retry(auth_file))
            headers = signin.build_headers(session)
            endpoint = ((session.get("auth") or {}).get("endpoint")
                        or signin.DEFAULT_ENDPOINT).rstrip("/")

            code, body = signin.get(endpoint + "/v2/activity/growth/tasks", headers)
            if code != 200:
                return {"ok": False, "error": "HTTP %s" % code}

            raw = signin.dig(body, "tasks") or []
            items = []
            for t in raw:
                if not isinstance(t, dict):
                    continue
                prog = t.get("progress") or {}
                items.append({
                    "code": t.get("task_code"),
                    "title": t.get("title") or t.get("task_code"),
                    "desc": t.get("task_desc") or t.get("description") or "",
                    "status": t.get("accept_status") or "unknown",
                    "locked": bool(t.get("locked")),
                    "credit": t.get("reward_credit") or 0,
                    "energy": t.get("reward_energy") or 0,
                    "buddy": bool(t.get("reward_buddy")),
                    "current": prog.get("current"),
                    "target": prog.get("target"),
                    "badge": t.get("badge_name") or "",
                    "new": bool(t.get("is_new")),
                })
            _trace("tasks: 拉到 %d 项" % len(items))
            return {"ok": True, "items": items}
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            _SIGNIN_LOCK.release()

    def growth(self):
        """读取成长中心全部数据，并把与上次快照的差异写进运行日志。

        与 tasks() 一样复用 signin.py 的认证链路，但多拉了 Buddy 旅行、
        补登、连登兑换、抽奖、能量和 Buddy 盲盒。所有请求都是 GET；写操作
        仍只由 run('auto' / 'growth') 触发。
        """
        try:
            signin = _load_signin()
        except Exception as exc:
            return {"ok": False, "error": "无法加载 signin.py：%s" % exc}

        if not _SIGNIN_LOCK.acquire(blocking=False):
            return {"ok": False, "error": "有命令正在执行，稍后再刷新成长中心"}
        try:
            # 仪表盘刷新是一组独立请求，别继承上一次命令残留的时间预算。
            # 必须放在读凭据之前：解密助手也受这个预算约束，负数预算会让它
            # 直接报 HELPER_TIMEOUT（详见 _reset_signin_budget）。
            _reset_signin_budget(signin)
            auth_file, _ = signin.find_auth_file()
            if not auth_file or not os.path.exists(auth_file):
                return {"ok": False, "error": "未找到登录凭据"}
            try:
                session = signin.resolve_session(signin.load_session_retry(auth_file))
                headers = signin.build_headers(session)
            except Exception as exc:
                return {"ok": False,
                        "error": "读取登录凭据失败：%s: %s" % (type(exc).__name__, exc)}

            endpoint = ((session.get("auth") or {}).get("endpoint")
                        or signin.DEFAULT_ENDPOINT).rstrip("/")
            data = _growth_snapshot(signin, endpoint, headers)
            at = time.strftime("%Y-%m-%d %H:%M:%S")

            previous = _load_growth_store()
            previous_data = previous.get("data")
            changes = []
            baseline = not (isinstance(previous_data, dict) and previous_data)
            if not data.get("errors"):
                if not baseline:
                    changes = _growth_changes(previous_data, data)
                _save_growth_store({"at": at, "data": data})

            stamp = time.strftime("%H:%M:%S")
            for line in changes:
                _append_log("[%s] 成长中心变化: %s" % (stamp, line))

            _trace("growth: 任务 %d 项，变化 %d 条"
                   % (len(data.get("tasks") or []), len(changes)))
            return {"ok": True, "at": at, "data": data,
                    "changes": changes, "baseline": baseline}
        except _GrowthAuthError as exc:
            return {"ok": False, "error": "认证被拒绝（%s）" % exc}
        except Exception as exc:
            _trace("growth: 失败 %r" % (exc,))
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            _SIGNIN_LOCK.release()

    # ---------- 脚本服务（Windows 计划任务） ----------

    def service_status(self):
        """读取后台计划任务的安装、启用、运行与最近执行状态。"""
        try:
            import win_service
            return win_service.status(_signin_log_path())
        except Exception as exc:
            return _service_status_failure(exc)

    def service_action(self, action):
        """安装/卸载/启用/停用/立即运行/停止后台计划任务。

        只接受固定动作名；具体任务名、触发器与命令全部在 win_service 里定义，
        前端无法借这个入口执行任意命令。
        """
        allowed = ("install", "uninstall", "enable", "disable", "run", "stop")
        if action not in allowed:
            return {"ok": False, "error": "未知服务操作：%s" % action}

        try:
            import win_service
            if action == "install":
                if not os.path.isfile(SIGNIN):
                    result = _missing_script("signin.py", "WorkBuddy")
                else:
                    runner, prefix = _service_runner("workbuddy")
                    result = win_service.install(
                        runner, prefix, workdir=os.path.dirname(SIGNIN))
            else:
                result = win_service.action(action)
        except Exception as exc:
            result = _service_error(exc)

        result["status"] = self.service_status()
        text = result.get("message") or result.get("error") or "操作完成"
        _append_log("脚本服务 %s: %s" % (action, text))
        return result

    # ---------- 计划任务失败提示与恢复 ----------

    def service_alerts(self):
        """读取计划任务的失败记录，供对应模块展示。"""
        return {"ok": True, "items": _read_service_alerts()}

    def service_alert_ack(self, kind):
        """「知道了」：只清掉这一个模块的提示，不影响其它模块。"""
        try:
            import win_service
            removed = win_service.clear_alert(_diag_dir(), kind)
            return {"ok": True, "removed": removed}
        except Exception as exc:
            return {"ok": False,
                    "error": "%s: %s" % (type(exc).__name__, exc)}

    def service_alert_retry(self, kind):
        """「重新导入并重试」：后台执行，结果通过 service_alert 事件推送。"""
        if kind not in SERVICE_TASK_NAME:
            return {"ok": False, "error": "未知的计划任务类型：%s" % kind}
        with self._alert_lock:
            if self._alert_retry:
                return {"ok": False, "error": "上一次恢复还在执行，请稍候"}
            self._alert_retry = kind
        threading.Thread(target=self._service_alert_worker,
                         args=(kind,), daemon=True).start()
        return {"ok": True, "started": True}

    def _service_alert_worker(self, kind):
        """重新导入本机登录态 → 跑一次自动签到 → 回写记录。"""
        event = {"kind": kind, "stage": "done", "ok": False, "report": ""}
        try:
            ok, report = _reimport_local(kind)
            self._push("service_alert", {"kind": kind, "stage": "reimport",
                                         "ok": bool(ok), "report": report})
            code, retry_report = _retry_service_action(kind, "auto", 0.0)
            recovered = code == 0
            self._update_alert(kind, reimport_ok=bool(ok),
                               reimport_report=report,
                               retry_exit=code,
                               retry_report=retry_report,
                               recovered=recovered, stage="manual")
            event.update(ok=recovered, report=retry_report)
        except Exception as exc:
            event["report"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            with self._alert_lock:
                self._alert_retry = None
        self._push("service_alert", event)

    @staticmethod
    def _update_alert(kind, **fields):
        """在已有记录上覆盖字段；没有记录就新建一条。"""
        try:
            import win_service
            items = {item.get("kind"): item
                     for item in win_service.read_alerts(_diag_dir())}
            record = dict(items.get(kind) or {"kind": kind})
            record.update(fields)
            win_service.write_alert(_diag_dir(), record)
        except Exception as exc:
            _trace("回写失败记录失败: %r" % (exc,))

    # ---------- 本机账号重新导入 ----------

    def workbuddy_import(self):
        """重新读取本机 WorkBuddy 登录态，供账号卡片「导入账号」使用。"""
        if not os.path.isfile(SIGNIN):
            return {"ok": False, "error": "未找到 signin.py"}
        try:
            with _SIGNIN_LOCK:
                ok, report = _reimport_local("workbuddy")
        except Exception as exc:
            return {"ok": False,
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        _append_log("WorkBuddy 导入账号：%s" % report)
        return {"ok": bool(ok), "report": report}

    def qoder_import(self):
        """重新读取本机 Qoder CN 登录态，供账号卡片「导入账号」使用。"""
        if not os.path.isfile(QODER):
            return {"ok": False, "error": "未找到 qoder_signin.py"}
        try:
            with _QODER_LOCK:
                ok, report = _reimport_local("qoder")
        except Exception as exc:
            return {"ok": False,
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        _append_qoder_log({"result": "OK" if ok else "ERROR", "report": report})
        _append_log("Qoder 导入账号：%s" % report)
        return {"ok": bool(ok), "report": report}

    # ---------- Qoder CN 监控 ----------

    @staticmethod
    def _summarize_qoder(payload):
        """把 qoder_signin.py 的 JSON 结果拍平成可读日志行。"""
        if not isinstance(payload, dict):
            return []
        lines = []
        for key, label in (
            ("result", "结果"),
            ("report", ""),
            ("credits", "Credits"),
            ("campaign", "活动"),
            ("http", "HTTP"),
            ("token_expires_at", "令牌到期"),
        ):
            value = payload.get(key)
            if value is None or value == "":
                continue
            text = ("是" if value is True else "否") if isinstance(value, bool) \
                else str(value)
            lines.append(("  %-14s %s" % (label, text)).rstrip()
                         if label else "  " + text)
        return lines

    def qoder_monitor(self):
        """读取 Qoder CN 登录态、活动列表、设备组件与最近日志。

        这个接口只读：status 查询活动，doctor 离线检查登录态，不会领取。
        网络请求与 signin 的锁完全独立，不会阻塞 WorkBuddy 面板。
        """
        result = {"path": QODER, "log": _qoder_log_snapshot(),
                  "desktop": _desktop_state("qoder")}
        if not os.path.isfile(QODER):
            extra = dict(ok=False, error="未找到 qoder_signin.py",
                         searched=list(QODER_SEARCHED))
        elif self._qoder_busy:
            extra = dict(ok=False, busy=True,
                         error="Qoder 领取正在执行，请稍后再刷新")
        else:
            status = doctor = None
            errors = []
            with _QODER_LOCK:
                try:
                    _code, out = _invoke_qoder("status")
                    status = self._parse_payload(out)
                    if status is None:
                        errors.append("status 未返回 JSON")
                except Exception as exc:
                    errors.append("status: %s: %s" % (type(exc).__name__, exc))

                try:
                    _code, out = _invoke_qoder("doctor")
                    doctor = self._parse_payload(out)
                    if doctor is None:
                        errors.append("doctor 未返回 JSON")
                except Exception as exc:
                    errors.append("doctor: %s: %s" % (type(exc).__name__, exc))

            extra = dict(
                ok=bool(status or doctor),
                healthy=bool(status and status.get("result") == "OK"),
                status=status,
                doctor=doctor,
                at=time.strftime("%Y-%m-%d %H:%M:%S"),
                errors=errors or None,
            )
        result.update(extra)
        return result

    def qoder_claim(self):
        """异步领取 Qoder 今日 Credits，结果通过 qoder_done 事件推送。"""
        if not os.path.isfile(QODER):
            return {"ok": False, "error": "未找到 qoder_signin.py"}
        with self._qoder_state_lock:
            if self._qoder_busy:
                return {"ok": False, "error": "Qoder 领取已经在执行"}
            self._qoder_busy = True

        threading.Thread(target=self._qoder_claim_worker, daemon=True).start()
        _append_log("Qoder：开始领取今日 Credits")
        return {"ok": True}

    def _qoder_claim_worker(self):
        event = {"exit": 2, "payload": None}
        try:
            self._push("qoder_log", "[%s] 开始领取 Qoder 今日 Credits"
                       % time.strftime("%H:%M:%S"))
            with _QODER_LOCK:
                code, out = _invoke_qoder("auto")
            payload = self._parse_payload(out)
            _append_qoder_log(payload)
            lines = self._summarize_qoder(payload)
            if not lines:
                lines = [line for line in out.splitlines() if line.strip()]
            for line in lines:
                self._push("qoder_log", line)
                _append_log("Qoder " + line.strip())
            event = {"exit": code, "payload": payload}
        except Exception as exc:
            detail = "%s: %s" % (type(exc).__name__, exc)
            _append_qoder_log({"result": "ERROR", "report": detail})
            self._push("qoder_log", "Qoder 领取异常：" + detail)
            _append_log("Qoder 领取异常：" + detail)
            event["error"] = detail
        finally:
            event["log"] = _qoder_log_snapshot()
            event["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            # 先把忙碌标记放下，再推完成事件：前端收到事件后会立刻
            # 调 qoder_monitor()，否则会被误判为「领取正在执行」。
            with self._qoder_state_lock:
                self._qoder_busy = False
            self._push("qoder_done", event)

    def qoder_service_status(self):
        """读取 QoderDailyCredits 计划任务状态。"""
        try:
            import win_service
            return win_service.qoder_status(_qoder_log_path())
        except Exception as exc:
            return _service_status_failure(exc)

    def qoder_service_action(self, action):
        """安装/卸载/启用/停用/立即运行/停止 Qoder 计划任务。"""
        allowed = ("install", "uninstall", "enable", "disable", "run", "stop")
        if action not in allowed:
            return {"ok": False, "error": "未知服务操作：%s" % action}

        result = None
        error = None
        try:
            import win_service
            if action == "install":
                if not os.path.isfile(QODER):
                    error = _missing_script("qoder_signin.py", "Qoder")
                else:
                    runner, prefix = _service_runner("qoder")
                    result = win_service.install_qoder(
                        QODER, runner=runner, runner_args=prefix)
            else:
                result = win_service.qoder_action(action)
        except Exception as exc:
            error = _service_error(exc)

        if result is None:
            result = error or {"ok": False, "error": "Qoder 计划任务安装失败"}
        result["status"] = self.qoder_service_status()
        text = result.get("message") or result.get("error") or "操作完成"
        _append_log("Qoder 计划任务 %s: %s" % (action, text))
        return result

    # ---------- Trae 多账号监控 ----------

    @staticmethod
    def _trae_exit(payload):
        if not isinstance(payload, dict):
            return 1
        codes = {"OK": 0, "ALREADY": 0, "CLAIMED": 0, "NO_CHANGE": 0,
                 "NO_ACCOUNT": 2, "NO_AUTH": 2, "NO_CLIENT": 2}
        return codes.get(payload.get("result"), 1)

    @staticmethod
    def _summarize_trae(payload, kind="checkin"):
        """把 trae_signin 的结果拍平成日志行。"""
        if not isinstance(payload, dict):
            return []
        lines = []
        report = payload.get("report")
        if report:
            lines.append("  " + str(report))
        pairs = []
        if kind == "usage":
            pairs = (("sessions_count", "会话数"),
                     ("credits", "消耗积分"),
                     ("input_token", "输入 Token"),
                     ("output_token", "输出 Token"),
                     ("cache_rate", "缓存命中率"))
        elif kind == "import":
            pairs = (("added", "新增"), ("updated", "更新"))
        else:
            pairs = (("total_gained", "本次获得"),
                     ("remaining_total", "剩余积分"),
                     ("checked", "成功"),
                     ("already", "已签到"),
                     ("failed", "失败"))
        for key, label in pairs:
            value = payload.get(key)
            if value is None or value == "":
                continue
            if key == "cache_rate":
                value = str(value) + "%"
            lines.append("  %-10s %s" % (label, value))
        for row in payload.get("accounts") or []:
            if not isinstance(row, dict):
                continue
            mark = "✔" if row.get("ok") else "✖"
            extra = ""
            if row.get("gained"):
                extra = " +%s" % row.get("gained")
            if row.get("remaining") is not None:
                extra += "（剩余 %s）" % row.get("remaining")
            lines.append("  %s %s%s %s" % (mark, row.get("name") or "账号",
                                           extra, row.get("message") or ""))
        return lines

    def trae_monitor(self):
        """读取 Trae 账号、签到历史、用量与日志（不联网、不签到）。"""
        result = {"path": TRAE, "log": _trae_log_snapshot(),
                  "desktop": _desktop_state("trae")}
        if not os.path.isfile(TRAE):
            result.update(ok=False, error="未找到 trae_signin.py",
                          searched=list(TRAE_SEARCHED))
            return result
        with self._trae_state_lock:
            busy = self._trae_busy
        if busy:
            cached = dict(self._trae_last or {})
            cached.update(ok=True, busy=True, log=result["log"], path=TRAE,
                          desktop=result["desktop"])
            return cached

        try:
            with _TRAE_LOCK:
                trae = _load_trae()
                trae._started_at = time.monotonic()
                data = trae.monitor(deep=False)
        except Exception as exc:
            return dict(result, ok=False,
                        error="%s: %s" % (type(exc).__name__, exc))

        data = dict(data or {})
        data.update(ok=True, busy=False, path=TRAE, log=result["log"])
        self._trae_last = data
        return data
    def _start_trae(self, kind, account_id=None, days=None):
        if not os.path.isfile(TRAE):
            return {"ok": False, "error": "未找到 trae_signin.py"}
        with self._trae_state_lock:
            if self._trae_busy:
                return {"ok": False, "error": "Trae 任务正在执行，请稍后再试"}
            self._trae_busy = True

        threading.Thread(target=self._trae_worker,
                         args=(kind, account_id, days), daemon=True).start()
        _append_log("Trae：开始 %s" % kind)
        return {"ok": True, "kind": kind}

    def _trae_worker(self, kind, account_id=None, days=None):
        event = {"kind": kind, "exit": 2, "payload": None}
        labels = {"checkin": "签到", "refresh": "刷新账号状态",
                  "usage": "同步用量", "import": "导入账号"}
        label = labels.get(kind, kind)
        try:
            self._push("trae_log", "[%s] 开始%s"
                       % (time.strftime("%H:%M:%S"), label))
            with _TRAE_LOCK:
                trae = _load_trae()
                trae._started_at = time.monotonic()
                if kind == "checkin":
                    payload = trae.run_auto(account_id)
                elif kind == "refresh":
                    payload = trae.refresh_account(account_id)
                elif kind == "usage":
                    payload = trae.run_usage(account_id, days)
                elif kind == "import":
                    payload = trae.import_local()
                else:
                    raise ValueError("未知 Trae 操作：%s" % kind)

            _append_trae_log(payload, kind)
            lines = self._summarize_trae(payload, kind)
            for line in lines:
                self._push("trae_log", line)
                _append_log("Trae " + line.strip())
            event = {"kind": kind, "exit": self._trae_exit(payload),
                     "payload": payload}
        except Exception as exc:
            detail = "%s: %s" % (type(exc).__name__, exc)
            _append_trae_log({"result": "ERROR", "report": detail}, kind)
            self._push("trae_log", "Trae %s异常：%s" % (label, detail))
            _append_log("Trae %s异常：%s" % (label, detail))
            event["error"] = detail
        finally:
            event["log"] = _trae_log_snapshot()
            event["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            with self._trae_state_lock:
                self._trae_busy = False
            self._push("trae_done", event)

    def trae_checkin(self, account_id=None):
        """异步签到（不传 account_id 时签所有启用账号）。"""
        return self._start_trae("checkin", account_id=account_id or None)

    def trae_refresh(self, account_id=None):
        """异步刷新账号状态与剩余积分。"""
        return self._start_trae("refresh", account_id=account_id or None)

    def trae_usage(self, account_id=None, days=None):
        """异步拉取指定账号的用量。"""
        try:
            days = int(days) if days not in (None, "") else None
        except (TypeError, ValueError):
            days = None
        return self._start_trae("usage", account_id=account_id or None,
                                days=days)

    def trae_import(self):
        """异步扫描本机 Trae 客户端并导入登录态。"""
        return self._start_trae("import")

    def trae_account_action(self, action, account_id=None, payload=None):
        """账号增删改、切换主号、换设备号（同步返回，前端 await）。"""
        if action not in ("add", "update", "remove", "activate",
                          "renew_device"):
            return {"ok": False, "error": "未知账号操作：%s" % action}
        with self._trae_state_lock:
            if self._trae_busy:
                return {"ok": False, "error": "Trae 任务正在执行，请稍后再试"}

        payload = payload if isinstance(payload, dict) else {}
        try:
            with _TRAE_LOCK:
                trae = _load_trae()
                trae._started_at = time.monotonic()
                if action == "add":
                    result = trae.add_account(
                        payload.get("credential") or "",
                        name=payload.get("name") or None)
                elif action == "update":
                    fields = {}
                    for key in ("name", "enabled", "is_member"):
                        if key in payload:
                            fields[key] = payload[key]
                    result = trae.update_account(account_id, **fields)
                elif action == "remove":
                    result = trae.remove_account(account_id)
                elif action == "activate":
                    result = trae.set_active(account_id)
                else:
                    result = trae.renew_device(account_id)
        except Exception as exc:
            return {"ok": False,
                    "error": "%s: %s" % (type(exc).__name__, exc)}

        _append_trae_log(result, "account-" + action)
        text = result.get("report") or result.get("error") or "账号操作完成"
        _append_log("Trae 账号 %s: %s" % (action, text))
        return result

    def trae_update_settings(self, payload):
        """保存自动签到间隔、用量天数、飞书 Webhook 等设置。"""
        with self._trae_state_lock:
            if self._trae_busy:
                return {"ok": False, "error": "Trae 任务正在执行，请稍后再试"}
        try:
            with _TRAE_LOCK:
                trae = _load_trae()
                result = trae.update_settings(payload)
        except Exception as exc:
            return {"ok": False,
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        _append_trae_log(result, "settings")
        return result

    def trae_service_status(self):
        """读取 TraeDailyCheckin 计划任务状态。"""
        try:
            import win_service
            return win_service.trae_status(_trae_log_path())
        except Exception as exc:
            return _service_status_failure(exc)

    def trae_service_action(self, action):
        """安装/卸载/启用/停用/立即运行/停止 Trae 计划任务。"""
        allowed = ("install", "uninstall", "enable", "disable", "run", "stop")
        if action not in allowed:
            return {"ok": False, "error": "未知服务操作：%s" % action}

        result = None
        error = None
        try:
            import win_service
            if action == "install":
                if not os.path.isfile(TRAE):
                    error = _missing_script("trae_signin.py", "Trae")
                else:
                    try:
                        trae = _load_trae()
                        primary = (trae.load_store()["settings"]
                                   .get("auto_time") or "08:05")
                    except Exception:
                        primary = "08:05"
                    base_times = win_service.trae_trigger_times(primary, jitter=False)
                    runner, prefix = _service_runner("trae")
                    result = win_service.install_trae(
                        TRAE, base_times=base_times, runner=runner,
                        runner_args=prefix)
            else:
                result = win_service.trae_action(action)
        except Exception as exc:
            error = _service_error(exc)

        if result is None:
            result = error or {"ok": False, "error": "Trae 计划任务安装失败"}
        result["status"] = self.trae_service_status()
        text = result.get("message") or result.get("error") or "操作完成"
        _append_log("Trae 计划任务 %s: %s" % (action, text))
        return result

    def system_theme(self):
        """把注册表里读到的亮/暗与强调色交给前端做初始配色。"""
        theme = _system_theme()
        return {"dark": theme["dark"], "accent": theme["accent"]}

    def native_title(self, text):
        """用 Win32 API 修正窗口标题。

        pywebview 走的是系统 WebView2，标题理论上已是正确 Unicode；
        这里保留一次兜底修正，成本极低。
        """
        try:
            import win_title
            return win_title.set_native_title(text)
        except Exception:
            return 0

    # ---------- 事件推送 ----------

    def log_error(self, text):
        """前端把 JS 异常送回来落盘。

        打包后没有控制台、stderr 不可见，前端报错除了显示在界面上
        没有任何留存，事后无从追溯。
        """
        try:
            with open(_crash_path(), "a", encoding="utf-8") as fh:
                fh.write("[js] %s\n" % text)
        except Exception:
            pass
        return True

    def _push(self, kind, payload):
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                "window.__onBackend && window.__onBackend(%s, %s)"
                % (json.dumps(kind), json.dumps(payload, ensure_ascii=False))
            )
        except Exception:
            pass  # 窗口已关闭


def _reshuffle_schedule(kind):
    """签到跑完后换一组触发时刻。

    放在 _handle_service_failure 之后：失败重试期间任务还在跑，这时重排会
    和正在执行的动作抢同一份任务定义。失败也要重排——否则每天在同一分钟
    失败，日志会一模一样，看不出这是「同一个时刻的老问题」而不是新增故障。

    全程吞异常：换时间只是顺带的优化，不能让它把签到结果改成失败。
    """
    try:
        import win_service
        win_service.apply_jitter_after_run(kind, _diag_dir())
    except Exception as exc:
        _append_log("[jitter] %s 重排异常：%s" % (kind, exc))


def _idle_skipped(kind):
    """这次是否被空闲补跑的冷却闸门挡下。

    挡住时既不算失败、也不该重排：跳过没有产生新结果，重排只会把当天
    已过的点每空闲一次就重抽一次，白写任务定义。
    """
    try:
        import idle_trigger
        return idle_trigger.skipped(kind)
    except Exception:
        return False


def _record_idle(kind, ok):
    """记下最终结果（含失败重试后的结论），供下一次空闲补跑判断。"""
    try:
        import idle_trigger
        idle_trigger.record(kind, ok, _diag_dir())
    except Exception:
        pass


def _service_main(argv):
    """计划任务入口：``--service silent`` / ``silent-poll``。

    由 win_service 注册的计划任务调用。直接复用 Api._invoke()，不创建窗口，
    也不需要 pywebview；silent 类命令的结果由 signin.emit() 写进 signin.log。

    失败时不再只是「静默退出」：把原文写进失败记录、拉起控制台、重新导入
    本机登录态并限时重试一次（见 _handle_service_failure）。
    """
    action = argv[0] if argv else ""
    allowed = ("silent", "silent-poll", "silent-growth")
    if action not in allowed:
        _append_log("[service] 未知或缺少动作：%r（可用：%s）"
                    % (action, " / ".join(allowed)))
        return 2

    started = time.monotonic()
    stdout = ""
    try:
        code, stdout, stderr = Api._invoke(action)
    except Exception as exc:
        import traceback
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        _append_log("[service] %s 启动失败：%s" % (action, detail))
        try:
            with open(_crash_path(), "a", encoding="utf-8") as fh:
                fh.write("[service %s] %s\n" % (action, detail))
        except Exception:
            pass
        code, stderr = 1, ""
        stdout = json.dumps({"result": "ERROR", "report": detail})

    # silent 正常时 stdout 为空；异常或加载失败才会有内容，落盘便于事后追溯。
    for line in (stderr or "").splitlines() + (stdout or "").splitlines():
        _append_log("[service] " + line)
    if _idle_skipped("workbuddy"):
        # 空闲触发撞上「刚跑过、还在冷却」：正常结束，不写失败记录、不重排
        return 0
    # 以 0 退出但 result 是 AUTH_ERROR 的情况也要走自愈，判断交给这里。
    exit_code = _handle_service_failure(
        "workbuddy", action, code if isinstance(code, int) else 1, stdout,
        time.monotonic() - started)
    _record_idle("workbuddy", exit_code == 0)
    _reshuffle_schedule("workbuddy")
    return exit_code


def _side_service_main(name, loader, argv):
    """``--qoder-service`` / ``--trae-service``：计划任务专用入口。

    直接调用对应脚本的 main()，与手工执行
    ``python qoder_signin.py silent`` 完全等价：结果由脚本自己的 emit()
    写进日志，进程不创建窗口、不碰 pywebview。

    存在的理由是解掉「计划任务必须依赖系统 Python」这个硬依赖：exe 里已经
    打包了解释器，找不到 pythonw 时任务可以直接指向 exe 自己。
    """
    action = argv[0] if argv else "silent"
    if action not in ("silent", "auto"):
        _append_log("[%s] 未知或缺少动作：%r（可用：silent / auto）"
                    % (name, action))
        return 2

    kind = "qoder" if name.startswith("qoder") else "trae"
    started = time.monotonic()
    stdout = ""
    try:
        code = _run_side_once(kind, action, loader=loader)
    except Exception as exc:
        import traceback
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        _append_log("[%s] %s 启动失败：%s" % (name, action, detail))
        try:
            with open(_crash_path(), "a", encoding="utf-8") as fh:
                fh.write("[%s %s] %s\n" % (name, action, detail))
        except Exception:
            pass
        code = 1
        stdout = json.dumps({"result": "ERROR", "report": detail})

    if _idle_skipped(kind):
        # 空闲触发撞上「刚跑过、还在冷却」：跳过不是失败，也不能让
        # _service_failure 去读 10 分钟内的旧日志——那会把上一次的真失败重复报一遍。
        return 0
    exit_code = _handle_service_failure(kind, action, code, stdout,
                                        time.monotonic() - started)
    _record_idle(kind, exit_code == 0)
    _reshuffle_schedule(kind)
    return exit_code

# ---------------------------------------------------------------------------
# 计划任务失败自愈
# ---------------------------------------------------------------------------
#
# 计划任务在后台跑，失败只在日志里留一行，用户看不到。这里按固定顺序处理：
#
#   ① 写失败记录（控制台起来后第一眼就能看到原文）
#   ② 拉起或激活控制台（同一类型 6 小时内只拉一次，避免半夜反复弹窗）
#   ③ 重新导入本机登录态（Trae 扫描客户端、WorkBuddy / Qoder 重读登录文件）
#   ④ 用剩余时间预算重试一次，结果回写记录
#
# 第 ③ 步针对的就是「没有会话 Cookie 可续期，请重新导入本机登录态」：
# 原样展示报错，同时真的去读一遍本机客户端的登录态，再重试。

SERVICE_TASK_NAME = {"workbuddy": "WorkBuddyAutoSignin",
                     "qoder": "QoderDailyCredits",
                     "trae": "TraeDailyCheckin"}

# 计划任务的 ExecutionTimeLimit 减去 60 秒安全余量；重试必须在这之内结束，
# 否则任务会被任务计划程序直接强杀，连重试结论都写不回去。
SERVICE_TIME_LIMIT = {"workbuddy": 540, "qoder": 240, "trae": 540}

RETRY_MIN_SECONDS = 60         # 剩余时间不足这么多就不重试
SURFACE_COOLDOWN = 6 * 3600    # 同一类型失败，6 小时内不重复拉起窗口

FAIL_RESULTS = ("ERROR", "AUTH_ERROR", "NO_AUTH", "NO_CLIENT",
                "INACTIVE", "EXPIRED", "FAILED")


def _service_log_path(kind):
    if kind == "qoder":
        return _qoder_log_path()
    if kind == "trae":
        return _trae_log_path()
    return _signin_log_path()


def _last_log_payload(kind):
    """从对应日志的最后几行里取一条 JSON 结果。

    silent 模式不往 stdout 打东西（emit 直接写日志），所以失败原因只能从
    日志里捞。Trae / Qoder / WorkBuddy 的日志行格式都是「[时间] {json}」。
    """
    lines = []
    try:
        with open(_service_log_path(kind), "r", encoding="utf-8",
                  errors="replace") as fh:
            lines = fh.readlines()[-40:]
    except OSError:
        pass
    for line in reversed(lines):
        idx = line.find("{")
        if idx < 0:
            continue
        try:
            payload = json.loads(line[idx:])
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _log_age(kind):
    """日志文件距今多少秒；拿不到就当成很旧。"""
    try:
        return time.time() - os.path.getmtime(_service_log_path(kind))
    except OSError:
        return 1e9


def _service_failure(kind, code, stdout=""):
    """判断这次运行算不算失败，并取出可展示的原文。

    除了退出码，还要看 result：脚本对「凭据过期」这类可预期失败有时仍以
    0 退出，只看退出码会漏报。反过来，0 退出时只看日志最后一行也不行——
    那可能是上一次失败的残留，所以要求日志是刚写入的。
    """
    payload = (Api._parse_payload(stdout or "") or {})
    if not payload:
        tail = _last_log_payload(kind)
        # 只认刚写入的日志：可能是上一次失败的残留，拿它当这次的原因会误导。
        fresh = _log_age(kind) < 600
        usable = bool(tail) and fresh and (
            code == 0 or str(tail.get("result") or "") in FAIL_RESULTS)
        payload = tail if usable else {}
    result = str(payload.get("result") or "")
    failed = code != 0 or result in FAIL_RESULTS
    if not failed:
        return None
    report = (payload.get("report") or payload.get("error")
              or payload.get("message") or "")
    if not report:
        report = "计划任务退出码 %s，日志里没有可读结论" % code
    return {"result": result or "ERROR", "report": str(report), "exit": code}


def _reimport_local(kind):
    """重新读取本机客户端登录态；返回 (ok, 一句可展示的结论)。"""
    try:
        if kind in ("workbuddy", "qoder"):
            if kind == "workbuddy":
                signin = _load_signin()
                auth_file, _looked = signin.find_auth_file()
                if not auth_file:
                    return False, "未找到本机 WorkBuddy 登录凭据"
                session = signin.resolve_session(
                    signin.load_session_retry(auth_file))
                label = "WorkBuddy"
            else:
                session = _load_qoder().load_session()
                label = "Qoder"
            if not session:
                return False, "未找到本机 %s 登录态" % label
            return True, "已重新读取本机 %s 登录态" % label

        trae = _load_trae()
        payload = trae.import_local()
        ok = payload.get("result") in ("OK", "NO_CHANGE")
        report = payload.get("report") or ("已重新导入本机 Trae 登录态"
                                           if ok else "本机 Trae 登录态不可用")
        return ok, str(report)
    except Exception as exc:
        return False, "重新导入登录态失败：%s: %s" % (type(exc).__name__, exc)


def _run_side_once(kind, action, loader=None, budget=None):
    """跑一次 Qoder / Trae 的 main()；budget 用于压缩重试的时间预算。"""
    module = (loader or (_load_qoder if kind == "qoder" else _load_trae))()
    old_budget = getattr(module, "BUDGET_SECONDS", None)
    if budget and old_budget is not None:
        module.BUDGET_SECONDS = float(budget)
    # 时间预算在模块加载时记下；长期驻留的进程里要重置，
    # 否则「预算耗尽」会让任务什么都不做。
    if hasattr(module, "_started_at"):
        module._started_at = time.monotonic()
    try:
        code = module.main([action])
    finally:
        # 重试只是临时收紧预算；不还原会让控制台后续的手动操作也变短。
        if old_budget is not None:
            module.BUDGET_SECONDS = old_budget
    return code if isinstance(code, int) else 1


def _retry_text(code, payload):
    """重试的结论：优先用脚本自己的 report，没有就按退出码兜底。"""
    if isinstance(payload, dict):
        report = payload.get("report") or payload.get("error")
        if report:
            return str(report)
    return "重试成功" if code == 0 else "重试仍失败（exit=%s）" % code


def _retry_service_action(kind, action, elapsed):
    """用剩余预算重试一次；返回 (exit_code 或 None, 结论)。"""
    budget = SERVICE_TIME_LIMIT.get(kind, 300) - elapsed - 30
    if budget < RETRY_MIN_SECONDS:
        return None, ("首轮已用 %.0f 秒，剩余时间不足，未自动重试"
                      % elapsed)
    budget = int(min(budget, 240))

    if kind == "workbuddy":
        old = os.environ.get("WORKBUDDY_BUDGET_SECONDS")
        os.environ["WORKBUDDY_BUDGET_SECONDS"] = str(budget)
        try:
            code, stdout, _stderr = Api._invoke(action)
        finally:
            if old is None:
                os.environ.pop("WORKBUDDY_BUDGET_SECONDS", None)
            else:
                os.environ["WORKBUDDY_BUDGET_SECONDS"] = old
        payload = (Api._parse_payload(stdout or "")
                   or _last_log_payload("workbuddy") or {})
    else:
        try:
            code = _run_side_once(kind, action, budget=budget)
        except Exception as exc:
            return 1, "重试异常：%s: %s" % (type(exc).__name__, exc)
        payload = _last_log_payload(kind) or {}
    return code, _retry_text(code, payload)


def _console_command():
    """拉起控制台要执行的命令与工作目录。

    冻结后跑 exe 自身（不带参数即界面）；源码运行时优先 pythonw，
    避免在用户面前弹出一个黑框。
    """
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        return [exe], os.path.dirname(exe)
    script = os.path.abspath(__file__)
    exe = sys.executable
    try:
        import win_service
        exe = win_service.find_pythonw() or exe
    except Exception:
        pass
    return [exe, script], os.path.dirname(script)


def _surface_console(kind, previous=None):
    """把控制台叫到用户面前；返回一句结果说明（写进失败记录）。"""
    if os.name != "nt" or os.environ.get("WORKBUDDY_NO_SURFACE") == "1":
        # 非 Windows 没有计划任务；排障/自动化测试时也不想被弹窗打断。
        return "skipped"

    last = (previous or {}).get("surface_at") or 0
    try:
        last = float(last)
    except (TypeError, ValueError):
        last = 0
    if time.time() - last < SURFACE_COOLDOWN:
        return "cooldown"

    try:
        import win_guard
        # find_own_window() 只看本进程的窗口；服务进程没有界面，永远返回
        # None。要判断「控制台是否已经开着」，必须按 exe 名找别的进程。
        if (getattr(sys, "frozen", False)
                and win_guard.find_window_by_process(
                    os.path.basename(sys.executable),
                    exclude_pid=os.getpid())):
            win_guard.activate_existing_window()
            return "activated"
    except Exception as exc:
        _trace("激活已有窗口失败: %r" % (exc,))

    try:
        argv, cwd = _console_command()
        # 清掉 PyInstaller 传给子进程的临时解包目录：不清的话，新进程会
        # 复用本进程的 _MEIxxxx，而本进程退出时会把那个目录删掉——窗口
        # 起来几秒后就因为找不到 Python.Runtime.dll 崩掉（实测如此）。
        env = dict(os.environ)
        for key in ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR",
                    "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL"):
            env.pop(key, None)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008   # DETACHED_PROCESS
        subprocess.Popen(argv, cwd=cwd, env=env, close_fds=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kwargs)
    except Exception as exc:
        return "failed: %s" % exc
    return "launched"


def _read_service_alerts():
    try:
        import win_service
        return win_service.read_alerts(_diag_dir())
    except Exception as exc:
        _trace("读取失败记录失败: %r" % (exc,))
        return []


def _write_service_alert(record):
    try:
        import win_service
        return win_service.write_alert(_diag_dir(), record)
    except Exception as exc:
        _trace("写入失败记录失败: %r" % (exc,))
        return None


def _log_recovery(kind, record):
    """把自愈过程写进对应产品的日志，界面日志面板能直接看到。"""
    lines = ["【自动恢复】计划任务失败：%s" % record.get("report", "")]
    if record.get("reimport_report"):
        lines.append("【自动恢复】重新导入本机登录态：%s"
                     % record["reimport_report"])
    if record.get("retry_report"):
        lines.append("【自动恢复】重试一次：%s" % record["retry_report"])
    for line in lines:
        if kind == "trae":
            _append_trae_log({"result": "RECOVERY", "report": line}, "recovery")
        elif kind == "qoder":
            _append_qoder_log({"result": "RECOVERY", "report": line})
        else:
            _append_log(line)


def _handle_service_failure(kind, action, code, stdout, elapsed):
    """失败后的固定动作；返回最终退出码（重试成功则算成功）。"""
    failure = _service_failure(kind, code, stdout)
    if failure is None:
        return code

    record = {"kind": kind,
              "task": SERVICE_TASK_NAME.get(kind, ""),
              "action": action,
              "at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "exit": code,
              "result": failure["result"],
              "report": failure["report"],
              "stage": "failed"}
    # 上一版记录要留到拉起判断之后：新一轮失败会覆盖它，
    # 先读出来才能实现「6 小时内不重复弹窗」。
    previous = next((item for item in _read_service_alerts()
                     if item.get("kind") == kind), {})
    # 先落一版快照：控制台一起来就能看到原文，不必等重试跑完。
    _write_service_alert(dict(record))

    record["surface"] = _surface_console(kind, previous)
    record["surface_at"] = time.time()

    ok, report = _reimport_local(kind)
    record["reimport_ok"] = ok
    record["reimport_report"] = report

    try:
        retry_code, retry_report = _retry_service_action(kind, action, elapsed)
    except Exception as exc:
        retry_code, retry_report = 1, "重试异常：%s: %s" % (
            type(exc).__name__, exc)
    record["retry_report"] = retry_report
    if retry_code is not None:
        record["retry_exit"] = retry_code
        record["recovered"] = retry_code == 0
    record["stage"] = "done"

    _write_service_alert(record)
    _log_recovery(kind, record)
    return 0 if record.get("recovered") else code



def main():
    import webview

    # 启动耗时的基准点：窗口创建与首帧显示的间隔都要靠它量化。
    # 实测这台机器上 webview.start() 到页面 loaded 要 1.9~2.1 秒，且与
    # 页面内容多少无关（只有骨架的空页面也一样），属于 WebView2 初始化
    # 的固定成本；把窗口延后到页面可绘制再显示，用户就不会看到那段底色。
    t_start = time.perf_counter()

    _trace("main 进入: HERE=%s ASSETS=%s index=%s SIGNIN=%s"
           % (HERE, ASSETS,
              os.path.isfile(os.path.join(ASSETS, "index.html")), SIGNIN))

    title = "Auto-Claim 控制台"
    guard_handle = None
    watchdog = None
    try:
        import win_guard
    except Exception as exc:
        # 单实例/看门狗是保护能力，不应因为它们导致整个控制台起不来。
        # 打包校验会检查 win_guard.py 是否真的进了 exe；这里只做 fail-open。
        _trace("win_guard 不可用: %r" % (exc,))
        win_guard = None

    try:
        import win_geometry
    except Exception as exc:
        # 窗口位置记忆同样只是体验优化：模块缺失（旧包/裁剪包）也不能让
        # 控制台起不来，顶多是每次回到默认位置。
        _trace("win_geometry 不可用: %r" % (exc,))
        win_geometry = None

    if win_guard is not None:
        try:
            guard_handle, already_running = win_guard.acquire_single_instance()
        except Exception as exc:
            _trace("单实例锁异常: %r" % (exc,))
            guard_handle, already_running = None, False

        if already_running:
            _trace("检测到已有实例，尝试激活已有窗口")
            try:
                state = win_guard.activate_existing_window()
            except Exception as exc:
                _trace("激活已有实例失败: %r" % (exc,))
                state = "missing"

            message = None
            warning = False
            if state == "hung":
                message = ("Auto-Claim 控制台已在运行，但现有窗口无响应。\n\n"
                           "看门狗（默认开启）会自动结束它；也可以稍后重试，"
                           "或在任务管理器中结束 Auto-Claim-dashboard.exe。")
                warning = True
            elif state == "missing":
                message = ("Auto-Claim 控制台已在运行，但暂时找不到窗口。\n"
                           "请稍等片刻后再试。")
            if message:
                win_guard.show_message(message, title, warning=warning)
            return

    # 创建窗口前先探测系统主题：原生窗口背景色和首帧图标都要立刻匹配，
    # 不能等 WebView2 加载完、JS bridge 就绪后再由前端纠正（深色下会
    # 先闪一下浅色窗口，约 1 秒）。同一份结果也随 bootstrap 交给前端。
    initial_theme = _system_theme()
    _trace("初始主题（创建窗口前）: dark=%s accent=%s"
           % (initial_theme["dark"], initial_theme["accent"]))

    # 默认等页面可绘制后再显示窗口；WORKBUDDY_INSTANT_WINDOW=1 恢复旧行为
    # （窗口立刻出现，但 WebView2 初始化期间会有一段空白底色）。
    instant_window = os.environ.get("WORKBUDDY_INSTANT_WINDOW") == "1"

    api = Api(initial_theme)

    # 原生图标（标题栏 / 任务栏 / 托盘）由 win_icon 在窗口显示后接管。
    # 这里用可变容器保存，方便 finally 里统一清理，也避免闭包 nonlocal。
    icon_holder = {"manager": None}

    def _on_system_theme(dark):
        """系统主题切换时顺手让前端跟随（手动选过主题的由 JS 忽略）。"""
        _trace("Windows 主题切换: dark=%s" % dark)
        api._push("theme", {"dark": bool(dark)})

    def _start_native_icons():
        """窗口有了 HWND 之后再挂载；失败只记日志，不影响界面。"""
        if icon_holder["manager"] is not None:
            return
        try:
            hwnd = _window_handle(window)
            if not hwnd:
                _trace("原生图标: 找不到窗口句柄，跳过")
                return
            import win_icon
            manager = win_icon.IconManager(
                hwnd,
                ASSETS,
                dark=_system_dark(),
                tray=os.environ.get("WORKBUDDY_TRAY", "1") != "0",
                on_theme=_on_system_theme,
            )
            if manager.start():
                icon_holder["manager"] = manager
                _trace("原生图标: hwnd=%#x dark=%s"
                       % (hwnd, _system_dark()))
            else:
                _trace("原生图标: 启动失败（hwnd=%r）" % (hwnd,))
        except Exception as exc:
            _trace("原生图标主题跟随启动失败: %r" % (exc,))

    # WebView2 会对被遮挡/后台窗口节流 JS 定时器（最慢 1 分钟一次）。
    # 看门狗靠 5 秒一次的心跳判断 UI 是否还活着；一旦被节流，窗口只是
    # 被别的窗口盖住就会被误判成卡死。这三个开关在 WebView2 初始化前
    # 必须设置，所以放在 create_window 之前。
    _flags = ("--disable-background-timer-throttling "
              "--disable-backgrounding-occluded-windows "
              "--disable-renderer-backgrounding")
    if os.environ.get("WORKBUDDY_DISABLE_GPU") == "1":
        # 卡死若与 DirectComposition/GPU 呈现有关，这个开关可用于对照实验。
        # 默认不开：软件渲染会改变滚动/动画观感。
        _flags += " --disable-gpu"
    _args = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    if _flags not in _args:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            _args + " " + _flags).strip()

    # 窗口位置/尺寸：用户拖过一次就该记住，不必每次启动都手工摆回来。
    # 读不到（首次运行、文件损坏）时返回默认尺寸且不带 x/y，交给 pywebview 居中。
    saved_geometry = _load_geometry(win_geometry, _diag_dir())

    window = webview.create_window(
        title,
        os.path.join(ASSETS, "index.html"),
        js_api=api,
        width=saved_geometry.get("width", 1180),
        height=saved_geometry.get("height", 800),
        x=saved_geometry.get("x"),
        y=saved_geometry.get("y"),
        # 上次是最大化就还原成最大化；此时 x/y/width/height 只作为「还原后
        # 回到哪」的备用值，由系统自己处理。
        maximized=bool(saved_geometry.get("maximized")),
        min_size=(940, 640),
        # 先隐藏：WebView2 初始化 + 首次导航期间窗口只有一块主题底色
        # （就是之前看到的"空白窗口"），等页面首帧就绪后再显示。
        hidden=not instant_window,
        background_color="#202020" if initial_theme["dark"] else "#f3f3f3",
    )
    # 立刻绑定：bootstrap / system_theme 等调用发生在 start() 之前，
    # _push 要靠它把结果送回前端
    api._window = window

    try:
        geometry = _track_geometry(win_geometry, window, _diag_dir(),
                                   saved_geometry)
    except Exception as exc:
        _trace("窗口几何记忆未启用: %r" % (exc,))
        geometry = None
    geometry_checked = {"done": False}

    def _verify_geometry(*_):
        """窗口真正出现后校验一次：旧坐标可能落在已拔掉的副屏之外。

        这一步必须在有 HWND 之后做——用物理像素比对显示器工作区，
        才不会因为 DPI 换算把「其实在屏幕里」的窗口误判成飞出桌面。
        """
        if geometry is None or geometry_checked["done"]:
            return
        geometry_checked["done"] = True
        if not geometry.restored_position:
            return
        try:
            result = win_geometry.ensure_visible(_window_handle(window))
            _trace("窗口位置校验: %s" % result)
            if result == "recentered":
                # 事件回调里记的还是旧坐标，必须用窗口实际值覆盖
                geometry.sync_from(window)
        except Exception as exc:
            _trace("窗口位置校验失败: %r" % (exc,))

    window.events.shown += _verify_geometry

    # 窗口显示后才能拿到稳定的 HWND；事件回调在独立线程里执行。
    window.events.shown += _start_native_icons

    # 页面可绘制后再把窗口露出来。
    #
    # 为什么是 _pywebviewready 而不是 loaded：pywebview 在 loaded 之前就把
    # bridge 注入并执行完，此刻 DOM 已完整、样式已应用；用公开事件 loaded
    # 兜底（两者几乎同时置位），再加一个 5 秒兜底线程，避免页面加载异常时
    # 窗口永远不出现。
    revealed = {"done": bool(instant_window)}

    def _reveal_window(*_):
        if revealed["done"]:
            return
        revealed["done"] = True
        try:
            window.show()
            _trace("窗口显示: main 后 %.2fs（不含 PyInstaller 解包时间）"
                   % (time.perf_counter() - t_start))
        except Exception as exc:
            # show() 内部要等 shown 事件；隐藏窗口在创建阶段已经触发过一次，
            # 正常不会超时。真失败也不能让启动流程崩掉。
            _trace("窗口显示失败: %r" % (exc,))

    ready_event = getattr(window.events, "_pywebviewready", None)
    if ready_event is not None:
        ready_event += _reveal_window
    window.events.loaded += _reveal_window

    def _reveal_fallback():
        time.sleep(5)
        if not revealed["done"]:
            _trace("窗口显示兜底触发：页面迟迟未就绪")
            _reveal_window()

    threading.Thread(target=_reveal_fallback, daemon=True).start()

    # UI 看门狗：窗口创建后开始等主窗口，连续几次 WM_NULL 超时就退出，
    # 避免卡死的 WebView2 窗口把 DWM/任务栏一起拖住。
    if (win_guard is not None
            and os.environ.get("WORKBUDDY_WATCHDOG", "1") != "0"):
        try:
            watchdog = win_guard.UIWatchdog(
                os.path.join(_diag_dir(), "watchdog.log"))
            watchdog.start()
            # 前端心跳直接喂给看门狗；这是判断 UI 线程是否还活着的第三路信号
            api._watchdog = watchdog
        except Exception as exc:
            _trace("UI 看门狗启动失败: %r" % (exc,))
            # 构造失败的场景 watchdog 仍是 None；构造成功但 start 失败的
            # 场景由 finally 里的 stop() 兜底，不必在这里重复赋值。

    # 仅调试模式：5 秒后主动问 DOM 要状态（见 _probe）
    threading.Thread(target=_probe, args=(window,), daemon=True).start()

    # gui="edgechromium" 是必须的，不能省。
    # pywebview 6.2.1 在这台机器上自动后端探测会失败（webview.guilib 一直是 None），
    # start() 表现为静默挂起、不建窗口也不报错，只能靠显式指定后端绕开。
    #
    # storage_path 也必须显式给：private_mode 下 pywebview 会把 WebView2
    # 缓存丢进 %TEMP%\\tmpXXXX，每次启动新建一份、退出也不回收——实测攒了
    # 上百份、1.4 GB。固定到数据目录后只保留一份，也便于用户统一清理。
    try:
        webview.start(gui="edgechromium", debug=False,
                      icon=icon_path(initial_theme["dark"]),
                      private_mode=True,
                      storage_path=os.path.join(_diag_dir(), "webview2"))
    finally:
        manager = icon_holder["manager"]
        if manager is not None:
            try:
                manager.stop()
            except Exception as exc:
                _trace("原生图标清理失败: %r" % (exc,))
        if watchdog is not None:
            watchdog.stop()
        if guard_handle is not None and win_guard is not None:
            win_guard.release_single_instance(guard_handle)
    _trace("webview.start 返回（窗口已关闭）")


if __name__ == "__main__":
    # 计划任务走无窗口服务分支，不创建 WebView2，也不触发单实例锁。
    # 三个入口一一对应三张计划任务卡片，任务运行时完全不依赖系统 Python。
    _entry = sys.argv[1] if len(sys.argv) > 1 else ""
    if _entry == "--service":
        sys.exit(_service_main(sys.argv[2:]))
    if _entry == "--qoder-service":
        sys.exit(_side_service_main("qoder-service", _load_qoder,
                                    sys.argv[2:]))
    if _entry == "--trae-service":
        sys.exit(_side_service_main("trae-service", _load_trae,
                                    sys.argv[2:]))

    # edgechromium 后端会接管 stdout/stderr，异常信息经常整个丢失。
    # 自建一份文件日志，出问题时至少还有痕迹。路径见 _crash_path()：
    # 绝不能写进 ASSETS（打包后那是临时解包目录，退出即删）。
    try:
        main()
    except Exception:
        import traceback
        try:
            with open(_crash_path(), "a", encoding="utf-8") as fh:
                fh.write(traceback.format_exc() + "\n")
        except Exception:
            pass
        raise