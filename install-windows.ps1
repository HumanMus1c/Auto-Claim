# install-windows.ps1 — WorkBuddy 自动签到一键安装（Windows）
#
# 作者：88lin
# 仓库：https://github.com/88lin/workbuddy-auto-signin
# 协议：MIT
#
# 签到接口系从桌面端逆向所得，服务端改一版就可能失效——修复都会推到上面这个仓库。
# 顺手点个 ⭐ Star，等哪天连签莫名其妙断了，你能一秒把它找回来。
#
# 用法：在本仓库目录下，用 PowerShell 运行
#     powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
#
# 会创建两个定时任务（触发时刻随机，不再固定整点）：
#   1) WorkBuddyAutoSignin   每天 7 次   基准 00:05/03:00/07:00/11:00/15:00/19:00/23:00
#   2) WorkBuddyGrowthPoll   每天 6 次   基准 01/05/09/13/17/21 点，各 ±15 分钟
#
# 两者都零 Token、无窗口、开机错过会自动补跑。

$ErrorActionPreference = "Stop"

# ====== 一般不用改；自动探测失败时手动填这两个 ======
$ManualPythonw = ""   # 例如 C:\Python313\pythonw.exe
$ManualSignin  = ""   # 例如 C:\Users\You\Desktop\checkin\signin.py
# ====================================================

function Find-Pythonw {
    # 1) PATH 里就有 pythonw
    $c = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if ($c -and (Test-Path -LiteralPath $c)) { return $c }

    # 2) PATH 里有 python.exe，取同目录的 pythonw
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($py) {
        $c = Join-Path (Split-Path $py) "pythonw.exe"
        if (Test-Path -LiteralPath $c) { return $c }
    }

    # 3) 常见安装目录（很多 Windows 装机 Python 并不进 PATH，这一步最常命中）
    foreach ($pat in @(
        "C:\Python3*\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python3*\pythonw.exe",
        "C:\Program Files\Python3*\pythonw.exe"
    )) {
        $hit = Get-ChildItem $pat -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }

    # 4) py 启动器（装在 C:\Windows，几乎总在）
    $launcher = Join-Path $env:SystemRoot "py.exe"
    if (Test-Path -LiteralPath $launcher) {
        try {
            $exe = (& $launcher -3 -c "import sys;print(sys.executable)" 2>$null | Select-Object -Last 1)
            if ($exe) {
                $c = Join-Path (Split-Path $exe.Trim()) "pythonw.exe"
                if (Test-Path -LiteralPath $c) { return $c }
            }
        } catch { }
    }
    return $null
}

Write-Host ""
Write-Host "WorkBuddy 自动签到 · 一键安装" -ForegroundColor Cyan
Write-Host ("-" * 46) -ForegroundColor DarkGray

# --- 1. 定位 pythonw.exe ---
Write-Host "[1/3] 定位 pythonw.exe ..." -NoNewline
$pythonw = $ManualPythonw
# 必须先判空再 Test-Path：Test-Path "" 抛的是参数校验异常（不是返回 $false），
# 配合顶部的 $ErrorActionPreference = "Stop"，会让脚本直接死在这一行。
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) { $pythonw = Find-Pythonw }
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "没找到 pythonw.exe。请先安装 Python 3：https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "装好后重跑本脚本；或把脚本顶部的 `$ManualPythonw 填成完整路径。" -ForegroundColor Yellow
    exit 1
}
Write-Host " $pythonw" -ForegroundColor Green

# --- 2. 定位 signin.py ---
Write-Host "[2/3] 定位 signin.py ..." -NoNewline
$signin = $ManualSignin
# 同上：$ManualSignin 默认为空串，不判空会崩在 Test-Path 的参数校验上。
# $PSScriptRoot 在脚本被直接粘进控制台执行时也是空的，Join-Path 同样不接受空 Path。
if (-not $signin -or -not (Test-Path -LiteralPath $signin)) {
    if ($PSScriptRoot) { $signin = Join-Path $PSScriptRoot "signin.py" }
}
if (-not $signin -or -not (Test-Path -LiteralPath $signin)) { $signin = Join-Path (Get-Location) "signin.py" }
if (-not $signin -or -not (Test-Path -LiteralPath $signin)) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "没找到 signin.py。请把本脚本和 signin.py 放在同一目录后重跑，" -ForegroundColor Yellow
    Write-Host "或把脚本顶部的 `$ManualSignin 填成完整路径。" -ForegroundColor Yellow
    exit 1
}
$signin = (Resolve-Path -LiteralPath $signin).Path
Write-Host " $signin" -ForegroundColor Green

