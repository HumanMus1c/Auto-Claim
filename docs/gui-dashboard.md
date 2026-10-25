# WorkBuddy 图形控制台

> 给 `signin.py`、`qoder_signin.py` 和 `trae_signin.py` 配一个本地图形界面：单文件 exe、无终端窗口、与脚本同级分发。
>
> 代码位于 `dashboard/webview/`，产物 `Auto-Claim-dashboard.exe` 落在仓库根目录（与三个签到脚本并排）。

## 目标与边界

控制台只是**外壳**，不复制签到逻辑：

- WorkBuddy 的所有网络请求、凭据解密、请求头构造都复用 `signin.py` 自己的函数；只在其 `run_doctor()` 中增加脱敏账号摘要，供账号卡片展示，不改变登录、签到和领取路径。
- Qoder CN 通过路径加载 `qoder_signin.py`；只在其 `run_doctor()` 中增加脱敏账号摘要，它只读取 Qoder 桌面端登录态，不接触 WorkBuddy 凭据。
- Trae 通过路径加载 `trae_signin.py`；后端移植自 TraeTools，只使用 Python 标准库，账号和签到历史独立存放。
- 只调用脚本已经存在的命令入口，不新增接口。
- WorkBuddy 界面只暴露非静默命令（`status` / `auto` / `growth` / `claim` / `all`）。`silent*` 系列只写日志文件、stdout 为空，对界面无意义，因此不出现。
- `doctor` 保留后端能力但不设独立按钮：启动时自动跑一次，用于填充「凭据格式」卡片和 WorkBuddy 账号卡片；账号卡片内提供「刷新账号」重新执行。
- Qoder 的 `status` / `doctor` / `auto` 都在控制台内调用；`status` 与 `doctor` 是只读查询，`auto` 才真实领取。
- Trae 的账号读取、签到历史与用量展示复用 `trae_signin.py`；`monitor()` 只读本地数据，`run_auto()` / `run_usage()` 才联网。

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

左侧是固定侧边栏，顶部仍为全局顶栏：

1. **侧边栏** —— 在「WorkBuddy」「Qoder CN」「Trae」三个监控页之间切换，记住上次选择（`localStorage` 的 `wbPage`）；三项各带连接状态圆点，左下角分别显示今日签到 / 领取结果：WorkBuddy 与 Trae 为「已签到 / 未签到」，Qoder 为「已领取 / 未领取」
2. 顶栏 —— 环境信息（Python 版本、平台、`signin.py` 定位结果）+ 主题切换

WorkBuddy 页自上而下：

1. 4 张指标卡 —— 今日积分 / 连签天数 / 总积分 / 凭据格式
2. 操作按钮 —— 写操作带二次确认，主按钮 hover 加深而非变白
3. 账号 —— 与 Trae / Qoder 共用的账号卡片网格；展示 WorkBuddy 本机账号的 UIN / UID 掩码、域名、状态、凭据格式和签到指标；卡片操作有「签到」（`claim` + `status`，二次确认）、「刷新」（`doctor` + `status`）和「查询签到」（仅 `status`）。WorkBuddy 账号来自本机客户端登录态，没有应用侧账号库，因此 Trae 的编辑 / 换设备 / 停用 / 删除不适用
4. 计划任务 —— 两个 Windows 计划任务的安装 / 启用 / 运行状态，以及安装、启停、立即运行、卸载操作
5. 成长中心 —— 独立整行，上方是 Buddy 旅行 / 补登 / 连登兑换 / 抽奖 / Buddy 盲盒概览卡，下方是云端任务，按「未完成 / 已完成」分页签，带 `当前/目标` 进度；**始终完整展开，不设列表内滚动**（窗口放不下时整页滚动）
6. 签到状态 + 当月日历 —— 同一行
7. 日志 —— 整行收尾，按级别着色带图标，支持一键复制

Qoder CN 页自上而下：

