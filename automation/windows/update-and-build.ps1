# update-and-build.ps1
# ----------------------------------------------------------------------------
# Auto-Claim 本地自动更新（由计划任务 AutoClaim-AutoUpdate 每 2 小时调用）：
#   1) 判断系统是否处于唤醒状态；休眠/睡眠直接退出
#   2) 用 GitHub Desktop 自带 git 拉取最新 commit
#   3) 仅在有新提交（或 -ForceRebuild）时重新编译，不启动图形控制台
#   4) 签到流程完全交给计划任务的 silent 服务，控制台只在用户手动打开时运行
#
# 本文件是仓库内的权威模板。安装时复制到 %LOCALAPPDATA%\AutoClaimUpdater\，
# 并设置用户级环境变量 AUTO_CLAIM_REPO 指向仓库根目录。日志随安装副本落在
# 同一目录，不写进仓库。
#
# 用法：
#   powershell -File update-and-build.ps1                 # 常规（无更新则跳过）
#   powershell -File update-and-build.ps1 -ForceRebuild   # 强制重新编译
# ----------------------------------------------------------------------------

param(
    [switch]$ForceRebuild
)

$ErrorActionPreference = 'Continue'

function Log($msg) {
    $ts   = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$ts] $msg"
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
    # 必须走 Host 流：Write-Output 会把日志字符串塞进调用方的返回值，
    # Test-SystemAwake 之类「返回布尔」的函数会因此永远返回非空数组（真值），
    # 休眠判断形同虚设——这里踩过一次。
    Write-Host $line
}

# ===== 可配置项 ============================================================
# 安装到仓库之外的目录时，用用户级环境变量 AUTO_CLAIM_REPO 指定仓库根目录：
#   [Environment]::SetEnvironmentVariable('AUTO_CLAIM_REPO', 'D:\path\to\Auto-claim', 'User')
$RepoPath = $env:AUTO_CLAIM_REPO
if (-not $RepoPath) {
    $inRepo = Join-Path $PSScriptRoot '..\..'
    if (Test-Path (Join-Path $inRepo 'dashboard\webview\build_exe.py')) {
        $RepoPath = (Resolve-Path $inRepo).Path
    }
}

# 是否额外用 GitHub Desktop 打开仓库以同步其界面（会弹一下窗口）。
$OpenGitHubDesktopUI = $false

# 签到流程完全由计划任务托管：本脚本只负责拉取与重编译，不再主动启动 / 补启
# 图形控制台。需要恢复旧行为（编译后自动打开窗口）时改成 $true 即可。
$LaunchConsole = $false
# ==========================================================================

$LogFile = Join-Path $PSScriptRoot 'update-and-build.log'

if (-not $RepoPath) {
    Write-Host "未指定仓库路径：请设置用户级环境变量 AUTO_CLAIM_REPO 指向仓库根目录。"
    exit 1
}

# 日志轮转：超过 200KB 时仅保留最近约 100KB，避免长期运行后无限膨胀。
try {
    if (Test-Path $LogFile) {
        $sz = (Get-Item $LogFile).Length
        if ($sz -gt 200KB) {
            $tail = Get-Content -Path $LogFile -Tail 6000 -ErrorAction SilentlyContinue
            Set-Content -Path $LogFile -Value $tail -Encoding UTF8 -ErrorAction SilentlyContinue
        }
    }
} catch {}

Log "===== 自动更新开始（仓库: $RepoPath；ForceRebuild=$ForceRebuild）====="

# ===== 0) 基本校验 =========================================================
if (-not (Test-Path $RepoPath)) {
    Log "仓库路径不存在：$RepoPath，终止。"
    exit 1
}

