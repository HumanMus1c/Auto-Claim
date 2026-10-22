"""win_service 的纯函数回归测试。

不实际注册/删除计划任务，只验证：

  - Windows 命令行与 PowerShell 字符串的转义
  - PowerShell 原始状态到界面结构的整理
  - 部分安装、停用、读取失败等边界

真正的计划任务操作由 Windows 实机验证，避免测试修改用户系统状态。
"""

import datetime
import os
import sys
import unittest

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
        times = win_service.qoder_trigger_times(now)
        expected = [datetime.datetime(2026, 10, 20, 2, 5,
                                      tzinfo=datetime.timezone.utc)
                    + datetime.timedelta(hours=h)
                    for h in win_service.QODER_OFFSETS]
        self.assertEqual(times, [t.astimezone().strftime("%H:%M")
                                 for t in expected])
        self.assertEqual(len(times), 5)


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
        times = win_service.trae_trigger_times("08:05")
        self.assertEqual(times,
                         ["08:05", "10:05", "13:05", "17:05", "21:05"])

    def test_trae_trigger_times_wrap_past_midnight(self):
        times = win_service.trae_trigger_times("22:30")
        self.assertEqual(times,
                         ["22:30", "00:30", "03:30", "07:30", "11:30"])

    def test_trae_trigger_times_fall_back_on_invalid_input(self):
        self.assertEqual(win_service.trae_trigger_times("oops"),
                         win_service.trae_trigger_times("08:05"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
