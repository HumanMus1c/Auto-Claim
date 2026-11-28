"""计划任务专用的客户端拉起助手。

定时签到只依赖本机登录态；如果目标桌面端没有运行，Electron 客户端可能不会
及时刷新凭据，托盘图标也不在。签到脚本在确认「真的要领取」或凭据需要刷新
时才会调用本模块（探测「是否已签到」本身是纯 HTTP，不经过这里）：

* 已在运行：什么都不做；
* 未运行：按环境变量 / 注册表 / 常见安装目录找到可执行文件，用隐藏窗口的
  方式启动，并在启动后把顶层窗口 ``ShowWindow(SW_HIDE)``，尽量只留托盘图标；
* 找不到或启动失败：返回状态，由调用方继续签到，不阻断主流程。

仅 Windows 生效。其他平台返回 ``unsupported``，不会尝试启动桌面端。
设置 ``AUTO_CLAIM_LAUNCH_CLIENT=0`` 可整体关闭该行为。

计划任务的默认 ``Priority=7`` 会让宿主进程以 ``BELOW_NORMAL_PRIORITY_CLASS``
启动，子进程（桌面端）还会继承 IoPriority / MemoryPriority 的降权值。拉起时显式
指定 ``NORMAL_PRIORITY_CLASS``，并把 Io/Memory 优先级改回正常值（2 / 5），避免
任务结束后桌面端长期处于"后台 I/O + 低内存优先级"的半死状态。
``AUTO_CLAIM_LAUNCH_VIA_SHELL=1`` 可改为委托 explorer.exe 启动，彻底断开优先级
与 Job 继承链（代价是无法用 STARTF_USESHOWWINDOW 隐藏首个窗口，仍由后续扫描兜底）。
"""

import ctypes
import ntpath
import os
import re
import subprocess
import sys
import time

try:
    import ctypes.wintypes as _wintypes
except Exception:  # pragma: no cover - 非 Windows 上的保守兜底
    _wintypes = None

IS_WINDOWS = sys.platform == "win32"
ENV_SWITCH = "AUTO_CLAIM_LAUNCH_CLIENT"
ENV_SHELL_LAUNCH = "AUTO_CLAIM_LAUNCH_VIA_SHELL"

# 启动后最多等多久；Electron 冷启动通常 2-5 秒，留足余量但不拖垮任务预算。
DEFAULT_WAIT_SECONDS = 12.0
# 进程消失这么久就认为启动失败，不必空等到超时。
PROCESS_GONE_SECONDS = 2.0
# 隐藏过一个窗口后，再观察这么久没有新窗口才结束。
QUIET_SECONDS = 1.5
POLL_SECONDS = 0.25

TH32CS_SNAPPROCESS = 0x00000002
SW_HIDE = 0
DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
# 计划任务默认 Priority=7 对应 BELOW_NORMAL_PRIORITY_CLASS；显式拉回正常优先级。
NORMAL_PRIORITY_CLASS = 0x00000020
PROCESS_SET_INFORMATION = 0x0200
PROCESS_MEMORY_PRIORITY = 28   # PROCESSINFOCLASS
PROCESS_IO_PRIORITY = 33       # PROCESSINFOCLASS
IO_PRIORITY_NORMAL = 2
MEMORY_PRIORITY_NORMAL = 5
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_CLIENTS = {
    "workbuddy": {
        "processes": ("workbuddy.exe",),
        "env": "WORKBUDDY_EXE",
        "registry_tokens": ("workbuddy",),
        "exe_names": ("WorkBuddy.exe",),
        "folders": ("WorkBuddy", "WorkBuddyAI"),
    },
    "qoder": {
        "processes": ("qoder cn.exe", "qoder.exe"),
        "env": "QODER_EXE",
        "registry_tokens": ("qoder",),
        "exe_names": ("Qoder CN.exe", "Qoder.exe"),
        "folders": ("Qoder CN", "Qoder"),
    },
    "trae": {
        "processes": ("trae cn.exe", "trae.exe", "trae solo cn.exe", "trae solo.exe"),
        "env": "TRAE_EXE",
        "registry_tokens": ("trae",),
        "exe_names": ("Trae CN.exe", "Trae.exe", "TRAE SOLO CN.exe", "TRAE SOLO.exe"),
        "folders": ("Trae CN", "TRAE CN", "Trae", "TRAE SOLO CN", "TRAE SOLO"),
    },
}

_ICON_SUFFIX = re.compile(r",\s*-?\d+\s*$")


def _enabled():
    value = os.environ.get(ENV_SWITCH, "").strip().lower()
    return value not in ("0", "false", "no", "off")


