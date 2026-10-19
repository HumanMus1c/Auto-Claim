"""_locate_signin() 的回归测试。

这个函数出过一次很难查的 bug：打包后程序一直报「未找到 signin.py」，
而文件明明就在 exe 旁边。原因是冻结模式下 _base_dir() 返回 sys._MEIPASS
（%TEMP% 下的解包目录），从那里上溯永远到不了 exe 所在目录。

这类"环境分支"用普通运行测不出来——必须在测试里显式伪造 sys.frozen /
sys.executable / sys._MEIPASS。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app  # noqa: E402

# HERE = <repo>/demos/webview，仓库根是它的上两级
REPO = os.path.dirname(os.path.dirname(HERE))


class LocateSigninTest(unittest.TestCase):
    """每个用例都要还原 sys 上的冻结标记，避免互相污染。"""

    def setUp(self):
        self._saved = {k: getattr(sys, k, None)
                       for k in ("frozen", "_MEIPASS", "executable")}
        self._saved_env = os.environ.pop("WORKBUDDY_DEMO_SIGNIN", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                if hasattr(sys, k):
                    delattr(sys, k)
            else:
                setattr(sys, k, v)
        if self._saved_env is not None:
            os.environ["WORKBUDDY_DEMO_SIGNIN"] = self._saved_env

    # ---------- 源码模式 ----------

    def test_source_mode_finds_repo_signin(self):
        """源码运行时能从 demos/webview 上溯找到仓库根的 signin.py"""
        # 源码模式下 sys 本就没有 frozen 属性，直接 del 会 AttributeError
        if hasattr(sys, "frozen"):
            del sys.frozen
        found = app._locate_signin()
        self.assertTrue(os.path.isfile(found),
                        "源码模式应找到 signin.py，实际 %s" % found)
        self.assertEqual(os.path.basename(found), "signin.py")

    # ---------- 冻结模式（关键回归）----------

    def test_frozen_searches_exe_directory_first(self):
        """冻结后必须优先从 exe 所在目录找。

        这是那次线上 bug 的核心：_MEIPASS 在 %TEMP% 下，从那里上溯
        只会一路走到 C:\\Users\\，永远碰不到 exe 旁边的 signin.py。
        """
        sys.frozen = True
        sys._MEIPASS = os.path.join(os.environ.get("TEMP", "/tmp"), "_MEI_FAKE")
        exe_dir = REPO
        sys.executable = os.path.join(exe_dir, "WorkBuddyConsole.exe")

        found = app._locate_signin()

        self.assertTrue(os.path.isfile(found),
                        "冻结模式应找到 signin.py，实际 %s" % found)
        self.assertEqual(
            os.path.dirname(os.path.abspath(found)),
            os.path.abspath(exe_dir),
            "应当直接命中 exe 同级目录，而不是从 _MEIPASS 慢慢上溯")
        self.assertEqual(app.SEARCHED[0],
                         os.path.join(exe_dir, "signin.py"),
                         "搜索列表第一项应是 exe 同级的 signin.py")

    def test_frozen_never_only_searches_meipass(self):
        """回归的另一半：搜索列表里必须包含 exe 目录。

        即使最终因别的原因命中，也要保证 exe 目录被搜过——
        否则用户把 exe 拷到别处、signin.py 不在那儿时，
        报错信息会误导人往错误方向排查。
        """
        sys.frozen = True
        sys._MEIPASS = os.path.join(os.environ.get("TEMP", "/tmp"), "_MEI_FAKE")
        exe_dir = REPO
        sys.executable = os.path.join(exe_dir, "WorkBuddyConsole.exe")

        app._locate_signin()

        expected = os.path.join(exe_dir, "signin.py")
        self.assertIn(expected, app.SEARCHED,
                      "搜索路径应包含 exe 同级的 signin.py")

    # ---------- 环境变量优先 ----------

    def test_env_override_wins(self):
        """WORKBUDDY_DEMO_SIGNIN 优先级最高"""
        sentinel = os.path.abspath(os.path.join(HERE, "nonexistent_marker.py"))
        os.environ["WORKBUDDY_DEMO_SIGNIN"] = sentinel
        if hasattr(sys, "frozen"):
            del sys.frozen
        self.assertEqual(app._locate_signin(), sentinel)


if __name__ == "__main__":
    unittest.main(verbosity=2)