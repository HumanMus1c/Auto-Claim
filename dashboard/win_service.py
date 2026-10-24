"""Windows 计划任务（脚本服务）管理。

控制台把后台签到交给两个固定名称的 WorkBuddy 计划任务：

  WorkBuddyAutoSignin  每天 00:05，完整签到 + 成长中心
  WorkBuddyGrowthPoll  每天 01/05/09/13/17/21 点，补签 + 成长中心

Qoder CN 使用另一个独立任务，互不影响：

  QoderDailyCredits    每天 5 次（UTC+8 10:05 起），领取 100 Credits

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

# 与 install-windows.ps1 / README 中的任务保持一致
TASKS = (
    {
        "name": "WorkBuddyAutoSignin",
        "label": "每日签到",
        "when": "每天 00:05",
        "what": "签到 + 成长中心",
        "action": "silent",
        "minutes": 10,
        "description": "WorkBuddy daily auto signin (silent, zero token)",
    },
    {
        "name": "WorkBuddyGrowthPoll",
        "label": "轮询补签",
        "when": "每天 01/05/09/13/17/21 点",
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
        "when": "每天 5 次（UTC+8 10:05 起，按本机时间）",
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

# Trae 多账号签到独立任务；触发点由 trae_trigger_times() 按设置里的
# 主时间生成，默认 08:05 起再补跑 4 次，覆盖漏签/睡眠唤醒。
TRAE_TASKS = (
    {
        "name": "TraeDailyCheckin",
        "label": "每日签到",
        "when": "每天 5 次（08:05 起，按本机时间）",
        "what": "多账号签到 + 剩余积分",
        "action": "silent",
        "minutes": 10,
        "description": "Trae multi-account daily checkin (silent, zero token)",
    },
)
TRAE_TASK_BY_NAME = {item["name"]: item for item in TRAE_TASKS}
TRAE_TASK_NAMES = tuple(TRAE_TASK_BY_NAME)

# 主触发点之后的小时偏移：+2/+5/+9/+13，覆盖白天与晚间
TRAE_OFFSETS = (0, 2, 5, 9, 13)

# Qoder 活动按 UTC+8 每天 10:00 刷新；主触发点晚 5 分钟，另排 4 次补跑。
QODER_OFFSETS = (0, 1, 5, 9, 13)

# 轮询用 6 个独立的 Daily 触发器，而不是「Daily + 每 4 小时重复」。
# 后者一旦错过（关机/睡眠）就永久跳过，StartWhenAvailable 不会补跑。
POLL_TIMES = ("01:00", "05:00", "09:00", "13:00", "17:00", "21:00")

ACTIONS = ("enable", "disable", "run", "stop", "uninstall")

CREATE_NO_WINDOW = 0x08000000

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
        $tasks += [pscustomobject]@{
            name = $n
            installed = $true
            state = $state
            enabled = ($state -ne 'Disabled')
            last_run = $last
            next_run = $next
            last_result = $result
            action = ($actions -join '; ')
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
            "when": spec.get("when", ""),
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
        lines += [
            "    $a = New-ScheduledTaskAction -Execute $exe "
            "-Argument %s -WorkingDirectory $wd" % _ps_quote(args),
            "    $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden "
            "-DontStopIfGoingOnBatteries -AllowStartIfOnBatteries "
            "-ExecutionTimeLimit (New-TimeSpan -Minutes %d)" % spec["minutes"],
        ]
        if spec["name"] == "WorkBuddyGrowthPoll":
            lines += [
                "    $triggers = @()",
                "    foreach ($hh in @(%s)) {"
                % ",".join(_ps_quote(t) for t in POLL_TIMES),
                "        $triggers += New-ScheduledTaskTrigger -Daily -At $hh",
                "    }",
                "    Register-ScheduledTask -TaskName %s -TaskPath '\\' "
                "-Action $a -Trigger $triggers -Settings $s -Principal $principal "
                "-Description %s -Force | Out-Null"
                % (_ps_quote(spec["name"]), _ps_quote(spec["description"])),
            ]
        else:
            lines += [
                "    $trigger = New-ScheduledTaskTrigger -Daily -At '00:05'",
                "    Register-ScheduledTask -TaskName %s -TaskPath '\\' "
                "-Action $a -Trigger $trigger -Settings $s -Principal $principal "
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
        "        message = '已安装并启用 2 个计划任务（WorkBuddyAutoSignin / WorkBuddyGrowthPoll）'",
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


def qoder_trigger_times(now=None):
    """把 UTC+8 每天 10:00 的刷新时间换算成本机 HH:mm 列表。

    与 install-qoder-windows.ps1 保持一致：主触发点 +5 分钟，再按
    0/1/5/9/13 小时排 4 次补跑。
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    reset = now.astimezone(datetime.timezone.utc).replace(
        hour=2, minute=0, second=0, microsecond=0)
    primary = reset.astimezone().replace(tzinfo=None) + datetime.timedelta(minutes=5)
    return [(primary + datetime.timedelta(hours=h)).strftime("%H:%M")
            for h in QODER_OFFSETS]


def trae_status(log_path=None):
    """读取 TraeDailyCheckin 任务的安装、启用、运行与最近执行信息。"""
    return status(log_path, TRAE_TASKS)


def trae_action(verb):
    """只操作 TraeDailyCheckin，不触碰 WorkBuddy / Qoder 任务。"""
    return action(verb, TRAE_TASKS)


def trae_trigger_times(primary="08:05"):
    """把设置里的主签到时间展开成 5 个本机触发时间。"""
    try:
        base = datetime.datetime.strptime(str(primary or "08:05")[:5],
                                          "%H:%M")
    except ValueError:
        base = datetime.datetime.strptime("08:05", "%H:%M")
    times = []
    for offset in TRAE_OFFSETS:
        moment = base + datetime.timedelta(hours=offset)
        times.append(moment.strftime("%H:%M"))
    return times


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
    return _install_single_task("qoder_signin.py", signin_path,
                                QODER_TASKS[0], qoder_trigger_times(),
                                pythonw=pythonw, runner=runner,
                                runner_args=runner_args, workdir=workdir)


def install_trae(signin_path, times=None, pythonw=None, workdir=None,
                 runner=None, runner_args=None):
    """注册/修复 TraeDailyCheckin，并回读确认。

    与 Qoder 同一套流程：pythonw 优先，控制台 exe 兜底。
    """
    return _install_single_task("trae_signin.py", signin_path,
                                TRAE_TASKS[0],
                                times or trae_trigger_times(),
                                pythonw=pythonw, runner=runner,
                                runner_args=runner_args, workdir=workdir)
