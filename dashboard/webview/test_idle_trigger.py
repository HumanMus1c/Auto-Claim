"""idle_trigger 的回归测试。

空闲补跑的关键不在「触发器怎么建」，而在「什么时候该跳过」。这里把
``idle_seconds`` 换成固定值，检查闸门的几条边界：

  - 不是空闲触发（人在机器前）→ 永远不跳过
  - 空闲触发、最近没跑过 → 照常执行（这是兜底的意义）
  - 空闲触发、刚成功跑过 → 跳过，并留下本进程标记（跳过时不重排时刻）
  - 失败不进冷却：自动重试要能立刻再过一次闸门
  - 冷却必须短于最密的定时间隔，否则会吃掉正常的定时执行

另外验证状态文件写坏了也只是「不跳过」，不能把签到挡死。
"""

import datetime
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.dirname(HERE)
sys.path.insert(0, DASHBOARD)

import idle_trigger  # noqa: E402


class StateFileTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb_idle_test_")
        idle_trigger.reset_skipped()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        idle_trigger.reset_skipped()

    def test_record_then_read(self):
        now = datetime.datetime(2026, 11, 5, 9, 30)
        idle_trigger.record("workbuddy", True, self.tmp, now=now)

        state = idle_trigger.read_state("workbuddy", self.tmp)
        self.assertTrue(state["ok"])
        self.assertEqual(state["kind"], "workbuddy")
        self.assertEqual(state["at"], now.timestamp())
        self.assertEqual(state["at_text"], "2026-11-05 09:30:00")

    def test_missing_file_is_empty(self):
        self.assertEqual(idle_trigger.read_state("qoder", self.tmp), {})

    def test_corrupt_file_does_not_raise(self):
        path = idle_trigger.state_path("trae", self.tmp)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(idle_trigger.read_state("trae", self.tmp), {})

    def test_kinds_are_stored_separately(self):
        now = datetime.datetime(2026, 11, 5, 9, 30)
        idle_trigger.record("workbuddy", True, self.tmp, now=now)
        self.assertTrue(idle_trigger.read_state("workbuddy", self.tmp)["ok"])
        self.assertEqual(idle_trigger.read_state("trae", self.tmp), {})

    def test_record_overwrites_previous_result(self):
        now = datetime.datetime(2026, 11, 5, 9, 30)
        idle_trigger.record("qoder", True, self.tmp, now=now)
        idle_trigger.record("qoder", False, self.tmp, now=now)
        self.assertFalse(idle_trigger.read_state("qoder", self.tmp)["ok"])


class SkipTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb_idle_skip_")
        self.now = datetime.datetime(2026, 11, 5, 10, 0)
        idle_trigger.reset_skipped()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        idle_trigger.reset_skipped()

    def _skip(self, idle, now=None):
        with mock.patch.object(idle_trigger, "idle_seconds", return_value=idle):
            return idle_trigger.should_skip("workbuddy", self.tmp,
                                            now=now or self.now)

    def test_active_user_never_skips(self):
        idle_trigger.record("workbuddy", True, self.tmp, now=self.now)
        self.assertFalse(self._skip(10))
        self.assertFalse(idle_trigger.skipped("workbuddy"))

    def test_idle_without_history_runs(self):
        self.assertFalse(self._skip(600))

    def test_idle_right_after_a_run_skips(self):
        idle_trigger.record("workbuddy", True, self.tmp, now=self.now)
        self.assertTrue(self._skip(600))
        # 跳过标记要给 app.py / 脚本的收尾逻辑看：跳过时不重排时刻
        self.assertTrue(idle_trigger.skipped("workbuddy"))
        self.assertFalse(idle_trigger.skipped("qoder"))

    def test_idle_after_cooldown_runs(self):
        earlier = self.now - datetime.timedelta(
            seconds=idle_trigger.COOLDOWN_SECONDS + 30)
        idle_trigger.record("workbuddy", True, self.tmp, now=earlier)
        self.assertFalse(self._skip(600))

    def test_failure_stays_retryable(self):
        """失败不进冷却：控制台的自动重试要能立刻再过一次闸门。"""
        earlier = self.now - datetime.timedelta(minutes=1)
        idle_trigger.record("workbuddy", False, self.tmp, now=earlier)
        self.assertFalse(self._skip(600))

    def test_cooldown_is_shorter_than_tightest_schedule(self):
        """Trae / Qoder 相邻两点最密 45 分钟，冷却必须更短。"""
        self.assertLess(idle_trigger.COOLDOWN_SECONDS, 45 * 60)

    def test_idle_threshold_matches_trigger_setting(self):
        """检测门槛要低于注册的空闲时长，否则真被触发时反而认不出来。"""
        self.assertLess(idle_trigger.DETECT_SECONDS,
                        idle_trigger.IDLE_MINUTES * 60)


class DescribeTest(unittest.TestCase):

    def test_describe_mentions_minutes(self):
        text = idle_trigger.describe()
        self.assertIn(str(idle_trigger.IDLE_MINUTES), text)
        self.assertIn(str(idle_trigger.COOLDOWN_SECONDS // 60), text)


class LogTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb_idle_log_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_log_appends_to_run_log(self):
        idle_trigger.log("测试跳过", self.tmp)
        with open(os.path.join(self.tmp, "run.log"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("[idle] 测试跳过", text)

    def test_log_never_raises(self):
        # 指向一个必然写不进去的路径（父级是文件）
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        idle_trigger.log("x", os.path.join(blocker, "sub"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
