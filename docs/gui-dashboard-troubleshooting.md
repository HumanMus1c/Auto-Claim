# 排障记录：十个真实问题与排查方法

> 这份文档记录控制台从「第一次打包」到「稳定运行」期间修掉的每一个真实缺陷。
>
> 它们的共同特征是：**不报错、日志看起来正常、界面照常渲染**。唯一的暴露方式往往是间接信号（进程数不对、缓存没文件、卡片空着）。

## 问题清单

### 1. 冻结后从错误的目录开始搜索

| | |
|---|---|
| 现象 | exe 提示「未找到 signin.py」，而文件明明就在 exe 旁边 |
| 根因 | `HERE` 在冻结模式下等于 `sys._MEIPASS`（`%TEMP%\_MEIxxxx`）。`_locate_signin()` 从这个临时目录逐级上溯，只会一路搜到 `C:\Users\`，**永远碰不到 exe 所在目录** |
| 修法 | 冻结时优先从 `os.path.dirname(sys.executable)` 起搜，再依次兜底到解包目录、cwd |

```python
roots = []
if getattr(sys, "frozen", False):
    roots.append(os.path.dirname(os.path.abspath(sys.executable)))  # exe 同级
    roots.append(HERE)
roots.append(HERE)
roots.append(os.getcwd())
```

同时把**实际搜索过的路径**通过 `bootstrap()` 返回给界面，失败时逐条列出——原来看不到「程序到底去哪儿找过」，无从排查。

---

### 2. exe 自我复制（fork bomb）

| | |
|---|---|
| 现象 | 点一次启动，十几秒后堆出 13 个进程，每 3 秒新生成一个 |
| 根因 | `subprocess.run([sys.executable, SIGNIN, action])`。**冻结后 `sys.executable` 就是 exe 自身**，于是「跑 status」等于又启动一份完整程序，新实例启动后同样自动跑 status，无限递归 |
| 修法 | 改为**进程内调用**，用 `StringIO` 接管 stdout 捕获 `signin.py` 打印的 JSON |

```python
buf = io.StringIO()
stdout, stderr = sys.stdout, sys.stderr
sys.stdout = sys.stderr = buf
try:
    with _SIGNIN_LOCK:
        code = signin._run(action)
finally:
    sys.stdout, sys.stderr = stdout, stderr
```

进程树是唯一的线索：

```text
34400          ← 启动
 └38736 (+1s)
  └28896  (+3s)
   └6896  (+3s)   ← 每 3 秒一个，父子关系成链
```

顺带两个收益：`status` 从「启动解释器的秒级」降到毫秒级；不再依赖目标机器有可用的 Python 解释器。

> 判断进程是否正常：单文件 exe 正常是 **2 个进程**（引导器 + 应用）。

---

### 3. `_push()` 参数个数不对，异常被线程吞掉

| | |
|---|---|
| 现象 | 界面永远停在「执行中…」，缓存不写入，后续命令全被挡掉 |
| 根因 | 改动时把 `self._push("log", msg)` 写成了 `self._push("log", msg, "cmd")`，而签名只接受两个参数。`TypeError` 抛在 `_worker()` 第一行，**打包后没有控制台**，异常只进 stderr，彻底消失 |
| 修法 | 补参数；更关键的是加一层包装，任何异常都必须可见 |

```python
def _safe_worker(self, action, spec):
    try:
        self._worker(action, spec)
    except Exception as exc:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        self._push("log", "内部错误：%s" % detail)
        with self._lock:
            self._busy = False        # 关键：无论如何都要复位
        self._push("done", {"action": action, "exit": 2, "payload": None})
