"""win_service 的纯函数回归测试。

不实际注册/删除计划任务，只验证：

  - Windows 命令行与 PowerShell 字符串的转义
  - PowerShell 原始状态到界面结构的整理
  - 部分安装、停用、读取失败等边界

真正的计划任务操作由 Windows 实机验证，避免测试修改用户系统状态。
"""

import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.dirname(HERE)
sys.path.insert(0, DASHBOARD)

import win_service  # noqa: E402


class QuoteTest(unittest.TestCase):

    def test_quote_arg_with_spaces(self):
        self.assertEqual(
            win_service._quote_arg(r"C:\Program Files\WorkBuddy\app.exe"),
            r'"C:\Program Files\WorkBuddy\app.exe"')

    def test_quote_arg_without_spaces(self):
        self.assertEqual(win_service._quote_arg(r"C:\WorkBuddy\app.exe"),
                         r"C:\WorkBuddy\app.exe")

    def test_ps_quote_escapes_single_quote(self):
        self.assertEqual(win_service._ps_quote("a'b"), "'a''b'")


class NormalizeStatusTest(unittest.TestCase):

    def test_full_status(self):
        raw = {
            "ok": True,
            "tasks": [
                {"name": "WorkBuddyAutoSignin", "installed": True,
                 "state": "Ready", "last_run": "2026-10-19 00:05:00",
                 "next_run": "2026-10-21 00:05:00", "last_result": 0,
                 "action": "a"},
                {"name": "WorkBuddyGrowthPoll", "installed": True,
                 "state": "Running", "last_run": "2026-10-20 09:00:00",
                 "next_run": "2026-10-20 13:00:00", "last_result": 267009,
                 "action": "b"},
            ],
        }
        status = win_service._normalize_status(raw)

        self.assertTrue(status["installed"])
        self.assertTrue(status["enabled"])
        self.assertTrue(status["running"])
        self.assertEqual(status["installed_count"], 2)
        self.assertEqual(status["next_run"], "2026-10-20 13:00:00")
        self.assertEqual(status["last_run"], "2026-10-20 09:00:00")
        self.assertEqual(status["tasks"][0]["label"], "每日签到")
        self.assertEqual(status["tasks"][1]["label"], "轮询补签")

    def test_partial_install_keeps_both_cards(self):
        raw = {
            "ok": True,
            "tasks": [
                {"name": "WorkBuddyAutoSignin", "installed": True,
                 "state": "Ready", "last_result": 267011},
            ],
        }
        status = win_service._normalize_status(raw)

        self.assertFalse(status["installed"])
        self.assertEqual(status["installed_count"], 1)
        self.assertEqual(len(status["tasks"]), 2)
        self.assertFalse(status["tasks"][1]["installed"])

    def test_disabled_task_hides_next_run(self):
        raw = {
            "ok": True,
            "tasks": [
                {"name": "WorkBuddyAutoSignin", "installed": True,
                 "state": "Disabled", "next_run": "2026-10-21 00:05:00",
                 "last_result": 267011},
                {"name": "WorkBuddyGrowthPoll", "installed": True,
                 "state": "Disabled", "next_run": "2026-10-21 01:00:00",
                 "last_result": 267011},
            ],
        }
        status = win_service._normalize_status(raw)

        self.assertTrue(status["installed"])
        self.assertFalse(status["enabled"])
        self.assertFalse(status["running"])
        self.assertIsNone(status["next_run"])
        self.assertIsNone(status["tasks"][0]["next_run"])
        self.assertFalse(status["tasks"][0]["enabled"])

    def test_error_is_raised(self):
        with self.assertRaises(RuntimeError):
            win_service._normalize_status({"ok": False, "error": "拒绝访问"})


