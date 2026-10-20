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
import tempfile
import time
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

    def test_null_window_state_is_all_false(self):
        self.assertEqual(win_guard.window_state(None), (False,) * 5)

    def test_release_none_is_safe(self):
        win_guard.release_single_instance(None)


class WatchdogHeartbeatTest(unittest.TestCase):
    """心跳阈值与休眠判定的纯逻辑，不需要真实窗口。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb_watchdog_test_")
        self.watchdog = win_guard.UIWatchdog(
            os.path.join(self.tmp, "watchdog.log"),
            heartbeat_stale_s=20,
        )

    def test_heartbeat_threshold_and_sleep_gap(self):
        # 一次心跳都没收到：不参与判定，避免启动阶段误杀
        self.assertFalse(self.watchdog._heartbeat_stale(False))

        self.watchdog.beat()
        self.assertFalse(self.watchdog._heartbeat_stale(False))

        # 超过阈值：判定停滞
        self.watchdog._last_beat = time.monotonic() - 30
        self.assertTrue(self.watchdog._heartbeat_stale(False))

        # 远大于阈值：按系统休眠处理，重新计时而不是退出
        self.watchdog._last_beat = time.monotonic() - 300
        self.assertFalse(self.watchdog._heartbeat_stale(False))

        # 最小化窗口不做心跳判定
        self.watchdog._last_beat = time.monotonic() - 30
        self.assertFalse(self.watchdog._heartbeat_stale(True))


@unittest.skipUnless(os.name == "nt", "仅 Windows 可抓 dump")
class MinidumpTest(unittest.TestCase):

    def test_minidump_contains_thread_data(self):
        folder = tempfile.mkdtemp(prefix="wb_dump_test_")
        path = os.path.join(folder, "hang.dmp")
        self.assertTrue(win_guard.write_minidump(path))
        # 只有线程栈的 dump 也有几十 KB；几 KB 说明没抓到线程信息
        self.assertGreater(os.path.getsize(path), 4096)


if __name__ == "__main__":
    unittest.main(verbosity=2)
