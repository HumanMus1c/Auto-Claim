# 同步代码后如何更新计划任务（Windows）

> 适用场景：其它机器 `git pull` 同步了「计划任务降权继承」修复之后，需要知道自己该做什么。
>
> 核心结论：**代码更新不等于计划任务更新**。`git pull` 只改仓库文件，不会改 Windows
> 任务计划程序数据库里已经注册的任务。旧任务仍保留 `<Priority>7</Priority>`
> （= `BELOW_NORMAL_PRIORITY_CLASS`），宿主进程依旧降权启动，下一次签到拉起桌面端时
> 就又回到老状态。同步代码后必须按本文更新一次任务。

## 一、先确认是否需要更新

只读检查，不会改动任何东西：

```powershell
$names = 'WorkBuddyAutoSignin','WorkBuddyGrowthPoll','QoderDailyCredits','TraeDailyCheckin'
foreach ($n in $names) {
    $t = Get-ScheduledTask -TaskName $n -TaskPath '\' -ErrorAction SilentlyContinue
    if ($t) {
        [pscustomobject]@{
            Task = $n
            Priority = $t.Settings.Priority   # 7 = 需要更新；5 = 已更新
            State = $t.State
            NextRun = (Get-ScheduledTaskInfo -TaskName $n).NextRunTime
        }
    }
}
```

也可以看任务 XML（`<Priority>` 节点）确认：

```powershell
schtasks /Query /TN "WorkBuddyAutoSignin" /XML
```

结果里 `<Priority>7</Priority>` 是需要更新，`<Priority>5</Priority>` 是新版本。

## 二、更新步骤

### 0. 同步代码，并确认运行的是新代码

```powershell
git pull --ff-only
```

- **源码运行**（`pythonw.exe ...` / `pythonw.exe dashboard/webview/app.py`）：拉完代码即生效，无需别的操作。
- **打包版**（`Auto-Claim-dashboard.exe`）：`client_launcher.py` 被打进 exe，必须重新打包：

  ```powershell
  python dashboard/webview/build_exe.py
  ```

  已安装 `AutoClaim-AutoUpdate` 任务的机器会在 2 小时内自动 pull + rebuild；想立刻重建可运行
  `powershell -File "$env:LOCALAPPDATA\AutoClaimUpdater\update-and-build.ps1" -ForceRebuild`。
  只 pull 不 rebuild，任务里跑的还是旧 exe，优先级修复不会生效。

### 1. 重新注册签到任务（推荐）

任选一种，都会以 `Priority=5` 重新注册：

- **控制台**：对应模块的「计划任务」卡片 →「安装 / 修复」（WorkBuddy / Qoder / Trae 三个模块各自操作自己的任务），按提示二次确认。
- **安装脚本**（在仓库根目录执行）：

  ```powershell
  powershell -ExecutionPolicy Bypass -File install-windows.ps1
  powershell -ExecutionPolicy Bypass -File install-qoder-windows.ps1
  ```

  Trae 的任务目前只在控制台里安装 / 修复。

注意：重新注册会重新抽取随机触发时刻，这是预期行为（同一个基准点每次都会重新打散）。
如果希望**保留现有触发时刻**，改用下面的方法 2。

### 2. 只改优先级、保留触发时刻

适合不想重排时刻的机器：

```powershell
$tasks = 'WorkBuddyAutoSignin','WorkBuddyGrowthPoll','QoderDailyCredits','TraeDailyCheckin'
foreach ($n in $tasks) {
    $t = Get-ScheduledTask -TaskName $n -TaskPath '\' -ErrorAction SilentlyContinue
    if ($t) {
        $s = $t.Settings
        $s.Priority = 5
        Set-ScheduledTask -TaskName $n -TaskPath '\' -Settings $s | Out-Null
    }
}
```

（`Set-ScheduledTask` 在 Windows PowerShell 5.1 不支持 `-WhatIf`，不要加这个参数。）

### 3. 自动更新任务（可选）

