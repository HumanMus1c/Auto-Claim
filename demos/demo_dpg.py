"""demo 2 · Dear PyGui

立即模式 GUI：内置深/浅色主题，控件自带 hover / 动画，观感现代。
观察重点：API 最直观（add_xxx + callback），自带主题系统；包体 ~1.9 MB。
"""

import threading

import dearpygui.dearpygui as dpg

import common

# 注意：DPG 2.3.1 在本机 Python 3.13 上调用 get_dearpygui_version() 会触发
# 底层访问冲突，所以这里改用包元数据取版本，避免碰 C++ 层
try:
    from importlib.metadata import version as _pkg_version
    VERSION = _pkg_version("dearpygui")
except Exception:
    VERSION = "?"

LOG_TAG = "log_text"
PANEL_TAG = "log_panel"
_lines = []


def _log(msg):
    _lines.append(msg)
    if len(_lines) > 300:
        del _lines[:100]
    dpg.set_value(LOG_TAG, "\n".join(_lines))
    # set_y_scroll 只接受容器类控件（window/child_window），Text 上会报 1002
    dpg.set_y_scroll(PANEL_TAG, -1)


def _clear(sender=None, app_data=None):
    del _lines[:]
    dpg.set_value(LOG_TAG, "")


def _bg(action):
    """后台线程跑命令；DPG 的 set_value 在简单场景可跨线程调用。"""
    def worker():
        _log(common.run_readonly(action))
    threading.Thread(target=worker, daemon=True).start()
    _log(">> 已提交 %s（后台线程）" % action)


def on_doctor(sender, app_data):
    _bg("doctor")


def on_status(sender, app_data):
    _bg("status")


def build():
    dpg.create_context()

    # 必须先建视口，show_viewport 才有对象可显示（2.x 的硬性顺序）
    dpg.create_viewport(title="Dear PyGui demo", width=660, height=580)

    with dpg.window(label="Dear PyGui", width=660, height=580, tag="main"):
        dpg.add_text("Dear PyGui %s · WorkBuddy 签到控制台" % VERSION,
                     color=(0.35, 0.35, 0.35))
        dpg.add_text(common.describe_env(), color=(0.55, 0.55, 0.55))
        dpg.add_text("signin.py 子命令：" + "  ".join(c for c, _ in common.discover_commands()[:6]),
                     color=(0.35, 0.35, 0.35))

        dpg.add_spacer(height=8)

        with dpg.group(horizontal=True):
            dpg.add_button(label="离线体检 (doctor)", callback=on_doctor, width=180)
            dpg.add_button(label="查签到状态 (status)", callback=on_status, width=180)
            dpg.add_button(label="清空", callback=_clear, width=90)
            dpg.add_button(label="退出", width=90,
                           callback=lambda s, a: dpg.stop_dearpygui())

        dpg.add_spacer(height=10)

        with dpg.child_window(height=350, border=True, tag=PANEL_TAG):
            dpg.add_text("", tag=LOG_TAG, wrap=0)

        dpg.add_text("立即模式：渲染循环每帧重画，状态靠 tag 直接改，无需手动刷新",
                     color=(0.6, 0.6, 0.6))

    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main", True)
    dpg.set_viewport_resize_callback(
        lambda: dpg.configure_item("main", width=dpg.get_viewport_client_width()))

    _log("Dear PyGui %s 已启动" % VERSION)
    _log("signin.py：%s" % common.SIGNIN)
    _log("只读命令：%s" % " / ".join(common.READONLY_ACTIONS))


if __name__ == "__main__":
    build()
    dpg.start_dearpygui()