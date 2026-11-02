"""schedule_jitter 的回归测试。

随机时间的算法不能靠「跑一次看看」验证，这里把四层熵源换成固定值，
检查：

  - 偏移永远落在窗口内，正向模式不会出现负数
  - 同一轮里不同触发点拿到不同偏移（不是一串雷同值）
  - 重排只改「今天已过去」的点，凌晨跑完不能把白天的点一起换掉
  - 跨午夜的组按列表顺序认日期，00:30 属于明天而不是今天已过
"""

import datetime
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.dirname(HERE)
sys.path.insert(0, DASHBOARD)

import schedule_jitter  # noqa: E402


def fixed_sources(clock_ns=123456789, boot_minute=98765):
    """把时钟与 uptime 钉死，让结果可复现。"""
    return mock.patch.multiple(
        schedule_jitter,
        time=mock.Mock(time_ns=lambda: clock_ns * 1_000_000_000 + clock_ns,
                       monotonic=lambda: 1.0),
        _boot_millis=lambda: boot_minute * 60_000,
    )


class ParseTest(unittest.TestCase):

    def test_parse_accepts_hh_mm_and_seconds(self):
        self.assertEqual(schedule_jitter.parse_hhmm("00:05"), 5)
        self.assertEqual(schedule_jitter.parse_hhmm("23:59:58"), 23 * 60 + 59)

    def test_parse_rejects_garbage(self):
        for raw in ("", "abc", "25:00", "12:60", None, "12"):
            self.assertIsNone(schedule_jitter.parse_hhmm(raw))

    def test_format_wraps_across_midnight(self):
        self.assertEqual(schedule_jitter.format_minutes(-5), "23:55")
        self.assertEqual(schedule_jitter.format_minutes(1440 + 7), "00:07")


class OffsetTest(unittest.TestCase):

    def test_offset_stays_inside_window(self):
        for index in range(200):
            offset = schedule_jitter.offset_seconds("task#%d" % index, "2026-11-02")
            self.assertGreaterEqual(offset, -schedule_jitter.JITTER_WINDOW)
            self.assertLessEqual(offset, schedule_jitter.JITTER_WINDOW)

    def test_offset_is_never_zero(self):
        """偏移 0 等于没打散；一年几百次抽样里撞上一次就够显眼。"""
        for index in range(500):
            self.assertNotEqual(
                schedule_jitter.offset_seconds("task#%d" % index, "2026-11-02"), 0)
            self.assertGreaterEqual(
                schedule_jitter.offset_seconds("task#%d" % index, "2026-11-02",
                                               forward=True), 60)

    def test_forward_offset_is_never_negative(self):
        for index in range(200):
            offset = schedule_jitter.offset_seconds(
                "task#%d" % index, "2026-11-02", forward=True)
            self.assertGreaterEqual(offset, 0)
            self.assertLessEqual(offset, schedule_jitter.JITTER_WINDOW)

    def test_offsets_differ_between_points_and_days(self):
        """同一天不同点、同一点不同天，都不该拿到同一个偏移。"""
        same_day = {schedule_jitter.offset_seconds("task#%d" % i, "2026-11-02")
                    for i in range(12)}
        self.assertGreater(len(same_day), 6)
        across_days = {schedule_jitter.offset_seconds("task#0", "2026-11-%02d" % d)
                       for d in range(1, 15)}
        self.assertGreater(len(across_days), 9)

    def test_same_entropy_is_reproducible(self):
        """熵源固定时结果必须一致，否则重排会变成每次运行都改一次。"""
        with fixed_sources():
            first = schedule_jitter.offset_seconds("task#0", "2026-11-02")
        with fixed_sources():
            second = schedule_jitter.offset_seconds("task#0", "2026-11-02")
        self.assertEqual(first, second)