1. 4 张指标卡 —— 可领取 Credits / 本周期状态 / 登录态 / 计划任务
2. 操作按钮 —— 「刷新监控」（只读）和「领取今日 Credits」（真实写操作，二次确认）
3. 账号 —— 与 Trae / WorkBuddy 共用的账号卡片；展示 Qoder CN 头像、昵称、UID、掩码手机 / 邮箱、国内区域标记、令牌到期、设备与可领取 Credits；卡片操作有「签到」（`qoder_claim` → `auto`，二次确认）和「刷新」（`status` + `doctor`）。Qoder 凭据与设备标识来自本机 CN 客户端，没有应用侧账号库，因此编辑 / 换设备 / 停用 / 删除不适用
4. 计划任务 —— `QoderDailyCredits` 的安装 / 启用 / 运行状态与操作，独立于 WorkBuddy 两个任务
5. 活动与领取状态 + 登录态与设备 —— 同一行；左侧列出活动 key、Credits、领取状态和领取窗口，右侧显示账号目录、令牌来源 / 到期、设备标识与设备组件
6. Qoder 日志 —— 读取 `qoder_signin.log` 末尾内容；手动领取结果也会补写进去

Trae 页自上而下：

1. 4 张指标卡 —— 今日获得 / 剩余积分 / 签到账号 / 计划任务
2. 操作按钮 —— 「刷新监控」（只读）、「一键签到」（真实写操作，二次确认）、「导入本机登录态」「添加账号」「设置」
3. 账号列表 —— 与 Qoder / WorkBuddy 共用同一套账号卡片；每个账号显示头像、备注、来源、**国内 / 国际区域徽标**、签到状态、剩余积分与掩码后的设备号；可单独签到、刷新、设为主号、编辑、换设备、启停、删除
4. 计划任务 —— `TraeDailyCheckin` 的安装 / 启用 / 运行状态与操作，独立于 WorkBuddy 和 Qoder；直接放在账号卡片下方
5. 签到状态 + 本月日历 —— 同一行，均以主号为准
6. 用量统计 —— 选择 1 / 7 / 30 / 90 天，展示积分、Token、缓存命中、按模型汇总、逐会话明细与即将过期积分
7. 签到历史 —— 读取 SQLite，显示日期、账号、积分与会员标记
8. Trae 日志 —— 读取 `trae_signin.log` 末尾内容

添加 / 编辑账号使用模态框：凭据支持 Cloud-IDE-JWT Token 或 `X-Cloudide-Session` 会话 Cookie，两者同时提供时优先 Token；备注名、会员标记和启用自动签到都是本地字段。设置模态框保存自动签到时间、账号间隔、默认用量天数和飞书 Webhook；Webhook 不回显，可单独清除。

除运行日志外，各面板标题栏右上角都提供「刷新」按钮。Qoder / Trae 的账号、活动与本地历史在启动后由浏览器空闲时段自动异步预取，不需要先切页；切回页面时若数据超过 1 分钟会静默补拉，之后由手动刷新和计划任务状态轮询更新。WorkBuddy 页按区块懒加载——指标卡 / 成长中心 / 计划任务分别在自己的区块进入视口（含 240px 预取边距）时才发起查询，启动阶段只渲染 `bootstrap()` 带回的缓存。

外观细节：

- **跟随系统主题与强调色**：`dashboard/win_theme.py` 读注册表 `HKCU\Software\Microsoft\Windows\DWM\ColorizationColor`；强调色按比例调整明度，避免嵌套元素上使用 CSS `filter` 产生副作用。系统主题在**创建窗口之前**探测，原生窗口背景色与首帧图标直接匹配；页面内联脚本在样式表之前按本地选择或 `prefers-color-scheme` 上色，不等待 pywebview bridge，深色桌面启动不再先闪白约 1 秒。
- **先就绪再显示**：窗口以 `hidden=True` 创建，等 pywebview 的 `_pywebviewready`（兜底 `loaded`，再兜底 5 秒定时器）触发后才 `show()`。实测 `webview.start()` 到页面 loaded 固定要 1.9~2.1 秒，与页面资源多少无关（只有骨架的空页面也一样），所以启动时那段空白是 WebView2 初始化，不是界面内容太重；懒加载数据也缩短不了它。隐藏窗口只是把「先出现空白窗口、再刷出内容」换成「页面画好后再出现」，首次可见时已经有完整的骨架和缓存数据。
- **主题切换**：顶栏按钮点开三选项菜单——「跟随系统 / 浅色 / 深色」。默认跟随系统；手动选择会记在 `localStorage` 的 `wbTheme` 里，系统主题变化时不再覆盖手动选择。`prefers-color-scheme` 变化与 `win_icon` 的原生主题推送共同负责运行期同步。
- **图标自动反色**：界面内的纯线条 SVG 取 `currentColor`，只需设置容器的 `color`；窗口、任务栏和托盘图标由 `dashboard/win_icon.py` 根据 `AppsUseLightTheme` 在 `icon_light.ico`（深色线条）与 `icon_dark.ico`（浅色线条）之间切换，后台每 2 秒轮询一次，系统主题一变就同步。exe 静态图标 `icon.ico` 用深底白线，因为资源管理器不会替文件图标自动反色。几何与三套 ICO 由 `assets/make_icon.py`（Pillow）生成。
- **托盘图标**：`win_icon.TrayIcon` 通过 `Shell_NotifyIcon` 创建，右键菜单提供「打开控制台 / 退出」；资源管理器重启后会在 `TaskbarCreated` 消息中自动重新注册。设 `WORKBUDDY_TRAY=0` 可临时关闭。
- **标题栏**：`dashboard/win_title.py` 用 Win32 API 设置 Unicode 标题，绕开早期编码问题。

