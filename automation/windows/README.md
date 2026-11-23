# Windows 本地自动更新

签到流程完全交给 Windows 计划任务的 `silent` 服务完成，图形控制台（`Auto-Claim-dashboard.exe` 不带参数）**不会被任何后台流程自动启动或激活**，只在用户手动打开时运行。

策略落在三个地方，缺一环都会在别的路径上把窗口弹回来：

| 位置 | 行为 |
|---|---|
| `dashboard/webview/app.py::_surface_console` | 默认返回 `skipped`；失败自愈只剩「写失败记录 → 重新导入登录态 → 限时重试一次 → 回写」，全程在计划任务进程内 |
| `update-and-build.ps1` 的 `$LaunchConsole = $false` | 拉取 / 重编译后不再 `Start-Process` 补启控制台；编译前仍会关掉占用 exe 的进程以释放文件锁 |
| 计划任务描述 | 明确 `never launches the console`，避免日后按旧描述「修复」回弹窗行为 |

`AUTO_CLAIM_LAUNCH_CLIENT` 不在关闭范围内：它以隐藏窗口拉起 WorkBuddy / Qoder / Trae 桌面端，属于签到链路本身，不是控制台。该拉起路径会以 `NORMAL_PRIORITY_CLASS` 创建客户端并重置 Io/Memory 优先级，同时签到任务的 `<Priority>` 为 5（正常）；若需要彻底断开计划任务的优先级与 Job 继承，可另设 `AUTO_CLAIM_LAUNCH_VIA_SHELL=1` 委托 `explorer.exe` 启动。

## 安装

1. 把 `update-and-build.ps1` 复制到 `%LOCALAPPDATA%\AutoClaimUpdater\`；
2. 设置用户级环境变量，指向仓库根目录（注册表 HKCU\Environment，重开进程生效）：

   ```powershell
   [Environment]::SetEnvironmentVariable('AUTO_CLAIM_REPO', 'D:\path\to\Auto-claim', 'User')
   ```

3. 注册任务（无需改 XML，脚本用 ScheduledTasks 模块生成）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File register-auto-update.ps1
   ```

   卸载：`powershell -ExecutionPolicy Bypass -File register-auto-update.ps1 -Unregister`

任务每 2 小时运行一次，仅在用户登录时触发；`StartWhenAvailable=false`（错过不补跑）、`WakeToRun=false`（不唤醒机器）。刻意不启用 `RunOnlyIfNetworkAvailable`：安装目录里的 XML 若没写 `NetworkSettings.Id`，网络判定会把任务静默拒绝，表现为 `LastTaskResult=0x800710E0` 且日志一行不写。

用任务 XML 注册旧方式仍然兼容，但 XML 用 UTF-16LE 保存，改完要用 `Register-ScheduledTask -Xml ... -Force` 重新注册。

## 恢复旧的「自动弹控制台」行为

- 控制台：设置用户级 `WORKBUDDY_SURFACE_CONSOLE=1`；旧变量 `WORKBUDDY_NO_SURFACE=1` 优先级更高，需先删掉。
- 自动更新：把安装副本里的 `$LaunchConsole` 改成 `$true`。

注意：仓库内这两个 `.ps1` 必须保留 **UTF-8 BOM** 和 CRLF 换行。PowerShell 5.1 读取无 BOM 的 UTF-8 时会按 GBK 解码，中文注释会变成乱码甚至解析失败。

## 为什么当初没有提交

最初这三项都只改在本机：安装副本在 `%LOCALAPPDATA%\AutoClaimUpdater\`、环境变量在注册表、任务描述在任务计划程序数据库，仓库源码没动。这样不用重编译 exe（火绒按哈希信任的白名单不会失效），也不会让 `git pull --ff-only` 被未提交改动挡住。代价是换机器 / 重装系统会退回旧行为，别人 clone 也拿不到——本目录把这套配置变成可分发、可提交的安装模板。
