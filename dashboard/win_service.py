"""Windows 计划任务（脚本服务）管理。

控制台把后台签到交给两个固定名称的 WorkBuddy 计划任务：

  WorkBuddyAutoSignin  每天 1 次（基准 00:05），完整签到 + 成长中心
  WorkBuddyGrowthPoll  每天 6 次（基准 01/05/09/13/17/21 点），补签 + 成长中心

Qoder CN 使用另一个独立任务，互不影响：

  QoderDailyCredits    每天 5 次（UTC+8 10:05 起），领取 100 Credits

**触发时刻是随机的**：固定整点最容易被判定成脚本行为，所以每个基准点都会
按 schedule_jitter 的三层系统熵源（系统时钟纳秒位、开机至今的分钟数、
10 分钟倍数的其他系统事件）打散到 ±15 分钟内。任务每跑完一次就重排一次，
每个点在它自己跑完的当天被换掉、第二天生效，因此第二天零点时整组时刻
已经全部是新的。

本模块只通过 PowerShell 的 ScheduledTasks 模块操作这些任务；任务名、
触发器和动作全部来自下面的常量，前端不能传入任意命令。

WorkBuddy 打包后直接运行控制台 exe 的 ``--service`` 模式（无窗口、无需系统
Python）；源码运行时运行 ``pythonw.exe app.py --service``。两种方式最终都
调用 ``signin.py`` 的 ``_run()``，与手工执行 ``install-windows.ps1`` 等价。
Qoder 任务则直接运行 ``pythonw.exe qoder_signin.py silent``，需要系统 Python。
"""

import base64
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import time

try:
    import schedule_jitter
except ImportError:
    # 冻结后 sys.path 不一定包含 dashboard/，按本文件位置补一次
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import schedule_jitter

# 轮询基准点。随机化只在这些基准点附近浮动，不改变「一天覆盖 6 个时段」
# 的结构。刻意用 6 个独立的 Daily 触发器，而不是「Daily + 每 4 小时重复」：
# 后者一旦错过（关机/睡眠）就永久跳过，StartWhenAvailable 不会补跑。
POLL_TIMES = ("01:00", "05:00", "09:00", "13:00", "17:00", "21:00")

# 每日签到的基准点：覆盖整天，而不是只压在凌晨。
# 签到在 UTC+8 00:00 重置，只有 00:05 一个点时电脑一睡过去当天就没机会了。
# 这里每 4 小时一个点，正好落在轮询任务（01/05/09/13/17/21）的间隙中点上，
# 两个任务合起来是「每 2 小时一次」的均匀覆盖：
#   00:05 01:00 03:00 05:00 07:00 09:00 11:00 13:00 15:00 17:00 19:00 21:00 23:00
SIGNIN_TIMES = ("00:05", "03:00", "07:00", "11:00",
                "15:00", "19:00", "23:00")

# 与 install-windows.ps1 / README 中的任务保持一致。
# base_times 是随机化的锚点，实际注册的时刻由 random_times() 生成；
# forward=True 表示只允许向后浮动——第一个点往前漂会跨过午夜，被算进前一天。
TASKS = (
    {
        "name": "WorkBuddyAutoSignin",
        "label": "每日签到",
        "when": "每天 7 次",
        "base_times": SIGNIN_TIMES,
        "forward": True,
        "what": "签到 + 成长中心",
        "action": "silent",
        "minutes": 10,
        "description": "WorkBuddy daily auto signin (silent, zero token)",
    },
    {
        "name": "WorkBuddyGrowthPoll",
        "label": "轮询补签",
        "when": "每天 6 次",
        "base_times": POLL_TIMES,
        "forward": False,
        "what": "补签 + 成长中心全套",
        "action": "silent-poll",
        "minutes": 5,
        "description": "WorkBuddy poll (catch-up signin + growth center)",
    },
)

# Qoder CN 是独立任务，和 WorkBuddy 的两个任务互不影响。
# 触发点由 qoder_trigger_times() 按 UTC+8 10:00 刷新时间换算成本机时间。
QODER_TASKS = (
    {
        "name": "QoderDailyCredits",
        "label": "每日 Credits",
        "when": "每天 8 次",
        "forward": True,
        "what": "查询并领取 Qoder CN 每日 100 Credits",
        "action": "silent",
        "minutes": 5,
        "description": "Qoder CN daily 100 Credits claim (silent, zero token)",
    },
)

TASK_BY_NAME = {item["name"]: item for item in TASKS}
TASK_NAMES = tuple(TASK_BY_NAME)
QODER_TASK_BY_NAME = {item["name"]: item for item in QODER_TASKS}
QODER_TASK_NAMES = tuple(QODER_TASK_BY_NAME)

# Trae 多账号签到独立任务；触发点由 trae_trigger_times() 按设置里的主时间
# 展开，默认 08:05 起每 1~2 小时一次，覆盖到晚上。
TRAE_TASKS = (
    {
        "name": "TraeDailyCheckin",
        "label": "每日签到",
        "when": "每天 8 次",
        "forward": True,
        "what": "多账号签到 + 剩余积分",
        "action": "silent",
        "minutes": 10,
        "description": "Trae multi-account daily checkin (silent, zero token)",
    },
)
TRAE_TASK_BY_NAME = {item["name"]: item for item in TRAE_TASKS}
TRAE_TASK_NAMES = tuple(TRAE_TASK_BY_NAME)

# 主触发点之后的小时偏移。间隔控制在 1~2 小时：睡眠错过一两个点也能在醒来
# 后一小时内补上，不依赖 StartWhenAvailable 的「只补最近一次」。
TRAE_OFFSETS = (0, 1, 3, 5, 7, 9, 11, 13)

# Qoder 活动按 UTC+8 每天 10:00 刷新，刷新前签了也领不到，所以基准点从
# 刷新 +5 分钟开始往后再排 7 次。刷新有硬门槛，只做正向浮动、绝不提前。
QODER_OFFSETS = (0, 1, 3, 5, 7, 9, 11, 13)

ACTIONS = ("enable", "disable", "run", "stop", "uninstall")

CREATE_NO_WINDOW = 0x08000000


def random_times(spec):
    """按 spec 的基准点生成一整套随机时刻（安装/修复时用）。

    每个任务拿到的偏移互相独立；``forward=True`` 的任务只会向后浮动。
    """
    base = spec.get("base_times") or POLL_TIMES
    return schedule_jitter.jitter_times(
        base, spec["name"], forward=bool(spec.get("forward")))


def _when_text(spec, times):
    """卡片第一行的「什么时候跑」，带上实际时刻。

    时刻是随机生成的，写死成「每天 00:05」就是在骗人——任务其实每天都不
    在同一分钟醒。卡片宽度放不下 6 个时刻（`.svc-line` 是 nowrap + 省略号），
    所以这里只露前两个，完整排期由前端放进 title。
    """
    label = spec.get("when") or ""
    if not times:
        return label
    head = " / ".join(times[:2])
    more = "" if len(times) <= 2 else " 等 %d 个时刻" % len(times)
    return "%s（%s%s）" % (label, head, more)