## 数据流

```text
前端  window.pywebview.api.<method>()   ──►  Api 方法
后端  _push(kind, payload)              ──►  window.__onBackend(kind, data)
```

启动顺序：

1. 窗口以隐藏状态创建；`bootstrap()` 返回环境信息与上次缓存结果（含 `growth.json` 快照），界面先用缓存填充卡片，避免开场一片「—」
2. 页面首帧就绪后显示窗口（见上文「先就绪再显示」）
3. 各区块按可见性懒加载：指标卡区块进入视口时自动执行 `status`，随后 `doctor`；成长中心区块进入视口时静默拉取成长中心；计划任务区块进入视口时才查询计划任务。`status` / `doctor` 与 `growth` 共用后端 `signin` 锁，因此成长中心会等签到查询跑完再发起
4. `auto` / `growth` 执行完成后仍会自动重拉一次成长中心
5. Qoder 数据在启动后由 `requestIdleCallback`（旧版 WebView2 兜底为定时器）异步预取，调用 `qoder_monitor()` 读取活动与登录态；切回页面时若缓存超过 1 分钟会静默补拉，点击「刷新监控」或领取完成后也会刷新
6. Trae 数据同样在启动后异步预取，调用 `trae_monitor()` 读取本地账号、历史、用量与日志；只有「一键签到」「刷新状态」「同步用量」「导入本机登录态」才会进入后台任务
7. 停在 Qoder / Trae 页或窗口最小化 / 隐藏时跳过无意义的计划任务状态轮询；切回 WorkBuddy 页会重新判定未加载区块的可见性

懒加载按数据源分区而不是一张卡一个请求：顶部四张卡共用同一次 `status` / `doctor` 查询，成长中心的概览卡与任务列表共用 `growth`，拆开只会增加往返和后端锁竞争。Qoder / Trae 的启动预取按页错开 1 秒，避免两个网络 / PowerShell 查询同时起跑。它只是把非首屏的查询推迟，并不会缩短 WebView2 的初始化时间；如果窗口高度足够、区块本来就在视口内，行为与启动即查询一致。

只有 `status` / `auto` / `claim` / `all` 完成后会更新顶部指标卡与签到详情；`growth` 的结果是另一套字段，只刷新成长中心，不会覆盖已有签到数据。失败或异常命令如果没有返回签到字段，也会保留上一次的显示值。

Qoder 的 `qoder_monitor()` 依次执行 `status`（查询活动）与 `doctor`（离线检查登录态），返回 `status` / `doctor` / 日志快照；手动领取由 `qoder_claim()` 启动后台线程，执行完成后通过 `qoder_log` 和 `qoder_done` 事件推送结果。两条链路都不读写 WorkBuddy 的缓存或锁。

Trae 的 `trae_monitor()` 不联网：它读取 `accounts.json`、`checkin.db`、最近用量快照与日志，并返回 `accounts` / `active_id` / `settings` / `history` / `usage` / `clients` / `doctor` / `log`。签到、刷新、用量和导入由 `_start_trae()` 启动后台线程；忙碌时监控接口返回缓存并带 `busy: true`，结果通过 `trae_log` 和 `trae_done` 事件推送。账号公开数据只含 Token / Session 掩码，不返回原文。签到完成后会重新查询状态再写历史，避免把「接口返回成功」误当成实际到账。导入本机登录态时，后端用 Windows CNG 解密 `storage.json` 里的 `iCubeAuthInfo://icube.cloudide`（AES-128-CBC/PKCS#7，明文带 SHA-512 校验），再按 `account_uid` 合并多客户端结果，优先保留 `Trae CN` 的活动账号。

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