class QoderTaskTest(unittest.TestCase):

    def test_qoder_task_is_independent(self):
        self.assertEqual(win_service.QODER_TASK_NAMES, ("QoderDailyCredits",))
        self.assertNotIn("QoderDailyCredits", win_service.TASK_NAMES)

    def test_qoder_status_normalization(self):
        raw = {
            "ok": True,
            "tasks": [
                {"name": "QoderDailyCredits", "installed": True,
                 "state": "Ready", "last_run": "2026-10-20 10:05:00",
                 "next_run": "2026-10-20 23:05:00", "last_result": 0,
                 "action": "pythonw qoder_signin.py silent"},
            ],
        }
        status = win_service._normalize_status(
            raw, specs=win_service.QODER_TASKS)

        self.assertTrue(status["installed"])
        self.assertTrue(status["enabled"])
        self.assertEqual(status["installed_count"], 1)
        self.assertEqual(len(status["tasks"]), 1)
        self.assertEqual(status["tasks"][0]["name"], "QoderDailyCredits")
        self.assertEqual(status["tasks"][0]["label"], "每日 Credits")
        self.assertEqual(status["next_run"], "2026-10-20 23:05:00")

    def test_trigger_times_follow_utc8_refresh(self):
        now = datetime.datetime(2026, 10, 20, 10, 0,
                                tzinfo=datetime.timezone.utc)
        times = win_service.qoder_trigger_times(now, jitter=False)
        expected = [datetime.datetime(2026, 10, 20, 2, 5,
                                      tzinfo=datetime.timezone.utc)
                    + datetime.timedelta(hours=h)
                    for h in win_service.QODER_OFFSETS]
        self.assertEqual(times, [t.astimezone().strftime("%H:%M")
                                 for t in expected])
        self.assertEqual(len(times), len(win_service.QODER_OFFSETS))

    def test_trigger_times_only_shift_forward(self):
        """Qoder 有「刷新之后才能领」的硬门槛，抖动不能提前。"""
        now = datetime.datetime(2026, 10, 20, 10, 0,
                                tzinfo=datetime.timezone.utc)
        base = win_service.qoder_trigger_times(now, jitter=False)
        times = win_service.qoder_trigger_times(now)
        self.assertEqual(len(times), len(base))
        for raw, shifted in zip(base, times):
            base_minute = win_service.schedule_jitter.parse_hhmm(raw)
            shifted_minute = win_service.schedule_jitter.parse_hhmm(shifted)
            self.assertGreaterEqual(shifted_minute, base_minute)
            self.assertLessEqual(shifted_minute - base_minute,
                                 win_service.schedule_jitter.JITTER_WINDOW // 60)


class TraeTaskTest(unittest.TestCase):

    def test_trae_task_is_independent(self):
        self.assertEqual(win_service.TRAE_TASK_NAMES,
                         ("TraeDailyCheckin",))
        self.assertNotIn("TraeDailyCheckin", win_service.TASK_NAMES)
        self.assertNotIn("TraeDailyCheckin", win_service.QODER_TASK_NAMES)

    def test_trae_status_normalization(self):
        raw = {
            "ok": True,
            "tasks": [
                {"name": "TraeDailyCheckin", "installed": True,
                 "state": "Ready", "last_run": "2026-10-20 08:05:00",
                 "next_run": "2026-10-20 10:05:00", "last_result": 0,
                 "action": "pythonw trae_signin.py silent"},
            ],
        }
        status = win_service._normalize_status(
            raw, specs=win_service.TRAE_TASKS)

        self.assertTrue(status["installed"])
        self.assertTrue(status["enabled"])
        self.assertEqual(status["installed_count"], 1)
        self.assertEqual(len(status["tasks"]), 1)
        self.assertEqual(status["tasks"][0]["name"], "TraeDailyCheckin")
        self.assertEqual(status["tasks"][0]["label"], "每日签到")
        self.assertEqual(status["next_run"], "2026-10-20 10:05:00")

    def test_trae_trigger_times_expand_offsets(self):
        times = win_service.trae_trigger_times("08:05", jitter=False)
        self.assertEqual(times,
                         ["08:05", "09:05", "11:05", "13:05",
                          "15:05", "17:05", "19:05", "21:05"])

    def test_trae_trigger_times_wrap_past_midnight(self):
        times = win_service.trae_trigger_times("22:30", jitter=False)
        self.assertEqual(times,
                         ["22:30", "23:30", "01:30", "03:30",
                          "05:30", "07:30", "09:30", "11:30"])

    def test_trae_trigger_times_cover_the_day(self):
        """间隔不超过 2 小时：睡一觉最多连丢一两个点，醒来很快能补上。"""
        minutes = [win_service.schedule_jitter.parse_hhmm(t)
                   for t in win_service.trae_trigger_times("08:05", jitter=False)]
        for left, right in zip(minutes, minutes[1:]):
            self.assertLessEqual(right - left, 120)

    def test_trae_trigger_times_fall_back_on_invalid_input(self):
        self.assertEqual(win_service.trae_trigger_times("oops", jitter=False),
                         win_service.trae_trigger_times("08:05", jitter=False))

    def test_trae_trigger_times_never_move_backwards(self):
        """主时间往前漂会跨过午夜，被算成前一天，连着两天签不上。"""
        base = win_service.trae_trigger_times("00:10", jitter=False)
        times = win_service.trae_trigger_times("00:10")
        for raw, shifted in zip(base, times):
            self.assertGreaterEqual(
                win_service.schedule_jitter.parse_hhmm(shifted),
                win_service.schedule_jitter.parse_hhmm(raw))

    def test_random_times_cover_every_base_point(self):
        """随机化只挪分钟，不许丢点或让相邻触发点撞在一起。"""
        for spec in win_service.TASKS:
            times = win_service.random_times(spec)
            self.assertEqual(len(times), len(spec["base_times"]))
            minutes = [win_service.schedule_jitter.parse_hhmm(t) for t in times]
            self.assertNotIn(None, minutes)
            for raw, shifted in zip(spec["base_times"], minutes):
                base = win_service.schedule_jitter.parse_hhmm(raw)
                self.assertLessEqual(
                    abs(shifted - base),
                    win_service.schedule_jitter.JITTER_WINDOW // 60)


class IdleTriggerTest(unittest.TestCase):
    """空闲补跑：每个任务的注册脚本都必须带空闲触发器和配套设置。

    这里的断言直接看生成的 PowerShell 文本——真正容易出错的地方不是
    「会不会写」，而是「重排时会不会把触发器丢了」：重排是从零重建任务，
    漏一条就等于每跑完一次就把兜底机制删掉，而且不报任何错。
    """

    def test_install_script_registers_idle_trigger_for_both_tasks(self):
        script = win_service._install_script("C:/app/app.exe", ["--service"],
                                             "C:/app")
        self.assertEqual(script.count("MSFT_TaskIdleTrigger"), len(win_service.TASKS))
        self.assertEqual(script.count("-DontStopOnIdleEnd"), len(win_service.TASKS))
        self.assertEqual(script.count("-IdleDuration"), len(win_service.TASKS))

    def test_single_task_script_registers_idle_trigger(self):
        script = win_service._single_task_script(
            "C:/Python/pythonw.exe", '"C:/app/qoder_signin.py" silent', "C:/app",
            win_service.QODER_TASKS[0], ["10:05", "11:07"], "ok")
        self.assertIn("MSFT_TaskIdleTrigger", script)
        self.assertIn("-DontStopOnIdleEnd", script)
        self.assertIn("-IdleWaitTimeout", script)

    def test_reshuffle_keeps_idle_trigger(self):
        """重排会重建整个任务定义，空闲触发器必须一起写回去。"""
        script = win_service._rewrite_times_script([{
            "name": "TraeDailyCheckin", "exe": "C:/app/pythonw.exe",
            "args": "trae_signin.py silent", "workdir": "C:/app",
            "minutes": 10, "description": "d", "times": ["08:05", "09:05"],
        }])
        self.assertIn("MSFT_TaskIdleTrigger", script)
        self.assertIn("-DontStopOnIdleEnd", script)

    def test_status_reports_idle_trigger(self):
        raw = {
            "ok": True,
            "tasks": [
                {"name": "WorkBuddyAutoSignin", "installed": True,
                 "state": "Ready", "times": ["00:07"], "idle": True},
                {"name": "WorkBuddyGrowthPoll", "installed": True,
                 "state": "Ready", "times": ["01:03"], "idle": False},
            ],
        }
        status = win_service._normalize_status(raw)
        self.assertTrue(status["tasks"][0]["idle"])
        self.assertFalse(status["tasks"][1]["idle"])
        self.assertIn("空闲补跑", status["tasks"][0]["when"])
        self.assertNotIn("空闲补跑", status["tasks"][1]["when"])

    def test_uninstalled_task_reports_no_idle(self):
        self.assertFalse(win_service._empty_tasks()[0]["idle"])


class ReshuffleIdleUpgradeTest(unittest.TestCase):
    """旧任务没有空闲触发器时，重排要顺手补上（不能只依赖重新安装）。"""

    def test_missing_idle_trigger_forces_rewrite(self):
        read_payload = json.dumps({"ok": True, "tasks": [{
            "name": "WorkBuddyAutoSignin",
            "times": ["00:05"],
            "idle": False,
            "exe": "C:/app/app.exe",
            "args": "--service silent",
            "workdir": "C:/app",
        }]})
        seen = {}

        def fake_run(script, timeout=40):
            if "Register-ScheduledTask" in script:
                seen["rewrite"] = script
                return {"ok": True, "tasks": ["WorkBuddyAutoSignin"]}
            return json.loads(read_payload)

        with mock.patch.object(win_service, "_run_ps_json", side_effect=fake_run), \
                mock.patch.object(win_service, "read_jitter_state", return_value={}), \
                mock.patch.object(win_service, "write_jitter_state"):
            outcome = win_service.reshuffle(
                "workbuddy", data_dir="unused", force=True,
                now=datetime.datetime(2026, 11, 5, 0, 1))

        self.assertTrue(outcome["ok"])
        self.assertEqual(len(outcome["changed"]), 1)
        self.assertTrue(outcome["changed"][0]["idle_added"])
        # 时刻没变，重写的唯一理由就是补空闲触发
        self.assertEqual(outcome["changed"][0]["old"],
                         outcome["changed"][0]["new"])
        self.assertIn("MSFT_TaskIdleTrigger", seen["rewrite"])


class ScheduleDensityTest(unittest.TestCase):
    """每个模块一天都要有多个点：电脑睡过去不能把当天唯一的机会睡没。"""

    def test_workbuddy_signin_has_multiple_points(self):
        self.assertGreaterEqual(len(win_service.SIGNIN_TIMES), 6)
        minutes = [win_service.schedule_jitter.parse_hhmm(t)
                   for t in win_service.SIGNIN_TIMES]
        self.assertNotIn(None, minutes)
        for left, right in zip(minutes, minutes[1:]):
            self.assertLessEqual(right - left, 4 * 60)

    def test_workbuddy_covers_the_whole_day(self):
        """签到点要贯穿 0~24 点，不能只堆在凌晨；合并轮询点后不留大空档。"""
        minutes = [win_service.schedule_jitter.parse_hhmm(t)
                   for t in win_service.SIGNIN_TIMES]
        self.assertLessEqual(minutes[0], 30)
        self.assertGreaterEqual(minutes[-1], 22 * 60)
        merged = sorted(minutes + [
            win_service.schedule_jitter.parse_hhmm(t)
            for t in win_service.POLL_TIMES])
        gaps = [right - left for left, right in zip(merged, merged[1:])]
        self.assertLessEqual(max(gaps), 2 * 60)

    def test_qoder_and_trae_points_are_no_more_than_two_hours_apart(self):
        for offsets in (win_service.QODER_OFFSETS, win_service.TRAE_OFFSETS):
            self.assertGreaterEqual(len(offsets), 6)
            for left, right in zip(offsets, offsets[1:]):
                self.assertLessEqual(right - left, 2)

    def test_when_label_matches_point_count(self):
        """卡片文案的次数必须和实际点数一致——改了点位忘了改文案就是在骗人。"""
        for spec in win_service.TASKS:
            self.assertIn("%d 次" % len(spec["base_times"]), spec["when"])
        self.assertIn("%d 次" % len(win_service.QODER_OFFSETS),
                      win_service.QODER_TASKS[0]["when"])
        self.assertIn("%d 次" % len(win_service.TRAE_OFFSETS),
                      win_service.TRAE_TASKS[0]["when"])

    def test_poll_times_stay_four_hours_apart(self):
        """轮询是补签兜底，间隔由 6 个固定时段保证，不跟着加密。"""
        self.assertEqual(len(win_service.POLL_TIMES), 6)


class AlertStoreTest(unittest.TestCase):
    """计划任务失败记录：界面只认这几种字段，写坏了也不能让控制台起不来。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb_alert_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_returns_empty(self):
        self.assertEqual(win_service.read_alerts(self.tmp), [])

    def test_corrupt_file_does_not_raise(self):
        with open(win_service.alert_path(self.tmp), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(win_service.read_alerts(self.tmp), [])

    def test_write_replaces_same_kind_and_keeps_order(self):
        win_service.write_alert(self.tmp, {
            "kind": "trae", "action": "silent", "exit": 1,
            "result": "ERROR", "report": "没有会话 Cookie 可续期，请重新导入本机登录态",
        })
        win_service.write_alert(self.tmp, {
            "kind": "workbuddy", "action": "silent", "exit": 1,
            "result": "ERROR", "report": "网络超时",
        })
        win_service.write_alert(self.tmp, {
            "kind": "trae", "action": "silent", "exit": 0,
            "result": "OK", "report": "重试成功", "recovered": True,
        })

        items = win_service.read_alerts(self.tmp)
        self.assertEqual([item["kind"] for item in items],
                         ["trae", "workbuddy"])
        self.assertEqual(items[0]["report"], "重试成功")
        self.assertTrue(items[0]["recovered"])
        self.assertNotIn("accounts", items[0])

    def test_write_replaces_previous_alert_of_same_kind(self):
        win_service.write_alert(self.tmp, {
            "kind": "qoder", "action": "silent", "exit": 1,
            "report": "第一次失败",
        })
        win_service.write_alert(self.tmp, {
            "kind": "qoder", "action": "silent", "exit": 0,
            "report": "重试成功", "recovered": True,
        })
        items = [item for item in win_service.read_alerts(self.tmp)
                 if item["kind"] == "qoder"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["report"], "重试成功")
        self.assertTrue(items[0]["recovered"])

    def test_report_is_trimmed(self):
        win_service.write_alert(self.tmp, {
            "kind": "trae", "report": "x" * (win_service.ALERT_REPORT_LIMIT + 50),
        })
        report = win_service.read_alerts(self.tmp)[0]["report"]
        self.assertEqual(len(report), win_service.ALERT_REPORT_LIMIT)
        self.assertTrue(report.endswith("…"))

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            win_service.write_alert(self.tmp, {"kind": "nope"})
        with self.assertRaises(ValueError):
            win_service.clear_alert(self.tmp, "nope")

    def test_clear_alert_only_removes_target_kind(self):
        win_service.write_alert(self.tmp, {"kind": "trae", "report": "a"})
        win_service.write_alert(self.tmp, {"kind": "workbuddy", "report": "b"})
        self.assertTrue(win_service.clear_alert(self.tmp, "trae"))
        self.assertFalse(win_service.clear_alert(self.tmp, "trae"))
        self.assertEqual([item["kind"] for item in
                          win_service.read_alerts(self.tmp)],
                         ["workbuddy"])

    def test_clear_last_alert_removes_file(self):
        win_service.write_alert(self.tmp, {"kind": "qoder", "report": "a"})
        self.assertTrue(win_service.clear_alert(self.tmp, "qoder"))
        self.assertFalse(os.path.exists(win_service.alert_path(self.tmp)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