def _as_text_list(value):
    """PowerShell 单元素数组可能退化成裸字符串，统一成列表。"""
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(item) for item in value or [] if str(item).strip()]

# 失败原因到「用户能照着做的一句话」的映射。
# PowerShell 的原始报错（中文/英文/HRESULT）对普通用户毫无意义，界面卡片上
# 必须给出可读结论 + 处置动作；原始文本只折叠进「详情」备查。
SERVICE_HINTS = {
    "python-missing": "安装 Python 3 后重试：命令行执行 "
                      "winget install -e --id Python.Python.3.12，"
                      "或从 python.org 下载安装（勾选 Add python.exe to PATH）。"
                      "装到默认目录后可直接点「安装 / 修复」，不必重启控制台。",
    "access-denied": "Windows 拒绝写入计划任务。请确认安全软件没有拦截 schtasks；"
                     "仍失败时用同一个 Windows 账号打开「管理员 PowerShell」重试，"
                     "或检查组策略里对任务计划程序的限制。",
    "no-powershell": "系统缺少 PowerShell。请在「启用或关闭 Windows 功能」中确认 "
                     "Windows PowerShell 未被移除，或安装 PowerShell 7 后重试。",
    "timeout": "PowerShell 操作超时。请稍后重试；若反复超时，检查安全软件是否拖慢系统，"
               "并到「任务计划程序」里手动确认同名任务的状态。",
    "target-missing": "计划任务要运行的程序或脚本不在预期位置。请确认控制台 exe "
                      "与 signin.py / qoder_signin.py / trae_signin.py 放在同一目录。",
    "verify-failed": "任务写入后回读失败。请在「任务计划程序」中查看同名任务是否被"
                     "安全软件锁定或已被删除，然后重试。",
    "ps-failed": "PowerShell 执行失败。请展开「详情」查看原始报错；"
                 "常见原因是同名任务被其他程序占用或受系统策略限制。",
    "file-missing": "缺少脚本文件。请确认控制台 exe 与 signin.py / qoder_signin.py / "
                    "trae_signin.py 放在同一目录。",
    "unsupported": "计划任务只能注册在 Windows 上；当前系统不支持，"
                   "请在 Windows 机器上使用控制台。",
    "unknown": "请展开「详情」复制报错内容后重试；仍失败可到 GitHub 提交 issue。",
}


class ServiceError(RuntimeError):
    """带「可读结论 + 解决办法」的失败。

    code 让前端能分支（例如缺 Python 时高亮「去安装」），message 是卡片上
    的一行结论，hint 是处置动作，detail 是 PowerShell / 系统原文。三者分开
    存，是因为它们面向的读者不同：用户看 message + hint，排查看 detail。
    """

    def __init__(self, message, code="unknown", hint=None, detail=None):
        super().__init__(message)
        self.code = code
        self.hint = hint or SERVICE_HINTS.get(code) or SERVICE_HINTS["unknown"]
        self.detail = detail

    def payload(self):
        """转成前端可直接渲染的结构。"""
        out = {"ok": False, "error": str(self), "code": self.code,
               "hint": self.hint}
        if self.detail:
            out["detail"] = self.detail
        return out


def _classify(detail):
    """把系统原始报错归类成 (code, 可读结论)。"""
    text = detail or ""
    low = text.lower()
    if "拒绝访问" in text or "access is denied" in low or "0x80070005" in low:
        return "access-denied", "Windows 拒绝写入计划任务（拒绝访问）"
    if "pythonw" in low or "python3" in low or "python 3" in low:
        return "python-missing", "缺失 Python 3 运行环境"
    if ("找不到" in text or "not found" in low or "cannot find" in low
            or "0x80070002" in low):
        return "target-missing", "找不到计划任务要运行的程序或脚本"
    return "ps-failed", "计划任务操作失败"


def _error_payload(exc):
    """把任意异常整理成前端可直接渲染的失败结构。

    ServiceError 自带分类与处置建议；其它异常（OSError、JSONDecodeError 等）
    现场再归类，避免上层到处写 isinstance 分支。
    """
    if isinstance(exc, ServiceError):
        return exc.payload()
    detail = "%s: %s" % (type(exc).__name__, exc)
    code, message = _classify(detail)
    if code == "ps-failed":
        message = detail
    return {"ok": False, "error": message, "code": code,
            "hint": SERVICE_HINTS.get(code) or SERVICE_HINTS["unknown"],
            "detail": detail}


def describe_error(exc):
    """公开入口：给控制台的失败统一成可读结构（供 app.py 调用）。"""
    return _error_payload(exc)

_PREAMBLE = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
"""


def supported():
    """计划任务管理只在 Windows 上可用。"""
    return os.name == "nt"


def _powershell():
    """定位 Windows PowerShell，找不到再退回 PowerShell 7。"""
    if not supported():
        return None
    for name in ("powershell.exe", "pwsh.exe"):
        path = shutil.which(name)
        if path:
            return path
    root = os.environ.get("SystemRoot", r"C:\Windows")
    path = os.path.join(root, "System32", "WindowsPowerShell", "v1.0",
                        "powershell.exe")
    return path if os.path.isfile(path) else None


def _run_ps(script, timeout=40):
    """执行一段 PowerShell，返回 stdout。脚本通过 UTF-16 编码传递，避开引号地狱。"""
    ps = _powershell()
    if not ps:
        raise ServiceError("未找到 PowerShell，无法管理计划任务",
                           "no-powershell")

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    cmd = [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-EncodedCommand", encoded]

    kwargs = {}
    if supported():
        kwargs["creationflags"] = CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise ServiceError("PowerShell 操作超时（%d 秒）" % timeout, "timeout")

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:] or "无输出"
        code, message = _classify(detail)
        raise ServiceError(message, code, detail=detail)
    return proc.stdout or ""


def _parse_json(text):
    """从 PowerShell 输出中取最后一行 JSON（忽略 CLIXML 等噪声）。"""
    for line in reversed((text or "").splitlines()):
        line = line.strip().lstrip("\ufeff")
        if not line or line[0] not in "{[":
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def _ps_quote(value):
    """把字符串安全地嵌进 PowerShell 单引号字面量。"""
    return "'" + str(value).replace("'", "''") + "'"


def _quote_arg(value):
    """按 Windows 命令行规则给一个参数加引号。"""
    value = str(value)
    if not value:
        return '""'
    if any(ch.isspace() for ch in value) or '"' in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _tail_line(path, limit=8192):
    """只读文件末尾，取最后一条非空行。避免每次轮询都扫完整份日志。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            data = fh.read()
    except OSError:
        return ""
    lines = [line.strip() for line in data.decode("utf-8", "replace").splitlines()
             if line.strip()]
    return lines[-1] if lines else ""


def _log_info(path):
    """返回 signin.log 的路径、修改时间与最后一行，供界面显示最近活动。"""
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return {
        "path": path,
        "at": time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(stat.st_mtime)),
        "line": _tail_line(path)[:400],
    }


