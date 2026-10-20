"""Windows 系统主题检测。

读注册表拿到亮/暗模式与强调色，跟随用户在「设置 → 个性化 → 颜色」里的选择。
读不到时退回浅色 + Win11 默认蓝。
"""

import winreg

# Win11 Fluent 默认强调色（Teal #4CC2C4）系，做无法读取时的兜底
FALLBACK_ACCENT = (76, 194, 196)


def _read_dark():
    """只读应用亮/暗模式；找不到键时按浅色处理。"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        with key:
            try:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
            except FileNotFoundError:
                return False
    except OSError:
        return False


def is_dark():
    """供图标主题轮询使用：只读一个值，不碰强调色。"""
    return _read_dark()


def _read_personalize():
    return _read_dark(), _read_accent()


def _read_accent():
    """强调色有三个来源，按可用性取第一个非零值。

    Personalize\\HKCUAccentColor   Win10 写的 0xAABBGGRR
    DWM\\AccentColor               Win11 的 0xAABBGGRR
    DWM\\ColorizationColor         Win32 COLORREF 0x00BBGGRR

    三个值在这台机器上实测：只有第三个存在，且高字节是 0xC4 而非 0x00，
    说明它被复用作别的用途——所以解码统一按 0xBBGGRR 取低三字节。
    """
    sources = (
        (r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "HKCUAccentColor"),
        (r"Software\Microsoft\Windows\DWM", "AccentColor"),
        (r"Software\Microsoft\Windows\DWM", "ColorizationColor"),
    )
    for subkey, name in sources:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey)
            with key:
                value = winreg.QueryValueEx(key, name)[0]
        except OSError:
            continue
        rgb = _unpack_abgr(value)
        if rgb != FALLBACK_ACCENT and any(rgb):
            return rgb
    return FALLBACK_ACCENT


def _unpack_abgr(value):
    """Windows 存的是 0xAABBGGRR 打包色，DPG 要的是 (r, g, b) 0~255。"""
    if not isinstance(value, int):
        return FALLBACK_ACCENT
    r = value & 0xFF
    g = (value >> 8) & 0xFF
    b = (value >> 16) & 0xFF
    if r == 0 and g == 0 and b == 0:      # 0x00000000 通常意味着"自动"
        return FALLBACK_ACCENT
    return (r, g, b)


def detect():
    return _read_personalize()