Qoder 使用独立的 `_QODER_LOCK`，与 WorkBuddy 的 `_SIGNIN_LOCK` 互不等待。`qoder_signin.py` 也靠模块级 `_started_at` 记时间预算，所以每次调用前都会重置；它只用 `emit()` 输出，控制台在调用期间临时替换 `emit()` 来捕获 JSON，**不重定向 `sys.stdout`**——否则两个后台线程会互相截走输出。`ALREADY` / `INACTIVE` 原本会走 quiet 分支不打印，现在也一并捕获，页面能显示真实领取结果。

Trae 同样使用独立的 `_TRAE_LOCK` 和 `_trae_busy` 状态。`trae_signin.py` 的 `_started_at` 在每次进入控制台任务前重置；后台线程不会与 WorkBuddy / Qoder 争锁。计划任务状态轮询由 `trae_service_status()` 单独负责，使用 `TraeDailyCheckin` 一个任务清单。

## 计划任务（Windows 任务计划程序）

控制台把后台签到注册成两个 Windows 计划任务，与 `install-windows.ps1` 创建的任务同名、同触发器：

| 任务 | 频率 | 内容 |
|---|---|---|
| `WorkBuddyAutoSignin` | 每天 00:05 | 签到 + 成长中心 |
| `WorkBuddyGrowthPoll` | 每天 01/05/09/13/17/21 点 | 补签（未签才签）+ 成长中心全套 |

面板显示每个任务的状态（就绪 / 运行中 / 已停用）、上次运行时间、上次结果和下次运行时间，并读取 `signin.log` 的最后一行作为最近活动。状态在空闲时每 3 分钟静默刷新一次，任务运行中每 20 秒刷新一次；执行操作后立即刷新。每次刷新都要起一次 PowerShell，所以前台停在 Qoder / Trae 页或窗口不可见时会跳过本轮，不做无用查询。

操作按钮：

- **安装 / 修复**：注册或更新两个任务。注册使用当前 Windows 账号、`Interactive`、`Limited` 权限，与手工安装脚本一致；需要二次确认。
- **启用 / 停用**：保留任务定义，只切换是否按计划自动运行。
- **立即运行**：只启动第一个可运行的任务（优先 `WorkBuddyAutoSignin`），避免两个任务同时进入签到 / 成长中心。
- **停止**：结束正在运行的任务。
- **卸载**：删除两个任务；需要二次确认。

计划任务执行的命令：

- 打包运行时：`Auto-Claim-dashboard.exe --service silent` / `--service silent-poll`。这样任务不依赖系统 Python，也不需要打开控制台窗口。
- 源码运行时：`pythonw.exe dashboard/webview/app.py --service silent`。`_service_runner()` 会优先找 `python.exe` 同目录的 `pythonw.exe`，避免弹出控制台。

`--service` 分支不创建 WebView2，也不触发单实例锁；它直接调用 `Api._invoke()`，最终仍然走 `signin.py` 的 `_run()`。因此计划任务和手工执行使用同一套签到逻辑。

> 若「安装 / 修复」报 `0x80070005`（拒绝访问），用登录 WorkBuddy 的同一账号以管理员身份启动控制台后再安装。提权只用于注册任务，任务本身仍以普通权限运行。

### QoderDailyCredits（独立任务）

Qoder 页的「计划任务」卡片只操作 `QoderDailyCredits` 一个任务，不会安装、启停或卸载 WorkBuddy 的两个任务。触发点与 `install-qoder-windows.ps1` 一致：按 UTC+8 每天 10:00 活动刷新，主触发点晚 5 分钟，另排 0/1/5/9/13 小时四次补跑，换算成本机时间后依次为 10:05、11:05、15:05、19:05、23:05（以 UTC+8 为例）。任务动作固定为：

```text
pythonw.exe <qoder_signin.py 的绝对路径> silent
```

- **安装 / 修复**：由 `win_service.install_qoder()` 直接注册任务，自动查找 `pythonw.exe`（当前解释器同目录、`PATH`、常见 Python 安装目录），注册后回读任务名和下次运行时间；需要二次确认。
- **启用 / 停用 / 立即运行 / 停止 / 卸载**：与 WorkBuddy 任务共用同一套 PowerShell 操作封装，但任务清单只有 `QoderDailyCredits`。
- **状态刷新**：任务运行中每 20 秒、空闲时每 180 秒读取一次状态，并显示 `qoder_signin.log` 最后一行。
- **手动领取**：调用 `qoder_signin.py auto`，不依赖计划任务；结果同时写入 Qoder 页日志和 `qoder_signin.log`，刷新后仍可追溯。