def _is_file(path):
    return bool(path) and os.path.isfile(path)


def _executable(path):
    """规范化并验证一个候选路径；Windows 上只要求是文件。"""
    if not path:
        return None
    path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path).strip().strip('"'))))
    if not _is_file(path):
        return None
    if os.name != "nt" and not os.access(path, os.X_OK):
        return None
    return path


def _kernel32():
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = (_wintypes.DWORD, _wintypes.DWORD)
    kernel32.Process32FirstW.restype = _wintypes.BOOL
    kernel32.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    kernel32.Process32NextW.restype = _wintypes.BOOL
    kernel32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    kernel32.CloseHandle.restype = _wintypes.BOOL
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    return kernel32


_PROCESS_ENTRY = None


def _process_entry_type():
    global _PROCESS_ENTRY
    if _PROCESS_ENTRY is None:
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", _wintypes.DWORD),
                ("cntUsage", _wintypes.DWORD),
                ("th32ProcessID", _wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", _wintypes.DWORD),
                ("th32Threads", _wintypes.DWORD),
                ("th32ParentProcessID", _wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", _wintypes.DWORD),
                ("szExeFile", _wintypes.WCHAR * 260),
            ]

        _PROCESS_ENTRY = PROCESSENTRY32W
    return _PROCESS_ENTRY


def _process_names(kind):
    spec = _CLIENTS.get(kind)
    return set(spec["processes"]) if spec else set()


def _client_pids(kind):
    """返回目标客户端所有同名进程的 PID；包括 Electron 的子进程。"""
    names = _process_names(kind)
    if not IS_WINDOWS or not names or _wintypes is None:
        return set()
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return set()
    pids = set()
    try:
        entry = _process_entry_type()()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            if str(name).lower() in names:
                pids.add(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return pids


def is_client_running(kind):
    """目标客户端是否已有任意同名进程在运行。"""
    return bool(_client_pids(str(kind or "").strip().lower()))


def _clean_registry_path(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith('"'):
        end = value.find('"', 1)
        if end > 0:
            value = value[1:end]
    else:
        value = _ICON_SUFFIX.sub("", value).strip()
    return os.path.expandvars(value.strip().strip('"'))


def _paths_from_value(value, spec):
    """把 InstallLocation / DisplayIcon 转成可执行文件候选。"""
    path = _clean_registry_path(value)
    if not path:
        return []
    if os.path.isdir(path):
        return [os.path.join(path, name) for name in spec["exe_names"]]
    ext = os.path.splitext(path)[1].lower()
    if ext == ".exe":
        return [path]
    # Qoder 的 DisplayIcon 指向 uninstallerIcon.ico，可执行文件在同一目录。
    parent = os.path.dirname(path)
    return [os.path.join(parent, name) for name in spec["exe_names"]] if parent else []


def _registry_candidates(spec):
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except Exception:
        return []
    candidates = []
    views = [0]
    for name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        value = getattr(winreg, name, 0)
        if value and value not in views:
            views.append(value)
    roots = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]
    for root in roots:
        for view in views:
            access = winreg.KEY_READ | view
            try:
                key = winreg.OpenKey(
                    root, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0, access)
            except OSError:
                continue
            try:
                count = winreg.QueryInfoKey(key)[0]
                for index in range(count):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, index), 0, access) as sub:
                            try:
                                display = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                            except OSError:
                                continue
                            lowered = display.lower()
                            if not any(token in lowered for token in spec["registry_tokens"]):
                                continue
                            for field in ("InstallLocation", "DisplayIcon"):
                                try:
                                    value = winreg.QueryValueEx(sub, field)[0]
                                except OSError:
                                    continue
                                candidates.extend(_paths_from_value(value, spec))
                    except OSError:
                        # 删除中的项、无权限的子键都是正常情况。
                        continue
            except OSError:
                continue
            finally:
                try:
                    winreg.CloseKey(key)
                except OSError:
                    pass
    return candidates


def _common_candidates(spec):
    direct_roots = []
    scan_roots = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        direct_roots.append(local)
        programs = os.path.join(local, "Programs")
        direct_roots.append(programs)
        scan_roots.append(programs)
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if base:
            direct_roots.append(base)
            scan_roots.append(base)
    candidates = []
    for root in direct_roots:
        for folder in spec["folders"]:
            for name in spec["exe_names"]:
                candidates.append(os.path.join(root, folder, name))
    # 兜底：安装目录名带版本号或别名时，扫一层常见根目录。
    for root in scan_roots:
        try:
            entries = os.scandir(root)
        except OSError:
            continue
        try:
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                    lowered = entry.name.lower()
                    if not any(folder.lower() in lowered for folder in spec["folders"]):
                        continue
                    for name in spec["exe_names"]:
                        candidates.append(os.path.join(entry.path, name))
                except OSError:
                    continue
        finally:
            entries.close()
    return candidates


