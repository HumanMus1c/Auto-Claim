"""验证 exe 里打包的资源（assets/*）是否是最新版本。

背景：exe 里 app.py 是最新版，但界面仍表现为旧行为。之前查过
run.log 仍写在 %TEMP% 解包目录——那是旧 _log_path() 的行为。
既然 Python 侧已确认是新版，那就要排除"资源是旧的"这一可能：
PyInstaller 的 datas 同样可能被缓存。
"""

import hashlib
import os
import sys

EXE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "Auto-claim-dashboard.exe")
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def digest(data):
    return hashlib.sha256(data).hexdigest()[:16]


def main():
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(EXE)
    toc = archive.toc

    targets = ["assets\\app.js", "assets\\index.html",
               "assets\\style.css", "assets\\icon.ico"]

    bad = []
    for name in targets:
        if name not in toc:
            print("[缺失] %s 不在 exe 内" % name)
            bad.append(name)
            continue
        packed = archive.extract(name)
        local_path = os.path.join(SRC, os.path.basename(name))
        with open(local_path, "rb") as f:
            local = f.read()
        same = digest(packed) == digest(local)
        print("%-24s exe=%s 本地=%s  %s"
              % (os.path.basename(name), digest(packed), digest(local),
                 "一致" if same else "不一致(旧?)"))
        if not same:
            bad.append(name)

    if bad:
        print("\n[NG] 以下资源与本地不一致：%s" % ", ".join(bad))
        return 1
    print("\n[OK] 资源均为最新")
    return 0


if __name__ == "__main__":
    sys.exit(main())