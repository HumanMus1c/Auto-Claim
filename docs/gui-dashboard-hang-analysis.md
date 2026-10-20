# 卡死事件分析：2026-10-19 / 2026-10-20 的 `AppHangB1`

> 现象：控制台窗口失去响应，**整个桌面无法切换窗口**；结束
> `Auto-claim-dashboard.exe` 后瞬间恢复。两天内发生两次。
>
> 这份文档只记录有证据的部分，并把“已确认”“已排除”“仍未确认”分开写，
> 避免下次又把猜测当成结论。

## 结论

1. **应用确实卡死了，不是错觉。** Windows 两次都记录了
   `Application Hang 1002`，`HangType` 都是 `Top level window is idle`，
   两次的 WER 签名完全一致（`P4=2472`、`P5=67246080`），说明是同一类
   可复现的 UI 线程停顿。
2. **不是 Python 层的死循环或死锁。** 命令执行、`service_status()` 等
   JS API 调用都由 `js_bridge_call()` 放到独立线程；`evaluate_js()` 在
   工作线程里等的是 Python `Semaphore`（会释放 GIL）。日志停在上一次
   业务动作之后，没有任何“任务卡住”的迹象。
3. **旧版看门狗有确认的检测盲区。** 它只发 `WM_NULL`，而且只在失败时
   写日志；本次 `watchdog.log` 里连一条“UI 无响应”都没有。也就是说，
   看门狗要么判定“正常”，要么根本没在跑——两种情况下它都没有起到
   保护作用，也几乎没有留下可分析的证据。
4. **卡死点在 WebView2 / WinForms / DWM 这一段原生代码里。** WebView2
   浏览器进程在 18:27:55 还在正常写偏好设置，18:38:44 正常退出
   （`profile.exit_type=Normal`），因此不是浏览器子进程崩溃；更像是
   UI 线程卡在 WebView2 的同步调用或 DirectComposition 表面上。
   精确到某一帧的调用栈，需要下一次卡死时抓到的 minidump 才能确认。

## 证据链

### 两次 AppHangB1

| 时间 | 程序 | PID | 进程启动时间 | HangType |
|---|---|---|---|---|
| 2026-10-19 22:49:57 | `WorkBuddyConsole.exe`（旧名） | 0xbfb8 | 22:44:13 | Top level window is idle |
| 2026-10-20 18:38:44 | `Auto-claim-dashboard.exe` | 0x40c4 (16580) | 17:21:59 | Top level window is idle |

两次 WER 问题签名：

```text
P1: 程序名
P2: 0.0.0.0
P3: exe 的 PE 时间戳（两次不同，正常）
P4: 2472
P5: 67246080
P6..P10: 空
```

`P4/P5` 相同，说明 WER 把它们归为同一类挂起。`HangType` 的含义是
UI 线程在 5 秒内没有调用 `PeekMessage`，和任务管理器显示“未响应”、
`IsHungAppWindow()` 返回真，是同一套判定。

### 2026-10-20 的时间线

| 时间 | 事件 |
|---|---|
| 17:21:59 | 进程启动（WER 记录） |
| 17:22:00 | 看门狗启动，`hwnd=0x800616` |
| 17:44:15 | `last_result.json` 写入（成长中心一轮结束） |
| 17:44:17 | `run.log` 最后一条：成长中心变化 |
| 18:27:55 | WebView2 仍在正常写 `profile.observed_session_time` |
| 18:38:42 | WER 开始生成挂起快照（出现 0 线程的反射进程 23036） |
| 18:38:44 | `Application Hang 1002` + `AppHangB1` 1001 |
| 18:38:44 | WebView2 正常退出（`exit_type=Normal`） |

关键点：

- 17:44:17 之后应用没有任何业务日志，但这不能证明卡死从那时开始——
  空闲时本来就不写日志。
- 18:27:55 WebView2 浏览器进程还活着，所以卡死发生在 **18:27:55 到
  18:38:42 之间**，不是浏览器进程一启动就坏。
- 18:38:44 浏览器进程能正常保存会话并退出，说明它没有死锁；卡住的是
  宿主进程的 UI 线程。

### `watchdog.log` 说明了什么

```text
[2026-10-20 17:22:00] [watchdog] 启动：hwnd=0x800616
```

之后再无任何记录。旧版看门狗只在“失败”和“恢复”时写日志，所以：

- 它没有检测到一次失败（否则会写 `UI 无响应 1/3`）；
- 也无法证明它当时还活着。

这正是本次要修的地方：**没有记录的正常，等于没有证据。**

## 已排除的方向

| 方向 | 依据 |
|---|---|
| 全局键盘/鼠标钩子 | 仓库中没有 `SetWindowsHookEx` / `RegisterHotKey` |
| 鼠标捕获、光标锁定 | 没有 `SetCapture` / `ClipCursor` |
| 置顶窗口遮住桌面 | 没有 `on_top` / `WS_EX_TOPMOST` |
| 无边框窗口的拖动模态循环 | 窗口是标准边框，没有 `frameless` / `easy_drag` |
| 永久的 CSS 动画 | `pulse` 只用于 8 秒的“待确认”和高亮状态，空闲时不存在 |
| Python 层死锁 | JS API 在工作线程执行；`evaluate_js` 等待的是 Python `Semaphore`，不占 GIL |
| WebView2 浏览器进程崩溃 | 18:27:55 仍在写配置，18:38:44 正常退出 |
| 显卡 TDR / 系统崩溃 | 当天 `Display` 只有无害的 4107，无 `LiveKernelEvent 141`，无 `Kernel-Power 41` |

## 已确认的缺陷：看门狗的三个盲区