def find_client_exe(kind):
    """按环境变量、注册表、常见安装目录的顺序定位客户端可执行文件。

    显式设置了 ``WORKBUDDY_EXE`` / ``QODER_EXE`` / ``TRAE_EXE`` 但路径无效时
    直接返回 ``None``，不再回退到别的安装——避免拉起一个与配置不符的程序。
    """
    spec = _CLIENTS.get(str(kind or "").strip().lower())
    if not spec:
        return None
    override = os.environ.get(spec["env"])
    if override:
        return _executable(override)
    candidates = _registry_candidates(spec) + _common_candidates(spec)
    # 同名安装可能有多个版本（例如 WorkBuddy 的旧 WorkBuddyAI.exe）；优先
    # 选择与客户端主程序同名的那一份，避免拉起一个已经废弃的副本。
    order = {name.lower(): index for index, name in enumerate(spec["exe_names"])}
    candidates.sort(key=lambda path: order.get(
        os.path.basename(path).lower(), len(order)))
    seen = set()
    for path in candidates:
        path = _executable(path)
        if not path:
            continue
        key = ntpath.normcase(path) if os.name == "nt" else os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        return path
    return None


def _clean_env():
    """计划任务环境里如果残留 ELECTRON_RUN_AS_NODE，Electron 会当 Node 跑完就退。"""
    return {key: value for key, value in os.environ.items()
            if key.upper() != "ELECTRON_RUN_AS_NODE"}


