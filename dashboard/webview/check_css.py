"""样式表完整性自检。

背景：曾用脚本按「起止选择器」批量删除 CSS 死代码，而起点与终点之间
还夹着别的规则，整块被连带删除。浏览器不报错、布局照旧渲染，
只是某类控件悄悄变回浏览器默认样式——极难察觉。

所以这里把关键选择器列成清单，每次改 CSS 后跑一遍。
"""

import io
import os
import re
import sys

CSS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "style.css")

# 必须存在的选择器（够用即可，不必穷举）。
# 注意要写实际写法：带 :not(:disabled) 的伪类链整体算一个选择器，
# 不要图省事只写 ".btn:hover"。
REQUIRED = [
    ".btn", ".btn:hover:not(:disabled)", ".btn code", ".btn.primary",
    ".btn.primary:hover:not(:disabled)", ".btn.act", ".btn.confirm",
    ".btn.ghost", ".btn.danger",
    ".act-group", ".act-sep", ".actions",
    ".card", ".card-value", ".cards",
    ".tasklist", ".task", ".task-title", ".bar", ".tag",
    ".growth-overview", ".growth-card", ".growth-card-value",
    ".growth-card-line", ".growth-subhead", ".growth-updated",
    ".kv", ".cal", ".cal-day", ".cal-grid",
    ".pane-service", ".svc-pill", ".svc-task", ".svc-actions", ".svc-log",
    ".svc-alert", ".svc-alert-hint", ".svc-alert code", ".svc-alert details",
    ".log .line", ".pane", ".pane-head", ".pane-tools", ".row2",
    ".topbar", ".brand", ".badge", ".meta",
    ".tabs", ".tab", ".mini-btn", ".hint", ".icon-btn",
]

AT_RULES = ["@keyframes", "@media"]


def main():
    text = io.open(CSS, encoding="utf-8").read()

    # 先剥注释：注释位于 depth 0，会被误当成选择器的一部分
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)

    # 花括号必须平衡
    if text.count("{") != text.count("}"):
        print("✗ 花括号不平衡：{ %d } %d" % (text.count("{"), text.count("}")))
        return 1

    # 顶层选择器：逐字符扫描并跟踪花括号深度，只在 depth 0 处收选择器。
    # 用正则抽取在多行选择器上很容易漏，踩过一次就别再用正则了。
    selectors = set()
    depth = 0
    buf = []
    for ch in text:
        if ch == "{":
            if depth == 0:
                head = "".join(buf).strip()
                if head and not head.startswith("@"):
                    for sel in head.split(","):
                        sel = " ".join(sel.split())
                        if sel:
                            selectors.add(sel)
            buf = []
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
            buf = []
        elif depth == 0:
            buf.append(ch)
        # depth > 0 属于声明块，忽略

    missing = [s for s in REQUIRED if s not in selectors]

    print("顶层规则数：%d" % len(selectors))
    print("@ 规则：%s" % ", ".join(
        a for a in AT_RULES if a in text) or "无")

    if missing:
        print("\n✗ 缺少以下选择器（%d 个）：" % len(missing))
        for s in missing:
            print("   %s" % s)
        return 1

    print("✓ 关键选择器齐全")
    return 0


if __name__ == "__main__":
    # 控制台默认 GBK，输出 ✓/✗ 会抛 UnicodeEncodeError，
    # 退出码非零会被 build_exe.py 误判为检查失败
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())