# --- 3. 创建两个定时任务 ---
Write-Host "[3/3] 创建定时任务 ..." -NoNewline

# 触发时刻的随机化：三层系统熵源叠加，与控制台里的 schedule_jitter.py 同构。
# 固定整点最容易被判定成脚本行为（每天 00:05:00 准时醒来比内容更显眼），
# 这里把每个基准点打散到 ±15 分钟内：① 系统时钟 Ticks 的末九位（100ns 级
# 读数抖动）② 开机至今的分钟数（内核 tick 推进，与整分钟边界错位）
# ③ epoch 与 uptime 各自的 10 分钟边界序号（时间服务、计划任务心跳的粒度）。
# 三者周期互质，叠加后过 SHA256 打散，不同任务、不同天不会撞到同一个偏移。
function Get-JitteredTime {
    param(
        [Parameter(Mandatory = $true)][string]$Base,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [int]$Window = 900,
        [switch]$Forward
    )

    $ticks = (Get-Date).Ticks
    $bootMs = 0
    try { $bootMs = [Environment]::TickCount64 } catch { $bootMs = [Environment]::TickCount }
    $bootMinute = [int64]($bootMs / 60000)
    $epochSlot = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() / 600)
    $uptimeSlot = [int64]($bootMinute / 10)

    $seed = "$TaskName|$($ticks % 1000000000)|$bootMinute|$epochSlot|$uptimeSlot"
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($seed))
    } finally {
        $sha.Dispose()
    }
    $value = [BitConverter]::ToUInt32($hash, 0)

    # 以分钟为粒度且跳过 0：偏移 0 等于没打散，而触发器只精确到 HH:mm，
    # 秒级抖动的结果会被格式化吃掉。Forward 只往后放——签到的第一个点
    # 往前漂会跨过午夜，被算进前一天。
    $span = [int]($Window / 60)
    if ($span -lt 1) { $span = 1 }
    if ($Forward) {
        $minutes = ($value % $span) + 1
    } else {
        $slot = $value % (2 * $span)
        if ($slot -lt $span) { $minutes = $slot - $span }
        else { $minutes = $slot - $span + 1 }
    }

    $baseTime = [datetime]::ParseExact($Base, 'HH:mm', $null)
    return $baseTime.AddSeconds($minutes * 60).ToString('HH:mm')
}

$taskRows = @(
    @{ Name = "WorkBuddyAutoSignin"; When = "每天 7 次（随机时刻）+ 空闲补跑"; What = "签到 + 成长中心" },
    @{ Name = "WorkBuddyGrowthPoll"; When = "每天 6 次（随机时刻）+ 空闲补跑"; What = "补签 + 成长中心全套" }
)

