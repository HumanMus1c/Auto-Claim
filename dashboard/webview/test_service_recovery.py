"""计划任务失败自愈的回归测试。

不启动窗口、不跑真实签到、不碰计划任务，只验证：

  - 什么算「失败」（退出码之外还要看 result）
  - 失败后按顺序写记录 → 拉起控制台 → 重新导入登录态 → 重试一次
  - 剩余时间不足时不重试，避免任务被任务计划程序强杀
  - 重试成功时把退出码归零，任务计划程序里显示成功

真实链路（弹窗、Trae 导入、签到请求）仍由实机验证。
"""

import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.dirname(HERE)
REPO = os.path.dirname(DASHBOARD)
for path in (DASHBOARD, REPO, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import app  # noqa: E402


class FailureDetectionTest(unittest.TestCase):

    def test_success_is_not_failure(self):
        stdout = json.dumps({"result": "CLAIMED", "report": "领取成功"})
        self.assertIsNone(app._service_failure("workbuddy", 0, stdout))

    def test_exit_code_marks_failure(self):
        got = app._service_failure(
            "workbuddy", 1,
            json.dumps({"result": "ERROR",
                        "report": "没有会话 Cookie 可续期，请重新导入本机登录态"}))
        self.assertIsNotNone(got)
        self.assertEqual(got["exit"], 1)
        self.assertIn("没有会话 Cookie 可续期", got["report"])

    def test_error_result_with_zero_exit_still_failure(self):
        """脚本对凭据过期有时仍以 0 退出，只看退出码会漏报。"""
        got = app._service_failure(
            "trae", 0, json.dumps({"result": "AUTH_ERROR",
                                   "report": "鉴权失败"}))
        self.assertIsNotNone(got)
        self.assertEqual(got["result"], "AUTH_ERROR")

    def test_missing_report_gets_placeholder(self):
        with mock.patch.object(app, "_last_log_payload", return_value=None):
            got = app._service_failure("trae", 1, "")
        self.assertIn("退出码 1", got["report"])

    def test_fresh_failure_tail_is_used(self):
        tail = {"result": "AUTH_ERROR",
                "report": "没有会话 Cookie 可续期，请重新导入本机登录态"}
        with mock.patch.object(app, "_last_log_payload", return_value=tail), \
             mock.patch.object(app, "_log_age", return_value=30):
            got = app._service_failure("trae", 1, "")
        self.assertIn("没有会话 Cookie 可续期", got["report"])

    def test_success_tail_never_explains_a_failure(self):
        """进程在写完成功日志后崩了，不能把「领取成功」当成失败原因。"""
        tail = {"result": "CLAIMED", "report": "成功领取 100 积分"}
        with mock.patch.object(app, "_last_log_payload", return_value=tail), \
             mock.patch.object(app, "_log_age", return_value=5):
            got = app._service_failure("workbuddy", 1, "")
        self.assertIn("退出码 1", got["report"])
        self.assertNotIn("成功领取", got["report"])

    def test_stale_failure_tail_is_ignored(self):
        """六小时前那次失败的日志，不能当成这一次的原因。"""
        tail = {"result": "AUTH_ERROR", "report": "上一次的旧报错"}
        with mock.patch.object(app, "_last_log_payload", return_value=tail), \
             mock.patch.object(app, "_log_age", return_value=6 * 3600):
            got = app._service_failure("trae", 1, "")
        self.assertIn("退出码 1", got["report"])
        self.assertNotIn("旧报错", got["report"])


class RetryBudgetTest(unittest.TestCase):

    def test_skips_when_no_time_left(self):
        code, report = app._retry_service_action("workbuddy", "silent", 540)
        self.assertIsNone(code)
        self.assertIn("未自动重试", report)

    def test_retries_when_time_is_enough(self):
        seen = {}

        def fake_invoke(action):
            seen["action"] = action
            seen["budget"] = os.environ.get("WORKBUDDY_BUDGET_SECONDS")
            return 0, json.dumps({"result": "CLAIMED", "report": "成功"}), ""

        with mock.patch.object(app.Api, "_invoke", staticmethod(fake_invoke)):
            code, report = app._retry_service_action("workbuddy", "silent", 10)

        self.assertEqual(code, 0)
        self.assertEqual(report, "成功")
        self.assertEqual(seen["action"], "silent")
        self.assertTrue(0 < int(seen["budget"]) <= 540)
        # 重试结束后必须把环境变量还原，否则同进程里的后续调用会一直用它
        self.assertIsNone(os.environ.get("WORKBUDDY_BUDGET_SECONDS"))


class ReimportTest(unittest.TestCase):

    def test_trae_import_reports_count(self):
        fake = mock.Mock()
        fake.import_local.return_value = {
            "result": "OK", "report": "已导入本机账号：新增 0 个，更新 1 个"}
        with mock.patch.object(app, "_load_trae", return_value=fake):
            ok, report = app._reimport_local("trae")
        self.assertTrue(ok)
        self.assertIn("更新 1 个", report)

    def test_trae_import_failure_is_reported(self):
        fake = mock.Mock()
        fake.import_local.return_value = {
            "result": "NO_AUTH", "report": "已找到客户端目录，但未读取到可用登录态"}
        with mock.patch.object(app, "_load_trae", return_value=fake):
            ok, report = app._reimport_local("trae")
        self.assertFalse(ok)
        self.assertIn("未读取到可用登录态", report)

    def test_exception_never_escapes(self):
        fake = mock.Mock()
        fake.import_local.side_effect = RuntimeError("boom")
        with mock.patch.object(app, "_load_trae", return_value=fake):
            ok, report = app._reimport_local("trae")
        self.assertFalse(ok)
        self.assertIn("boom", report)


class HandleFailureTest(unittest.TestCase):

    def _run(self, retry_code=0, failure=True, surface="launched"):
        writes = []
        logs = []
        with mock.patch.object(app, "_write_service_alert",
                               side_effect=lambda r: writes.append(dict(r))), \
             mock.patch.object(app, "_surface_console",
                               return_value=surface), \
             mock.patch.object(app, "_reimport_local",
                               return_value=(True, "已重新读取本机登录态")), \
             mock.patch.object(app, "_retry_service_action",
                               return_value=(retry_code, "重试结论")), \
             mock.patch.object(app, "_log_recovery",
                               side_effect=lambda kind, rec: logs.append((kind, rec))):
            code = app._handle_service_failure(
                "trae", "silent", 1,
                json.dumps({"result": "AUTH_ERROR",
                            "report": "没有会话 Cookie 可续期，请重新导入本机登录态"}),
                12.0)
        return code, writes, logs

    def test_successful_retry_clears_exit_code(self):
        code, writes, logs = self._run(retry_code=0)
        self.assertEqual(code, 0)
        self.assertEqual(len(writes), 2)
        final = writes[-1]
        self.assertTrue(final["recovered"])
        self.assertEqual(final["surface"], "launched")
        self.assertTrue(final["reimport_ok"])
        self.assertEqual(final["stage"], "done")
        self.assertIn("没有会话 Cookie 可续期", final["report"])
        self.assertEqual(logs[0][0], "trae")

    def test_failed_retry_keeps_exit_code(self):
        code, writes, _logs = self._run(retry_code=1)
        self.assertEqual(code, 1)
        self.assertFalse(writes[-1]["recovered"])

    def test_first_write_happens_before_surface(self):
        """界面一起来就要能读到原文，不能等重试跑完才落盘。"""
        order = []
        with mock.patch.object(app, "_write_service_alert",
                               side_effect=lambda r: order.append("write")), \
             mock.patch.object(app, "_surface_console",
                               side_effect=lambda *args: order.append("surface")), \
             mock.patch.object(app, "_reimport_local",
                               return_value=(True, "ok")), \
             mock.patch.object(app, "_retry_service_action",
                               return_value=(0, "成功")), \
             mock.patch.object(app, "_log_recovery"):
            app._handle_service_failure(
                "workbuddy", "silent", 1,
                json.dumps({"result": "ERROR", "report": "失败"}), 5.0)
        self.assertEqual(order[:2], ["write", "surface"])
        self.assertEqual(order[-1], "write")


if __name__ == "__main__":
    unittest.main(verbosity=2)
