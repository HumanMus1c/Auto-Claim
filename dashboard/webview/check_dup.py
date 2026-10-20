"""检测函数体内的重复代码块。

起因：一次用 edit 工具替换 subprocess 调用时，只匹配到一半，
结果 _worker() 里出现了两份完全相同的"解析+推日志"逻辑。
第二份会把 stdout 重新解析一遍（虽然 stdout 未被消耗，但重复推送日志、
以及后续分支的维护成本都是隐患）。

浏览器不报错、程序照常跑，只是日志会成对出现——这类问题只能靠静态检查兜。
"""

import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 这些"短语句"天然会重复出现，不算问题。
# 后两条出现在 if/else 两个互斥分支里（推日志的两条路径），属正常结构。
BENIGN = {
    "break", "continue", "return", "pass", "}", "{", ")", "(",
    "try:", "finally:", "else:", "except Exception as exc:", "return []",
    'self._push("log", line)', "_append_log(line)",
    # 双重检查锁的标准写法：锁外先看一次、锁内再看一次
    "if _SIGNIN_MODULE is not None:", "return _SIGNIN_MODULE",
}


def dup_in(body_lines):
    """返回出现次数 >1 且不属于 BENIGN 的行。"""
    seen = {}
    for raw in body_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        seen[line] = seen.get(line, 0) + 1
    return [l for l, c in seen.items() if c > 1 and l not in BENIGN]


def check(path):
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)          # 语法有问题会直接抛，算作失败
    lines = src.split("\n")

    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef,)):
            continue
        start, end = node.lineno, node.end_lineno
        dup = dup_in(lines[start - 1:end])
        if dup:
            problems.append((node.name, node.lineno, dup))

    return problems


def main():
    targets = sys.argv[1:] or [os.path.join(HERE, "app.py")]
    rc = 0
    for path in targets:
        name = os.path.basename(path)
        try:
            problems = check(path)
        except SyntaxError as exc:
            print("✗ %s 语法错误：%s" % (name, exc))
            rc = 1
            continue

        if problems:
            rc = 1
            print("✗ %s 发现疑似重复代码：" % name)
            for fn, lineno, dup in problems:
                print("   %s() (行 %d)" % (fn, lineno))
                for d in dup[:5]:
                    print("      重复 %d 次: %s" % (2, d[:70]))
        else:
            print("✓ %s 无重复代码" % name)
    return rc


if __name__ == "__main__":
    # Windows 控制台默认 GBK，输出 ✓/✗ 这类符号会抛 UnicodeEncodeError，
    # 退出码非零又被 build_exe.py 当成"检查不通过"——曾经因此误报过一次。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())