Qoder 任务依赖系统 Python；打包后的控制台 exe 不会被用作 Qoder 任务的运行器。若机器上没有 `pythonw.exe`，「安装 / 修复」会直接报错，而不会注册一个跑不起来的任务。

### TraeDailyCheckin（独立任务）

Trae 页的「计划任务」卡片只操作 `TraeDailyCheckin`。默认以本地时间 08:05 为主触发点，再按 +2 / +5 / +9 / +13 小时补跑四次，覆盖漏签、睡眠唤醒和短暂断网；主时间可在 Trae 设置里修改，修改后需重新「安装 / 修复」才会写入任务。任务动作固定为：

```text
pythonw.exe <trae_signin.py 的绝对路径> silent
```

- **安装 / 修复**：`win_service.install_trae()` 注册或更新任务，使用 `StartWhenAvailable`，执行时限 10 分钟；注册后回读任务与下次运行时间。需要二次确认。
- **启用 / 停用 / 立即运行 / 停止 / 卸载**：复用同一套 PowerShell 操作封装，但任务清单只有 `TraeDailyCheckin`。
- **状态刷新**：任务运行中每 20 秒、空闲时每 180 秒读取一次状态，并显示 `trae_signin.log` 最后一行。
- **手动签到**：调用 `trae_signin.py auto`，不依赖计划任务，结果同时写入 Trae 页日志和 `trae_signin.log`。

与 Qoder 一样，Trae 任务依赖系统 Python；`pythonw.exe` 缺失时安装会直接失败。它不会复用控制台 exe 的 `--service` 分支。

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

Qoder 是独立脚本，默认日志 `qoder_signin.log` 落在 `qoder_signin.py` 同目录（源码运行即仓库根目录），不在 `%LOCALAPPDATA%` 下；可用 `QODER_SIGNIN_LOG` 改到别处。打包后同样要求 `qoder_signin.py` 与 exe 并排，和 `signin.py` 的分发方式一致。

Trae 的账号和历史单独放在 `%APPDATA%\Auto-Claim\trae\`：

```text
accounts.json    多账号；Token / Session 在 Windows 上用 DPAPI 加密
checkin.db       签到历史与每日积分快照（SQLite）
```

默认日志 `trae_signin.log` 同样落在脚本同目录，可用 `TRAE_SIGNIN_LOG` 覆盖；`TRAE_DATA_DIR` 可整体迁移数据目录。打包后要求 `trae_signin.py` 与 exe 并排。

## 调试开关

```powershell
$env:WORKBUDDY_DEBUG = "1"
.\Auto-Claim-dashboard.exe
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
| `WORKBUDDY_QODER_SIGNIN` / `QODER_SIGNIN` | 直接指定 `qoder_signin.py` 路径，跳过自动搜索 |
| `WORKBUDDY_TRAE_SIGNIN` / `TRAE_SIGNIN` | 直接指定 `trae_signin.py` 路径，跳过自动搜索 |
| `QODER_SIGNIN_LOG` | Qoder 日志路径，同时也是监控页读取的日志 |
| `QODER_AUTH_DIR` / `QODER_TOKEN` / `QODER_BASE_URL` | 透传给 `qoder_signin.py`：登录态目录、直接提供令牌、覆盖 API 地址 |
| `TRAE_DATA_DIR` | Trae 账号与签到历史目录，默认 `%APPDATA%\Auto-Claim\trae` |
| `TRAE_SIGNIN_LOG` | Trae 日志路径，同时也是监控页读取的日志 |
| `TRAE_BASE_URL` | 覆盖 Trae API 端点，默认 `https://api.trae.cn` |
| `WORKBUDDY_DEBUG` | `1` 时启用上述追踪与探针 |
| `WORKBUDDY_WATCHDOG` | `0` 时关闭 UI 看门狗（仅用于调试） |
| `WORKBUDDY_HANG_DUMP` | `0` 时卡死不抓 minidump（只写线程栈） |
| `WORKBUDDY_DISABLE_GPU` | `1` 时给 WebView2 加 `--disable-gpu`，用于排查 DirectComposition 相关卡死 |
| `WORKBUDDY_INSTANT_WINDOW` | `1` 时恢复「窗口立刻显示」（WebView2 初始化期间会有一段空白底色），默认等首帧就绪后再显示 |
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
