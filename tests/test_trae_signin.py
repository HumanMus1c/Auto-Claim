import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import trae_signin as trae


class LaunchTests(unittest.TestCase):
    def setUp(self):
        # 本机真实计划任务会写 idle-guard/idle-plan，隔离掉避免测试被闸门跳过。
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "LOCALAPPDATA": str(Path(self.temp.name) / "local"),
            "TRAE_SIGNIN_LOG": str(Path(self.temp.name) / "trae.log"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_silent_does_not_prepare_client_up_front(self):
        # 探测是否已签到只靠本地账号 + HTTP，进入 silent 时不能拉起客户端。
        with mock.patch.object(trae, "_ensure_client_running") as ensure, \
                mock.patch.object(trae, "run_auto", return_value={"result": "NO_ACCOUNT"}), \
                mock.patch.object(trae, "emit"):
            self.assertEqual(trae.run("silent"), 2)
        ensure.assert_not_called()

    def test_auto_does_not_prepare_client(self):
        with mock.patch.object(trae, "_ensure_client_running") as ensure, \
                mock.patch.object(trae, "run_auto", return_value={"result": "NO_ACCOUNT"}), \
                mock.patch.object(trae, "emit"):
            self.assertEqual(trae.run("auto"), 2)
        ensure.assert_not_called()

    def test_claim_prepares_client_before_request(self):
        account = {"id": "a", "token": "t", "last_checkin_date": None,
                   "is_member": False}
        store = {"settings": {"interval_seconds": 0}}
        api = mock.Mock()
        status = {"code": 0, "checked_in": False}
        api.claim.return_value = {"code": 0, "credits": 100}
        api.remaining_credits.return_value = 10
        with mock.patch.object(trae, "_prepare_api", return_value=(api, status)), \
                mock.patch.object(trae, "_remember_status"), \
                mock.patch.object(trae, "record_checkin"), \
                mock.patch.object(trae, "record_snapshot"), \
                mock.patch.object(trae, "save_store"), \
                mock.patch.object(trae, "_ensure_client_running") as ensure:
            trae._SCHEDULED_LAUNCH = True
            trae._CLIENT_LAUNCH_DONE = False
            try:
                out = trae._checkin_one(store, account, 0, 1)
            finally:
                trae._SCHEDULED_LAUNCH = False
                trae._CLIENT_LAUNCH_DONE = False
        self.assertEqual(out["result"], "CLAIMED")
        ensure.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
