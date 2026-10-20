# WorkBuddy 图形控制台

> 给 `signin.py` 配一个本地图形界面：单文件 exe、无终端窗口、与脚本同级分发。
>
> 代码位于 `dashboard/webview/`，产物 `Auto-claim-dashboard.exe` 落在仓库根目录（与 `signin.py` 并排）。

## 目标与边界

控制台只是**外壳**，不复制签到逻辑：

- 所有网络请求、凭据解密、请求头构造都复用 `signin.py` 自己的函数，上游脚本一行未改。
- 只调用 `signin.py` 已经存在的命令入口，不新增接口。
- 界面只暴露非静默命令（`status` / `auto` / `growth` / `claim` / `all`）。`silent*` 系列只写日志文件、stdout 为空，对界面无意义，因此不出现。
- `doctor` 保留后端能力但不设按钮：它对使用者没有可操作性，改为启动时自动跑一次，用于填充「凭据格式」卡片。

## 技术选型

| 方案 | 结论 |
|---|---|
| PyGui / pyfltk / DPG / Kivy | **放弃**。中文渲染存在字形缺失与 GBK 标题乱码，反复尝试未彻底解决 |
| Tauri | 放弃。本机没有 Rust 工具链 |
| **pywebview + 系统 WebView2** | **采用**。渲染交给 WebView2，中文字体问题从根上消失，且不需要前端构建步骤 |

两个必须注意的点：

1. **`webview.start(gui="edgechromium")` 不能省。** pywebview 6.2.1 在本机自动探测后端会失败（`webview.guilib` 始终为 `None`），表现为静默挂起：不建窗口、不报错。
2. **`create_window()` 没有 `icon` 参数**，图标属于 `webview.start(icon=...)`。

## 界面结构

自上而下：

1. 顶栏 —— 环境信息（Python 版本、平台、`signin.py` 定位结果）+ 主题切换
2. 4 张指标卡 —— 今日积分 / 连签天数 / 总积分 / 凭据格式
3. 操作按钮 —— 写操作带二次确认，主按钮 hover 加深而非变白
4. 脚本服务 —— 两个 Windows 计划任务的安装 / 启用 / 运行状态，以及安装、启停、立即运行、卸载操作
5. 成长中心 —— 独立整行，上方是 Buddy 旅行 / 补登 / 连登兑换 / 抽奖 / Buddy 盲盒概览卡，下方是云端任务，按「未完成 / 已完成」分页签，带 `当前/目标` 进度；**始终完整展开，不设列表内滚动**（窗口放不下时整页滚动）
6. 签到状态 + 当月日历 —— 同一行
7. 日志 —— 整行收尾，按级别着色带图标，支持一键复制

除运行日志外，各面板标题栏右上角都提供「刷新」按钮：脚本服务重新读取计划任务状态，成长中心重新拉取概览与任务，签到状态 / 本月签到重新查询签到数据。

外观细节：

- **跟随系统主题与强调色**：`dashboard/win_theme.py` 读注册表 `HKCU\Software\Microsoft\Windows\DWM\ColorizationColor`；强调色按比例调整明度，避免嵌套元素上使用 CSS `filter` 产生副作用。
- **图标自动反色**：界面内的纯线条 SVG 取 `currentColor`，只需设置容器的 `color`；窗口、任务栏和托盘图标由 `dashboard/win_icon.py` 根据 `AppsUseLightTheme` 在 `icon_light.ico`（深色线条）与 `icon_dark.ico`（浅色线条）之间切换，后台每 2 秒轮询一次，系统主题一变就同步。exe 静态图标 `icon.ico` 用深底白线，因为资源管理器不会替文件图标自动反色。几何与三套 ICO 由 `assets/make_icon.py`（Pillow）生成。
- **托盘图标**：`win_icon.TrayIcon` 通过 `Shell_NotifyIcon` 创建，右键菜单提供「打开控制台 / 退出」；资源管理器重启后会在 `TaskbarCreated` 消息中自动重新注册。设 `WORKBUDDY_TRAY=0` 可临时关闭。
- **标题栏**：`dashboard/win_title.py` 用 Win32 API 设置 Unicode 标题，绕开早期编码问题。

## 数据流

```text
前端  window.pywebview.api.<method>()   ──►  Api 方法
后端  _push(kind, payload)              ──►  window.__onBackend(kind, data)
```

启动顺序：

