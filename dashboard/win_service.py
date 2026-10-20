"""Windows 计划任务（脚本服务）管理。

控制台把后台签到交给两个固定名称的计划任务：

  WorkBuddyAutoSignin  每天 00:05，完整签到 + 成长中心
  WorkBuddyGrowthPoll  每天 01/05/09/13/17/21 点，补签 + 成长中心

本模块只通过 PowerShell 的 ScheduledTasks 模块操作这两个任务；任务名、
触发器和动作全部来自下面的常量，前端不能传入任意命令。

打包后计划任务直接运行控制台 exe 的 ``--service`` 模式（无窗口、无需系统
Python）；源码运行时运行 ``pythonw.exe app.py --service``。两种方式最终都
调用 ``signin.py`` 的 ``_run()``，与手工执行 ``install-windows.ps1`` 等价。
"""

import base64
import json
import os
import shutil
import subprocess
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

TASK_BY_NAME = {item["name"]: item for item in TASKS}
TASK_NAMES = tuple(TASK_BY_NAME)

# 轮询用 6 个独立的 Daily 触发器，而不是「Daily + 每 4 小时重复」。
# 后者一旦错过（关机/睡眠）就永久跳过，StartWhenAvailable 不会补跑。
POLL_TIMES = ("01:00", "05:00", "09:00", "13:00", "17:00", "21:00")

ACTIONS = ("enable", "disable", "run", "stop", "uninstall")

CREATE_NO_WINDOW = 0x08000000

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
        raise RuntimeError("未找到 PowerShell，无法管理计划任务")

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
        raise RuntimeError("PowerShell 操作超时（%d 秒）" % timeout)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError("PowerShell 退出码 %s：%s"
                           % (proc.returncode, detail[-500:] or "无输出"))
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


def _status_script():
    names = ",".join(_ps_quote(name) for name in TASK_NAMES)
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


def _empty_tasks():
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
        for item in TASKS
    ]


def _normalize_status(raw, log_path=None):
    """把 PowerShell 的原始对象整理成前端直接可用的结构。"""
    if not isinstance(raw, dict):
        raise RuntimeError("PowerShell 返回无法解析")
    if not raw.get("ok"):
        raise RuntimeError(raw.get("error") or "读取计划任务状态失败")

    tasks = []
    for item in raw.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        spec = TASK_BY_NAME.get(name, {})
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

    # PowerShell 输出不完整时也保证两个任务都出现在界面上
    seen = {t["name"] for t in tasks}
    for placeholder in _empty_tasks():
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
        "installed": len(installed) == len(TASKS),
        "installed_count": len(installed),
        "enabled": len(enabled) == len(TASKS),
        "running": bool(running),
        "next_run": next_runs[0] if next_runs else None,
        "last_run": last_runs[-1] if last_runs else None,
        "log": _log_info(log_path),
    }


def status(log_path=None):
    """读取两个计划任务的安装、启用、运行与最近执行信息。"""
    if not supported():
        return {
            "supported": False,
            "ok": False,
            "error": "计划任务管理仅支持 Windows",
            "tasks": _empty_tasks(),
            "installed": False,
            "installed_count": 0,
            "enabled": False,
            "running": False,
            "next_run": None,
            "last_run": None,
            "log": None,
        }
    try:
        raw = _parse_json(_run_ps(_status_script(), timeout=25))
        return _normalize_status(raw, log_path)
    except Exception as exc:
        return {
            "supported": True,
            "ok": False,
            "error": "%s: %s" % (type(exc).__name__, exc),
            "tasks": _empty_tasks(),
            "installed": False,
            "installed_count": 0,
            "enabled": False,
            "running": False,
            "next_run": None,
            "last_run": None,
            "log": _log_info(log_path),
        }


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
        raise RuntimeError("计划任务管理仅支持 Windows")
    if not runner or not os.path.isfile(runner):
        raise RuntimeError("找不到运行计划任务的程序：%s" % runner)
    if workdir is None:
        workdir = os.path.dirname(os.path.abspath(runner))

    raw = _parse_json(_run_ps(_install_script(runner, prefix_args, workdir),
                              timeout=60))
    if not isinstance(raw, dict):
        raise RuntimeError("PowerShell 返回无法解析")
    return {
        "ok": bool(raw.get("ok")),
        "message": raw.get("message") or raw.get("error") or "安装失败",
        "tasks": raw.get("tasks") or [],
    }


def _action_script(verb):
    if verb not in ACTIONS:
        raise ValueError("未知计划任务操作：%s" % verb)

    lines = [
        _PREAMBLE,
        "try {",
        "    $done = @()",
        "    $failed = @()",
        "    foreach ($n in @(%s)) {"
        % ",".join(_ps_quote(name) for name in TASK_NAMES),
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


def action(verb):
    """执行启用/停用/立即运行/停止/卸载；返回 ok、message 与明细。"""
    if not supported():
        raise RuntimeError("计划任务管理仅支持 Windows")
    raw = _parse_json(_run_ps(_action_script(verb), timeout=40))
    if not isinstance(raw, dict):
        raise RuntimeError("PowerShell 返回无法解析")
    return {
        "ok": bool(raw.get("ok")),
        "message": raw.get("message") or raw.get("error") or "操作失败",
        "done": raw.get("done") or [],
        "failed": raw.get("failed") or [],
    }
