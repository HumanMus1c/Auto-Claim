"""Dear PyGui · Win11 Fluent 风格 · 跟随 Windows 系统主题

设计要点：
  - 亮/暗跟随「设置 → 个性化 → 颜色」的 AppsUseLightTheme
  - 强调色取自 DWM 注册表，与系统任务栏/开始菜单同色
  - 圆角、留白、卡片式层次（而非 DPG 默认的紧凑灰底）

避开本机 Python 3.13 上的两个坑：
  - get_dearpygui_version() 会触发底层访问崩溃，改用包元数据
  - set_y_scroll() 不能作用于 Text，只能挂在 child_window 上
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from importlib.metadata import version as _pkg_version

import dearpygui.dearpygui as dpg

import common
import win_theme
import win_title

try:
    VERSION = _pkg_version("dearpygui")
except Exception:
    VERSION = "?"

DARK, ACCENT = win_theme.detect()


# ---------------------------------------------------------------- 调色板

def _mix(a, b, t):
    """线性插值，用来从强调色派生出一整套 hover/active 状态色。"""
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def palette():
    """按当前系统主题生成完整调色板。"""
    accent = ACCENT
    if DARK:
        return {
            "window": (32, 32, 32),
            "surface": (43, 43, 43),          # 卡片底
            "surface_hi": (56, 56, 56),
            "border": (69, 69, 69),
            "text": (241, 241, 241),
            "text_dim": (160, 160, 160),
            "accent": accent,
            "accent_hi": _mix(accent, (255, 255, 255), 0.14),
            "accent_lo": _mix(accent, (0, 0, 0), 0.14),
            "on_accent": (255, 255, 255),
            "success": (92, 196, 126),
            "warn": (214, 168, 74),
            "danger": (214, 92, 92),
            "log_bg": (24, 24, 24),
        }
    return {
        "window": (243, 243, 243),
        "surface": (255, 255, 255),
        "surface_hi": (247, 247, 247),
        "border": (225, 225, 225),
        "text": (28, 28, 28),
        "text_dim": (110, 110, 110),
        "accent": accent,
        "accent_hi": _mix(accent, (0, 0, 0), 0.10),
        "accent_lo": _mix(accent, (0, 0, 0), 0.20),
        "on_accent": (255, 255, 255),
        "success": (16, 124, 65),
        "warn": (168, 116, 0),
        "danger": (190, 40, 40),
        "log_bg": (252, 252, 252),
    }


P = palette()


# ---------------------------------------------------------------- 主题
#
# DPG 2.x 的两个约束，决定了这里为什么写成包装函数：
#   1. add_theme() 返回 int，不支持 `with theme:` 上下文管理器语法
#   2. add_theme_style / add_theme_color 没有隐式"当前主题"，必须显式传 parent=
# 所以用 _T() 持有当前主题，ts()/tc() 负责补上 parent=。

_T = {"theme": None}


def _use(theme):
    """add_theme() 只建了空壳，必须再挂一个 theme_component 才能作为 parent。

    直接把 theme 传给 add_theme_style 会报 1003：
    "Incompatible parent. Acceptable parents include: mvThemeComponent"
    """
    component = dpg.add_theme_component(dpg.mvAll, parent=theme)
    _T["theme"] = component
    return theme


def ts(var, x, y=0, cat=None):
    kwargs = {"parent": _T["theme"]}
    if cat is not None:
        kwargs["category"] = cat
    return dpg.add_theme_style(var, x, y, **kwargs)


def tc(col, value, cat=None):
    kwargs = {"parent": _T["theme"]}
    if cat is not None:
        kwargs["category"] = cat
    return dpg.add_theme_color(col, value, **kwargs)


def build_theme():
    """构建 Fluent 质感主题：8px 圆角、宽松留白、扁平无边框卡片。"""
    theme = _use(dpg.add_theme())

    # —— 全局样式：圆角与留白是「现代感」的主要来源
    ts(dpg.mvStyleVar_WindowPadding, 18, 18)
    ts(dpg.mvStyleVar_FramePadding, 12, 8)
    ts(dpg.mvStyleVar_ItemSpacing, 12, 10)
    ts(dpg.mvStyleVar_ItemInnerSpacing, 8, 6)
    ts(dpg.mvStyleVar_WindowRounding, 0, 0)
    ts(dpg.mvStyleVar_ChildRounding, 10, 0)
    ts(dpg.mvStyleVar_FrameRounding, 8, 0)
    ts(dpg.mvStyleVar_PopupRounding, 8, 0)
    ts(dpg.mvStyleVar_GrabRounding, 8, 0)
    ts(dpg.mvStyleVar_TabRounding, 6, 0)
    ts(dpg.mvStyleVar_ScrollbarRounding, 8, 0)
    ts(dpg.mvStyleVar_ChildBorderSize, 0, 0)
    ts(dpg.mvStyleVar_FrameBorderSize, 0, 0)
    ts(dpg.mvStyleVar_PopupBorderSize, 0, 0)
    ts(dpg.mvStyleVar_GrabMinSize, 26, 0)
    ts(dpg.mvStyleVar_ScrollbarSize, 10, 0)
    ts(dpg.mvStyleVar_TabMinWidthBase, 90, 0)
    ts(dpg.mvStyleVar_WindowTitleAlign, 0, 0)

    # —— 基础色
    tc(dpg.mvThemeCol_WindowBg, P["window"])
    tc(dpg.mvThemeCol_ChildBg, P["surface"])
    tc(dpg.mvThemeCol_PopupBg, P["surface"])
    tc(dpg.mvThemeCol_Border, P["border"])
    tc(dpg.mvThemeCol_Text, P["text"])
    tc(dpg.mvThemeCol_TextDisabled, P["text_dim"])
    tc(dpg.mvThemeCol_FrameBg, P["surface"])
    tc(dpg.mvThemeCol_FrameBgHovered, P["surface_hi"])
    tc(dpg.mvThemeCol_FrameBgActive, P["surface_hi"])
    tc(dpg.mvThemeCol_TitleBg, P["surface"])
    tc(dpg.mvThemeCol_TitleBgActive, P["surface"])
    tc(dpg.mvThemeCol_TitleBgCollapsed, P["surface"])
    tc(dpg.mvThemeCol_MenuBarBg, P["surface"])
    tc(dpg.mvThemeCol_ScrollbarBg, P["window"])
    tc(dpg.mvThemeCol_ScrollbarGrab, P["border"])
    tc(dpg.mvThemeCol_ScrollbarGrabHovered, P["text_dim"])
    tc(dpg.mvThemeCol_ScrollbarGrabActive, P["accent"])
    tc(dpg.mvThemeCol_Separator, P["border"])
    tc(dpg.mvThemeCol_CheckMark, P["accent"])
    tc(dpg.mvThemeCol_SliderGrab, P["accent"])
    tc(dpg.mvThemeCol_SliderGrabActive, P["accent_hi"])
    tc(dpg.mvThemeCol_Tab, P["window"])
    tc(dpg.mvThemeCol_TabHovered, P["surface_hi"])
    tc(dpg.mvThemeCol_TabActive, P["surface"])
    tc(dpg.mvThemeCol_TabSelected, P["surface"])
    tc(dpg.mvThemeCol_TabDimmed, P["window"])
    tc(dpg.mvThemeCol_TabDimmedSelected, P["surface"])
    tc(dpg.mvThemeCol_TextSelectedBg, P["accent"])

    # —— 按钮：主按钮用强调色，次按钮用描边
    tc(dpg.mvThemeCol_Button, P["surface"])
    tc(dpg.mvThemeCol_ButtonHovered, P["surface_hi"])
    tc(dpg.mvThemeCol_ButtonActive, P["border"])
    # DPG 没有独立的 ButtonText 常量，按钮文字取 mvThemeCol_Text
    tc(dpg.mvThemeCol_Text, P["text"])

    # —— 表格
    tc(dpg.mvThemeCol_TableHeaderBg, P["surface_hi"])
    tc(dpg.mvThemeCol_TableBorderStrong, P["border"])
    tc(dpg.mvThemeCol_TableBorderLight, P["border"])
    tc(dpg.mvThemeCol_TableRowBg, P["surface"])
    tc(dpg.mvThemeCol_TableRowBgAlt, P["surface_hi"])

    dpg.bind_theme(theme)
    return theme
# ---------------------------------------------------------------- 字体
#
# DPG 2.x 的文字排版模型：
#   - 没有 add_text(font=22) 这种写法，font 关键字会直接抛 "font keyword does not exist"
#   - 正确做法是先 add_font(path, size) 建字体资源，再 bind_font / bind_item_font 绑给控件
#   - .ttc 字体集合（msyh.ttc）DPG 支持不稳定，这里统一用单字体 .ttf

FONT_CANDIDATES = {
    11: "C:/Windows/Fonts/segoeui.ttf",
    13: "C:/Windows/Fonts/segoeui.ttf",
    16: "C:/Windows/Fonts/segoeuib.ttf",
    28: "C:/Windows/Fonts/segoeuib.ttf",
}
FONT_MONO = "C:/Windows/Fonts/consola.ttf"

_fonts = {}


def load_fonts():
    """建立 字号 → 字体资源 的映射。

    两个必须知道的 DPG 2.x 事实：
      1. add_font 的 parent 默认值是 mvReservedUUID_0，直接调用会抛
         "Error 1011: Parent could not be deduced"，必须显式给 font_registry()。
      2. 字符范围是**自动**的：add_font_range_hint / add_font_range / add_font_chars
         三个 API 在 2.x 全部是 no-op（会发 DeprecationWarning），
         所以只要把字体文件接上，中文/日文等字形就会自动进图集，无需手工加范围。
    """
    with dpg.font_registry() as registry:
        for size, path in FONT_CANDIDATES.items():
            if not os.path.exists(path):
                _fonts[size] = None
                continue
            try:
                _fonts[size] = dpg.add_font(path, size, parent=registry)
            except Exception as exc:
                _fonts[size] = None
                print("字体加载失败 %s(%d): %s" % (path, size, exc), file=sys.stderr)
        if os.path.exists(FONT_MONO):
            try:
                _fonts["mono"] = dpg.add_font(FONT_MONO, 13, parent=registry)
            except Exception as exc:
                _fonts["mono"] = None
                print("等宽字体加载失败: %s" % exc, file=sys.stderr)
    return _fonts


def apply_font(item, size):
    """给单个控件绑字体；字体缺失时静默跳过，DPG 会用内置 ProggyClean。"""
    font = _fonts.get(size)
    if font:
        try:
            dpg.bind_item_font(item, font)
        except Exception:
            pass
    return item


def bind_theme_to(item, theme):
    """给单个控件绑局部主题。

    DPG 2.x 去掉了 add_xxx(theme=...) 关键字，只能事后 bind_item_theme()。
    """
    if theme:
        try:
            dpg.bind_item_theme(item, theme)
        except Exception:
            pass
    return item


def txt(value, size=13, color=None, **kw):
    """建一段文本并自动绑好字体，替代 add_text(font=...)。"""
    item = dpg.add_text(value, color=color if color is not None else P["text"], **kw)
    apply_font(item, size)
    return item


def make_component():
    """单用途子主题：主按钮（强调色实心）/ 次按钮（幽灵）/ 日志区（等宽深底）。"""
    made = {}

    primary = _use(dpg.add_theme())
    tc(dpg.mvThemeCol_Button, P["accent"])
    tc(dpg.mvThemeCol_ButtonHovered, P["accent_hi"])
    tc(dpg.mvThemeCol_ButtonActive, P["accent_lo"])
    # 按钮文字色没有独立常量：组件主题里用 Text 覆盖全局的文字色
    tc(dpg.mvThemeCol_Text, P["on_accent"])
    ts(dpg.mvStyleVar_FrameRounding, 8, 0)
    ts(dpg.mvStyleVar_FramePadding, 16, 9)
    made["primary"] = primary

    ghost = _use(dpg.add_theme())
    tc(dpg.mvThemeCol_Button, (0, 0, 0, 0))
    tc(dpg.mvThemeCol_ButtonHovered, P["surface_hi"])
    tc(dpg.mvThemeCol_ButtonActive, P["border"])
    tc(dpg.mvThemeCol_Text, P["text_dim"])
    ts(dpg.mvStyleVar_FramePadding, 12, 7)
    ts(dpg.mvStyleVar_FrameRounding, 8, 0)
    made["ghost"] = ghost

    log = _use(dpg.add_theme())
    tc(dpg.mvThemeCol_ChildBg, P["log_bg"])
    tc(dpg.mvThemeCol_Text, P["text_dim"])
    ts(dpg.mvStyleVar_ChildRounding, 10, 0)
    ts(dpg.mvStyleVar_WindowPadding, 12, 12)
    ts(dpg.mvStyleVar_ScrollbarSize, 10, 0)
    made["log"] = log

    return made

# ---------------------------------------------------------------- 日志

LOG_TAG = "log_text"
PANEL_TAG = "log_panel"
_lines = []
_ui_theme = {}


def _log(msg):
    _lines.append(msg)
    if len(_lines) > 400:
        del _lines[:150]
    dpg.set_value(LOG_TAG, "\n".join(_lines))
    # set_y_scroll 只接受容器类控件（window/child_window）
    dpg.set_y_scroll(PANEL_TAG, -1)


def _log_clear(sender=None, app_data=None):
    del _lines[:]
    dpg.set_value(LOG_TAG, "")


def _submit(action):
    """后台线程跑 signin.py 的只读命令，结果追加到日志区。"""
    def worker():
        _log(common.run_readonly(action))
    threading.Thread(target=worker, daemon=True).start()
    _log(">> 已提交 %s（后台线程）" % action)


# ---------------------------------------------------------------- 界面

def build():
    dpg.create_context()

    mode = "深色" if DARK else "浅色"
    WINDOW_TITLE = "WorkBuddy 控制台 · %s主题" % mode
    dpg.create_viewport(title=WINDOW_TITLE, width=980, height=660)
    dpg.set_viewport_vsync(True)

    build_theme()
    t = make_component()
    _ui_theme.update(t)
    load_fonts()

    with dpg.window(tag="root", no_move=True):
        # —— 标题区
        txt("WorkBuddy 控制台", size=16)
        meta = "Dear PyGui %s · %s主题 · 强调色 %s" % (
            VERSION, mode, "#%02X%02X%02X" % ACCENT)
        txt(meta, size=11, color=P["text_dim"])
        dpg.add_spacer(height=14)

        # —— 指标卡片行
        with dpg.group(horizontal=True):
            txt("今日积分", size=11, color=P["text_dim"])
            txt("—", size=28, color=P["accent"], tag="v_credit")
            txt("   连签天数", size=11, color=P["text_dim"])
            txt("—", size=28, color=P["accent"], tag="v_streak")
            txt("   能量", size=11, color=P["text_dim"])
            txt("—", size=28, color=P["accent"], tag="v_energy")
            txt("   补登卡", size=11, color=P["text_dim"])
            txt("—", size=28, color=P["accent"], tag="v_cards")

        dpg.add_spacer(height=6)
        txt("说明：只读预览，点下方按钮执行真实的 doctor / status 查询",
            size=11, color=P["text_dim"])
        dpg.add_spacer(height=14)

        # —— 操作按钮
        # DPG 2.x 没有 theme= 关键字，必须建完控件再 bind_item_theme()
        with dpg.group(horizontal=True):
            bind_theme_to(dpg.add_button(
                label="离线体检 doctor", width=170,
                callback=lambda s, a: _submit("doctor")), t["primary"])
            bind_theme_to(dpg.add_button(
                label="查签到状态 status", width=190,
                callback=lambda s, a: _submit("status")), t["primary"])
            bind_theme_to(dpg.add_button(
                label="清空日志", width=100, callback=_log_clear), t["ghost"])
            bind_theme_to(dpg.add_button(
                label="退出", width=90,
                callback=lambda s, a: dpg.stop_dearpygui()), t["ghost"])

        dpg.add_spacer(height=12)

        # —— 日志区
        with dpg.child_window(height=-1, border=True, tag=PANEL_TAG) as panel:
            bind_theme_to(panel, t["log"])
            item = dpg.add_text("", tag=LOG_TAG, wrap=-1)
            apply_font(item, "mono")

    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("root", True)

    dpg.set_viewport_resize_callback(lambda: dpg.configure_item("root", width=-1))

    # DPG 在中文 Windows 上会把 UTF-8 标题按 GBK 误解码（"控制台"→"鎺у埗鍙?"），
    # 这里用 Win32 API 事后覆盖成真正的 Unicode 标题。详见 win_title.py。
    fixed = win_title.set_native_title(WINDOW_TITLE)
    if fixed:
        _log("窗口标题编码已修正（DPG 中文标题缺陷）")

    _log("Dear PyGui %s 已启动" % VERSION)
    _log("系统主题：%s（AppsUseLightTheme=%d）" % (
        mode, 0 if DARK else 1))
    _log("系统强调色：rgb%s" % (ACCENT,))
    _log("signin.py：%s" % common.SIGNIN)
    _log("可用只读命令：%s" % " / ".join(common.READONLY_ACTIONS))


if __name__ == "__main__":
    build()
    dpg.start_dearpygui()