def _status_script(specs=TASKS):
    names = ",".join(_ps_quote(item["name"]) for item in specs)
    return _PREAMBLE + r"""
try {
    $tasks = @()
    foreach ($n in @(%s)) {
        $t = Get-ScheduledTask -TaskName $n -TaskPath '\' -ErrorAction SilentlyContinue
        if ($null -eq $t) {
            $tasks += [pscustomobject]@{ name = $n; installed = $false }
            continue
        }
        $i = Get-ScheduledTaskInfo -TaskName $n -TaskPath '\' -ErrorAction SilentlyContinue
        $state = [string]$t.State
        $last = $null
        $next = $null
        $result = $null
        if ($null -ne $i) {
            # 任务从未运行时 Task Scheduler 返回 1999-11-30，不是真实时间
            if ($null -ne $i.LastRunTime -and $i.LastRunTime.Year -gt 2000) {
                $last = $i.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss')
            }
            if ($null -ne $i.NextRunTime -and $i.NextRunTime.Year -gt 2000) {
                $next = $i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss')
            }
            $result = $i.LastTaskResult
        }
        $actions = @($t.Actions | ForEach-Object {
            (($_.Execute) + ' ' + ($_.Arguments)).Trim()
        })
        # 触发时刻是随机生成的，界面要显示真实排期而不是常量
        $times = @($t.Triggers | ForEach-Object {
            if ($_.StartBoundary) { ([datetime]$_.StartBoundary).ToString('HH:mm') }
        } | Sort-Object)
        $tasks += [pscustomobject]@{
            name = $n
            installed = $true
            state = $state
            enabled = ($state -ne 'Disabled')
            last_run = $last
            next_run = $next
            last_result = $result
            action = ($actions -join '; ')
            times = @($times)
        }
    }
    [pscustomobject]@{ ok = $true; tasks = $tasks } |
        ConvertTo-Json -Depth 5 -Compress
} catch {
    [pscustomobject]@{ ok = $false; error = $_.Exception.Message } |
        ConvertTo-Json -Compress
}
""" % names


def _empty_tasks(specs=TASKS):
    return [
        {
            "name": item["name"],
            "label": item["label"],
            "when": item["when"],
            "times": [],
            "what": item["what"],
            "installed": False,
            "enabled": False,
            "state": None,
            "last_run": None,
            "next_run": None,
            "last_result": None,
            "action": "",
        }
        for item in specs
    ]


def _normalize_status(raw, log_path=None, specs=TASKS):
    """把 PowerShell 的原始对象整理成前端直接可用的结构。"""
    if not isinstance(raw, dict):
        raise ServiceError("PowerShell 返回无法解析，未能读取计划任务状态",
                           "ps-failed")
    if not raw.get("ok"):
        detail = raw.get("error") or "读取计划任务状态失败"
        code, message = _classify(detail)
        raise ServiceError(message, code, detail=detail)

    spec_by_name = {item["name"]: item for item in specs}
    tasks = []
    for item in raw.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        spec = spec_by_name.get(name, {})
        installed = bool(item.get("installed"))
        state = item.get("state") if installed else None
        enabled = bool(installed and state != "Disabled")

        result = item.get("last_result")
        try:
            result = int(result) if result is not None else None
        except (TypeError, ValueError):
            result = None

        tasks.append({
            "name": name,
            "label": spec.get("label", name),
            "when": _when_text(spec, _as_text_list(item.get("times"))),
            "times": _as_text_list(item.get("times")),
            "what": spec.get("what", ""),
            "installed": installed,
            "enabled": enabled,
            "state": state,
            "last_run": item.get("last_run"),
            # 停用状态下 Task Scheduler 仍会返回一个 next_run，展示出来会误导
            "next_run": None if state == "Disabled" else item.get("next_run"),
            "last_result": result,
            "action": item.get("action") or "",
        })

    # PowerShell 输出不完整时也保证清单里的任务都出现在界面上
    seen = {t["name"] for t in tasks}
    for placeholder in _empty_tasks(specs):
        if placeholder["name"] not in seen:
            tasks.append(placeholder)

    installed = [t for t in tasks if t["installed"]]
    enabled = [t for t in installed if t["enabled"]]
    running = [t for t in installed if t["state"] == "Running"]
    next_runs = sorted(t["next_run"] for t in installed if t["next_run"])
    last_runs = sorted(t["last_run"] for t in installed if t["last_run"])

    return {
        "supported": True,
        "ok": True,
        "tasks": tasks,
        "installed": len(installed) == len(specs),
        "installed_count": len(installed),
        "enabled": len(enabled) == len(specs),
        "running": bool(running),
        "next_run": next_runs[0] if next_runs else None,
        "last_run": last_runs[-1] if last_runs else None,
        "log": _log_info(log_path),
    }


def status(log_path=None, specs=TASKS):
    """读取计划任务的安装、启用、运行与最近执行信息。"""
    if not supported():
        return {
            "supported": False,
            "ok": False,
            "error": "计划任务管理仅支持 Windows",
            "tasks": _empty_tasks(specs),
            "installed": False,
            "installed_count": 0,
            "enabled": False,
            "running": False,
            "next_run": None,
            "last_run": None,
            "log": None,
        }
    try:
        raw = _parse_json(_run_ps(_status_script(specs), timeout=25))
        return _normalize_status(raw, log_path, specs)
    except Exception as exc:
        # 读不到状态也要把「为什么」和「怎么办」交给界面，否则卡片只会显示
        # 「ServiceError: ...」这种对用户没有信息量的东西。
        payload = _error_payload(exc)
        return {
            "supported": True,
            "ok": False,
            "error": payload["error"],
            "code": payload["code"],
            "hint": payload["hint"],
            "detail": payload.get("detail"),
            "tasks": _empty_tasks(specs),
            "installed": False,
            "installed_count": 0,
            "enabled": False,
            "running": False,
            "next_run": None,
            "last_run": None,
            "log": _log_info(log_path),
        }


def qoder_status(log_path=None):
    """读取 QoderDailyCredits 任务的安装、启用、运行与最近执行信息。"""
    return status(log_path, QODER_TASKS)


