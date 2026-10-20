"""win_service 的纯函数回归测试。

不实际注册/删除计划任务，只验证：

  - Windows 命令行与 PowerShell 字符串的转义
  - PowerShell 原始状态到界面结构的整理
  - 部分安装、停用、读取失败等边界

真正的计划任务操作由 Windows 实机验证，避免测试修改用户系统状态。
"""

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
