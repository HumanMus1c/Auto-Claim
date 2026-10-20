"""验证 exe 里确实是本版代码。

背景：PyInstaller 的 PYZ 归档会缓存 .pyc，曾出现「exe 时间戳是新的、
内容却是旧逻辑」的故障，且不报任何错。所以打包后必须验证内容。

实现上踩过的坑：直接在 exe 原始字节里 grep 函数名并不可靠——代码被
压缩进 PYZ，字符串可能被压缩流切断。这里改为用 PyInstaller 官方
CArchive API 打开 exe，列出 PYZ 内模块，并解出 app 的字节码检查符号。
"""

import marshal
import os
import sys

EXE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "Auto-claim-dashboard.exe")

# app.py 里后期才加入的函数名/属性，用作"新版标记"
FINGERPRINTS = ["_diag_dir", "_safe_worker", "_service_main",
                "service_status", "service_action",
                "_growth_snapshot", "_growth_changes",
                "acquire_single_instance", "UIWatchdog",
                "IconManager"]

# spec 以 datas 形式放在包根的运行期模块；app.py 采用 fail-open import，
# 模块缺失时程序仍能启动但对应功能会静默消失，所以这里单独核对。
REQUIRED_ENTRIES = ["win_theme.py", "win_title.py", "win_guard.py",
                    "win_service.py", "win_icon.py"]


def main():
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(EXE)
    names = list(archive.toc)
    print("CArchive 条目数: %d" % len(names))

    missing_entries = [name for name in REQUIRED_ENTRIES
                       if name not in archive.toc]
    for name in REQUIRED_ENTRIES:
        print("  %-18s %s"
              % (name, "缺失" if name in missing_entries else "存在"))
    if missing_entries:
        print("\n[NG] exe 缺少运行期模块 %s" % ", ".join(missing_entries))
        return 1

    # app 是入口脚本，PyInstaller 把它放在 CArchive 顶层（entry 名就是 "app"），
    # 而不是 PYZ 里——所以直接从 CArchive 取它的字节码。
    if "app" not in archive.toc:
        print("CArchive 里没有 app 条目，无法验证内容")
        return 1

    blob = archive.extract("app")
    print("app 条目大小  : %d 字节" % len(blob))
    code = blob if hasattr(blob, "co_names") else marshal.loads(blob)

    # 收集所有代码对象里的名字
    names_in_code = set()

    def walk(c):
        names_in_code.update(c.co_names)
        for const in c.co_consts:
            if isinstance(const, str):
                names_in_code.add(const)
            elif hasattr(const, "co_names"):
                walk(const)

    walk(code)
    print("app 字节码符号数: %d" % len(names_in_code))

    missing = []
    for probe in FINGERPRINTS:
        ok = probe in names_in_code
        print("  %-18s %s" % (probe, "存在" if ok else "缺失"))
        if not ok:
            missing.append(probe)

    if missing:
        print("\n[NG] exe 缺少新版标识 %s —— 打包的是旧代码"
              % ", ".join(missing))
        return 1

    print("\n[OK] exe 确实是本版代码")
    return 0


if __name__ == "__main__":
    sys.exit(main())