# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

单文件 + 无控制台窗口，产物直接落在仓库根目录，与 signin.py 同级——
这样运行时 _locate_signin() 能自动命中，exe 可以单独拷走使用。
"""

import os

# SPEC 由 PyInstaller 注入，指向本 .spec 文件所在目录
HERE = os.path.abspath(os.path.dirname(SPEC))
ASSETS = os.path.join(HERE, 'assets')
DASHBOARD = os.path.dirname(HERE)      # win_theme / win_title 在上一级
REPO = os.path.dirname(DASHBOARD)      # 仓库根：signin.py 在这里

# 打包资源时跳过运行时产物。
# run.log 一旦被打进去，会在每次启动时解包到 %TEMP%，时间戳看起来
# 像是"程序正在写日志"——排查时被它误导过好几轮。
SKIP_ASSETS = {'run.log', 'debug.log'}
datas = [
    (os.path.join(ASSETS, name), 'assets')
    for name in sorted(os.listdir(ASSETS))
    if os.path.isfile(os.path.join(ASSETS, name)) and name not in SKIP_ASSETS
]
datas += [
    (os.path.join(DASHBOARD, 'win_theme.py'), '.'),
    (os.path.join(DASHBOARD, 'win_title.py'), '.'),
    (os.path.join(DASHBOARD, 'win_guard.py'), '.'),
]

# win_theme / win_title / win_guard 是在函数体内 import 的，
# 且不在 pathex 里，静态分析抓不到。必须显式声明；否则冻结后 sys.path 变了，
# 就 ModuleNotFoundError。它们和 app.py 并排放进包内，
# HERE（冻结时= sys._MEIPASS）本就在 sys.path 上。
hidden = [
    'win_theme',
    'win_title',
    'win_guard',
    # app.py 走 _load_signin() 按路径加载 signin.py，没有字面量 import，
    # 静态分析看不到它——必须显式声明，否则它内部的依赖
    # （plistlib / ntpath / winreg）一个都不会被打包。
    'signin',
    'plistlib',
    'ntpath',
    'winreg',
    # pywebview 按平台动态加载后端，不声明会退回到无窗口的默认行为
    'webview.platforms.edgechromium',
    'webview.platforms.winforms',
    'clr',
    'pythonnet',
]

a = Analysis(
    # 用绝对路径。曾用相对 'app.py'，在别的 cwd 下执行 pyinstaller 时
    # 会解析到错误的文件，打出来的 exe 跑的是旧代码——而且不报任何错。
    [os.path.join(HERE, 'app.py')],
    # 仓库根必须在 pathex 里，否则 PyInstaller 找不到 signin.py，
    # 也就不会分析它内部的 import——plistlib / ntpath 会整个漏掉，
    # 冻结后 import signin 直接 ModuleNotFoundError。
    pathex=[HERE, REPO],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'pydoc_data',
        # 已放弃的 GUI 实验方案依赖，排除以控制体积
        'matplotlib', 'numpy', 'scipy', 'pandas',
        'kivy', 'sdl2', 'OpenGL',
    ],
    noarchive=False,
)

# 关键：必须重新编译字节码。
# PyInstaller 会把 .pyc 缓存进 PYZ 归档，若沿用旧的 PYZ-00.pyz，
# 改动 app.py 后再打包，exe 里跑的仍是旧逻辑，且不报任何错——
# 实测踩过：日志仍写进 %TEMP% 解包目录，而不是新代码指定的 LOCALAPPDATA。
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Auto-claim-dashboard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,                    # --windowed：无控制台窗口
    disable_windowed_traceback=False,
    icon=os.path.join(ASSETS, 'icon.ico'),
)