1. `bootstrap()` —— 返回环境信息与上次缓存结果（含 `growth.json` 快照），界面先用缓存填充卡片，避免开场一片「—」
2. 自动执行 `status`，随后 `doctor`
3. 后台静默拉取一次成长中心，不打断启动；`auto` / `growth` 执行完成后自动重拉一次

只有 `status` / `auto` / `claim` / `all` 完成后会更新顶部指标卡与签到详情；`growth` 的结果是另一套字段，只刷新成长中心，不会覆盖已有签到数据。失败或异常命令如果没有返回签到字段，也会保留上一次的显示值。

成长中心走 `GET /v2/activity/growth/{tasks,buddy/travel/status,streak,heatmap,redeem/summary,lottery/chances,energy,buddy/quota}`，认证链路直接复用 `signin.py` 的 `find_auth_file` / `resolve_session` / `build_headers` / `get`，不另写一份 crypto。所有请求都是 GET；只有 `run('auto' / 'growth')` 会触发写操作。

每次拉取会与 `%LOCALAPPDATA%\WorkBuddyDashboard\growth.json` 里的上次快照比较：

- 任务新增/移除、状态或进度变化
- Buddy 旅行状态、地点、今日名额
- 连签天数、补登卡余额、可补断签日期
- 连登兑换各档位状态
- 抽奖机会、能量、Buddy 盲盒是否可开、能量单价

有变化时逐条写进 `run.log`，同时推送到界面运行日志；`auto` / `growth` 执行完成后也会自动比较并记录，方便确认脚本到底改变了哪些数据。单次拉取部分接口失败时不会覆盖快照、也不会误报数据消失。

### 并发约束

`signin.py` 用模块级全局变量记录本轮时间预算（`_started_at` / `_budget_seconds`），并发进入会互相干扰。因此：

- 所有进入 `signin` 的路径共用一把模块级 `_SIGNIN_LOCK`。
- `tasks()` / `growth()` 用**非阻塞**加锁：`auto` 可能跑几分钟，拿数据时干等会让界面像卡死，拿不到就快速返回「有命令正在执行」。

## 脚本服务（Windows 计划任务）

控制台把后台签到注册成两个 Windows 计划任务，与 `install-windows.ps1` 创建的任务同名、同触发器：

| 任务 | 频率 | 内容 |
|---|---|---|
| `WorkBuddyAutoSignin` | 每天 00:05 | 签到 + 成长中心 |
| `WorkBuddyGrowthPoll` | 每天 01/05/09/13/17/21 点 | 补签（未签才签）+ 成长中心全套 |

面板显示每个任务的状态（就绪 / 运行中 / 已停用）、上次运行时间、上次结果和下次运行时间，并读取 `signin.log` 的最后一行作为最近活动。状态在空闲时每 60 秒静默刷新一次，任务运行中每 15 秒刷新一次；执行操作后立即刷新。

操作按钮：

- **安装 / 修复**：注册或更新两个任务。注册使用当前 Windows 账号、`Interactive`、`Limited` 权限，与手工安装脚本一致；需要二次确认。
- **启用 / 停用**：保留任务定义，只切换是否按计划自动运行。
- **立即运行**：只启动第一个可运行的任务（优先 `WorkBuddyAutoSignin`），避免两个任务同时进入签到 / 成长中心。
- **停止**：结束正在运行的任务。
- **卸载**：删除两个任务；需要二次确认。

计划任务执行的命令：

- 打包运行时：`Auto-claim-dashboard.exe --service silent` / `--service silent-poll`。这样任务不依赖系统 Python，也不需要打开控制台窗口。
- 源码运行时：`pythonw.exe dashboard/webview/app.py --service silent`。`_service_runner()` 会优先找 `python.exe` 同目录的 `pythonw.exe`，避免弹出控制台。

`--service` 分支不创建 WebView2，也不触发单实例锁；它直接调用 `Api._invoke()`，最终仍然走 `signin.py` 的 `_run()`。因此计划任务和手工执行使用同一套签到逻辑。

> 若「安装 / 修复」报 `0x80070005`（拒绝访问），用登录 WorkBuddy 的同一账号以管理员身份启动控制台后再安装。提权只用于注册任务，任务本身仍以普通权限运行。

## 单实例与 UI 看门狗

两者都在 `dashboard/win_guard.py`，只依赖 Win32 `ctypes`。