# ===== 1) 休眠 / 睡眠判断 =====================================================
function Test-SystemAwake {
    # Win32_ComputerSystem.PowerState: 1=Full Power, 2=Low Power, 3=Standby,
    # 5=Hibernate, 6=Power Cycle, 7=Power Off, 8=Hibernating; 0/4=Unknown。
    # 0 和 4 只说明「查不到」，不能当作睡眠，否则常年报告 0 的机器永远不会更新。
    try {
        $cs = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
        $state = $cs.PowerState
        if ($state -eq 1) {
            # 完全供电
        } elseif ($state -eq 0 -or $state -eq 4) {
            Log "系统电源状态为 $state（未知），按唤醒处理。"
        } else {
            Log "系统电源状态为 $state（非完全供电），判定为休眠/睡眠，跳过本次。"
            return $false
        }
    } catch { Log "读取 Win32_ComputerSystem.PowerState 失败：$_" }

    # 电源事件只写日志、不再据此跳过。唤醒事件在新版 Windows 上是 107，
    # 旧脚本只查 1，于是「有睡眠、无唤醒」，把醒着的机器判成睡着，更新永久停摆。
    # 真正的兜底是任务本身 StartWhenAvailable=false + WakeToRun=false。
    try {
        $sleepEvt  = Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=42} -MaxEvents 1 -ErrorAction SilentlyContinue
        $resumeEvt = Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=@(1,107)} -MaxEvents 1 -ErrorAction SilentlyContinue
        $sTxt = if ($sleepEvt)  { $sleepEvt.TimeCreated.ToString('MM-dd HH:mm:ss') }  else { '无' }
        $rTxt = if ($resumeEvt) { $resumeEvt.TimeCreated.ToString('MM-dd HH:mm:ss') } else { '无' }
        Log "电源事件参考：最近睡眠 $sTxt，最近唤醒 $rTxt（不阻断更新）。"
    } catch { Log "读取电源事件失败：$_" }

    return $true
}

if (-not (Test-SystemAwake)) {
    Log "===== 因系统休眠/睡眠，本次任务结束 ====="
    exit 0
}
Log "系统处于唤醒状态，继续执行。"

# ===== 2) 定位 git 并拉取 =====================================================
function Get-GitHubDesktopGit {
    $base = Join-Path $env:LOCALAPPDATA 'GitHubDesktop'
    if (-not (Test-Path $base)) { return $null }
    $apps = Get-ChildItem (Join-Path $base 'app-*') -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending
    foreach ($app in $apps) {
        $g = Join-Path $app.FullName 'resources\app\git\cmd\git.exe'
        if (Test-Path $g) { return $g }
    }
    return $null
}

$gitExe = Get-GitHubDesktopGit
if (-not $gitExe) {
    $gitExe = (Get-Command git -ErrorAction SilentlyContinue).Source
}
if (-not $gitExe) {
    Log "未找到 git（GitHub Desktop 自带或系统 PATH），无法拉取，终止。"
    exit 1
}
Log "使用 git: $gitExe"

