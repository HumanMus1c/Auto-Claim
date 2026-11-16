# register-auto-update.ps1
# ----------------------------------------------------------------------------
# 注册（或修复）AutoClaim-AutoUpdate 计划任务：每 2 小时拉取 + 重编译，
# 只更新产物，从不主动启动图形控制台。使用 ScheduledTasks 模块而非
# schtasks.exe，以便在 schtasks 被策略限制的机器上也能注册。
#
#   powershell -ExecutionPolicy Bypass -File register-auto-update.ps1
#   powershell -ExecutionPolicy Bypass -File register-auto-update.ps1 -Unregister
# ----------------------------------------------------------------------------

param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'AutoClaimUpdater'),
    [string]$TaskName = 'AutoClaim-AutoUpdate',
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已卸载计划任务 $TaskName"
    exit 0
}

$script = Join-Path $InstallDir 'update-and-build.ps1'
if (-not (Test-Path $script)) {
    throw "找不到更新脚本：$script。请先把 automation/windows/update-and-build.ps1 复制到 $InstallDir"
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument @(
    '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $script)
) -WorkingDirectory $InstallDir

# 每 2 小时一次；起始时间往后错 5 分钟，避开整点注册风暴。
$start = (Get-Date).Date.AddMinutes(5)
if ($start -lt (Get-Date)) { $start = (Get-Date).AddMinutes(5) }
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Hours 2) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

# StartWhenAvailable=false：错过不补跑。WakeToRun=false：不唤醒机器。
# 不启用 RunOnlyIfNetworkAvailable：任务 XML 里若没写 NetworkSettings.Id，
# NLA 判定会把任务静默拒绝（0x800710E0，脚本一行日志都不写）。
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable:$false -WakeToRun:$false `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal `
    -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
    -LogonType InteractiveToken -RunLevel Limited

$description = 'Auto-Claim local auto update: git pull + rebuild only. Sign-in is fully handled by scheduled tasks; never launches the console (every 2 hours, only while logged on and awake)'

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description $description -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName |
    Format-List TaskName, State, Description
Write-Host "已注册计划任务 $TaskName；卸载命令：register-auto-update.ps1 -Unregister"