# 空闲触发器：New-ScheduledTaskTrigger 没有空闲类型，只能直接造 CIM 实例。
# 它和每天的随机时刻并行：机器睡过整组时刻、或断电重启后，只要空闲下来
# 就会补跑一次。是否重复由脚本里的 idle_trigger 闸门判断（30 分钟内成功跑过跳过）。
# 下面的 IdleDuration 只是让任务 UI 的数字和真实行为一致：Windows 8 以后该字段
# 已弃用，真正生效的是系统按「4 分钟无输入 + CPU/磁盘不忙」判定。
function New-IdleTrigger {
    return New-CimInstance -CimClass (Get-CimClass -ClassName MSFT_TaskIdleTrigger `
        -Namespace Root/Microsoft/Windows/TaskScheduler) -ClientOnly
}
$registeredTasks = @()
$verifiedTasks = @()
$operation = "创建"
$currentTask = "WorkBuddyAutoSignin"
try {
    # 模块命令可能使用自己的错误偏好；必须在调用处显式指定 Stop。
    # 即使安装时需要提权，定时运行仍使用当前账号的普通权限。
    # 本机实测：裸用户名（如 "DANNYSL"）注册主体会被任务计划服务以“参数错误”拒绝，
    # 必须带上域/计算机名前缀的完整形式（"DANNYSL\DANNYSL"）才能注册成功。
    $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    if (-not $userId) { $userId = "$env:USERDOMAIN\$env:USERNAME" }
    if (-not $userId) { $userId = $env:USERNAME }
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive `
        -RunLevel Limited -ErrorAction Stop

    # 任务 1：每日签到（7 个基准点，覆盖整天，各自随机延后最多 15 分钟）
    # 签到在 UTC+8 00:00 重置，只压在凌晨的话电脑一睡过去当天就没机会了。
    # 每 4 小时一个点，正好落在下面轮询任务（01/05/09/13/17/21）的间隙中间：
    # 两个任务合起来是「每 2 小时一次」的均匀覆盖。
    $act1 = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$signin`" silent" -ErrorAction Stop
    $tri1 = @()
    foreach ($hh in @("00:05", "03:00", "07:00", "11:00", "15:00", "19:00", "23:00")) {
        $jittered = Get-JitteredTime -Base $hh -TaskName "WorkBuddyAutoSignin" -Forward
        $tri1 += New-ScheduledTaskTrigger -Daily -At $jittered -ErrorAction Stop
    }
    $set1 = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
            -IdleDuration (New-TimeSpan -Minutes 4) `
            -IdleWaitTimeout (New-TimeSpan -Minutes 30) -DontStopOnIdleEnd -ErrorAction Stop
    # 空闲触发与随机时刻并行：随机时刻负责分散，空闲触发负责兜底
    $tri1 += New-IdleTrigger
    Register-ScheduledTask -TaskName $currentTask -TaskPath "\" `
        -Action $act1 -Trigger $tri1 -Settings $set1 -Principal $principal `
        -Description "WorkBuddy daily auto signin (silent, zero token)" -Force -ErrorAction Stop | Out-Null
    $registeredTasks += $currentTask

    # 任务 2：轮询（每天 01/05/09/13/17/21 点，共 6 次）
    # 刻意用 6 个独立的 Daily 触发器，而不是"Daily + 每 4 小时重复"：
    # 实测后者的重复实例一旦错过（关机/睡眠）就永久跳过，StartWhenAvailable
    # 不会为它补跑——本机就出现过 05:00 跑完后 09/13 点直接跳到 17:00 的情况。
    # 独立触发器则每次错过都会在下次开机时补上。
    $currentTask = "WorkBuddyGrowthPoll"
    $tri2 = @()
    foreach ($hh in @("01:00", "05:00", "09:00", "13:00", "17:00", "21:00")) {
        $jittered = Get-JitteredTime -Base $hh -TaskName "WorkBuddyGrowthPoll"
        $tri2 += New-ScheduledTaskTrigger -Daily -At $jittered -ErrorAction Stop
    }
    # 跑的是 silent-poll：先查签到状态、未签才补签，再跑成长中心。签到的机会因此
    # 从"一天一次"变成"一天七次"——00:05 那次撞上关机/睡眠/刚开机网络没就绪时，
    # 01 点那轮就能兜住，而不是眼睁睁断掉连签。（silent-growth 是它的旧名，
    # 已经装过旧版计划任务的机器继续用那个名字也能跑，行为一致。）
    $act2 = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$signin`" silent-poll" -ErrorAction Stop
    $set2 = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
            -IdleDuration (New-TimeSpan -Minutes 4) `
            -IdleWaitTimeout (New-TimeSpan -Minutes 30) -DontStopOnIdleEnd -ErrorAction Stop
    $tri2 += New-IdleTrigger
    Register-ScheduledTask -TaskName $currentTask -TaskPath "\" `
        -Action $act2 -Trigger $tri2 -Settings $set2 -Principal $principal `
        -Description "WorkBuddy poll (catch-up signin + growth center)" -Force -ErrorAction Stop | Out-Null
    $registeredTasks += $currentTask

    # 两个任务及运行信息均回读成功后才宣布完成；同名旧任务不能掩盖注册失败。
    $operation = "校验"
    foreach ($row in $taskRows) {
        $currentTask = $row.Name
        $tasks = @(Get-ScheduledTask -TaskName $currentTask -TaskPath "\" -ErrorAction Stop)
        if ($tasks.Count -ne 1) { throw "未找到唯一的计划任务：\$currentTask" }
        $info = Get-ScheduledTaskInfo -TaskName $currentTask -TaskPath "\" -ErrorAction Stop
        if ($null -eq $info) { throw "未读取到计划任务运行信息：\$currentTask" }
        # 回读真实触发时刻：随机化之后写死「每天 00:05」就是在骗人
        $at = @($tasks[0].Triggers | ForEach-Object {
            if ($_.StartBoundary) { ([datetime]$_.StartBoundary).ToString('HH:mm') }
        } | Sort-Object) -join ' / '
        $verifiedTasks += @{
            Name = $row.Name; When = $at; What = $row.What
            State = $tasks[0].State; NextRunTime = $info.NextRunTime
        }
    }
} catch {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "$operation $currentTask 失败，安装未完成。" -ForegroundColor Red
    Write-Host "错误信息：$($_.Exception.Message)" -ForegroundColor Red
    if ($registeredTasks.Count -gt 0) {
        Write-Host ("已注册：" + ($registeredTasks -join "、") + "。这些任务会保留；排除问题后重跑可更新同名任务。") -ForegroundColor Yellow
    }
    Write-Host "若提示拒绝访问（0x80070005），请使用同一 Windows 账号以管理员身份打开 PowerShell 后重试。" -ForegroundColor Yellow
    Write-Host "若受系统策略限制，请联系管理员；也可使用 README 的模式 A：AI 自动化，无需计划任务。" -ForegroundColor Yellow
    exit 1
}
Write-Host " 完成" -ForegroundColor Green

# --- 收尾汇报 ---
Write-Host ""
Write-Host "两个定时任务已就位：" -ForegroundColor Green
foreach ($row in $verifiedTasks) {
    Write-Host ("  {0,-22} {1,-7} {2}" -f $row.Name, $row.State, $row.What)
    # 触发时刻是随机生成的，单独一行，6 个点的列表不会把表格挤变形
    Write-Host ("  {0,-22} 触发时刻 {1}" -f "", $row.When) -ForegroundColor DarkGray
    Write-Host ("  {0,-22} 下次运行 {1}" -f "", $row.NextRunTime) -ForegroundColor DarkGray
}

Write-Host ""
$logFile = Join-Path (Split-Path -LiteralPath $signin) "signin.log"
Write-Host "日志：$logFile" -ForegroundColor Cyan
Write-Host "想确认是否跑通：" -ForegroundColor DarkGray
Write-Host "  Get-Content -LiteralPath '$logFile' -Tail 3"
Write-Host ""
Write-Host "卸载（两个一起删）：" -ForegroundColor DarkGray
Write-Host '  Unregister-ScheduledTask -TaskName "WorkBuddyAutoSignin" -Confirm:$false' -ForegroundColor DarkGray
Write-Host '  Unregister-ScheduledTask -TaskName "WorkBuddyGrowthPoll" -Confirm:$false' -ForegroundColor DarkGray
Write-Host ""