`AutoClaim-AutoUpdate` 不拉起桌面端，优先级对它没有实际影响，可不改。若想统一，在任务
计划程序里把它属性的「优先级」设为「正常」，或用方法 2 的写法把任务名换成
`AutoClaim-AutoUpdate`。改动只影响更新任务本身，与签到链路无关。

### 4. 按需要设置环境变量

两个开关都是用户级环境变量，计划任务进程在**启动时**读取，因此设置后下一次任务运行才生效；
已经开着的控制台需要重启。

```powershell
# 完全不自动拉起桌面端（签到仍继续，只是不检查/启动 WorkBuddy / Qoder / Trae）
[Environment]::SetEnvironmentVariable('AUTO_CLAIM_LAUNCH_CLIENT','0','User')

# 或：改由 explorer.exe 委托启动，彻底断开计划任务的优先级与 Job 继承链
[Environment]::SetEnvironmentVariable('AUTO_CLAIM_LAUNCH_VIA_SHELL','1','User')
```

取消设置：把上面的值改成 `''`（空串）即可。

## 三、验证更新结果

```powershell
# 1) 任务优先级应为 5
$t = Get-ScheduledTask -TaskName 'WorkBuddyAutoSignin' -TaskPath '\'
$t.Settings.Priority

# 2) 手动跑一次，确认任务成功
Start-ScheduledTask -TaskName 'WorkBuddyAutoSignin'
Start-Sleep -Seconds 20
Get-ScheduledTaskInfo -TaskName 'WorkBuddyAutoSignin' | Select-Object LastRunTime, LastTaskResult

# 3) 客户端进程的优先级类应为 Normal，而不是 BelowNormal
Get-Process WorkBuddy,Qoder*,Trae* -ErrorAction SilentlyContinue |
    Select-Object ProcessName, Id, PriorityClass
```

`LastTaskResult = 0` 表示任务本身执行成功。若任务已修好但客户端仍是 `BelowNormal`，
说明计划任务实际运行的还是旧 exe，回到步骤 0 重新打包。

`IoPriority` / `MemoryPriority` 无法用 `Get-Process` 查看；新 launcher 会在拉起时通过
`SetProcessInformation` / `NtSetInformationProcess` 把它们改回 5 / 2。

## 四、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 同步了代码，任务还是 `Priority=7` | 正常：git 不写任务计划程序数据库，必须执行第二节的更新步骤 |
| 重装后触发时刻变了 | 「安装 / 修复」会按基准点重新随机；只想改优先级请用方法 2 |
| 客户端进程仍是 `BelowNormal` | 打包版没重建 exe；或还有旧任务残留，检查所有相关任务 |
| PowerShell 改完脚本报中文乱码 / 解析错误 | 安装脚本必须保留 UTF-8 BOM + CRLF，不要直接用无 BOM 的编辑器另存 |
| 想手工改任务 XML | XML 是 UTF-16LE，改完要用 `Register-ScheduledTask -Xml ... -Force` 重新注册，不要直接覆盖系统里的任务文件 |
| 重装时提示参数错误 / 拒绝访问 | 任务主体必须是当前账号 `Interactive` + `Limited`，不要用 SYSTEM 或「最高权限」注册 |

## 五、这次改动涉及的文件

| 文件 | 作用 |
|---|---|
| `client_launcher.py` | 拉起客户端时指定 `NORMAL_PRIORITY_CLASS`，并重置 Io/Memory 优先级；可选 `AUTO_CLAIM_LAUNCH_VIA_SHELL` 委托外壳 |
| `install-windows.ps1`、`install-qoder-windows.ps1` | 新注册 / 重装任务时写入 `Priority=5` |
| `dashboard/win_service.py` | 控制台「安装 / 修复」写入 `Priority=5`（WorkBuddy / Qoder / Trae） |
| `README.md` | 原理说明与快速修复命令 |
| `docs/scheduled-task-update.md` | 本文，供其它机器同步后照做 |
