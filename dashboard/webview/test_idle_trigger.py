"""idle_trigger 的回归测试。

两块逻辑都不靠「跑一次看看」：

  - 闸门：普通空闲触发最近成功过就跳过；计划点在人还在时必执行、人回来了就跳过
  - 睡眠窗口：按 powercfg 的睡眠/休眠倒计时算截止，在窗口内随机排点，
    最后一个点必须离截止还有 margin

powercfg 的输出按中英文两种样本测试，只认 GUID 与 0x 数值，不依赖本地化标签。
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


def powercfg_sample(ac_sleep="0x00000e10", dc_sleep="0x00000000",
                    ac_hibernate="0x00001c20", dc_hibernate="0x00000708",
                    chinese=True):
    """拼一段和 `powercfg /query SCHEME_CURRENT SUB_SLEEP` 同构的输出。"""
    if chinese:
        head = "电源方案 GUID: 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c  (高性能)"
        sub = "子组 GUID: 238c9fa8-0aad-41ed-83f4-97be242c8f20  (睡眠)"
        label = "电源设置 GUID"
        index = "当前交流电源设置索引"
        index_dc = "当前直流电源设置索引"
    else:
        head = "Power Scheme GUID: 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c  (High performance)"
        sub = "Subgroup GUID: 238c9fa8-0aad-41ed-83f4-97be242c8f20  (Sleep)"
        label = "Power Setting GUID"
        index = "Current AC Power Setting Index"
        index_dc = "Current DC Power Setting Index"
    return "\n".join([
        head,
        "  GUID Alias: SCHEME_MIN",
        sub,
        "    GUID Alias: SUB_SLEEP",
        "    %s: 29f6c1db-86da-48c5-9fdb-f2b67b1f44da  (Sleep after)" % label,
        "      GUID Alias: STANDBYIDLE",
        "      Minimum Possible Setting: 0x00000000",
        "      Maximum Possible Setting: 0xffffffff",
        "      Possible Setting Increment: 0x00000001",
        "    %s: %s" % (index, ac_sleep),
        "    %s: %s" % (index_dc, dc_sleep),
        "    %s: 9d7815a6-7ee4-497e-8888-515a05f02364  (Hibernate after)" % label,
        "      GUID Alias: HIBERNATEIDLE",
        "      Minimum Possible Setting: 0x00000000",
        "      Maximum Possible Setting: 0xffffffff",
        "      Possible Setting Increment: 0x00000001",
        "    %s: %s" % (index, ac_hibernate),
        "    %s: %s" % (index_dc, dc_hibernate),
        "    %s: 94ac6d29-73ce-41a6-809f-6363ba21b47e  (Hybrid sleep)" % label,
        "      Possible Setting Index: 000",
        "      Possible Setting Index: 001",
    ])


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

    def test_active_user_without_history_runs(self):
        """登录 / 解锁兜底：没有成功记录时照常执行。"""
        self.assertFalse(self._skip(10))
        self.assertFalse(idle_trigger.skipped("workbuddy"))

    def test_unlock_right_after_a_run_skips(self):
        """解锁很频繁：30 分钟内成功过就不能每次解锁都重跑。"""
        idle_trigger.record("workbuddy", True, self.tmp, now=self.now)
        self.assertTrue(self._skip(10))
        self.assertTrue(idle_trigger.skipped("workbuddy"))

    def test_unlock_after_cooldown_runs(self):
        earlier = self.now - datetime.timedelta(
            seconds=idle_trigger.COOLDOWN_SECONDS + 30)
        idle_trigger.record("workbuddy", True, self.tmp, now=earlier)
        self.assertFalse(self._skip(10))

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


class PlanStateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb_idle_plan_")
        self.now_ts = datetime.datetime(2026, 11, 5, 21, 0).timestamp()
        idle_trigger.reset_skipped()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        idle_trigger.reset_skipped()

    def test_round_trip_and_hit(self):
        points = [self.now_ts + 600, self.now_ts + 1800]
        idle_trigger.write_plan("workbuddy", points, self.now_ts - 300,
                                self.now_ts + 3300, self.tmp)

        plan = idle_trigger.read_plan("workbuddy", self.tmp)
        self.assertEqual(len(plan["points"]), 2)
        at = datetime.datetime.fromtimestamp(self.now_ts + 601)
        self.assertTrue(idle_trigger.in_plan("workbuddy", at, self.tmp))

    def test_point_outside_tolerance_is_not_plan(self):
        points = [self.now_ts + 600]
        idle_trigger.write_plan("workbuddy", points, self.now_ts - 300,
                                self.now_ts + 3300, self.tmp)
        at = datetime.datetime.fromtimestamp(
            self.now_ts + 600 + idle_trigger.PLAN_TOLERANCE + 20)
        self.assertFalse(idle_trigger.in_plan("workbuddy", at, self.tmp))

    def test_expired_plan_is_ignored(self):
        # 截止点过去一小时以上，计划已经没有意义
        points = [self.now_ts - 7000]
        idle_trigger.write_plan("workbuddy", points, self.now_ts - 9000,
                                self.now_ts - 7800, self.tmp)
        at = datetime.datetime.fromtimestamp(self.now_ts)
        self.assertFalse(idle_trigger.in_plan("workbuddy", at, self.tmp))

    def test_clear_plan_removes_file(self):
        idle_trigger.write_plan("trae", [self.now_ts + 60], self.now_ts,
                                self.now_ts + 600, self.tmp)
        idle_trigger.clear_plan("trae", self.tmp)
        self.assertEqual(idle_trigger.read_plan("trae", self.tmp), {})

    def test_planned_point_runs_even_after_success(self):
        """排好的 3 次之一：人还在空闲就一定执行，不被 30 分钟冷却挡掉。"""
        idle_trigger.record("workbuddy", True, self.tmp,
                            now=datetime.datetime(2026, 11, 5, 20, 59))
        idle_trigger.write_plan("workbuddy", [self.now_ts],
                                self.now_ts - 1200, self.now_ts + 3000,
                                self.tmp)
        with mock.patch.object(idle_trigger, "idle_seconds", return_value=900):
            self.assertFalse(idle_trigger.should_skip(
                "workbuddy", self.tmp,
                now=datetime.datetime.fromtimestamp(self.now_ts)))

    def test_planned_point_skips_when_user_is_back(self):
        idle_trigger.write_plan("workbuddy", [self.now_ts],
                                self.now_ts - 1200, self.now_ts + 3000,
                                self.tmp)
        with mock.patch.object(idle_trigger, "idle_seconds", return_value=5):
            self.assertTrue(idle_trigger.should_skip(
                "workbuddy", self.tmp,
                now=datetime.datetime.fromtimestamp(self.now_ts)))
        self.assertTrue(idle_trigger.skipped("workbuddy"))


class PowerParseTest(unittest.TestCase):

    def test_chinese_output(self):
        settings = idle_trigger.parse_power_settings(powercfg_sample())
        self.assertEqual(settings[idle_trigger.SLEEP_SETTING_GUID],
                         (3600, 0))
        self.assertEqual(settings[idle_trigger.HIBERNATE_SETTING_GUID],
                         (7200, 1800))

    def test_english_output(self):
        settings = idle_trigger.parse_power_settings(
            powercfg_sample(chinese=False, ac_sleep="0x00000708",
                            dc_sleep="0x0000012c"))
        self.assertEqual(settings[idle_trigger.SLEEP_SETTING_GUID],
                         (1800, 300))

    def test_garbage_returns_empty(self):
        self.assertEqual(idle_trigger.parse_power_settings(""), {})
        self.assertEqual(idle_trigger.parse_power_settings("GUID: nope"), {})

    def test_sleep_timeout_picks_first_nonzero(self):
        text = powercfg_sample(ac_sleep="0x00000000",
                               dc_sleep="0x00000000",
                               ac_hibernate="0x00000e10",
                               dc_hibernate="0x00000708")
        proc = mock.Mock(stdout=text.encode("utf-8"))
        with mock.patch.object(idle_trigger.subprocess, "run",
                               return_value=proc), \
                mock.patch.object(idle_trigger, "on_battery", return_value=False):
            self.assertEqual(idle_trigger.sleep_timeout_seconds(), 3600)
        with mock.patch.object(idle_trigger.subprocess, "run",
                               return_value=proc), \
                mock.patch.object(idle_trigger, "on_battery", return_value=True):
            self.assertEqual(idle_trigger.sleep_timeout_seconds(), 1800)

    def test_sleep_timeout_never_returns_zero(self):
        text = powercfg_sample(ac_sleep="0x00000000", dc_sleep="0x00000000",
                               ac_hibernate="0x00000000",
                               dc_hibernate="0x00000000")
        proc = mock.Mock(stdout=text.encode("utf-8"))
        with mock.patch.object(idle_trigger.subprocess, "run",
                               return_value=proc):
            self.assertEqual(idle_trigger.sleep_timeout_seconds(), 0)


class PickPointsTest(unittest.TestCase):

    NOW = 1_800_000_000.0

    def test_points_stay_inside_the_safe_window(self):
        deadline = self.NOW + 3600
        for _ in range(50):
            points = idle_trigger.pick_points(deadline, self.NOW)
            self.assertEqual(len(points), idle_trigger.PLAN_COUNT)
            self.assertEqual(points, sorted(points))
            self.assertGreaterEqual(points[0],
                                    self.NOW + idle_trigger.PLAN_EARLIEST)
            # 最后一个点绝不能碰到睡眠截止，更不能越过
            self.assertLessEqual(points[-1],
                                 deadline - idle_trigger.PLAN_MARGIN)
            for left, right in zip(points, points[1:]):
                self.assertGreaterEqual(right - left,
                                        idle_trigger.PLAN_MIN_GAP)

    def test_short_window_keeps_fewer_points(self):
        # 只剩 12 分钟：留 4 分钟起步、8 分钟余量后一个点都放不下
        points = idle_trigger.pick_points(self.NOW + 12 * 60, self.NOW)
        self.assertEqual(points, [])
        # 15 分钟窗口：可用 3 分钟，只放得下 1 个
        points = idle_trigger.pick_points(self.NOW + 15 * 60, self.NOW)
        self.assertEqual(len(points), 1)

    def test_points_are_random(self):
        """同一窗口连续抽样的落点不能每次都一样。"""
        deadline = self.NOW + 3600
        samples = {tuple(idle_trigger.pick_points(deadline, self.NOW))
                   for _ in range(8)}
        self.assertGreater(len(samples), 1)

    def test_deadline_before_start_returns_empty(self):
        self.assertEqual(idle_trigger.pick_points(self.NOW + 60, self.NOW), [])


class DescribeTest(unittest.TestCase):

    def test_describe_mentions_minutes(self):
        text = idle_trigger.describe()
        self.assertIn(str(idle_trigger.IDLE_MINUTES), text)
        self.assertIn(str(idle_trigger.PLAN_COUNT), text)


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
