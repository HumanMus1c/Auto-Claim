# WorkBuddy 图形控制台

> 给 `signin.py` 配一个本地图形界面：单文件 exe、无终端窗口、与脚本同级分发。
>
> 代码位于 `demos/webview/`，产物 `WorkBuddyConsole.exe` 落在仓库根目录（与 `signin.py` 并排）。

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
4. 云端任务 —— 独立整行，按「未完成 / 已完成」分页签，带 `当前/目标` 进度
5. 签到状态 + 当月日历 —— 同一行
6. 日志 —— 整行收尾，按级别着色带图标，支持一键复制

外观细节：

- **跟随系统主题与强调色**：`demos/win_theme.py` 读注册表 `HKCU\Software\Microsoft\Windows\DWM\ColorizationColor`；强调色按比例调整明度，避免嵌套元素上使用 CSS `filter` 产生副作用。
- **图标自动反色**：纯线条 SVG，`stroke` 取 `currentColor`，只需设置容器的 `color`，无需维护亮/暗两套图。几何由 `assets/make_icon.py`（Pillow）同时生成 `icon.svg` 与 7 种尺寸的 `icon.ico`。
- **标题栏**：`demos/win_title.py` 用 Win32 API 设置 Unicode 标题，绕开早期编码问题。

## 数据流

```text
前端  window.pywebview.api.<method>()   ──►  Api 方法
后端  _push(kind, payload)              ──►  window.__onBackend(kind, data)
```

启动顺序：

1. `bootstrap()` —— 返回环境信息与上次缓存结果，界面先用缓存填充卡片，避免开场一片「—」
2. 自动执行 `status`，随后 `doctor`
3. 后台静默拉取一次任务列表，不打断启动

任务列表走 `GET /v2/activity/growth/tasks`，认证链路直接复用 `signin.py` 的 `find_auth_file` / `resolve_session` / `build_headers` / `get`，不另写一份 crypto。

### 并发约束

`signin.py` 用模块级全局变量记录本轮时间预算（`_started_at` / `_budget_seconds`），并发进入会互相干扰。因此：

- 所有进入 `signin` 的路径共用一把模块级 `_SIGNIN_LOCK`。
- `tasks()` 用**非阻塞**加锁：`auto` 可能跑几分钟，拿任务列表时干等会让界面像卡死，拿不到就快速返回「有命令正在执行」。

## 运行时文件

全部落在用户目录，不在程序目录也不在临时解包目录：

```text
%LOCALAPPDATA%\WorkBuddyConsole\
├── last_result.json   上次查询结果（下次启动先渲染，再刷新）
├── run.log            历史日志（界面日志区的持久化副本）
├── crash.log          Python 与前端 JS 的异常
└── trace.log          仅 WORKBUDDY_DEBUG=1 时产生，见下
```

## 调试开关

```powershell
$env:WORKBUDDY_DEBUG = "1"
.\WorkBuddyConsole.exe
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

## 运行方式

```powershell
# 源码运行（无终端窗口）
pythonw.exe demos/webview/app.py

# 打包
python demos/webview/build_exe.py
```

打包与校验流水线见 [gui-console-packaging.md](gui-console-packaging.md)；
开发过程中踩过的坑与排查方法见 [gui-console-troubleshooting.md](gui-console-troubleshooting.md)。
