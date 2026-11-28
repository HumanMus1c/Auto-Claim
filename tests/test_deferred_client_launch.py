"""计划任务的「探测已签到」不得拉起目标平台客户端。

以前三个签到脚本的 silent 动作一进来就无条件调用 client_launcher，
哪怕 HTTP 状态查询的结论是「今日已签到」。同一天里 WorkBuddy 7 个每日点
+ 7 个轮询点 + 空闲补跑，桌面端没开时就会一次次被拉起。现在拉起被推迟到
真正要领取（或凭据需要刷新）的时刻，并且一轮进程里最多一次。
"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import qoder_signin as qoder
import signin
import trae_signin as trae


NOW = int(time.time())


def campaign(key, status, start=None, end=None):
    start = NOW - 3600 if start is None else start
    end = NOW + 3600 if end is None else end
    return {
        "campaignId": "id-" + key,
        "campaignKey": key,
        "actionType": "CLAIM_BENEFIT",
        "claimStatus": status,
        "benefit": {"kind": "CREDITS", "amount": 100,
                    "validity": {"mode": "RELATIVE_DAYS", "days": 30}},
        "startAt": start,
        "endAt": end,
    }



class FakeApi:
    def __init__(self, payload):
        self.payload = payload

    def campaigns(self, retry=True):
        return 200, self.payload, None

    def claim(self, campaign_id, retry=True):
        return 200, {"status": "CLAIMED"}, None


class DeferredLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.auth_file = Path(self.temp.name) / "auth.json"
        self.auth_file.write_text('{"auth": {"accessToken": "synthetic-token", '
                                  '"endpoint": "https://copilot.tencent.com"}, '
                                  '"account": {"uid": "synthetic-user"}}',
                                  encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {
            "AUTO_CLAIM_LAUNCH_CLIENT": "0",
            "LOCALAPPDATA": str(Path(self.temp.name) / "local"),
            "WORKBUDDY_AUTH_FILE": str(self.auth_file),
            "WORKBUDDY_SIGNIN_LOG": str(Path(self.temp.name) / "signin.log"),
            "QODER_SIGNIN_LOG": str(Path(self.temp.name) / "qoder.log"),
            "TRAE_SIGNIN_LOG": str(Path(self.temp.name) / "trae.log"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _reset(self, module):
        module._SCHEDULED_LAUNCH = False
        module._CLIENT_LAUNCH_DONE = False

    # ---------------- WorkBuddy ----------------

    def test_workbuddy_manual_actions_never_launch(self):
        with mock.patch.object(signin, "client_launcher") as launcher:
            for action in ("auto", "status", "doctor"):
                self._reset(signin)
                signin._SCHEDULED_LAUNCH = action in signin.IDLE_ACTIONS
                signin._ensure_client_running()
                self._reset(signin)
            launcher.ensure_client_running.assert_not_called()

    def test_workbuddy_probe_reports_already_without_launch(self):
        # status 返回 today_checked_in=True：探测阶段结束，客户端一次都不拉起。
        with mock.patch.object(signin, "client_launcher") as launcher, \
                mock.patch.object(signin, "post", return_value=(
                    200, {"active": True, "today_checked_in": True,
                          "today_credit": 100})):
            signin._SCHEDULED_LAUNCH = True
            signin._CLIENT_LAUNCH_DONE = False
            try:
                code, out = signin.run_auto({}, "https://example.com")
            finally:
                self._reset(signin)
        self.assertEqual((code, out["result"]), (0, "ALREADY"))
        launcher.ensure_client_running.assert_not_called()

    def test_workbuddy_claim_launches_at_most_once_per_run(self):
        # 第一次探测明确未签 → 拉起；同一轮里的失败自愈重试（第二次调用）
        # 受 _CLIENT_LAUNCH_DONE 保护，不再重复拉起。
        with mock.patch.object(signin, "client_launcher") as launcher, \
                mock.patch.object(signin, "find_workbuddy_runtime", return_value=None), \
                mock.patch.object(signin, "post", return_value=(
                    200, {"active": True, "today_checked_in": False})):
            launcher.ensure_client_running.return_value = "launched"
            signin._SCHEDULED_LAUNCH = True
            signin._CLIENT_LAUNCH_DONE = False
            try:
                signin.run_auto({}, "https://example.com")
                signin.run_auto({}, "https://example.com")
            finally:
                self._reset(signin)
        launcher.ensure_client_running.assert_called_once_with("workbuddy", exe=None)

    def test_workbuddy_silent_already_never_launches(self):
        with mock.patch.object(signin, "client_launcher") as launcher, \
                mock.patch.object(signin, "run_daily", return_value=(
                    0, {"result": "ALREADY", "report": "今日已签"}, False)), \
                mock.patch.object(signin, "emit"):
            signin._run("silent")
        launcher.ensure_client_running.assert_not_called()

    # ---------------- Qoder ----------------

    def test_qoder_probe_already_never_launches(self):
        payload = {"campaigns": [campaign("done", "CLAIMED")]}
        with mock.patch.object(qoder, "client_launcher") as launcher:
            qoder._SCHEDULED_LAUNCH = True
            qoder._CLIENT_LAUNCH_DONE = False
            try:
                out = qoder.claim_once(FakeApi(payload))
            finally:
                self._reset(qoder)
        self.assertEqual(out["result"], "ALREADY")
        launcher.ensure_client_running.assert_not_called()

    def test_qoder_claim_launches_before_write(self):
        payload = {"campaigns": [campaign("today", "CLAIMABLE")]}
        with mock.patch.object(qoder, "client_launcher") as launcher:
            qoder._SCHEDULED_LAUNCH = True
            qoder._CLIENT_LAUNCH_DONE = False
            try:
                out = qoder.claim_once(FakeApi(payload))
            finally:
                self._reset(qoder)
        self.assertEqual(out["result"], "CLAIMED")
        launcher.ensure_client_running.assert_called_once_with("qoder")

    def test_qoder_dry_run_never_launches(self):
        payload = {"campaigns": [campaign("today", "CLAIMABLE")]}
        with mock.patch.object(qoder, "client_launcher") as launcher:
            qoder._SCHEDULED_LAUNCH = True
            qoder._CLIENT_LAUNCH_DONE = False
            try:
                out = qoder.claim_once(FakeApi(payload), dry_run=True)
            finally:
                self._reset(qoder)
        self.assertEqual(out["result"], "DRY_RUN")
        launcher.ensure_client_running.assert_not_called()

    # ---------------- Trae ----------------

    def test_trae_already_never_launches(self):
        account = {"id": "a", "token": "t", "last_checkin_date": None}
        store = {"settings": {"interval_seconds": 0}}
        status = {"code": 0, "checked_in": True, "credits": 100}
        api = mock.Mock()
        api.remaining_credits.return_value = 10
        with mock.patch.object(trae, "client_launcher") as launcher, \
                mock.patch.object(trae, "_prepare_api", return_value=(api, status)), \
                mock.patch.object(trae, "_remember_status"), \
                mock.patch.object(trae, "record_checkin"), \
                mock.patch.object(trae, "record_snapshot"), \
                mock.patch.object(trae, "save_store"):
            trae._SCHEDULED_LAUNCH = True
            trae._CLIENT_LAUNCH_DONE = False
            try:
                out = trae._checkin_one(store, account, 0, 1)
            finally:
                self._reset(trae)
        self.assertEqual(out["result"], "ALREADY")
        launcher.ensure_client_running.assert_not_called()


if __name__ == "__main__":
    unittest.main()