if ($OpenGitHubDesktopUI) {
    $uri = "github-desktop://openRepo/$([uri]::EscapeDataString($RepoPath))"
    Log "打开 GitHub Desktop 仓库：$uri"
    Start-Process $uri -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

# 用 cmd /c 运行 git 并在 cmd 层合并 stderr：
#  1) PowerShell 不会收到 "git.exe : From ..." 的错误记录；
#  2) 规避 Start-Process 在本机会话同时含 http_proxy / HTTP_PROXY 时抛
#     「字典中的关键字重复」。
$pullUpToDate = $false
try {
    $gitCmdLine = '"{0}" -C "{1}" pull --ff-only 2>&1' -f $gitExe, $RepoPath
    $gitRaw = cmd /c $gitCmdLine 2>$null
    $gitLines = if ($gitRaw -is [array]) { $gitRaw } else { @($gitRaw) }
    $gitExit = $LASTEXITCODE
    Log "git pull 输出："
    if ($gitLines.Count -gt 0 -and ($gitLines -join '').Trim().Length -gt 0) { Log ($gitLines -join "`n") } else { Log "(无输出，仓库已是最新)" }
    if ($gitExit -ne 0) { Log ("git pull 返回非零退出码 {0}。" -f $gitExit) }
    $pullUpToDate = ($gitLines -join "`n") -match 'Already up to date\.'
} catch {
    Log "git pull 执行异常：$_"
}

# ===== 定位产物名称（从 build_exe.py 读取 NAME，避免仓库改名后硬编码脱节）=====
$exeName = $null
$buildPy = Join-Path $RepoPath 'dashboard\webview\build_exe.py'
if (Test-Path $buildPy) {
    $src = Get-Content -Raw -Path $buildPy
    if ($src -match 'NAME\s*=\s*[''"]+([^''"]+)[''"]+') { $exeName = $Matches[1] }
}
if (-not $exeName) {
    $cands = Get-ChildItem -Path $RepoPath -Filter '*dashboard*.exe' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($cands) { $exeName = $cands.Name }
}
if (-not $exeName) { $exeName = 'Auto-Claim-dashboard.exe' }
$exe = Join-Path $RepoPath $exeName
Log "本次产物名称：$exeName"

$DoUpdate = (-not $pullUpToDate) -or $ForceRebuild

if (-not $DoUpdate) {
    Log "git 无新提交且无 -ForceRebuild，跳过重新编译。"
    if (-not $LaunchConsole) {
        Log "控制台由计划任务托管，不再主动补启（需要时手动打开）。"
    } else {
        $alive = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path -eq $exe }
        if (-not $alive) {
            if (Test-Path $exe) { Log "产物未运行，启动：$exe"; Start-Process $exe -ErrorAction SilentlyContinue }
            else { Log "未找到产物 $exe，无法启动。" }
        }
    }
} else {
    # ===== 3) 重新编译 ============================================================
    Log "开始重新编译 build_exe.py ..."
    $buildScript = Join-Path $RepoPath 'dashboard\webview\build_exe.py'

    $pythonExe = $null
    $venvPy = Join-Path $RepoPath 'venv\Scripts\python.exe'
    if (Test-Path $venvPy) { $pythonExe = $venvPy }
    $managedVenv = Join-Path $env:USERPROFILE '.workbuddy\binaries\python\envs\default\Scripts\python.exe'
    if (-not $pythonExe -and (Test-Path $managedVenv)) { $pythonExe = $managedVenv }
    if (-not $pythonExe) { $pythonExe = (Get-Command python  -ErrorAction SilentlyContinue).Source }
    if (-not $pythonExe) { $pythonExe = (Get-Command python3 -ErrorAction SilentlyContinue).Source }

    $buildOk = $false
    if (-not $pythonExe) {
        Log "未找到可用的 python，跳过编译。"
    } elseif (-not (Test-Path $buildScript)) {
        Log "未找到 build_exe.py：$buildScript，跳过编译。"
    } else {
        Log "使用 python: $pythonExe"
        # 构建前关掉正在运行的产物，否则 PyInstaller 覆盖同名 exe 会 PermissionError。
        # 这里只是释放文件锁，构建结束后不再自动重启窗口。
        $running = Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -and $_.Path.StartsWith($RepoPath, [System.StringComparison]::OrdinalIgnoreCase) -and $_.Path -like '*dashboard*.exe'
        }
        foreach ($p in $running) {
            Log ("关闭正在运行的产物进程（PID={0}, {1}）以释放文件锁..." -f $p.Id, $p.Path)
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
        if ($running) { Start-Sleep -Seconds 1 }

        try {
            $buildOut = & $pythonExe $buildScript 2>&1 | Out-String
            Log "编译输出："
            Log $buildOut
            Log ("编译结束（rc=$LASTEXITCODE）。")
            $buildOk = ($LASTEXITCODE -eq 0)
        } catch {
            Log "编译异常：$_"
        }
    }

    if (-not $buildOk) {
        Log "编译未成功，保留上一次成功构建的产物：$exe"
    }

    # ===== 4) 不再主动唤起控制台 ==================================================
    if ($LaunchConsole -and (Test-Path $exe)) {
        Log "按配置启动产物：$exe"
        Start-Process $exe -ErrorAction SilentlyContinue
    } else {
        Log "编译完成，未启动控制台：签到由计划任务静默托管，需要时手动打开。"
    }
}

Log "===== 自动更新结束 ====="