```

裸起线程 + 无控制台 = 异常黑洞。包装层保证异常**显示在日志区**且 **`_busy` 一定复位**。

---

### 4. `_worker()` 里出现重复代码块

| | |
|---|---|
| 现象 | 无现象。日志会成对出现，功能照常 |
| 根因 | 用编辑工具替换 `subprocess` 那段时只匹配到一半，留下两份相同的「解析 + 推日志」逻辑 |
| 修法 | 切除重复段，并新增 `check_dup.py`：用 AST 遍历函数体，报告重复出现的行（可维护豁免名单） |

这类问题浏览器不报错、程序照常跑，只能靠静态检查兜住。

---

### 5. 日志写进了临时解包目录

| | |
|---|---|
| 现象 | exe 里的任何异常都查无实据，界面异常后什么痕迹都没有 |
| 根因 | `crash.log` 写在 `HERE`、`run.log` 写在 `ASSETS`，冻结后它们都在 `%TEMP%\_MEIxxxx`，**进程退出整个目录被删** |
| 修法 | 统一到用户目录：`_diag_dir()` → `%LOCALAPPDATA%\WorkBuddyDashboard\`，并让前端 JS 异常也通过 `log_error()` 落盘 |

这一条本身不修复任何缺陷，但它**是解开僵局的那一步**——改完才第一次看见真实错误：

```text
异常：AttributeError: module 'signin' has no attribute '_run'
```

---

### 6. 被打包进 exe 的旧 `run.log`

| | |
|---|---|
| 现象 | 每次启动后 `%TEMP%\_MEIxxx\assets\run.log` 都存在，时间戳是刚刚 |
| 误判 | 据此推断「exe 里跑的还是旧代码（旧版路径才指向 ASSETS）」，为此改了两处无关的地方 |
| 真相 | 那是打包时被 `datas` 一并收进去的**死文件**。解包出来自然带上新时间戳，内容一个字都没变 |
| 修法 | spec 中过滤运行时产物（`SKIP_ASSETS`）；`.gitignore` 也补上 `assets/*.log` |

验证方式很简单——比对内容而不是时间戳：

```text
exe 内 run.log : 7258 字节
本地 run.log   : 7258 字节
内容是否相同   : True
```

---

### 7. spec 里 `Analysis` 用了相对路径

| | |
|---|---|
| 现象 | 无现象，属于潜在风险 |
| 根因 | `Analysis(['app.py'])` 按执行目录解析。换一个 cwd 执行 `pyinstaller`，可能打到别的文件 |
| 修法 | 改为 `os.path.join(HERE, 'app.py')` |

---

### 8. `plistlib` / `ntpath` 没有被打包

| | |
|---|---|
| 现象 | `status` 与 `doctor` 一律 `exit=1`，且不产生任何日志文件 |
| 根因 | 两层叠加：① `pathex` 缺仓库根目录 → PyInstaller 找不到 `signin.py`；② 改用按路径加载后，代码里没有字面量 `import signin`，静态分析完全看不到它 |
| 修法 | `pathex=[HERE, REPO]` + `hiddenimports` 显式声明 `signin`、`plistlib`、`ntpath`、`winreg` |

```text
ModuleNotFoundError: No module named 'plistlib'
```

`signin.py` 顶部会 `import plistlib`（用于读取 macOS 凭据），在 Windows 上用不到，但导入时照样要执行。

---

### 9. 并发 import 拿到半成品模块

| | |
|---|---|
| 现象 | 报错为 `module 'signin' has no attribute '_run'`，与真实原因完全无关 |
| 根因 | 启动时 `tasks()` 与 `run()` 几乎同时执行 `import signin`。Python 的 `import` 先查 `sys.modules`，后到的线程会直接拿到**尚未执行完**的模块对象 |
| 修法 | `_load_signin()`：按文件路径显式加载 + 独立加载锁 + 只加载一次 + 失败时摘掉半成品 |

```python
def _load_signin():
    global _SIGNIN_MODULE
    if _SIGNIN_MODULE is not None:
        return _SIGNIN_MODULE
    with _SIGNIN_LOAD_LOCK:
        if _SIGNIN_MODULE is not None:
            return _SIGNIN_MODULE
        spec = importlib.util.spec_from_file_location("_workbuddy_signin", SIGNIN)
        module = importlib.util.module_from_spec(spec)
        sys.modules["_workbuddy_signin"] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop("_workbuddy_signin", None)   # 不留半成品
            raise
        _SIGNIN_MODULE = module
        return module
```

按路径加载还额外解决了一个隐患：PyInstaller 的 `FrozenImporter` 排在 `sys.meta_path` 最前，**优先级高于 `sys.path`**，一旦 PYZ 里有同名模块，`sys.path.insert(0, 仓库根)` 也救不回来。

---

### 10. 「独占整行」的成长中心栏被压成 1px，看起来像没实现

| | |
|---|---|
| 现象 | 界面只有指标卡、按钮行、签到状态/本月签到、日志——成长中心整栏「消失」，像独占整行那版没生效（源码与 exe 资源都验证过是最新的） |
| 根因 | `main` 是纵向 flex 滚动容器；`.row2` 固定 300px、`.pane-log` 最少 260px，卡片/按钮/说明也是固定高，合计已超过默认窗口高度。`.pane` 默认 `min-height: 0`，而 `.pane-tasks` 只有默认的 `flex: 0 1 auto`——**窗口不够高时只有它能被压缩**，于是塌到 1px。任务其实一直在 DOM 里（`tasks` 探针能看到 7 个），只是高度没了 |
| 放大因素 | 系统「文本大小」140%（辅助功能）会被 Chromium 内核叠加到 DPR 上（125% 显示缩放 × 1.4 = 1.75），1180×800 的窗口实际 CSS 视口只有 833×544。任何窗口高度下都可能触发 |
| 修法 | `.pane-tasks { flex: 0 0 auto; }`：任务栏不参与压缩，窗口矮时由 `main` 自身滚动兜底 |

判断这类「元素在不在、多高」的问题不要靠截图猜（WebView2 用 DirectComposition，GDI 截图经常不可靠）：

```text
WORKBUDDY_DEBUG=1                    # 启用 _probe() 探针，或
WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222   # 接 CDP 直接查询
```

```js
document.querySelector('.pane-tasks').getBoundingClientRect().height
// 修复前：1        修复后：364
```

`main.scrollHeight(1219) > clientHeight(487)` 也是同一根因的证据：内容本来就放不下、应该滚动，而不是把某一栏挤没。

---

### 11. 长驻进程里刷新成长中心固定报「凭据处理超时，已停止子进程」

| | |
|---|---|
| 现象 | 日志出现 `×刷新成长中心失败：读取登录凭据失败：AuthError：凭据处理超时，已停止子进程，请稍后重试。`；点重试仍然失败，重启程序后恢复；同一时刻命令行 `python signin.py doctor` 却正常 |
| 根因 | `signin.py` 的 `_started_at` / `_budget_seconds` 是模块级全局量（默认 420 秒预算），而仪表盘是长驻进程。`growth()` 原先在 `resolve_session()` **之后**才重置预算；距上次刷新超过 420 秒后 `_budget_left()` 变成负数，`_run_auth_helper()` 里 `timeout = min(10, _budget_left()) <= 0` 直接抛 `HELPER_TIMEOUT`——解密子进程根本没启动。重置预算的代码又排在解密之后，永远走不到，故障会一直粘住直到重启 |
| 修法 | 新增 `_reset_signin_budget()`；`tasks()` / `growth()` 都在 `resolve_session()` 之前调用。`HELPER_TIMEOUT` 的文案「已停止子进程」在这个分支里具有误导性 |
| 验证 | 手动把 `_started_at` 拨到 20 分钟前：修复前 `resolve_session()` 12 毫秒即报 `HELPER_TIMEOUT`，修复后 `growth()` 2.5 秒成功拉到 16 个任务 |

判据：同一份凭据下 `doctor` 正常、仪表盘刷新却在**瞬间**失败（不是等满 10 秒），基本就是这个预算时钟问题；只有真等满 10 秒，才需要怀疑 `WorkBuddy.exe` 启动慢或被安全软件拦截。

---

### 附带：一个把排查带偏的观察

诊断 exe 显示 `sys.executable` 是 8.3 短路径 `C:\Users\SILVER~1\...`，而我用长路径 `C:\Users\SilverT1ger\...` 去检查文件是否存在，据此判断「缓存没写入」。

**两者指向同一个目录**，缓存其实一直是写成功的。判断文件是否存在时不要混用两种路径形态。

---

## 排查方法

### 无控制台的 GUI 程序，异常必须主动落盘

打包后既没有 stdout 也没有 stderr。**日志落盘的目录必须是进程退出后仍然存在的地方**（用户目录），不能是资源目录——单文件 exe 的资源目录在临时解包路径里，退出即删。

### 让程序自己汇报状态，而不是从外面猜

一度连续几轮都在外部推测（猜路径优先级、猜环境变量、猜权限、猜缓存写入），方向全错。真正有效的是两件事：

**一、同步调用内部方法，异常立刻可见**

```python
a = app.Api(); a._window = None
a._worker('status', app.ACTIONS['status'])   # 不用开窗口、不用等异步
```

**二、直接向 DOM 提问**

WebView2 用 DirectComposition 渲染，**GDI 截图只会得到黑屏**，图像方式在这里没有意义。改用 `evaluate_js` 主动查询页面状态：

```python
window.evaluate_js("JSON.stringify({ready:document.readyState,"
                   "badge:..., credit:..., todo:..., tasks:...,"
                   "jsErrors:window.__errs||[]})")
```

一次就同时排除了「页面没加载」「JS 报错」「bridge 没通」三种可能。这个探针现在固化在 `_probe()` 里，由 `WORKBUDDY_DEBUG=1` 启用。

### 「时间戳是新的」不等于「内容是新的」

任何带编译缓存的构建系统（PyInstaller 的 PYZ、webpack 的 cache、Java 的 class 文件）都有这个问题。可靠的判据是**内容指纹**：解出产物里的字节码看符号表，或者比对资源哈希。这也是 `verify_exe.py` / `verify_assets.py` 存在的原因。

### 环境分支必须用伪造的 `sys` 来测

`frozen` 相关逻辑用普通运行根本测不出来。`test_locate_signin.py` 显式伪造：

```python
sys.frozen = True
sys._MEIPASS = r"...\Temp\_MEI_FAKE"
sys.executable = r"...\Auto-Claim-dashboard.exe"
assert app.SEARCHED[0] == os.path.join(exe_dir, "signin.py")
```

### 退出码代表业务结果时，别让无关异常污染它

检查脚本输出 `✓` 遇到 GBK 控制台抛异常，退出码非零，被上层当成「检查不通过」而中止打包。**实际检查是全过的**。凡是 `except: pass` 与「用退出码表示业务结论」的组合，都要留意这类误报。

---

## 验收数据

最终 exe（`WORKBUDDY_DEBUG=1` 探针实测）：

```json
{"ready":"complete","badge":"就绪","env":"Python 3.13.0 / win32",
 "envSignin":"signin.py 已找到","credit":"100","streak":"17","total":"1700",
 "credType":"sym-v1 加密","todo":"7","done":"9","tasks":7,
 "calSigned":19,"logLines":27,"jsErrors":[]}
```

进程数 2（引导器 + 应用），`last_result.json` 正常写入，`crash.log` 为空。
