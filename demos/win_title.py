"""Win32 标题修正。

DPG 2.3.1 在中文 Windows 上有编码 bug：它把 UTF-8 窄字符串直接交给
SetWindowTextW（按系统 ANSI 代码页解读），于是

    "控制台" (UTF-8: e6 8e a7 e5 88 b6 e5 8f b0)
        → 被当成 GBK 解码 → 鎺у埗鍙?  → codepoint 0x93ba 0x443 0x57d7 0x9359 0xfffd

实测 Win32 窗口标题的 codepoint 与上面这串逐字节吻合，而
dpg.get_viewport_title() 返回的却是正确的 —— 说明 DPG 内部存储没问题，
坏在"窄字符串 → 宽字符"这一段转换。

对策：viewport 显示之后，用 SetWindowTextW 显式写一次真正的 Unicode 标题。
"""

import ctypes
import os

user32 = ctypes.windll.user32

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def _own_windows():
    """列出本进程所有顶层窗口，(hwnd, title)。"""
    found = []

    def cb(hwnd, _lparam):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


def _looks_mojibake(title):
    """判断标题是否被 UTF-8-as-ANSI 错误解码。

    不靠字表猜测，改用码位区段特征——UTF-8 三个字节序列被按 GBK 两字节切分时，
    必然产生 CJK 统一表里的一整片生僻字（U+93BA、U+0443 混排等），
    且极少出现 U+3000~U+303F 的中文标点区。这是与正常中文标题最稳的区分点。
    """
    if not title:
        return False
    for ch in title:
        cp = ord(ch)
        # 替换字符：解码失败的直接证据
        if cp in (0xFFFD, 0xFFFE):
            return True
        # GBK 误解码的产物落在 CJK 扩展区 / 私用区
        if 0xE000 <= cp <= 0xF8FF or 0x3400 <= cp <= 0x4DBF:
            return True
    # 标题里有中文，却没有中文该有的标点/间隔特征 —— 保守判定为可疑
    cjk = [ord(c) for c in title if 0x4E00 <= ord(c) <= 0x9FFF]
    return len(cjk) >= 2 and all(0x4E00 <= c <= 0x9FFF for c in cjk) is False


def set_native_title(text):
    """把真实 Unicode 标题写进 Win32 窗口，绕开 DPG 的编码缺陷。

    策略更简单也更可靠：直接改掉「非 ASCII 且当前标题不等于目标标题」的窗口，
    不去猜它是否乱码——DPG 在中文 Windows 上一定是错的。

    返回被修正的窗口数量。调用时机：create_viewport + show_viewport 之后。
    """
    fixed = 0
    for hwnd, current in _own_windows():
        if not current or current == text:
            continue
        # 跳过 ASCII 标题（MSCTFIME UI / Default IME 之类），那些不该动
        if all(ord(c) < 0x80 for c in current):
            continue
        user32.SetWindowTextW(hwnd, text)
        fixed += 1
    return fixed