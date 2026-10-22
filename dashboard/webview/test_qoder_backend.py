"""Qoder 后端桥接的纯函数测试。

不联网、不读取真实 Qoder 登录态：
  - emit() 捕获必须覆盖 quiet 结果（ALREADY / INACTIVE）
  - 捕获完成后必须还原 emit，避免影响后续调用
  - 手动领取结果要能写入并重新读出 qoder_signin.log
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import app  # noqa: E402
import qoder_signin as qoder  # noqa: E402


class FakeQoder(object):

    def __init__(self, result="ALREADY", fail=False):
        self.result = result
        self.fail = fail
        self._started_at = 0
        self.emit_calls = 0

    def _redact(self, text):
        return text.replace("SECRET", "[REDACTED]")

    def emit(self, out, action, quiet=False):
        self.emit_calls += 1

    def run(self, action, dry_run=False, once=False):
        if self.fail:
            raise RuntimeError("boom")
        self.emit({"result": self.result, "report": "SECRET",
                   "needs_attention": False}, action, quiet=True)
        return 0


class InvokeQoderTest(unittest.TestCase):

    def setUp(self):
        self._saved = app._QODER_MODULE

    def tearDown(self):
        app._QODER_MODULE = self._saved

    def test_quiet_payload_is_captured(self):
        fake = FakeQoder("ALREADY")
        saved_emit = fake.emit.__func__
        app._QODER_MODULE = fake

        code, out = app._invoke_qoder("auto")

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["result"], "ALREADY")
        self.assertEqual(payload["report"], "[REDACTED]")
        self.assertGreater(fake._started_at, 0)
        # 调用结束后必须还原为模块自己的 emit，而不是停在 capture
        self.assertIs(fake.emit.__func__, saved_emit)
        self.assertEqual(len(app._invoke_qoder("auto")[1].splitlines()), 1)

    def test_exception_is_reported_and_emit_restored(self):
        fake = FakeQoder(fail=True)
        saved_emit = fake.emit.__func__
        app._QODER_MODULE = fake

        code, out = app._invoke_qoder("auto")

        self.assertEqual(code, 1)
        self.assertIn("RuntimeError: boom", out)
        self.assertIs(fake.emit.__func__, saved_emit)


class QoderLogFileTest(unittest.TestCase):

    def setUp(self):
        self._saved = os.environ.get("QODER_SIGNIN_LOG")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["QODER_SIGNIN_LOG"] = os.path.join(self._tmp.name,
                                                      "qoder_signin.log")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("QODER_SIGNIN_LOG", None)
        else:
            os.environ["QODER_SIGNIN_LOG"] = self._saved
        self._tmp.cleanup()

    def test_append_and_snapshot_round_trip(self):
        payload = {"result": "CLAIMED", "report": "已领取 100 Credits",
                   "credits": 100}
        app._append_qoder_log(payload)

        snapshot = app._qoder_log_snapshot()

        self.assertTrue(snapshot["at"])
        self.assertEqual(len(snapshot["lines"]), 1)
        self.assertIn('"result": "CLAIMED"', snapshot["lines"][0])

    def test_non_dict_payload_is_ignored(self):
        app._append_qoder_log(None)
        app._append_qoder_log("not a dict")

        self.assertEqual(app._qoder_log_snapshot()["lines"], [])


class QoderAccountSummaryTest(unittest.TestCase):

    def test_public_user_masks_contact_fields(self):
        account = qoder._public_user({
            "id": "q-123",
            "name": "Qoder 用户",
            "email": "alice@example.com",
            "phone": "13800138000",
            "avatarUrl": "https://example.com/avatar.png",
        })

        self.assertEqual(account["id"], "q-123")
        self.assertEqual(account["name"], "Qoder 用户")
        self.assertEqual(account["email_masked"], "al***@example.com")
        self.assertEqual(account["phone_masked"], "138****8000")
        self.assertEqual(account["avatar_url"],
                         "https://example.com/avatar.png")

    def test_public_user_ignores_invalid_input(self):
        self.assertEqual(qoder._public_user(None), {})
        self.assertEqual(qoder._public_user("not-a-dict"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