def _shell_launch_enabled():
    value = os.environ.get(ENV_SHELL_LAUNCH, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _launch_via_shell(exe):
    """委托桌面外壳（explorer.exe）启动客户端。

    explorer.exe 会把请求交给当前登录会话里已有的外壳实例，由它创建客户端进程。
    外壳不在计划任务的优先级链与 Job 里，因此优先级、IoPriority、MemoryPriority
    全部是桌面上的正常值——这是比事后改信息类更彻底的断开方式。
    返回的是短暂的 explorer.exe 进程；真正的客户端会继续存活，由后续窗口扫描兜底。
    """
    kwargs = {
        "cwd": os.path.dirname(os.path.abspath(exe)) or None,
        "env": _clean_env(),
        "close_fds": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    proc = subprocess.Popen(["explorer.exe", exe], **kwargs)
    return proc


def _reset_child_priority(pid):
    """把子进程从计划任务继承来的 Io/Memory 优先级恢复为正常值。

    Windows 会在 CreateProcess 时让子进程继承父进程的 IoPriority 与 MemoryPriority；
    仅靠 NORMAL_PRIORITY_CLASS 只能改优先级类，改不了这两项。这里用
    ``SetProcessInformation`` 设 MemoryPriority=5，用 ``NtSetInformationProcess``
    设 IoPriority=2。任何失败都只忽略：不同 Windows 版本对这些信息类的开放程度不同，
    不能因为改优先级失败而让桌面端彻底起不来。
    """
    if not IS_WINDOWS or not pid:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, int(pid))
        if not handle or handle == INVALID_HANDLE_VALUE:
            return False
        changed = False
        try:
            # MEMORY_PRIORITY_INFORMATION { ULONG MemoryPriority; }
            class MEMORY_PRIORITY_INFORMATION(ctypes.Structure):
                _fields_ = [("MemoryPriority", ctypes.c_ulong)]

            info = MEMORY_PRIORITY_INFORMATION()
            info.MemoryPriority = MEMORY_PRIORITY_NORMAL
            kernel32.SetProcessInformation.argtypes = (
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong)
            kernel32.SetProcessInformation.restype = ctypes.c_int
            if kernel32.SetProcessInformation(
                    handle, PROCESS_MEMORY_PRIORITY,
                    ctypes.byref(info), ctypes.sizeof(info)):
                changed = True

            # ProcessIoPriority 没有公开的 kernel32 包装，只能走 ntdll。
            io = ctypes.c_ulong(IO_PRIORITY_NORMAL)
            ntdll.NtSetInformationProcess.argtypes = (
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong)
            ntdll.NtSetInformationProcess.restype = ctypes.c_long  # NTSTATUS
            # NTSTATUS >= 0 即成功（含 INFO 级别的正数返回值）。
            if ntdll.NtSetInformationProcess(
                    handle, PROCESS_IO_PRIORITY,
                    ctypes.byref(io), ctypes.sizeof(io)) >= 0:
                changed = True
        finally:
            kernel32.CloseHandle(handle)
        return changed
    except Exception:
        return False


def _launch(exe):
    """以隐藏窗口、脱离任务进程、正常优先级的方式启动客户端。

    优先级类在 CreateProcess 时通过 NORMAL_PRIORITY_CLASS 直接指定；IoPriority 和
    MemoryPriority 在进程创建后由 ``_reset_child_priority`` 改回正常值。若设置了
    ``AUTO_CLAIM_LAUNCH_VIA_SHELL=1``，则改走 explorer.exe 委托，从源头断开继承。
    """
    if IS_WINDOWS and _shell_launch_enabled():
        try:
            return _launch_via_shell(exe)
        except OSError:
            # 外壳未运行或 explorer.exe 不可用时，退回下面的直接启动路径。
            pass

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = SW_HIDE
    base_flags = DETACHED_PROCESS | NORMAL_PRIORITY_CLASS
    kwargs = {
        "cwd": os.path.dirname(os.path.abspath(exe)) or None,
        "env": _clean_env(),
        "close_fds": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "startupinfo": startupinfo,
        "creationflags": base_flags | CREATE_BREAKAWAY_FROM_JOB,
    }
    try:
        proc = subprocess.Popen([exe], **kwargs)
    except OSError:
        # 任务计划程序可能把子进程放进不允许 breakaway 的 Job；退回普通隐藏启动，
        # 但 NORMAL_PRIORITY_CLASS 必须保留，否则又变回继承来的降权值。
        kwargs["creationflags"] = base_flags
        proc = subprocess.Popen([exe], **kwargs)
    _reset_child_priority(getattr(proc, "pid", None))
    return proc


def _hide_windows_for_pids(pids):
    if not pids or not IS_WINDOWS or _wintypes is None:
        return 0
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(
        _wintypes.BOOL, _wintypes.HWND, _wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, _wintypes.LPARAM)
    user32.EnumWindows.restype = _wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = (
        _wintypes.HWND, ctypes.POINTER(_wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = _wintypes.DWORD
    user32.IsWindowVisible.argtypes = (_wintypes.HWND,)
    user32.IsWindowVisible.restype = _wintypes.BOOL
    user32.ShowWindowAsync.argtypes = (_wintypes.HWND, ctypes.c_int)
    user32.ShowWindowAsync.restype = _wintypes.BOOL

    hidden = [0]

    def callback(hwnd, _lparam):
        pid = _wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return True
        if int(pid.value) not in pids:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        user32.ShowWindowAsync(hwnd, SW_HIDE)
        hidden[0] += 1
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return hidden[0]


def _hide_client_windows(kind, wait):
    wait = DEFAULT_WAIT_SECONDS if wait is None else max(0.0, float(wait))
    started = time.monotonic()
    deadline = started + wait
    seen_window = False
    last_hidden = 0.0
    missing_since = None
    while True:
        pids = _client_pids(kind)
        if pids:
            missing_since = None
            count = _hide_windows_for_pids(pids)
            if count:
                seen_window = True
                last_hidden = time.monotonic()
        else:
            now = time.monotonic()
            if missing_since is None:
                missing_since = now
            elif now - missing_since >= PROCESS_GONE_SECONDS:
                return
        now = time.monotonic()
        if seen_window and now - last_hidden >= QUIET_SECONDS:
            return
        if now >= deadline:
            return
        time.sleep(POLL_SECONDS)


def ensure_client_running(kind, exe=None, wait=None):
    """确保目标客户端在运行。

    返回 ``running`` / ``launched`` / ``not-found`` / ``unsupported`` /
    ``disabled`` / ``error``。调用方不需要根据返回值改变签到流程。
    """
    kind = str(kind or "").strip().lower()
    if not _enabled():
        return "disabled"
    if not IS_WINDOWS or _wintypes is None:
        return "unsupported"
    if kind not in _CLIENTS:
        return "unsupported"
    try:
        if is_client_running(kind):
            return "running"
    except Exception:
        return "error"
    try:
        path = _executable(exe) if exe else find_client_exe(kind)
    except Exception:
        return "error"
    if not path:
        return "not-found"
    try:
        _launch(path)
    except Exception:
        return "error"
    try:
        _hide_client_windows(kind, wait)
    except Exception:
        # 窗口隐藏失败不应影响签到；客户端至少已经启动。
        pass
    return "launched"
