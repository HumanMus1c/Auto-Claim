"""win_geometry 的纯函数回归测试。

窗口位置记忆的几个关键点都是「坏数据不能让程序开在看不见的地方」，
所以重点测三件事：

  - 读盘：缺文件/坏 JSON/字段类型不对 → 退回默认值，不抛异常
  - 尺寸与坐标：低于最小尺寸、超出合理范围、布尔值冒充数字 → 收敛掉
  - 跟踪：最大化/最小化期间的尺寸与坐标绝不能写进存储（否则会覆盖掉
    用户真正想要的还原尺寸）

Win32 部分（ensure_visible / center_on_primary）只在有真实窗口句柄时才有意义，
这里只验证无句柄/非 Windows 时安全返回。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.dirname(HERE)
sys.path.insert(0, DASHBOARD)

import win_geometry  # noqa: E402


class GeometryStoreTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="wbgeom-")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def write_raw(self, text):
        with open(win_geometry.store_path(self.folder), "w",
                  encoding="utf-8") as fh:
            fh.write(text)

    def test_missing_file_returns_defaults(self):
        rect = win_geometry.load(self.folder)
        self.assertEqual(rect["width"], win_geometry.DEFAULT_SIZE[0])
        self.assertEqual(rect["height"], win_geometry.DEFAULT_SIZE[1])
        self.assertFalse(rect["maximized"])
        self.assertNotIn("x", rect)
        self.assertNotIn("y", rect)

    def test_roundtrip_keeps_rect(self):
        saved = {"x": 120, "y": 90, "width": 1400, "height": 900,
                 "maximized": False}
        self.assertTrue(win_geometry.save(self.folder, saved))
        rect = win_geometry.load(self.folder)
        for key in ("x", "y", "width", "height"):
            self.assertEqual(rect[key], saved[key], key)
        self.assertFalse(rect["maximized"])

    def test_maximized_flag_roundtrip(self):
        win_geometry.save(self.folder, {"x": 10, "y": 10, "width": 1200,
                                        "height": 700, "maximized": True})
        self.assertTrue(win_geometry.load(self.folder)["maximized"])

    def test_corrupt_json_falls_back(self):
        self.write_raw("{ 这不是 json")
        rect = win_geometry.load(self.folder)
        self.assertEqual(rect["width"], win_geometry.DEFAULT_SIZE[0])
        self.assertNotIn("x", rect)

    def test_non_dict_payload_falls_back(self):
        self.write_raw("[1, 2, 3]")
        self.assertEqual(win_geometry.load(self.folder)["width"],
                         win_geometry.DEFAULT_SIZE[0])

    def test_size_is_clamped_to_minimum(self):
        self.write_raw(json.dumps({"width": 100, "height": 100}))
        rect = win_geometry.load(self.folder)
        self.assertEqual((rect["width"], rect["height"]),
                         win_geometry.MIN_SIZE)

    def test_absurd_size_is_capped(self):
        self.write_raw(json.dumps({"width": 999999, "height": 999999}))
        rect = win_geometry.load(self.folder)
        self.assertEqual((rect["width"], rect["height"]),
                         (win_geometry.MAX_SIDE, win_geometry.MAX_SIDE))

    def test_bad_types_are_ignored(self):
        self.write_raw(json.dumps({"width": "1200", "height": True,
                                   "x": "left", "y": None,
                                   "maximized": "yes"}))
        rect = win_geometry.load(self.folder)
        self.assertEqual(rect["width"], win_geometry.DEFAULT_SIZE[0])
        self.assertEqual(rect["height"], win_geometry.DEFAULT_SIZE[1])
        self.assertNotIn("x", rect)
        self.assertNotIn("y", rect)
        # 非布尔的真值不当成最大化，否则会「启动就最大化」
        self.assertFalse(rect["maximized"])

    def test_coordinate_out_of_range_is_dropped(self):
        self.write_raw(json.dumps({"width": 1200, "height": 700,
                                   "x": 999999, "y": 5}))
        rect = win_geometry.load(self.folder)
        self.assertNotIn("x", rect)
        self.assertNotIn("y", rect)

    def test_save_creates_folder(self):
        nested = os.path.join(self.folder, "a", "b")
        self.assertTrue(win_geometry.save(nested, {"width": 1200,
                                                   "height": 700}))
        self.assertTrue(os.path.isfile(win_geometry.store_path(nested)))


class TrackerTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="wbgeom-")
        self.tracker = win_geometry.Tracker(self.folder,
                                           {"x": 100, "y": 100,
                                            "width": 1200, "height": 700},
                                           delay=0.05)

    def tearDown(self):
        self.tracker.flush()
        shutil.rmtree(self.folder, ignore_errors=True)

    def stored(self):
        return win_geometry.load(self.folder)

    def test_move_and_resize_are_persisted(self):
        self.tracker.on_moved(300, 220)
        self.tracker.on_resized(1500, 950)
        self.tracker.flush()
        rect = self.stored()
        self.assertEqual((rect["x"], rect["y"]), (300, 220))
        self.assertEqual((rect["width"], rect["height"]), (1500, 950))

    def test_delayed_write_happens_without_flush(self):
        self.tracker.on_moved(400, 300)
        time.sleep(0.4)          # 超过 delay，节流计时器应已落盘
        rect = self.stored()
        self.assertEqual((rect["x"], rect["y"]), (400, 300))

    def test_maximized_size_is_ignored(self):
        self.tracker.on_maximized()
        self.tracker.on_resized(2560, 1440)     # 最大化后的屏幕尺寸
        self.tracker.on_moved(-8, -8)           # 最大化窗口的左上角
        self.tracker.flush()

        rect = self.stored()
        self.assertTrue(rect["maximized"])
        # 还原后要回到的仍然是用户原来的矩形
        self.assertEqual((rect["width"], rect["height"]), (1200, 700))
        self.assertEqual((rect["x"], rect["y"]), (100, 100))

    def test_restore_resumes_tracking(self):
        self.tracker.on_maximized()
        self.tracker.on_restored()
        self.tracker.on_resized(1100, 680)
        self.tracker.flush()
        rect = self.stored()
        self.assertFalse(rect["maximized"])
        self.assertEqual((rect["width"], rect["height"]), (1100, 680))

    def test_minimized_events_are_ignored(self):
        self.tracker.on_minimized()
        self.tracker.on_resized(160, 28)        # 最小化后的任务栏尺寸
        self.tracker.flush()
        rect = self.stored()
        self.assertEqual((rect["width"], rect["height"]), (1200, 700))

    def test_too_small_size_is_ignored(self):
        self.tracker.on_resized(200, 120)       # 小于 min_size
        self.tracker.flush()
        self.assertEqual(self.stored()["width"], 1200)

    def test_restored_position_flag(self):
        self.assertTrue(self.tracker.restored_position)
        fresh = win_geometry.Tracker(self.folder, {"width": 1200,
                                                   "height": 700})
        self.assertFalse(fresh.restored_position)

    def test_sync_from_window_overrides_rect(self):
        class FakeWindow:
            x = 640
            y = 320
            width = 1280
            height = 760

        self.tracker.sync_from(FakeWindow())
        rect = self.stored()
        self.assertEqual((rect["x"], rect["y"]), (640, 320))
        self.assertEqual((rect["width"], rect["height"]), (1280, 760))


class BindTest(unittest.TestCase):
    """锁住「事件名」这一层契约。

    pywebview 升级后事件名可能变（例如 moved 变成 move），而绑定失败会被
    app.py 的 try 吞掉——只剩「窗口位置记不住了」这个现象。这里逐个断言
    六个事件都真的挂上了回调。
    """

    EVENTS = ("moved", "resized", "maximized", "restored", "minimized",
              "closing")

    def test_all_events_bound(self):
        class FakeEvent:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        class FakeEvents:
            def __init__(self):
                for name in BindTest.EVENTS:
                    setattr(self, name, FakeEvent())

        class FakeWindow:
            def __init__(self):
                self.events = FakeEvents()

        folder = tempfile.mkdtemp(prefix="wbgeom-")
        try:
            tracker = win_geometry.Tracker(folder, {"width": 1200,
                                                    "height": 700})
            window = FakeWindow()
            tracker.bind(window)

            for name in self.EVENTS:
                handlers = getattr(window.events, name).handlers
                self.assertEqual(len(handlers), 1, name)
            # 关闭事件必须接 flush，否则最后一次拖动可能丢
            self.assertEqual(window.events.closing.handlers[0], tracker.flush)
            self.assertEqual(window.events.moved.handlers[0], tracker.on_moved)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class VisibilityTest(unittest.TestCase):

    def test_no_handle_is_skipped(self):
        self.assertEqual(win_geometry.ensure_visible(None), "skipped")
        self.assertEqual(win_geometry.ensure_visible(0), "skipped")

    def test_center_without_handle_returns_false(self):
        self.assertFalse(win_geometry.center_on_primary(None))
        self.assertFalse(win_geometry.center_on_primary(0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