- **单实例**：启动时创建命名互斥量 `Local\WorkBuddyDashboard.SingleInstance`。第二个实例不会创建 WebView2，而是找到已有窗口并恢复/置前；已有窗口无响应时给出原生提示后退出。
- **UI 看门狗**：后台线程每 1 秒检查一次主窗口，同时看三路信号：Windows 自己的 `IsHungAppWindow()`（和任务管理器“未响应”、WER `AppHangB1` 同一套 5 秒判定）、`SendMessageTimeoutW(WM_NULL, 1000ms)`，以及前端每 5 秒一次的 `Api.heartbeat()` 端到端心跳。连续 2 次（约 2–3 秒）判定卡死时，写入所有 Python 线程栈、抓一份原生 minidump，然后 `os._exit(3)`，立即释放 WebView2 子进程和 DirectComposition 表面，避免整个 DWM/任务栏被拖住。

  只看 `WM_NULL` 是不够的：2026-10-20 那次 `AppHangB1` 里 Windows 已判定窗口 idle，旧版看门狗却一次失败都没记录到。现在每次判定正常也会每 2 分钟写一行 `探测正常`，用于区分“真的正常”和“看门狗根本没在跑”。

看门狗刻意不调用 `DestroyWindow`：窗口卡死时销毁操作本身也可能阻塞。自动退出后重新启动即可。调试时可设 `WORKBUDDY_WATCHDOG=0` 关闭。

## 运行时文件

全部落在用户目录，不在程序目录也不在临时解包目录：

```text
%LOCALAPPDATA%\WorkBuddyDashboard\
├── last_result.json   上次查询结果（下次启动先渲染，再刷新）
├── growth.json        上次成长中心快照（成长中心缓存 + 数据变化比较基线）
├── run.log            历史日志（界面日志区的持久化副本，含成长中心变化）
├── crash.log          Python 与前端 JS 的异常
├── watchdog.log       UI 看门狗日志；探测记录、卡死线程栈
├── dumps\             卡死时抓的原生 minidump（hang-*.dmp，最多保留 3 份）
└── trace.log          仅 WORKBUDDY_DEBUG=1 时产生，见下
```

## 调试开关

```powershell
$env:WORKBUDDY_DEBUG = "1"
.\Auto-claim-dashboard.exe
```

开启后：

- `_trace()` 在关键节点落盘（`main` 入口、`bootstrap`、`run`、任务条数、模块加载结果）
- `_probe()` 线程在启动数秒后**主动向 DOM 提问**，把页面真实状态写进 `trace.log`：

```json
{"ready":"complete","badge":"就绪","env":"Python 3.13.0 / win32",
 "envSignin":"signin.py 已找到","credit":"100","streak":"17","total":"1700",
 "credType":"sym-v1 加密","todo":"7","done":"9","tasks":7,
 "calSigned":19,"logLines":27,"jsErrors":[]}
```

`readyState` / 标题 / bridge 是否就绪 / 卡片与列表文本，这几项足以区分「页面没加载」「加载了但 JS 报错」「bridge 没通」三种情况——在此之前只能靠盯着窗口猜。

## 环境变量

| 变量 | 作用 |
|---|---|
| `WORKBUDDY_DEMO_SIGNIN` | 直接指定 `signin.py` 路径，优先级最高，跳过所有自动搜索 |
| `WORKBUDDY_DEBUG` | `1` 时启用上述追踪与探针 |
| `WORKBUDDY_WATCHDOG` | `0` 时关闭 UI 看门狗（仅用于调试） |
| `WORKBUDDY_HANG_DUMP` | `0` 时卡死不抓 minidump（只写线程栈） |
| `WORKBUDDY_DISABLE_GPU` | `1` 时给 WebView2 加 `--disable-gpu`，用于排查 DirectComposition 相关卡死 |
| `WORKBUDDY_TRAY` | `0` 时不创建托盘图标（仅用于调试/截图） |

## 运行方式

```powershell
# 源码运行（无终端窗口）
pythonw.exe dashboard/webview/app.py

# 打包
python dashboard/webview/build_exe.py
```

打包与校验流水线见 [gui-dashboard-packaging.md](gui-dashboard-packaging.md)；
开发过程中踩过的坑与排查方法见 [gui-dashboard-troubleshooting.md](gui-dashboard-troubleshooting.md)；
两次 `AppHangB1` 卡死事件的证据与看门狗改造见
[gui-dashboard-hang-analysis.md](gui-dashboard-hang-analysis.md)。
