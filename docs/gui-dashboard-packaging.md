# 打包与校验流水线

> 单文件、无控制台、产物落在仓库根目录（与 `signin.py` 同级），这样 `_locate_signin()` 能直接命中，exe 可以单独拷走使用。
>
> 一条命令完成全部检查与打包：`python dashboard/webview/build_exe.py`

## 为什么需要流水线

单独调用 PyInstaller 很容易打出「**能跑、不报错、但跑的是旧代码**」的包。这类故障没有任何报错，只能靠**对产物本身做校验**发现。流水线的存在就是为了让每一步都可验证。

## spec 关键点

文件：`dashboard/webview/Auto-claim-dashboard.spec`

```python
HERE  = os.path.abspath(os.path.dirname(SPEC))
ASSETS = os.path.join(HERE, 'assets')
DASHBOARD = os.path.dirname(HERE)      # win_theme / win_title / win_guard 在这里
REPO  = os.path.dirname(DASHBOARD)     # 仓库根：signin.py 在这里
```

| 配置 | 取值 / 原因 |
|---|---|
| `Analysis` 入口 | **绝对路径**。曾用相对 `'app.py'`，在别的 cwd 下执行会解析到错误文件，打出来的 exe 跑旧代码且不报错 |
| `pathex` | `[HERE, REPO]`。仓库根必须在里面，否则 PyInstaller 找不到 `signin.py`，也就不会分析它内部的 import |
| `hiddenimports` | `win_theme`、`win_title`、`win_guard`、`win_icon`、`signin`、`plistlib`、`ntpath`、`winreg`、`webview.platforms.edgechromium`、`webview.platforms.winforms`、`clr`、`pythonnet` |
| `datas` | 逐文件列出 assets，并跳过 `run.log` / `debug.log`；`win_theme.py` / `win_title.py` / `win_guard.py` / `win_service.py` / `win_icon.py` 以数据文件放在包根 |
| `excludes` | `tkinter`、`unittest`、`pydoc_data`、`matplotlib`、`numpy`、`scipy`、`pandas`、`kivy`、`sdl2`、`OpenGL` |
| `console` | `False`（`--windowed`），不弹终端窗口 |

### 为什么 `signin` 必须显式声明

`app.py` 通过 `_load_signin()` **按文件路径**加载 `signin.py`，代码里没有字面量 `import signin`，PyInstaller 的静态分析完全看不到它——它内部的依赖（`plistlib` / `ntpath` / `winreg`）一个都不会被打包，冻结后直接：

```text
ModuleNotFoundError: No module named 'plistlib'
```

### 为什么打包时要跳过运行时日志

`assets/run.log` 一旦被打进 exe，每次启动都会解包到 `%TEMP%\_MEIxxxx\assets\`，**时间戳看起来是刚刚写的**，很容易被误当成「程序正在写日志」的证据，进而推出错误的结论。运行时产物不应进入打包源：

```python
SKIP_ASSETS = {'run.log', 'debug.log'}
datas = [
    (os.path.join(ASSETS, name), 'assets')
    for name in sorted(os.listdir(ASSETS))
    if os.path.isfile(os.path.join(ASSETS, name)) and name not in SKIP_ASSETS
]
```

## 流水线步骤

`build_exe.py` 按顺序执行，任何一步不过就中止：

| 阶段 | 检查 | 工具 |
|---|---|---|
| 打包前 | `app.py` / `win_guard.py` 语法 | `python -m py_compile` |
| 打包前 | `app.js` 语法 | `node --check` |
| 打包前 | 前端事件回归（命令完成不清空无关数据） | `test_status_event.js` |
| 打包前 | 函数体内重复代码 / 语法 | `check_dup.py`（AST） |
| 打包前 | 样式表关键选择器齐全 | `check_css.py` |
| 打包前 | 冻结路径 / 单实例 / 原生图标 | `test_locate_signin.py`、`test_win_guard.py`、`test_win_icon.py` |
| 打包 | PyInstaller | `--clean --noconfirm` |
| 打包后 | exe 里确实是本版代码 | `verify_exe.py` |
| 打包后 | 资源与本地逐字节一致 | `verify_assets.py` |

前两项是为了拦「批量改代码时改坏一块」，后两项是为了拦「exe 与源码不同步」。

## verify_exe.py：怎么确认 exe 里是本次的代码

**不能**在 exe 原始字节里 grep 函数名。Python 代码被编译成 `.pyc` 后压缩进 PYZ，字符串可能被压缩流切断，会把正确的包误判为旧的（此行踩过，导致打包被错误中止）。

正确做法是解出字节码看符号表：

```python
from PyInstaller.archive.readers import CArchiveReader

archive = CArchiveReader(EXE)
blob = archive.extract("app")            # 入口脚本在 CArchive 顶层
code = marshal.loads(blob)
# 递归遍历 co_names 与字符串常量，检查 _diag_dir / _safe_worker 等「新版标记」
```

两个容易踩的细节：

- PyInstaller 6.x 里 PYZ 条目的名字是 **`PYZ.pyz`**，不是 `PYZ-00.pyz`。
- 入口脚本 `app` 位于 **CArchive 顶层**，不在 PYZ 内（PYZ 里是 603 个依赖模块）。

## verify_assets.py

把 `assets/*`（`app.js` / `index.html` / `style.css` / `icon.ico` / `icon_light.ico` / `icon_dark.ico`）从 exe 中取出，与磁盘上的同名文件逐个比对 SHA-256。前端资源同样是「改了没生效」的高发区。

## 编码：一个会造成误报的坑

`check_dup.py` / `check_css.py` 原本会输出 `✓` / `✗`，Windows 控制台默认 GBK 编不了这两个字符，直接抛 `UnicodeEncodeError` → 退出码非零 → 上层的 `build_exe.py` 判定「检查不通过」而中止打包，**而实际检查是全过的**。

两侧都做了处理：

```python
# 检查脚本入口
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# build_exe.py 调用子进程时
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
```

教训：**用退出码代表业务结果时，任何与业务无关的异常都可能被当成失败**。

## 产物

```text
Auto-claim-dashboard.exe    约 15.0 MB    2026-10-20 12:07
```

参照 `signin.py` 放在仓库根目录，可单独拷走（同目录需有 `signin.py`）。