def _install_script(runner, prefix_args, workdir):
    base = " ".join(_quote_arg(arg) for arg in prefix_args)
    lines = [
        _PREAMBLE,
        "try {",
        "    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
        "    $principal = New-ScheduledTaskPrincipal -UserId $user "
        "-LogonType Interactive -RunLevel Limited",
        "    $exe = %s" % _ps_quote(runner),
        "    $wd = %s" % _ps_quote(workdir),
        "    $base = %s" % _ps_quote(base),
    ]

    for spec in TASKS:
        args = (base + " " + spec["action"]).strip()
        times = random_times(spec)
        lines += [
            "    $a = New-ScheduledTaskAction -Execute $exe "
            "-Argument %s -WorkingDirectory $wd" % _ps_quote(args),
            "    $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden "
            "-DontStopIfGoingOnBatteries -AllowStartIfOnBatteries "
            "-ExecutionTimeLimit (New-TimeSpan -Minutes %d)" % spec["minutes"],
            # 每个基准点各自随机，注册后时刻就固定下来，直到下次运行后的重排
            "    $triggers = @()",
            "    foreach ($t in @(%s)) {" % ",".join(_ps_quote(t) for t in times),
            "        $triggers += New-ScheduledTaskTrigger -Daily -At $t",
            "    }",
            "    Register-ScheduledTask -TaskName %s -TaskPath '\\' "
            "-Action $a -Trigger $triggers -Settings $s -Principal $principal "
            "-Description %s -Force | Out-Null"
            % (_ps_quote(spec["name"]), _ps_quote(spec["description"])),
        ]

    lines += [
        "    $verified = @()",
        "    foreach ($n in @(%s)) {"
        % ",".join(_ps_quote(name) for name in TASK_NAMES),
        "        $t = Get-ScheduledTask -TaskName $n -TaskPath '\\' -ErrorAction Stop",
        "        $i = Get-ScheduledTaskInfo -TaskName $n -TaskPath '\\' -ErrorAction Stop",
        "        $verified += [pscustomobject]@{",
        "            name = $n",
        "            state = [string]$t.State",
        "            next_run = if ($null -ne $i.NextRunTime -and $i.NextRunTime.Year -gt 2000) "
        "{ $i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }",
        "        }",
        "    }",
        "    [pscustomobject]@{",
        "        ok = $true",
        "        message = '已安装并启用 2 个计划任务（WorkBuddyAutoSignin / WorkBuddyGrowthPoll），触发时刻已随机化'",
        "        tasks = $verified",
        "    } | ConvertTo-Json -Depth 5 -Compress",
        "} catch {",
        "    [pscustomobject]@{ ok = $false; error = $_.Exception.Message } | "
        "ConvertTo-Json -Compress",
        "}",
    ]
    return "\n".join(lines)


def install(runner, prefix_args, workdir=None):
    """注册/修复两个计划任务，并回读确认。"""
    if not supported():
        raise ServiceError("计划任务管理仅支持 Windows", "unsupported")
    if not runner or not os.path.isfile(runner):
        raise ServiceError("找不到运行计划任务的程序，无法安装", "target-missing",
                           detail=str(runner))
    if workdir is None:
        workdir = os.path.dirname(os.path.abspath(runner))

    raw = _parse_json(_run_ps(_install_script(runner, prefix_args, workdir),
                              timeout=60))
    if not isinstance(raw, dict):
        raise ServiceError("PowerShell 返回无法解析，安装未完成", "ps-failed")
    if not raw.get("ok"):
        detail = raw.get("error") or "安装失败"
        code, text = _classify(detail)
        raise ServiceError(text, code, detail=detail)
    # 记下基准点，后续重排以它为锚（否则每天在上一天结果上再抽，会漂）
    remember_base("workbuddy",
                  {item["name"]: item["base_times"] for item in TASKS})
    return {
        "ok": True,
        "message": raw.get("message") or "已安装并启用计划任务",
        "tasks": raw.get("tasks") or [],
        "runner": "console",
    }


def _action_script(verb, specs=TASKS):
    if verb not in ACTIONS:
        raise ValueError("未知计划任务操作：%s" % verb)

    lines = [
        _PREAMBLE,
        "try {",
        "    $done = @()",
        "    $failed = @()",
        "    foreach ($n in @(%s)) {"
        % ",".join(_ps_quote(item["name"]) for item in specs),
        "        $t = Get-ScheduledTask -TaskName $n -TaskPath '\\' "
        "-ErrorAction SilentlyContinue",
        "        if ($null -eq $t) { $failed += ($n + '：未安装'); continue }",
        "        try {",
    ]

    if verb == "enable":
        lines += [
            "            Enable-ScheduledTask -TaskName $n -TaskPath '\\' "
            "-ErrorAction Stop | Out-Null",
            "            $done += $n",
        ]
    elif verb == "disable":
        lines += [
            "            Disable-ScheduledTask -TaskName $n -TaskPath '\\' "
            "-ErrorAction Stop | Out-Null",
            "            $done += $n",
        ]
    elif verb == "run":
        # 只启动第一个可运行的任务，避免两个任务同时跑签到/成长中心。
        lines += [
            "            if ($t.State -eq 'Running') { $done += ($n + '：已在运行'); break }",
            "            if ($t.State -eq 'Disabled') { $failed += ($n + '：已停用'); continue }",
            "            Start-ScheduledTask -TaskName $n -TaskPath '\\' -ErrorAction Stop",
            "            $done += $n",
            "            break",
        ]
    elif verb == "stop":
        lines += [
            "            if ($t.State -ne 'Running') { $done += ($n + '：未在运行'); continue }",
            "            Stop-ScheduledTask -TaskName $n -TaskPath '\\' -ErrorAction Stop",
            "            $done += $n",
        ]
    elif verb == "uninstall":
        lines += [
            "            if ($t.State -eq 'Running') {",
            "                Stop-ScheduledTask -TaskName $n -TaskPath '\\' -ErrorAction Stop",
            "            }",
            "            Unregister-ScheduledTask -TaskName $n -TaskPath '\\' "
            "-Confirm:$false -ErrorAction Stop",
            "            $done += $n",
        ]

    lines += [
        "        } catch { $failed += ($n + '：' + $_.Exception.Message) }",
        "    }",
        "    $message = if ($done.Count -gt 0) { '完成：' + ($done -join '；') } else { '' }",
        "    if ($failed.Count -gt 0) {",
        "        if ($message) { $message += '。' }",
        "        $message += '失败：' + ($failed -join '；')",
        "    }",
        "    [pscustomobject]@{",
        "        ok = ($failed.Count -eq 0)",
        "        message = $message",
        "        done = $done",
        "        failed = $failed",
        "    } | ConvertTo-Json -Depth 4 -Compress",
        "} catch {",
        "    [pscustomobject]@{ ok = $false; error = $_.Exception.Message } | "
        "ConvertTo-Json -Compress",
        "}",
    ]
    return "\n".join(lines)


def action(verb, specs=TASKS):
    """执行启用/停用/立即运行/停止/卸载；返回 ok、message 与明细。"""
    if not supported():
        raise RuntimeError("计划任务管理仅支持 Windows")
    raw = _parse_json(_run_ps(_action_script(verb, specs), timeout=40))
    if not isinstance(raw, dict):
        raise RuntimeError("PowerShell 返回无法解析")
    return {
        "ok": bool(raw.get("ok")),
        "message": raw.get("message") or raw.get("error") or "操作失败",
        "done": raw.get("done") or [],
        "failed": raw.get("failed") or [],
    }


def qoder_action(verb):
    """只操作 QoderDailyCredits，不触碰 WorkBuddy 的两个任务。"""
    return action(verb, QODER_TASKS)


