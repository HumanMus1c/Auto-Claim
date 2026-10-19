"""demo 3 · Kivy

声明式 + 触摸优先的跨平台框架：自带 Graphics/OpenGL 渲染与动画。
观察重点：布局用 BoxLayout/GridLayout 声明，写法最贴近移动端；包体 ~20 MB。
"""

import os
import threading

# Kivy 会解析 sys.argv 并弹配置/控制台，必须在 import 前钉死环境
os.environ.setdefault("KIVY_NO_ARGS", "1")
os.environ.setdefault("KIVY_NO_CONSOLELOG", "0")
os.environ.setdefault("KIVY_HOME", os.path.dirname(os.path.abspath(__file__)))

import kivy                                   # noqa: E402
from kivy.app import App                       # noqa: E402
from kivy.clock import Clock                   # noqa: E402
from kivy.core.window import Window            # noqa: E402
from kivy.metrics import dp                    # noqa: E402
from kivy.uix.boxlayout import BoxLayout       # noqa: E402
from kivy.uix.button import Button             # noqa: E402
from kivy.uix.label import Label               # noqa: E402
from kivy.uix.scrollview import ScrollView     # noqa: E402
from kivy.graphics import Color, RoundedRectangle  # noqa: E402

import common                                 # noqa: E402

KIVY_VERSION = kivy.__version__


class Root(BoxLayout):
    orientation = "vertical"
    # Kivy 的 padding/spacing 必须是可解包序列，标量会在布局阶段抛
    # TypeError: cannot unpack non-iterable float object
    padding = [dp(14), dp(14), dp(14), dp(14)]
    spacing = dp(8)

    def __init__(self, **kw):
        super().__init__(**kw)
        self._lines = []
        self.out = None

        title = Label(
            text="Kivy %s · WorkBuddy 签到控制台" % KIVY_VERSION,
            font_size=dp(17), bold=True, color=(0.15, 0.15, 0.15, 1),
            size_hint_y=None, height=dp(28), halign="left", valign="middle")
        title.bind(size=lambda i, v: setattr(i, "text_size", v))

        sub = Label(
            text=common.describe_env(),
            font_size=dp(12), color=(0.5, 0.5, 0.5, 1),
            size_hint_y=None, height=dp(20), halign="left", valign="middle")
        sub.bind(size=lambda i, v: setattr(i, "text_size", v))

        cmds = common.discover_commands()
        head = Label(
            text="signin.py 子命令：" + " · ".join(c for c, _ in cmds[:6]),
            font_size=dp(12), color=(0.35, 0.35, 0.35, 1),
            size_hint_y=None, height=dp(20), halign="left", valign="middle")
        head.bind(size=lambda i, v: setattr(i, "text_size", v))

        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(38),
                        spacing=dp(6))
        row.add_widget(Button(text="离线体检 doctor", on_release=lambda *_: self.run("doctor")))
        row.add_widget(Button(text="查状态 status", on_release=lambda *_: self.run("status")))
        row.add_widget(Button(text="清空", on_release=lambda *_: self.clear()))
        row.add_widget(Button(text="退出", on_release=lambda *_: App.get_running_app().stop()))

        scroll = ScrollView(do_scroll_x=False, bar_width=dp(6))
        self.out = Label(text="", font_name="RobotoMono-Regular", font_size=dp(12),
                         color=(0.1, 0.1, 0.1, 1), halign="left", valign="top", markup=False)
        self.out.bind(size=lambda i, v: setattr(i, "text_size", (v[0], None)))
        self.out.bind(texture_size=lambda i, v: setattr(self.out, "height", v[1]))
        scroll.add_widget(self.out)

        foot = Label(
            text="Kivy：UI 更新必须经 Clock 切回主线程，后台线程不能直接改控件",
            font_size=dp(11), color=(0.6, 0.6, 0.6, 1),
            size_hint_y=None, height=dp(20), halign="left", valign="middle")
        foot.bind(size=lambda i, v: setattr(i, "text_size", v))

        with self.canvas.before:
            Color(0.965, 0.965, 0.965, 1)
            self._bg_rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(8)])
        self.bind(pos=lambda i, v: setattr(i._bg_rect, "pos", v),
                  size=lambda i, v: setattr(i._bg_rect, "size", v))

        for w in (title, sub, head, row, scroll, foot):
            self.add_widget(w)

    def log(self, msg):
        self._lines.append(msg)
        if len(self._lines) > 300:
            del self._lines[:100]
        self.out.text = "\n".join(self._lines)

    def clear(self):
        self._lines = []
        self.out.text = ""

    def run(self, action):
        self.log(">> 已提交 %s（后台线程）" % action)

        def worker():
            text = common.run_readonly(action)
            # Kivy 的控件只能在主线程改，必须经 Clock 投递回去
            Clock.schedule_once(lambda *_: self.log(text))

        threading.Thread(target=worker, daemon=True).start()


class DemoApp(App):
    def build(self):
        Window.size = (640, 580)
        Window.title = "Kivy demo"
        Window.clearcolor = (0.97, 0.97, 0.97, 1)
        root = Root()
        # app.root 在 run() 之前是 None，初始日志必须在 build() 内部写
        root.log("Kivy %s 已启动" % KIVY_VERSION)
        root.log("signin.py：%s" % common.SIGNIN)
        root.log("只读命令：%s" % " / ".join(common.READONLY_ACTIONS))
        return root


if __name__ == "__main__":
    DemoApp().run()