1. **只看 `WM_NULL`。** Windows 判定“无响应”的依据是线程 5 秒内没有
   调用 `PeekMessage`；某些卡死状态下 `WM_NULL` 仍可能被派发，导致
   探测通过。旧版从不调用 `IsHungAppWindow()`，等于放弃了操作系统
   已经给出的结论。
2. **只认一个 HWND。** 启动时记下句柄后不再校验归属。窗口句柄被销毁
   并复用后，看门狗会一直探测一个不属于本进程的窗口，而且看起来
   一切正常。
3. **失败才写日志。** 卡死时如果探测恰好通过，日志里没有任何痕迹，
   事后无法区分“看门狗判定正常”和“看门狗线程已经死了”。

## 修复内容

### 1. `dashboard/win_guard.py`

- 每轮重新枚举本进程的所有可见顶层窗口，并校验 `GetWindowThreadProcessId`
  返回的 PID，避免句柄复用。
- 同时检查：
  - `IsHungAppWindow()`（Windows 自己的判定）；
  - `SendMessageTimeoutW(WM_NULL, 1000ms)`；
  - 前端心跳（见下）。
- 连续 **2 次 / 约 2–3 秒** 判定卡死就退出（旧版是 3 次 × 2000ms，
  最快也要 6–9 秒）。
- 退出前：
  - 写所有 Python 线程栈到 `watchdog.log`；
  - 用 `MiniDumpWriteDump` 抓一份原生 minidump 到
    `%LOCALAPPDATA%\WorkBuddyDashboard\dumps\hang-*.dmp`（最多保留 3 份）；
  - 启动一个 20 秒的硬退出兜底，防止转储本身卡住。
- 每 2 分钟写一行 `探测正常：... 心跳=...`。下次就能直接看出看门狗
  是否还活着、心跳是否正常。

### 2. 前端心跳

`app.js` 每 5 秒调用一次 `api.heartbeat()`，并且**等上一次 Promise
落地后才排下一次**。这个往返要经过：

```text
JS setInterval
  → WebMessageReceived（WinForms UI 线程）
  → js_bridge_call 工作线程
  → Api.heartbeat()
  → evaluate_js（Invoke 回 UI 线程）
  → Promise resolve
```

UI 线程或 WebView2 任何一段卡住，下一次心跳都不会到达。超过 20 秒
没有心跳，看门狗按卡死处理。

为避免窗口被其他窗口遮住时 Chromium 节流定时器造成误判，启动
WebView2 前会追加：

```text
--disable-background-timer-throttling
--disable-backgrounding-occluded-windows
--disable-renderer-backgrounding
```

最小化时仍然跳过心跳检查（只看 `IsHungAppWindow` / `WM_NULL`）。

### 3. 降低无效唤醒

`service_status()` 每次都要起一次 PowerShell。前端轮询从
“空闲 60 秒 / 运行中 15 秒”改为“空闲 3 分钟 / 运行中 20 秒”；
手动刷新按钮和操作后的立即刷新不受影响。这不一定是卡死的根因，
但能显著减少 `evaluate_js` 往返次数和 PowerShell 进程创建。

## 下次复现时怎么取证

1. 先看 `%LOCALAPPDATA%\WorkBuddyDashboard\watchdog.log`：

   ```text
   探测正常：...            ← 看门狗活着
   UI 无响应 1/2：... IsHungAppWindow=1
   UI 无响应 2/2：...
   连续无响应，强制退出...
   已写入卡死转储：...\dumps\hang-20261020-191238.dmp
   ```

   如果只有“启动”没有“探测正常”，说明看门狗线程本身没跑起来，
   问题在更早的初始化阶段。

2. 用 WinDbg 打开 dump：

   ```text
   windbg -z "%LOCALAPPDATA%\WorkBuddyDashboard\dumps\hang-*.dmp"
   ```

   ```text
   .symfix
   .reload
   !analyze -hang -v
   ~*k
   ```

   重点看主线程（通常是有 `Application.Run` / `DispatchMessage` 的那条）
   停在哪个模块：

   - `combase.dll` / `ole32.dll` → COM 同步调用；
   - `dcomp.dll` / `dwmapi.dll` / `d3d11.dll` → DirectComposition / 呈现；
   - `Microsoft.Web.WebView2.*` / `EmbeddedBrowserWebView.dll` → WebView2；
   - `user32.dll` + `SendMessage` → 跨进程同步消息。

3. 如果 dump 里主线程停在 WebView2 的
   `ExecuteScriptAsync` / `Invoke` / `Present` 一类调用上，就可以基本
   确认是 WebView2 的 UI 线程同步等待；下一步用
   `WORKBUDDY_DISABLE_GPU=1` 或降级 WebView2 Runtime 做对照实验。

## 仍未确认的部分

- **UI 线程具体卡在哪个原生调用上。** 现有日志只能证明“UI 线程停摆”，
  不能证明停在哪一帧。需要下一次卡死时抓到的 minidump。
- **为什么整个桌面的窗口切换也会被拖住。** 最合理的解释是卡死窗口
  处于前台，DWM/外壳在等它的合成表面或同步消息；但本次没有抓到
  explorer/DWM 的线程栈，不能把这条写成结论。
- **6.8% CPU 的来源。** 任务管理器截图里该进程的 6.8% 是瞬时采样，
  可能来自 WER 生成快照，也可能来自 UI 线程的自旋；单凭一张截图
  不能定性。

## 验证方式

- 源码运行并观察 `watchdog.log` 是否每 2 分钟出现 `探测正常`。
- 人为卡死 UI 线程（例如在 `webview.start()` 之后的主线程里
  `time.sleep(60)`），确认 2–3 秒内进程以退出码 3 结束，并生成
  `dumps\hang-*.dmp`。
- 正常使用一天，确认没有因为心跳误判而被看门狗结束。