def qoder_trigger_times(now=None, jitter=True):
    """把 UTC+8 每天 10:00 的刷新时间换算成本机 HH:mm 列表。

    与 install-qoder-windows.ps1 保持一致：主触发点 +5 分钟，再按
    0/1/5/9/13 小时排 4 次补跑。

    ``jitter=True`` 时只做正向浮动：活动在刷新之后才能领，提前一分钟就是
    空跑。``jitter=False`` 返回未打散的基准点，供测试与界面说明使用。
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    reset = now.astimezone(datetime.timezone.utc).replace(
        hour=2, minute=0, second=0, microsecond=0)
    primary = reset.astimezone().replace(tzinfo=None) + datetime.timedelta(minutes=5)
    times = [(primary + datetime.timedelta(hours=h)).strftime("%H:%M")
             for h in QODER_OFFSETS]
    if not jitter:
        return times
    return schedule_jitter.jitter_times(times, QODER_TASKS[0]["name"],
                                        forward=True)


def trae_status(log_path=None):
    """读取 TraeDailyCheckin 任务的安装、启用、运行与最近执行信息。"""
    return status(log_path, TRAE_TASKS)


def trae_action(verb):
    """只操作 TraeDailyCheckin，不触碰 WorkBuddy / Qoder 任务。"""
    return action(verb, TRAE_TASKS)


def trae_trigger_times(primary="08:05", jitter=True):
    """把设置里的主签到时间展开成 5 个本机触发时间。

    主时间由用户设置，抖动只往后放：往前漂可能跨过午夜，被算成前一天，
    连着两天都会签不上。``jitter=False`` 返回未打散的基准点。
    """
    try:
        base = datetime.datetime.strptime(str(primary or "08:05")[:5],
                                          "%H:%M")
    except ValueError:
        base = datetime.datetime.strptime("08:05", "%H:%M")
    times = []
    for offset in TRAE_OFFSETS:
        moment = base + datetime.timedelta(hours=offset)
        times.append(moment.strftime("%H:%M"))
    if not jitter:
        return times
    return schedule_jitter.jitter_times(times, TRAE_TASKS[0]["name"],
                                        forward=True)


# python.org 安装器必写的位置；非默认目录安装也能从这里找到
_PY_REGISTRY = (
    r"SOFTWARE\Python\PythonCore",
    r"SOFTWARE\WOW6432Node\Python\PythonCore",
)

# 任务动作的来源，用于在界面上说明「这个任务靠谁跑」
RUNTIME_LABEL = {
    "pythonw": "系统 Python",
    "console": "控制台内置运行时",
}


def _registry_pythonw():
    """从注册表读 python.org 版 Python 的 pythonw.exe。

    「装到非默认目录 + 没勾 Add to PATH」是 Windows 上 Python 最常见的状态，
    只靠 PATH 和固定目录会漏。注册表是官方安装器必写的，覆盖最全。
    """
    try:
        import winreg
    except ImportError:
        return None

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for root in _PY_REGISTRY:
            try:
                key = winreg.OpenKey(hive, root)
            except OSError:
                continue
            with key:
                index = 0
                while True:
                    try:
                        version = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        sub = winreg.OpenKey(key, version + r"\InstallPath")
                    except OSError:
                        continue
                    with sub:
                        # ExecutablePath 指向 python.exe；默认值才是安装目录
                        for value_name in ("ExecutablePath", ""):
                            try:
                                value = winreg.QueryValueEx(sub, value_name)[0]
                            except OSError:
                                continue
                            if not value:
                                continue
                            candidate = (value if value.lower().endswith(".exe")
                                         else os.path.join(value, "pythonw.exe"))
                            if os.path.basename(candidate).lower() == "python.exe":
                                candidate = os.path.join(
                                    os.path.dirname(candidate), "pythonw.exe")
                            if os.path.isfile(candidate):
                                return candidate
    return None


def _launcher_pythonw():
    r"""用 py 启动器问出真实解释器路径。

    这一条专治 Microsoft Store 的 python 存根：PATH 里那个
    ``WindowsApps\python.exe`` 是 0 字节的转发器，同级没有 pythonw，
    真正的解释器藏在 WindowsApps\PythonSoftwareFoundation.* 下面。
    """
    launcher = shutil.which("py") or os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "py.exe")
    if not launcher or not os.path.isfile(launcher):
        return None

    kwargs = {}
    if supported():
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [launcher, "-3", "-c", "import sys;print(sys.executable)"],
            capture_output=True, text=True, timeout=10, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None

    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        return None
    candidate = os.path.join(os.path.dirname(lines[-1].strip()), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else None


def _python_candidates():
    """按可靠性顺序产出 pythonw.exe 候选路径。

    注册表排在 PATH 之前是有意的：PATH 里可能是 venv 的临时解释器，也可能是
    别的软件自带的私有运行时（例如控制台自己那套托管 Python），它们随时可能
    被升级或删除；计划任务要长期存活，指向真正的安装版更稳。
    """
    exe = os.path.abspath(sys.executable)
    if os.path.basename(exe).lower() in ("python.exe", "pythonw.exe"):
        yield os.path.join(os.path.dirname(exe), "pythonw.exe")

    yield _registry_pythonw()
    yield shutil.which("pythonw")
    yield _launcher_pythonw()

    local = os.environ.get("LOCALAPPDATA", "")
    program = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_x86 = os.environ.get("ProgramFiles(x86)",
                                 r"C:\Program Files (x86)")
    patterns = [
        os.path.join(program, "Python3*", "pythonw.exe"),
        os.path.join(program_x86, "Python3*", "pythonw.exe"),
        r"C:\Python3*\pythonw.exe",
    ]
    if local:
        # python.org 默认的「仅为当前用户安装」落在 LOCALAPPDATA 下
        patterns.insert(0, os.path.join(local, "Programs", "Python",
                                        "Python3*", "pythonw.exe"))
        patterns.insert(1, os.path.join(local, "Programs", "Python", "*",
                                        "pythonw.exe"))
    for pattern in patterns:
        for match in sorted(glob.glob(pattern), reverse=True):
            yield match


def find_pythonw():
    """定位 pythonw.exe，供 Qoder / Trae 计划任务使用。

    依次尝试：本进程解释器同级 → PATH → 注册表 → py 启动器 → 常见目录。
    找不到返回 None，由调用方决定是退回控制台 exe 还是报「缺 Python」。
    """
    for candidate in _python_candidates():
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _task_command(signin_path, action, pythonw=None, runner=None,
                  runner_args=None):
    """决定计划任务要执行的命令：(exe, 参数字符串, 运行时来源)。

    系统 Python 优先——与手工跑 install-windows.ps1 等价，行为可预期。
    拿不到 pythonw 时退回控制台 exe 自身的子命令（``--trae-service`` 之类）：
    exe 里已经打包了解释器，这样没装 Python 的机器也能装任务。此前这里直接
    抛错，导致 Trae / Qoder 的任务在纯 exe 机器上永远装不上。
    """
    found = pythonw if (pythonw and os.path.isfile(pythonw)) else find_pythonw()
    if found:
        return found, '"%s" %s' % (signin_path, action), "pythonw"

    if runner and os.path.isfile(runner):
        base = " ".join(runner_args or [])
        return runner, (base + " " + action).strip(), "console"

    raise ServiceError(
        "缺失 Python 3 运行环境，计划任务无法启动", "python-missing",
        detail="未找到 pythonw.exe，且控制台程序不可用（runner=%r）" % (runner,))


def _single_task_script(exe, args, workdir, spec, times, message):
    """生成「注册一个任务 + 回读确认」的 PowerShell。

    Qoder / Trae 的安装只差任务名、触发时间和动作参数，脚本体共用一份。
    此前两处各写一遍，结果一边加了回读校验、另一边漏了——共用后不会再漂移。
    """
    name = _ps_quote(spec["name"])
    return "\n".join([
        _PREAMBLE,
        "try {",
        "    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
        "    $principal = New-ScheduledTaskPrincipal -UserId $user "
        "-LogonType Interactive -RunLevel Limited",
        "    $exe = %s" % _ps_quote(exe),
        "    $wd = %s" % _ps_quote(workdir),
        "    $a = New-ScheduledTaskAction -Execute $exe -Argument %s "
        "-WorkingDirectory $wd" % _ps_quote(args),
        "    $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden "
        "-DontStopIfGoingOnBatteries -AllowStartIfOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes %d)" % spec["minutes"],
        "    $triggers = @()",
        "    foreach ($t in @(%s)) {" % ",".join(_ps_quote(t) for t in times),
        "        $triggers += New-ScheduledTaskTrigger -Daily -At $t",
        "    }",
        "    Register-ScheduledTask -TaskName %s -TaskPath '\\' "
        "-Action $a -Trigger $triggers -Settings $s -Principal $principal "
        "-Description %s -Force | Out-Null"
        % (name, _ps_quote(spec["description"])),
        "    $t = Get-ScheduledTask -TaskName %s -TaskPath '\\' "
        "-ErrorAction Stop" % name,
        "    $i = Get-ScheduledTaskInfo -TaskName %s -TaskPath '\\' "
        "-ErrorAction Stop" % name,
        "    if ($null -eq $t -or $null -eq $i) { throw '任务回读失败' }",
        "    [pscustomobject]@{",
        "        ok = $true",
        "        message = %s" % _ps_quote(message),
        "        tasks = @([pscustomobject]@{",
        "            name = $t.TaskName",
        "            state = [string]$t.State",
        "            next_run = if ($null -ne $i.NextRunTime -and $i.NextRunTime.Year -gt 2000) "
        "{ $i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }",
        "        })",
        "    } | ConvertTo-Json -Depth 5 -Compress",
        "} catch {",
        "    [pscustomobject]@{ ok = $false; error = $_.Exception.Message } | "
        "ConvertTo-Json -Compress",
        "}",
    ])


def _install_single_task(signin_name, signin_path, spec, times,
                         pythonw=None, runner=None, runner_args=None,
                         workdir=None):
    """Qoder / Trae 共用的安装流程：定命令 → 注册 → 回读 → 结构化结果。"""
    if not supported():
        raise ServiceError("计划任务管理仅支持 Windows", "unsupported")

    signin_path = os.path.abspath(signin_path)
    if not os.path.isfile(signin_path):
        raise ServiceError("找不到 %s，无法安装计划任务" % signin_name,
                           "file-missing", detail=signin_path)
    if workdir is None:
        workdir = os.path.dirname(signin_path)

    exe, args, source = _task_command(signin_path, spec["action"],
                                      pythonw=pythonw, runner=runner,
                                      runner_args=runner_args)
    times = list(times)
    runtime = RUNTIME_LABEL.get(source, source)
    message = "%s 已安装并启用（每天 %s，使用%s）" % (
        spec["name"], " / ".join(times), runtime)

    raw = _parse_json(_run_ps(
        _single_task_script(exe, args, workdir, spec, times, message),
        timeout=60))
    if not isinstance(raw, dict):
        raise ServiceError("PowerShell 返回无法解析，安装未完成", "ps-failed")
    if not raw.get("ok"):
        detail = raw.get("error") or "安装失败"
        code, text = _classify(detail)
        raise ServiceError(text, code, detail=detail)
    return {
        "ok": True,
        "message": raw.get("message") or message,
        "tasks": raw.get("tasks") or [],
        "runner": source,
        "runner_path": exe,
    }


def install_qoder(signin_path, pythonw=None, workdir=None,
                  runner=None, runner_args=None):
    """注册/修复 QoderDailyCredits，并回读确认。

    命令优先用系统 pythonw 跑 ``qoder_signin.py silent``；找不到 pythonw 时
    退回控制台 exe 的 ``--qoder-service``（runner 由调用方给出，exe 内已
    打包解释器）。任务名、触发器和动作全部在服务端固定，前端不能传入任意命令。
    """
    # 基准与注册用的时刻从同一次换算里出，避免两次调用跨分钟导致对不上
    base = qoder_trigger_times(jitter=False)
    times = schedule_jitter.jitter_times(base, QODER_TASKS[0]["name"],
                                         forward=True)
    result = _install_single_task("qoder_signin.py", signin_path,
                                  QODER_TASKS[0], times,
                                  pythonw=pythonw, runner=runner,
                                  runner_args=runner_args, workdir=workdir)
    remember_base("qoder", {QODER_TASKS[0]["name"]: base})
    return result


def install_trae(signin_path, base_times=None, times=None, pythonw=None,
                 workdir=None, runner=None, runner_args=None):
    """注册/修复 TraeDailyCheckin，并回读确认。

    与 Qoder 同一套流程：pythonw 优先，控制台 exe 兜底。``base_times`` 是
    配置出来的基准点（主时间随 Trae 设置变），重排以它为锚；``times`` 不通
    常用，传了就直接注册。
    """
    base = list(base_times or trae_trigger_times(jitter=False))
    if times is None:
        times = schedule_jitter.jitter_times(base, TRAE_TASKS[0]["name"],
                                             forward=True)
    result = _install_single_task("trae_signin.py", signin_path,
                                  TRAE_TASKS[0], times,
                                  pythonw=pythonw, runner=runner,
                                  runner_args=runner_args, workdir=workdir)
    remember_base("trae", {TRAE_TASKS[0]["name"]: base})
    return result


# ---------------------------------------------------------------------------
# 触发时刻的随机重排
# ---------------------------------------------------------------------------
#
# 安装时的随机只能决定「这一次」的时刻；Daily 触发器一旦写进任务计划，
# 往后每天都会在同一分钟醒来，几天之后模式又固定了。所以任务每次跑完都
# 重排一次：把「今天已经过去」的点重新抽样，未到的点不动。
#
# 未到的点保留是有意的——poll 任务凌晨跑完就把 05/09/13 一起换掉的话，
# 白天那几个点会被推到已经过去的时段，当天直接少跑 5 次。而每个点在它
# 自己跑完的当天都会被换掉，所以到第二天零点，整组时刻已经全是新的。
#
# 重排是尽力而为：任务计划的读写在后台发生，任何失败都只记录、不抛错，
# 绝不能让「换个随机时间」把签到本身搞挂。
#
# 去重只在**同一个进程内**做，不按时间冷却。早期版本用 90 分钟冷却，结果
# 「手动运行一次」会把第二天的随机时刻锁死 90 分钟——用户要的是「每跑完
# 一次就换一组」，而不是「每 90 分钟最多换一次」。同一进程里重复调用的
# 场景只有一个：控制台 exe 的 --qoder-service / --trae-service 先跑脚本
# 自己的钩子，再回到 _side_service_main 调一次，两次间隔不到一秒。

RESHUFFLED_IN_PROCESS = set()
JITTER_STATE_FILE = "schedule-jitter.json"

RESHUFFLE_TABLES = {
    "workbuddy": TASKS,
    "qoder": QODER_TASKS,
    "trae": TRAE_TASKS,
}


def default_data_dir():
    """重排状态的落盘目录，与控制台的 _diag_dir() 保持同一路径。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Auto-Claim", "dashboard")


