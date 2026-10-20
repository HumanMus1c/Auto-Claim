"""win_guard 的回归测试。

只测不依赖真实 GUI 的 Windows 路径：
  - 命名互斥量确实能拦住第二个实例
  - 找不到目标进程时激活函数安全地返回 missing
  - 空窗口句柄不会被误判为响应

看门狗本身会 os._exit，不能直接在测试进程里跑完整流程；
它的窗口查找/探测函数已经在这些用例里覆盖。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.dirname(HERE)
sys.path.insert(0, DASHBOARD)

import win_guard  # noqa: E402


@unittest.skipUnless(os.name == "nt", "仅 Windows 可测")
class SingleInstanceTest(unittest.TestCase):

    def test_mutex_blocks_second_instance(self):
        name = r"Local\WorkBuddyDashboard.Test.%d" % os.getpid()
        first, already = win_guard.acquire_single_instance(name)
        self.assertTrue(first, "第一次创建互斥量应成功")
        self.assertFalse(already)

        second, already = win_guard.acquire_single_instance(name)
        try:
            self.assertIsNone(second, "第二个实例不应拿到新句柄")
            self.assertTrue(already, "第二个实例应看到 ERROR_ALREADY_EXISTS")
        finally:
            win_guard.release_single_instance(first)
            if second:
                win_guard.release_single_instance(second)

    def test_missing_process_is_safe(self):
        self.assertIsNone(
            win_guard.find_window_by_process("__no_such_process__.exe"))
        self.assertEqual(
            win_guard.activate_existing_window(
                "__no_such_process__.exe", retry_s=0),
            "missing")


class ResponsivenessTest(unittest.TestCase):

    def test_null_window_is_not_responsive(self):
        self.assertFalse(win_guard.is_window_responsive(None))

    def test_release_none_is_safe(self):
        win_guard.release_single_instance(None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
