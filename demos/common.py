"""三个 demo 共用的非 GUI 逻辑。

目的：让三份 demo 的**行为完全一致**，只有 GUI 部分不同，
这样并排比较时看到的差异全部来自框架本身。

安全约束：只跑 signin.py 的只读子命令（doctor / status），
不碰 claim、auto 等会真实写入服务端的操作。
"""

import datetime
import os
import re
import subprocess
import sys
import threading

SIGNIN = os.environ.get(
    "WORKBUDDY_DEMO_SIGNIN",
    r"D:\Documents\GitHub\workbuddy-auto-signin\signin.py",
)

# 只读命令：doctor 离线、status 只查状态。都不写服务端。
READONLY_ACTIONS = ("doctor", "status")


def _ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


def discover_commands():
    """从 signin.py 顶部 docstring 里抠出子命令清单。

    正则只认这一种写法："python signin.py auto           # 说明"，
    避免误抓正文里的示例。
    """
    try:
        with open(SIGNIN, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError as exc:
        return [("（读取 signin.py 失败：%s）" % exc, "")]
    out = []
    for name, desc in re.findall(r"python\s+signin\.py\s+(\S+)\s*(?:#\s*(.*))?", source):
        item = (name, (desc or "").strip())
        if item not in out:
            out.append(item)
    return out


def run_readonly(action, timeout=90):
    """跑一次 signin.py <action>，返回可直接塞进文本框的输出。

    同步执行，由调用方放到后台线程里 —— 加密凭据解密可能阻塞 10 秒，
    auto 整轮最长 420 秒预算，直接调会冻住界面。
    """
    if action not in READONLY_ACTIONS:
        return "[%s] 拒绝：demo 只跑只读命令 %s" % (_ts(), "/".join(READONLY_ACTIONS))
    exe = sys.executable or "python"
    try:
        proc = subprocess.run(
            [exe, SIGNIN, action],
            capture_output=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return "[%s] %s 超时 %ss" % (_ts(), action, timeout)
    except OSError as exc:
        return "[%s] 启动失败：%s" % (_ts(), exc)

    parts = ["[%s] $ python signin.py %s" % (_ts(), action)]
    if proc.stdout:
        parts.append(proc.stdout.rstrip())
    if proc.stderr:
        parts.append("-- stderr --\n" + proc.stderr.rstrip())
    parts.append("[%s] exit=%s" % (_ts(), proc.returncode))
    return "\n".join(parts)


def describe_env():
    return "%s · Python %s" % (os.path.basename(sys.executable), sys.version.split()[0])