def jitter_state_path(data_dir=None):
    return os.path.join(data_dir or default_data_dir(), JITTER_STATE_FILE)


def read_jitter_state(data_dir=None):
    """读取各类任务上次重排的时间戳；文件缺失或损坏都当作空状态。"""
    try:
        with open(jitter_state_path(data_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_jitter_state(data_dir, state):
    """原子写回：先写临时文件再替换，断电不会留下半份 JSON。"""
    path = jitter_state_path(data_dir)
    temp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    except OSError:
        try:
            os.remove(temp)
        except Exception:
            pass


def _state_entry(state, kind):
    """取某一类任务的状态项，兼容早期「裸时间戳」的写法。"""
    entry = state.get(kind)
    if isinstance(entry, dict):
        return dict(entry)
    if isinstance(entry, (int, float)):
        return {"at": float(entry)}
    return {}


def remember_base(kind, mapping, data_dir=None):
    """记下这次注册用的基准点。

    重排必须以基准为锚：早期版本拿任务里的当前时刻再抽 ±15 分钟，误差会
    随天数滚雪球（00:20 → 00:25 → 00:40 → 00:57 …），一个月就漂到白天。
    基准点只有安装时才知道（Trae 的主时间还随设置变），所以在这里留一份。
    """
    state = read_jitter_state(data_dir)
    entry = _state_entry(state, kind)
    base = dict(entry.get("base") or {})
    base.update({name: list(times) for name, times in mapping.items()})
    entry["base"] = base
    state[kind] = entry
    write_jitter_state(data_dir, state)


def _read_times_script(specs):
    """读出任务真实的触发时刻与动作，供重排复用。

    动作从任务本身读，而不是按当前环境重新推导：任务可能是用系统 Python
    装的，也可能指向控制台 exe，重排只负责换时间，不该顺手把运行方式也改掉。
    """
    names = ",".join(_ps_quote(item["name"]) for item in specs)
    return _PREAMBLE + r"""
try {
    $out = @()
    foreach ($n in @(%s)) {
        $t = Get-ScheduledTask -TaskName $n -TaskPath '\' -ErrorAction SilentlyContinue
        if ($null -eq $t) { continue }
        $act = @($t.Actions)[0]
        if ($null -eq $act) { continue }
        $times = @($t.Triggers | ForEach-Object {
            if ($_.StartBoundary) { ([datetime]$_.StartBoundary).ToString('HH:mm') }
        } | Sort-Object)
        $out += [pscustomobject]@{
            name = $n
            times = @($times)
            exe = [string]$act.Execute
            args = [string]$act.Arguments
            workdir = [string]$act.WorkingDirectory
        }
    }
    [pscustomobject]@{ ok = $true; tasks = @($out) } |
        ConvertTo-Json -Depth 6 -Compress
} catch {
    [pscustomobject]@{ ok = $false; error = $_.Exception.Message } |
        ConvertTo-Json -Compress
}
""" % names


def _rewrite_times_script(changes):
    """用新的触发时刻重新注册同名任务，动作与设置保持原样。

    参数走 base64 传，避免路径里的引号、中文和反斜杠在 PowerShell 里
    层层转义；报错几乎都出在这一步。
    """
    payload = json.dumps(changes, ensure_ascii=False)
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return _PREAMBLE + r"""
try {
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $json = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('%s'))
    # 不能写成 @($json | ConvertFrom-Json)：PowerShell 5.1 的 ConvertFrom-Json
    # 把整个 JSON 数组当成「一个」对象送进管道，@() 再包一层就成了嵌套数组，
    # foreach 只转一圈、$item.exe 取到的是所有路径拼成的数组，报
    # 「无法将值转换为类型 System.String」。先赋值、再在 foreach 里包 @()。
    $items = $json | ConvertFrom-Json
    $done = @()
    foreach ($item in @($items)) {
        $triggers = @()
        foreach ($t in @($item.times)) {
            $triggers += New-ScheduledTaskTrigger -Daily -At $t
        }
        if ($item.workdir) {
            $a = New-ScheduledTaskAction -Execute $item.exe -Argument $item.args -WorkingDirectory $item.workdir
        } else {
            $a = New-ScheduledTaskAction -Execute $item.exe -Argument $item.args
        }
        $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes ([int]$item.minutes))
        Register-ScheduledTask -TaskName $item.name -TaskPath '\' -Action $a -Trigger $triggers -Settings $s -Principal $principal -Description $item.description -Force | Out-Null
        $done += $item.name
    }
    [pscustomobject]@{ ok = $true; tasks = @($done) } |
        ConvertTo-Json -Depth 5 -Compress
} catch {
    [pscustomobject]@{ ok = $false; error = $_.Exception.Message } |
        ConvertTo-Json -Compress
}
""" % encoded


def _norm_path(value):
    """空串原样返回；其余转成当前系统的标准写法（反斜杠）。"""
    text = "" if value is None else str(value).strip()
    return os.path.normpath(text) if text else ""


def _run_ps_json(script, timeout=40):
    """执行 PowerShell 并把 JSON 结果解出来；失败统一抛 ServiceError。"""
    raw = _parse_json(_run_ps(script, timeout=timeout))
    if not isinstance(raw, dict):
        raise ServiceError("PowerShell 返回无法解析", "ps-failed")
    if not raw.get("ok"):
        detail = raw.get("error") or "计划任务操作失败"
        code, message = _classify(detail)
        raise ServiceError(message, code, detail=detail)
    return raw


def _as_task_list(raw):
    """PowerShell 5.1 会把单元素数组序列化成对象，这里统一成列表。"""
    items = raw.get("tasks") or []
    if isinstance(items, dict):
        return [items]
    return [item for item in items if isinstance(item, dict)]


def _reshuffle_error(message):
    return {"ok": False, "error": message, "changed": []}


def reshuffle(kind, data_dir=None, now=None, force=False):
    """签到跑完后重排触发时刻；返回可直接写进日志的小结。

    ``kind`` 取 ``workbuddy`` / ``qoder`` / ``trae``。同一个进程内只重排一次
    （见 ``RESHUFFLED_IN_PROCESS``）；``force=True`` 供「安装 / 修复」后
    立即重排，也用来在同一个进程里重复验证。
    """
    if not supported():
        return _reshuffle_error("计划任务管理仅支持 Windows")
    specs = RESHUFFLE_TABLES.get(kind)
    if not specs:
        return _reshuffle_error("未知的计划任务类型：%s" % kind)
    if kind in RESHUFFLED_IN_PROCESS and not force:
        return {"ok": True, "changed": [], "skipped": "in-process"}

    now = now or datetime.datetime.now()
    state = read_jitter_state(data_dir)
    entry = _state_entry(state, kind)
    bases = entry.get("base") or {}

    by_name = {item["name"]: item for item in specs}
    changes = []
    summary = []
    try:
        raw = _run_ps_json(_read_times_script(specs), timeout=30)
        for item in _as_task_list(raw):
            spec = by_name.get(item.get("name"))
            if spec is None:
                continue
            old = [t for t in _as_text_list(item.get("times"))
                   if schedule_jitter.parse_hhmm(t) is not None]
            if not old:
                # 读不到 StartBoundary（任务被手工改成登录触发之类），不去动它
                continue
            base = bases.get(spec["name"]) or spec.get("base_times")
            new = schedule_jitter.reshuffle(
                old, spec["name"], base=base, now=now,
                forward=bool(spec.get("forward")))
            if new == old:
                continue
            changes.append({
                "name": spec["name"],
                # 任务里的路径可能是正斜杠（手工改过、或从别的工具写入），
                # 统一成 Windows 形式，免得同一批任务里两种写法混着
                "exe": _norm_path(item.get("exe")),
                "args": item.get("args") or "",
                "workdir": _norm_path(item.get("workdir")),
                "minutes": int(spec.get("minutes") or 10),
                "description": spec.get("description") or "",
                "times": new,
            })
            summary.append({"name": spec["name"], "old": old, "new": new})
        if changes:
            _run_ps_json(_rewrite_times_script(changes), timeout=60)
    except Exception as exc:
        return {"ok": False, "changed": [], "error": "%s" % exc}

    # 无论有没有改动都记一次时间戳：没改说明这轮无需重排，同一进程内不必再读
    entry["at"] = time.time()
    state[kind] = entry
    write_jitter_state(data_dir, state)
    RESHUFFLED_IN_PROCESS.add(kind)
    if not changes:
        return {"ok": True, "changed": [], "skipped": "unchanged"}
    return {"ok": True, "changed": summary}

def apply_jitter_after_run(kind, data_dir=None):
    """在签到流程末尾调用：重排并把结果写进日志，永不抛错。"""
    if not supported():
        # macOS 走 launchd，没有可重排的 Daily 触发器，静默跳过
        return {"ok": True, "changed": [], "skipped": "unsupported"}
    try:
        outcome = reshuffle(kind, data_dir=data_dir)
    except Exception as exc:
        outcome = {"ok": False, "changed": [], "error": "%s" % exc}
    if outcome.get("changed"):
        preview = "; ".join(
            "%s %s→%s" % (item["name"], "/".join(item["old"]),
                          "/".join(item["new"]))
            for item in outcome["changed"])
        _append_console_log(
            "[jitter] %s 触发时刻已重排：%s" % (kind, preview), data_dir)
    elif not outcome.get("ok"):
        _append_console_log("[jitter] %s 重排失败：%s"
                            % (kind, outcome.get("error")), data_dir)
    return outcome


def _append_console_log(text, data_dir=None):
    """重排结果写进控制台日志；没有控制台（pythonw）时静默丢弃。

    这里刻意不 import app.py 的日志函数：win_service 被 Qoder / Trae 脚本
    单独 import 时不一定会加载控制台，反向依赖会绕成环。
    """
    try:
        path = os.path.join(data_dir or default_data_dir(), "run.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 计划任务失败记录
# ---------------------------------------------------------------------------
#
# 计划任务在后台跑，失败时没人看见——日志里躺着「没有会话 Cookie 可续期」，
# 但用户不会主动去翻。这里把每次失败写进一份 JSON，控制台启动和轮询时读取，
# 在对应模块（WorkBuddy / Qoder / Trae）的计划任务区块原样展示。
#
# 为什么不复用 signin.log：三个产品各有自己的日志文件，格式也不一样
# （有的写 JSON、有的写文本），界面要同时展示「哪次失败、重试结果如何」，
# 需要结构化字段。日志仍然照写，两边不冲突。

ALERT_FILENAME = "service-alert.json"
ALERT_KINDS = ("workbuddy", "qoder", "trae")
ALERT_REPORT_LIMIT = 800       # 单条原文上限，避免整份响应写进文件

# 界面会直接显示的字段。其余（账号列表、原始 payload）一律不落盘。
_ALERT_FIELDS = ("task", "action", "exit", "result", "report", "stage",
                 "reimport_ok", "reimport_report", "retry_exit",
                 "retry_report", "recovered", "surface", "surface_at")
_ALERT_NUMBERS = ("exit", "retry_exit", "surface_at")


def alert_path(data_dir):
    """失败记录文件路径；data_dir 由控制台传 _diag_dir()。"""
    return os.path.join(data_dir, ALERT_FILENAME)


def _alert_text(value, limit=ALERT_REPORT_LIMIT):
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _normalize_alert(item):
    kind = str(item.get("kind") or "")
    if kind not in ALERT_KINDS:
        raise ValueError("未知的计划任务类型：%s" % kind)
    out = {"kind": kind,
           "at": item.get("at") or time.strftime("%Y-%m-%d %H:%M:%S")}
    for key in _ALERT_FIELDS:
        if key not in item:
            continue
        value = item[key]
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)) and key in _ALERT_NUMBERS:
            out[key] = value
        elif value is not None:
            out[key] = _alert_text(value)
    if "surface" in out:
        out["surface"] = out["surface"][:40]
    return out


def read_alerts(data_dir):
    """读取全部失败记录；文件缺失或损坏都返回空列表。

    损坏时不能抛异常：这个函数在控制台启动路径上，一条写坏的 JSON 不该
    让整个界面起不来。
    """
    try:
        with open(alert_path(data_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [item for item in items
            if isinstance(item, dict) and item.get("kind") in ALERT_KINDS]


def write_alert(data_dir, item):
    """写入或替换同类记录：一个模块只显示一条最新提示。

    用「写临时文件 + os.replace」：控制台可能正好在轮询，读到半截 JSON
    会把它当成损坏文件，提示直接消失。
    """
    record = _normalize_alert(item)
    others = [old for old in read_alerts(data_dir)
              if old.get("kind") != record["kind"]]
    items = [record] + others

    path = alert_path(data_dir)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "items": items}, fh,
                  ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return record


def clear_alert(data_dir, kind):
    """用户点「知道了」后清掉该类型的提示；返回是否真的有记录。"""
    if kind not in ALERT_KINDS:
        raise ValueError("未知的计划任务类型：%s" % kind)
    items = read_alerts(data_dir)
    kept = [item for item in items if item.get("kind") != kind]
    if len(kept) == len(items):
        return False
    path = alert_path(data_dir)
    try:
        if kept:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "items": kept}, fh,
                          ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        else:
            os.remove(path)
    except OSError:
        return False
    return True
