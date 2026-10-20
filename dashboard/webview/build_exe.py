"""打包前后的自检 + 打包，一步到位。

单独跑 pyinstaller 很容易打出"能跑、但跑的是旧代码"的包，而且不报错。
这里把几道检查串在一起，任何一道不过就不打包：

  1. app.py / app.js 无重复代码、语法正确
  2. style.css 关键选择器齐全（曾因批量删 CSS 整块丢失）
  3. 冻结路径逻辑的单元测试
  4. 打包完成后，核对 exe 内确实含本版代码的标志字符串
"""

import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.dirname(HERE)
REPO = os.path.dirname(DASHBOARD)
SPEC = os.path.join(HERE, "Auto-claim-dashboard.spec")
DIST = REPO
NAME = "Auto-claim-dashboard.exe"

PY = sys.executable
# 子进程统一用 UTF-8 输出，否则中文/符号在 GBK 控制台会被替换或抛异常，
# 退出码随之失真，检查脚本就会被误判为"未通过"
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

# 本进程的 stdout 同样要兜底：检查脚本输出里的 ✓ 在 GBK 控制台
# 会让下面的 print 直接抛 UnicodeEncodeError，构建被误判为失败。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def run(label, args, cwd=None):
    print("\n--- %s ---" % label, flush=True)
    p = subprocess.run(args, cwd=cwd or HERE, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env=ENV)
    out = (p.stdout or "") + (p.stderr or "")
    print(out.strip()[-1500:], flush=True)
    return p.returncode


def pre_checks():
    failed = []

    r = run("语法检查", [PY, "-m", "py_compile",
                        os.path.join(HERE, "app.py"),
                        os.path.join(DASHBOARD, "win_guard.py")])
    if r != 0:
        failed.append("app.py 或 win_guard.py 语法错误")

    js = os.path.join(HERE, "assets", "app.js")
    if run("JS 语法检查", ["node", "--check", js]) != 0:
        failed.append("app.js 语法错误")

    if run("重复代码检查", [PY, os.path.join(HERE, "check_dup.py")]) != 0:
        failed.append("存在重复代码块")

    if run("CSS 选择器检查", [PY, os.path.join(HERE, "check_css.py")]) != 0:
        failed.append("style.css 缺关键选择器")

    if run("单元测试", [PY, "-X", "utf8", "-m", "unittest",
                        "test_locate_signin", "test_win_guard",
                        "test_win_service"]) != 0:
        failed.append("单元测试未通过")

    return failed


def build():
    return run("打包", [PY, "-m", "PyInstaller", "--clean", "--noconfirm",
                        "--distpath", DIST, "--workpath",
                        os.path.join(HERE, "build"), SPEC])


def post_check():
    """核对 exe 里确实是本版代码。

    只比时间戳是不够的：踩过一次 PYZ 字节码缓存的坑——exe 时间戳是新的，
    内容却是旧逻辑。这里改为检查 exe 里是否含本版代码特有的标识字符串。
    """
    exe = os.path.join(DIST, NAME)
    if not os.path.isfile(exe):
        return ["exe 未生成"]

    exe_mtime = os.path.getmtime(exe)

    # 1) 时间戳粗筛。运行期模块是 datas 直接拷进包的，也要算进来；
    #    否则只改 win_guard.py 时，exe 时间戳看起来仍可能比 app.py 新。
    stale = []
    paths = [
        (os.path.join(HERE, "app.py"), "app.py"),
        (os.path.join(HERE, "assets", "app.js"), "assets/app.js"),
        (os.path.join(HERE, "assets", "index.html"), "assets/index.html"),
        (os.path.join(HERE, "assets", "style.css"), "assets/style.css"),
        (SPEC, SPEC),
    ]
    for name in ("win_theme.py", "win_title.py", "win_guard.py",
                 "win_service.py"):
        paths.append((os.path.join(DASHBOARD, name), "dashboard/" + name))
    for path, label in paths:
        if os.path.isfile(path) and os.path.getmtime(path) > exe_mtime + 1:
            stale.append(label)
    if stale:
        return ["exe 比这些源文件旧，打包的是旧代码：%s" % ", ".join(stale)]

    # 2) 内容校验：解包 exe 里的 app 字节码，确认含新版函数符号。
    #    曾用「在 exe 原始字节里 grep 函数名」，不可靠——代码被压缩进 PYZ，
    #    字符串可能被压缩流切断，把正确的包误判为旧的（真踩过）。
    for checker in ("verify_exe.py", "verify_assets.py"):
        r = subprocess.run([PY, os.path.join(HERE, checker), exe],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=ENV)
        print((r.stdout or "") + (r.stderr or ""), flush=True)
        if r.returncode != 0:
            return ["%s 校验未通过（见上方输出）" % checker]

    size = os.path.getsize(exe) / 1024 / 1024
    print("\n--- 产物 ---\n%s  %.1f MB  %s"
          % (NAME, size, time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(exe_mtime))))
    return []


def main():
    print("=" * 56)
    print("Auto-claim-dashboard 打包")
    print("=" * 56)

    failed = pre_checks()
    if failed:
        print("\n✗ 打包前检查未通过，已中止：")
        for f in failed:
            print("   · %s" % f)
        return 1

    if build() != 0:
        print("\n✗ 打包失败")
        return 1

    problems = post_check()
    if problems:
        print("\n✗ 打包后检查未通过：")
        for p in problems:
            print("   · %s" % p)
        return 1

    print("\n✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())