class JitterTimesTest(unittest.TestCase):

    def test_every_point_shifts_within_window(self):
        base = ["01:00", "05:00", "09:00", "13:00", "17:00", "21:00"]
        times = schedule_jitter.jitter_times(base, "WorkBuddyGrowthPoll")
        self.assertEqual(len(times), len(base))
        for raw, shifted in zip(base, times):
            before = schedule_jitter.parse_hhmm(raw)
            after = schedule_jitter.parse_hhmm(shifted)
            self.assertLessEqual(abs(after - before),
                                 schedule_jitter.JITTER_WINDOW // 60)

    def test_invalid_point_passes_through(self):
        self.assertEqual(schedule_jitter.jitter_times(["oops"], "x"), ["oops"])

    def test_forward_never_moves_before_base(self):
        base = ["10:05", "11:05", "15:05", "19:05", "23:05"]
        times = schedule_jitter.jitter_times(base, "QoderDailyCredits",
                                             forward=True)
        for raw, shifted in zip(base, times):
            self.assertGreaterEqual(schedule_jitter.parse_hhmm(shifted),
                                    schedule_jitter.parse_hhmm(raw))


class ReshuffleTest(unittest.TestCase):

    BASE = ["00:52", "04:47", "08:58", "13:12", "16:50", "21:07"]

    def test_keeps_points_that_are_still_ahead(self):
        now = datetime.datetime(2026, 11, 2, 1, 10)
        times = schedule_jitter.reshuffle(self.BASE, "WorkBuddyGrowthPoll",
                                          now=now)
        # 第一个点已经跑过，换掉；后面 5 个原样保留
        self.assertNotEqual(times[0], self.BASE[0])
        self.assertEqual(times[1:], self.BASE[1:])

    def test_all_points_rotate_after_last_run(self):
        now = datetime.datetime(2026, 11, 2, 22, 30)
        times = schedule_jitter.reshuffle(self.BASE, "WorkBuddyGrowthPoll",
                                          now=now)
        for old, new in zip(self.BASE, times):
            self.assertNotEqual(old, new)

    def test_minute_after_midnight_counts_as_expired(self):
        now = datetime.datetime(2026, 11, 2, 0, 1)
        times = schedule_jitter.reshuffle(["00:00", "06:00"], "X", now=now)
        self.assertEqual(len(times), 2)
        self.assertNotEqual(times[0], "00:00")
        self.assertEqual(times[1], "06:00")

    def test_wrapped_schedule_keeps_next_day_points(self):
        """22:30 开头的组里，00:30 是明天凌晨，不是今天已过。"""
        base = ["22:30", "00:30", "03:30", "07:30", "11:30"]
        now = datetime.datetime(2026, 11, 2, 23, 0)
        times = schedule_jitter.reshuffle(base, "TraeDailyCheckin", now=now)
        self.assertNotEqual(times[0], base[0])   # 22:30 已经跑过
        self.assertEqual(times[1:], base[1:])    # 其余都还没到

    def test_reshuffled_points_stay_apart(self):
        now = datetime.datetime(2026, 11, 2, 23, 0)
        times = schedule_jitter.reshuffle(self.BASE, "WorkBuddyGrowthPoll",
                                          now=now)
        minutes = sorted(schedule_jitter.parse_hhmm(t) for t in times)
        for left, right in zip(minutes, minutes[1:]):
            self.assertGreaterEqual(right - left,
                                    schedule_jitter.MIN_GAP // 60)

    def test_invalid_point_survives_reshuffle(self):
        now = datetime.datetime(2026, 11, 2, 23, 0)
        times = schedule_jitter.reshuffle(["oops", "08:00"], "X", now=now)
        self.assertEqual(times[0], "oops")
        self.assertEqual(len(times), 2)

    def test_base_anchors_instead_of_current_value(self):
        """当前值已经漂到 00:50，重抽仍要回到 00:05 附近，不能顺势往外走。"""
        for _ in range(30):
            times = schedule_jitter.reshuffle(
                ["00:50"], "WorkBuddyAutoSignin", base=["00:05"],
                now=datetime.datetime(2026, 11, 3, 0, 52), forward=True)
            minute = schedule_jitter.parse_hhmm(times[0])
            self.assertGreaterEqual(minute, 6)
            self.assertLessEqual(minute, 20)

    def test_daily_reshuffle_does_not_drift(self):
        """连续 40 天重排，时刻必须始终落在基准窗口内（曾经会滚雪球）。"""
        times = ["00:05"]
        now = datetime.datetime(2026, 11, 3, 0, 10)
        for _ in range(40):
            times = schedule_jitter.reshuffle(
                times, "WorkBuddyAutoSignin", base=["00:05"], now=now,
                forward=True)
            minute = schedule_jitter.parse_hhmm(times[0])
            self.assertGreaterEqual(minute, 6)
            self.assertLessEqual(minute, 20)
            now += datetime.timedelta(days=1)

    def test_pairing_handles_sorted_cross_midnight(self):
        """任务读回来的时刻是排过序的，配对不能按索引硬套。"""
        base = ["22:30", "00:30", "03:30"]
        current = ["00:30", "03:30", "22:30"]
        now = datetime.datetime(2026, 11, 2, 23, 0)
        times = schedule_jitter.reshuffle(current, "TraeDailyCheckin",
                                          base=base, now=now, forward=True)
        # 22:30 已经跑过（换掉），00:30 / 03:30 属于明天（保留）
        self.assertNotEqual(times[0], "22:30")
        self.assertEqual(times[1], "00:30")
        self.assertEqual(times[2], "03:30")


class DescribeTest(unittest.TestCase):

    def test_describe_mentions_window(self):
        self.assertIn("15", schedule_jitter.describe())
        self.assertIn("1~15", schedule_jitter.describe(forward=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
