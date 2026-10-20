"""win_icon 的回归测试。

不创建真实托盘图标（会闪现在用户的通知区域），只验证：

  - 三套 ICO 都存在，且包含 16 / 256 两种尺寸
  - 主题到文件名的选择、缺文件时的兜底
  - HICON 能真正加载并释放
  - 空句柄/非法窗口不会抛异常

窗口图标写入、托盘增删和主题轮询由实机验证；这些路径依赖真实的
Win32 消息循环，放进单元测试会干扰桌面。
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.dirname(HERE)
ASSETS = os.path.join(HERE, "assets")
sys.path.insert(0, DASHBOARD)

import win_icon  # noqa: E402

try:
    import win_theme  # noqa: E402
except Exception:                   # 非 Windows 环境下 winreg 不存在
    win_theme = None


@unittest.skipUnless(os.name == "nt", "仅 Windows 可测")
class IconFilesTest(unittest.TestCase):

    def test_variants_contain_required_sizes(self):
        try:
            from PIL import Image
        except Exception:               # 没装 Pillow 时只做存在性检查
            Image = None

        for name in ("icon.ico", "icon_light.ico", "icon_dark.ico"):
            path = os.path.join(ASSETS, name)
            self.assertTrue(os.path.isfile(path), "%s 不存在" % name)
            self.assertGreater(os.path.getsize(path), 1024)

            if Image is None:
                continue
            with Image.open(path) as image:
                sizes = image.info.get("sizes") or set()
            self.assertIn((16, 16), sizes, "%s 缺 16px" % name)
            self.assertIn((256, 256), sizes, "%s 缺 256px" % name)

    def test_icon_path_prefers_theme_variant(self):
        self.assertTrue(win_icon.icon_path(ASSETS, False)
                        .endswith("icon_light.ico"))
        self.assertTrue(win_icon.icon_path(ASSETS, True)
                        .endswith("icon_dark.ico"))

    def test_icon_path_falls_back_to_static_icon(self):
        with tempfile.TemporaryDirectory(prefix="wb_icon_test_") as tmp:
            fallback = os.path.join(tmp, "icon.ico")
            with open(fallback, "wb") as fh:
                fh.write(b"ico")
            self.assertEqual(win_icon.icon_path(tmp, False), fallback)
            self.assertEqual(win_icon.icon_path(tmp, True), fallback)

    def test_icon_path_missing_returns_none(self):
        with tempfile.TemporaryDirectory(prefix="wb_icon_test_") as tmp:
            self.assertIsNone(win_icon.icon_path(tmp, False))


@unittest.skipUnless(os.name == "nt", "仅 Windows 可测")
class LoadIconTest(unittest.TestCase):

    def test_load_and_destroy(self):
        path = win_icon.icon_path(ASSETS, True)
        self.assertTrue(path)
        handle = win_icon.load_icon(path, small=True)
        self.assertTrue(handle, "HICON 加载失败")
        win_icon._destroy_icon(handle)

    def test_missing_file_is_safe(self):
        self.assertIsNone(win_icon.load_icon(
            os.path.join(ASSETS, "__no_such_icon__.ico")))

    def test_iconset_without_window_is_safe(self):
        icons = win_icon._IconSet(win_icon.icon_path(ASSETS, False))
        self.assertFalse(icons.apply(None))
        icons.close()
        icons.close()                   # 重复释放不应抛异常


class ManagerLifecycleTest(unittest.TestCase):

    def test_invalid_window_is_noop(self):
        manager = win_icon.IconManager(0, ASSETS, tray=False)
        self.assertFalse(manager.start())
        manager.stop()

    def test_without_windows_module_still_works(self):
        # 非 Windows 平台也应能 import 并安全地走空实现
        if os.name == "nt" and win_theme is not None:
            self.assertIsInstance(win_theme.is_dark(), bool)
        else:
            self.assertIsNone(win_icon.load_icon("x.ico"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
