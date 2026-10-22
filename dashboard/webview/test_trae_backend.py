"""Trae 后端（trae_signin.py）的离线回归测试。

不联网、不读取真实 Trae 登录态：
  - 凭证清洗 / JWT 解析 / 会话提取
  - 账号增删改、设备号唯一、DPAPI 落盘
  - 签到、刷新、用量入库、历史与快照
  - monitor() 的只读汇总与日志脱敏

真实接口口径来自 star620/TraeTools，测试里统一用假的 TraeApi 覆盖。
"""

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

import trae_signin as trae  # noqa: E402


def b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_jwt(uid="1234567890123456", expires_in=3600):
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({
        "data": {"id": uid, "exp": int(time.time()) + expires_in},
    }).encode())
    return "%s.%s.signature123" % (header, payload)


# 用 Trae 当前 iCubeAuthInfo 格式生成的合成登录态（无真实凭证）。
# 内容：uid=1234567890123456、昵称=本地测试号、手机=138****0000。
ICUBE_FIXTURE = (
    "dGMFEAAAAAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8h2NbzX6B/vRib"
    "CEFhr9ZulvZ6peNbQfk3dPgoYY415wkQUtNLVz0LUeW3AsNDFzzzepRgbeC6Fe+d"
    "flrsNEFHtuBumfdbE2SU3cTPtsk65RSgdbQH2htqDxqO6x743LRTnex7YUdFZbNC"
    "LFZ9+zpMhR6EiegiDHaF94WzBVGMhhFkKqpcvEqgM7yylh3cMyIHTOHPRuebF2uq"
    "El77mH1eL7+riZU8M3TIyO8V8UlYmMSfNQCa0URdC8lfN1GQ4B/EW/Un2Ta6CO+K"
    "BBTY9aOLK+drR2WeLDiLAJCgWRw15lL2XM31BdM+VdmER2hTb2UIrFZtBRYoqnTI"
    "ra0zRg5U2IHj4KqW2fWwkdsUlp06AHg1UPuUSPnPvBNGwfPvOXJOlpPfkHbFuaUp"
    "VtA0t44ZvBRm+Fh3qKO1XeCzdDK8wSmU5z8mPrkwNTC3P/PGUTlYCHAeU9bxLFF8"
    "wCL8HiEWGrGcOGvp6Zjlj1E0BCi3y8iX6Wxqd9b5VOv+TLQSo7qgUo5DwnO5ygDm"
    "mXvCsLp2WrS2zDddfC1ZJ8bGJgOizYEcnUHlxTac3mcnxhIlfLtyIVvBE5dzNMd5"
    "PplsjEQ8lBO1tCxnVeO+8kd1M4KP0jYFvsZX/LBx/Z3FjpIGcVZbDCXv5ib62+fE"
    "shiky4JD5XbQPVj4m8XWJw+SYWaXYHFtua2wiSsb2PfiyyjSzwA=")
ICUBE_TOKEN = (
    "eyJhbGciOiAiUlMyNTYiLCAidHlwIjogIkpXVCJ9.eyJkYXRhIjogeyJpZCI6ICIx"
    "MjM0NTY3ODkwMTIzNDU2IiwgImV4cCI6IDQxMDI0NDQ4MDB9fQ.Zml4dHVyZS1zaWdu"
    "YXR1cmU")


class TraeTestBase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("TRAE_DATA_DIR", "TRAE_SIGNIN_LOG", "APPDATA")
        }
        os.environ["TRAE_DATA_DIR"] = self._tmp.name
        os.environ["TRAE_SIGNIN_LOG"] = os.path.join(self._tmp.name, "trae.log")
        os.environ["APPDATA"] = os.path.join(self._tmp.name, "appdata")
        os.makedirs(os.environ["APPDATA"], exist_ok=True)
        trae._sensitive.clear()
        trae._started_at = time.monotonic()
        self._scenario = {}
        self._patch_api()

    def tearDown(self):
        for name, func in self._saved_api.items():
            setattr(trae.TraeApi, name, func)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        trae._sensitive.clear()
        self._tmp.cleanup()

    # -- 假 TraeApi -------------------------------------------------

    def _patch_api(self):
        base = self

        def status(self, retries=1):
            queue = base._scenario.get("status")
            if isinstance(queue, list):
                return queue.pop(0) if queue else None
            return queue

        def claim(self, retries=1):
            return base._scenario.get("claim", {"code": 0, "message": "ok"})

        def get_user_token(self, session=None, retries=1):
            return base._scenario.get("renewed")

        def remaining_credits(self):
            return base._scenario.get("remaining", -1)

        def profile(self):
            return base._scenario.get(
                "profile", {"user_id": "", "screen_name": "测试昵称",
                            "mobile_masked": "138****0000",
                            "avatar_url": ""})

        def student_status(self):
            return base._scenario.get("student", 0)

        def usage_page(self, start_seconds, end_seconds, page_num,
                       page_size=50, retries=1):
            return base._scenario.get("usage")

        def expired_ents(self):
            return base._scenario.get("expired", [])

        self._saved_api = {}
        for name, func in (
            ("status", status), ("claim", claim),
            ("get_user_token", get_user_token),
            ("remaining_credits", remaining_credits),
            ("profile", profile), ("student_status", student_status),
            ("usage_page", usage_page), ("expired_ents", expired_ents),
        ):
            self._saved_api[name] = getattr(trae.TraeApi, name)
            setattr(trae.TraeApi, name, func)

    def add(self, uid="1234567890123456", name="测试号", **extra):
        self._scenario.setdefault("profile", {
            "user_id": uid, "screen_name": name,
            "mobile_masked": "138****0000", "avatar_url": "",
        })
        result = trae.add_account(make_jwt(uid), name=name, **extra)
        self.assertTrue(result.get("ok"), result)
        return result["account"]


class CredentialTest(TraeTestBase):

    def test_normalize_prefers_jwt_and_strips_cookie_shell(self):
        token = make_jwt()
        raw = "Authorization: Cloud-IDE-JWT " + token
        self.assertEqual(trae.normalize_credential(raw), token)
        cookie = "Cookie: X-Cloudide-Session=abcdef0123456789abcdef; a=1"
        self.assertEqual(trae.normalize_credential(cookie),
                         "abcdef0123456789abcdef")

    def test_classify_token_and_session(self):
        self.assertEqual(trae.classify_credential(make_jwt()), "TOKEN")
        self.assertEqual(trae.classify_credential("abcdef0123456789abcdef"),
                         "SESSION")
        self.assertEqual(trae.classify_credential("short"), "UNKNOWN")

    def test_jwt_helpers(self):
        token = make_jwt("9988776655443322")
        self.assertEqual(trae.jwt_uid(token), "9988776655443322")
        self.assertIsNotNone(trae.jwt_expiry(token))
        self.assertFalse(trae._token_expired(token))
        expired = make_jwt(expires_in=-3600)
        self.assertTrue(trae._token_expired(expired))

    def test_region_from_auth_prefers_structured_fields(self):
        self.assertEqual(trae._region_from_auth({
            "userRegion": {"region": "SG", "_aiRegion": "SG"},
            "host": "https://growsg-normal.trae.ai",
        }), "SG")
        self.assertEqual(trae._region_from_auth({"userRegion": "SG"}), "SG")
        self.assertEqual(trae._region_from_auth({
            "host": "https://api.trae.cn",
        }), "CN")
        self.assertEqual(trae._region_from_auth({}), "")

    def test_extract_session_from_json_and_leveldb_bytes(self):
        text = '"X-Cloudide-Session":"abcdef0123456789abcdef"'
        self.assertEqual(trae._extract_session(text),
                         "abcdef0123456789abcdef")
        # leveldb 常见形式：键与值之间夹着不可打印字节
        raw = b"X-Cloudide-Session\x00\x01abcdef0123456789abcdef"
        self.assertEqual(trae._extract_session(raw.decode("latin-1")),
                         "abcdef0123456789abcdef")


class StoreTest(TraeTestBase):

    def test_add_public_view_has_no_secret(self):
        self.add()
        store = trae.load_store()
        self.assertEqual(len(store["accounts"]), 1)
        public = trae.public_account(store["accounts"][0], store)
        self.assertNotIn("token", public)
        self.assertNotIn("session", public)
        self.assertTrue(public["has_token"])
        self.assertEqual(public["account_uid"], "1234567890123456")
        self.assertEqual(public["region"], "CN")
        self.assertEqual(public["region_label"], "国内")
        self.assertEqual(public["region_kind"], "cn")

    @unittest.skipUnless(os.name == "nt", "DPAPI 只在 Windows 上可用")
    def test_store_is_encrypted_at_rest(self):
        self.add()
        token = trae.load_store()["accounts"][0]["token"]
        with open(trae.store_path(), "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn(token, raw)
        self.assertIn("dpapi:", raw)
        self.assertEqual(trae.load_store()["accounts"][0]["token"], token)

    def test_duplicate_account_is_updated_by_uid(self):
        first = self.add(name="旧名")
        second = self.add(name="新名")

        self.assertEqual(first["id"], second["id"])
        store = trae.load_store()
        self.assertEqual(len(store["accounts"]), 1)
        self.assertEqual(store["accounts"][0]["name"], "新名")

    def test_device_ids_are_unique_and_renewable(self):
        one = self.add(uid="1111111111111111", name="一号")
        two = self.add(uid="2222222222222222", name="二号")
        self.assertNotEqual(one["device_id_masked"], two["device_id_masked"])

        store = trae.load_store()
        before = store["accounts"][0]["device_id"]
        result = trae.renew_device(store["accounts"][0]["id"])
        self.assertTrue(result["ok"])
        after = trae.load_store()["accounts"][0]["device_id"]
        self.assertNotEqual(before, after)
        self.assertRegex(after, r"^\d{16}$")

    def test_invalid_stored_device_id_is_regenerated(self):
        self.add()
        store = trae.load_store()
        store["accounts"][0]["device_id"] = "12345678"
        trae.save_store(store)

        reloaded = trae.load_store()

        self.assertRegex(reloaded["accounts"][0]["device_id"], r"^\d{16}$")

    def test_public_account_marks_international_region(self):
        self.add()
        store = trae.load_store()
        store["accounts"][0]["region"] = "SG"
        store["accounts"][0]["host"] = "https://growsg-normal.trae.ai"
        trae.save_store(store)

        public = trae.public_account(trae.load_store()["accounts"][0],
                                     trae.load_store())

        self.assertEqual(public["region"], "SG")
        self.assertEqual(public["region_label"], "国际")
        self.assertEqual(public["region_kind"], "intl")
        self.assertEqual(public["host"], "https://growsg-normal.trae.ai")

    def test_remove_and_active_switch(self):
        one = self.add(uid="1111111111111111", name="一号")
        two = self.add(uid="2222222222222222", name="二号")

        result = trae.set_active(two["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(trae.load_store()["active_id"], two["id"])

        result = trae.remove_account(two["id"])
        self.assertTrue(result["ok"])
        store = trae.load_store()
        self.assertEqual(len(store["accounts"]), 1)
        self.assertEqual(store["active_id"], one["id"])

    def test_update_account_and_settings_validation(self):
        account = self.add()
        result = trae.update_account(account["id"], name="备注", is_member=True)
        self.assertTrue(result["ok"])
        self.assertTrue(trae.load_store()["accounts"][0]["is_member"])

        bad = trae.update_settings({"auto_time": "25:99"})
        self.assertFalse(bad["ok"])
        good = trae.update_settings({"auto_time": "09:30",
                                     "interval_seconds": 3,
                                     "usage_days": 14})
        self.assertTrue(good["ok"])
        self.assertEqual(good["settings"]["auto_time"], "09:30")
        self.assertEqual(good["settings"]["usage_days"], 14)


class CheckinTest(TraeTestBase):

    def test_refresh_records_status_and_remaining(self):
        account = self.add()
        self._scenario["status"] = {
            "code": 0, "checked_in": True,
            "credits": 150, "extra_credits": 50, "message": "ok",
        }
        self._scenario["remaining"] = 800.5

        result = trae.refresh_account(account["id"])

        self.assertEqual(result["result"], "OK")
        self.assertEqual(result["checked"], 1)
        row = result["accounts"][0]
        self.assertEqual(row["result"], "ALREADY")
        self.assertEqual(row["credits"], 150)
        self.assertEqual(row["remaining"], 800.5)
        history = trae.history_summary(account["id"])
        self.assertTrue(history["today_checked"])
        self.assertEqual(history["today_credits"], 150)

    def test_member_extra_credits_counted(self):
        account = self.add()
        trae.update_account(account["id"], is_member=True)
        self._scenario["status"] = {
            "code": 0, "checked_in": True,
            "credits": 150, "extra_credits": 50,
        }
        self._scenario["remaining"] = 10

        result = trae.refresh_account(account["id"])
        self.assertEqual(result["accounts"][0]["credits"], 200)
        self.assertEqual(trae.history_summary(account["id"])["today_credits"],
                         200)

    def test_auto_claims_once_then_uses_local_record(self):
        self.add()
        self._scenario["status"] = [
            {"code": 0, "checked_in": False},
            {"code": 0, "checked_in": True,
             "credits": 150, "extra_credits": 50},
        ]
        self._scenario["claim"] = {"code": 0, "message": "ok"}
        self._scenario["remaining"] = 1200

        first = trae.run_auto()
        self.assertEqual(first["result"], "OK")
        self.assertEqual(first["accounts"][0]["result"], "CLAIMED")
        self.assertEqual(first["total_gained"], 150)
        self.assertEqual(first["checked"], 1)
        self.assertEqual(first["remaining_total"], 1200)

        second = trae.run_auto()
        self.assertEqual(second["result"], "ALREADY")
        self.assertEqual(second["total_gained"], 0)

    def test_auth_failure_without_session_is_explicit(self):
        self.add()
        self._scenario["status"] = {"code": 9074, "message": "风控"}
        result = trae.run_auto()
        self.assertEqual(result["result"], "ERROR")
        self.assertIn("会话 Cookie", result["accounts"][0]["message"])

    def test_renew_from_session(self):
        token = make_jwt(expires_in=-3600)
        self._scenario["renewed"] = make_jwt("7777777777777777")
        result = trae.add_account(
            "X-Cloudide-Session=abcdef0123456789abcdef", name="续期号")
        # add_account 会先把会话换成 Token；公开视图只暴露掩码
        self.assertTrue(result["ok"])
        self.assertTrue(result["account"]["has_token"])
        self.assertTrue(result["account"]["token_masked"])
        self.assertNotIn("token", result["account"])

    def test_record_checkin_keeps_larger_credit_per_day(self):
        account = self.add()
        now = datetime.now()
        trae.record_checkin(account, now, 100, False)
        trae.record_checkin(account, now, 150, False)
        trae.record_snapshot(account["id"], 500)
        trae.record_snapshot(account["id"], 480)

        history = trae.history_summary(account["id"])
        self.assertEqual(len(history["records"]), 1)
        self.assertEqual(history["records"][0]["credits"], 150)
        self.assertEqual(history["today_credits"], 150)
        self.assertEqual(history["streak"], 1)


class UsageTest(TraeTestBase):

    def _usage_item(self):
        return {
            "session_id": "s-1",
            "usage_time": int(time.time() * 1000),
            "model_name": "claude-sonnet",
            "mode": "agent",
            "credits_float": 12.5,
            "cost_money_float": 0.01,
            "user_input_preview": "写一个登录页",
            "extra_info": {
                "input_token": 1000,
                "output_token": 500,
                "cache_read_token": 300,
                "cache_write_token": 20,
            },
        }

    def test_run_usage_merges_and_summarizes(self):
        account = self.add()
        self._scenario["usage"] = {
            "total": 1,
            "user_usage_group_by_sessions": [self._usage_item()],
        }
        self._scenario["expired"] = [{
            "name": "签到积分",
            "credits_limit": 100,
            "expire_time_ms": int(time.time() * 1000) + 86400000,
        }]

        result = trae.run_usage(account["id"], days=7)

        self.assertEqual(result["result"], "OK")
        self.assertEqual(result["sessions_count"], 1)
        self.assertEqual(result["credits"], 12.5)
        self.assertEqual(result["input_token"], 1000)
        self.assertEqual(result["output_token"], 500)
        self.assertEqual(result["cache_rate"], 23.1)
        self.assertEqual(result["models"][0]["model"], "claude-sonnet")
        self.assertGreater(result["sessions"][0]["usage_time"],
                           10_000_000_000)
        self.assertEqual(result["expiring"][0]["name"], "签到积分")

        # 再拉一次同一条会话：只更新，不重复计数
        trae.run_usage(account["id"], days=7)
        summary = trae.usage_summary(account["id"], 7)
        self.assertEqual(summary["sessions_count"], 1)
        self.assertEqual(summary["credits"], 12.5)

    def test_run_usage_without_account(self):
        result = trae.run_usage()
        self.assertEqual(result["result"], "NO_ACCOUNT")


class ClientScanTest(TraeTestBase):

    def test_scan_client_dir_reads_token_session_and_device(self):
        root = os.path.join(os.environ["APPDATA"], "Trae CN")
        storage_dir = os.path.join(root, "User", "globalStorage")
        leveldb = os.path.join(root, "Local Storage", "leveldb")
        os.makedirs(storage_dir)
        os.makedirs(leveldb)
        token = make_jwt()
        with open(os.path.join(storage_dir, "storage.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({
                "device": "iCubeAuthInfo://icube-dc:1234567890123456",
                "cookie": "X-Cloudide-Session=abcdef0123456789abcdef",
                "token": token,
            }, fh)
        with open(os.path.join(leveldb, "000003.log"), "wb") as fh:
            fh.write(("junk " + token).encode())

        clients = trae.find_client_dirs()
        self.assertEqual(len(clients), 1)
        result = trae._scan_client_dir(clients[0])
        self.assertTrue(result["ok"])
        self.assertEqual(result["device_id"], "1234567890123456")
        self.assertEqual(result["session"], "abcdef0123456789abcdef")
        self.assertEqual(result["token"], token)

    def test_scan_client_dir_decrypts_icube_auth(self):
        root = os.path.join(os.environ["APPDATA"], "Trae CN")
        storage_dir = os.path.join(root, "User", "globalStorage")
        os.makedirs(storage_dir)
        with open(os.path.join(storage_dir, "storage.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({
                "iCubeAuthInfo://icube-dc:1234567890123456": "device",
                "iCubeAuthInfo://icube.cloudide": ICUBE_FIXTURE,
            }, fh)

        result = trae._scan_client_dir(trae.find_client_dirs()[0])

        self.assertTrue(result["ok"])
        self.assertEqual(result["token"], ICUBE_TOKEN)
        self.assertEqual(result["account_uid"], "1234567890123456")
        self.assertEqual(result["device_id"], "1234567890123456")
        self.assertEqual(result["screen_name"], "本地测试号")
        self.assertEqual(result["mobile_masked"], "138****0000")
        self.assertEqual(result["avatar_url"],
                         "https://example.com/avatar.png")
        self.assertEqual(result["region"], "CN")

    def test_scan_client_dir_marks_trae_client_as_international(self):
        root = os.path.join(os.environ["APPDATA"], "TRAE")
        storage_dir = os.path.join(root, "User", "globalStorage")
        os.makedirs(storage_dir)
        token = make_jwt("5555555555555555")
        with open(os.path.join(storage_dir, "storage.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"token": token}, fh)

        result = trae._scan_client_dir(trae.find_client_dirs()[0])

        self.assertTrue(result["ok"])
        self.assertEqual(result["region"], "SG")
        self.assertEqual(result["client_name"], "TRAE")

    def test_import_deduplicates_same_uid_and_prefers_trae_cn(self):
        token = make_jwt()
        for client in ("TRAE", "Trae CN"):
            storage_dir = os.path.join(
                os.environ["APPDATA"], client, "User", "globalStorage")
            os.makedirs(storage_dir)
            with open(os.path.join(storage_dir, "storage.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"token": token}, fh)

        result = trae.import_local()

        self.assertEqual(result["result"], "OK")
        self.assertEqual(result["added"], 1)
        store = trae.load_store()
        self.assertEqual(len(store["accounts"]), 1)
        self.assertTrue(store["accounts"][0]["source"].startswith("Trae CN"))
        self.assertEqual(store["accounts"][0]["region"], "CN")

    def test_scan_without_login_reports_actionable_error(self):
        root = os.path.join(os.environ["APPDATA"], "Trae CN")
        os.makedirs(os.path.join(root, "User", "globalStorage"))
        result = trae._scan_client_dir({
            "name": "Trae CN", "path": root,
            "storage": False, "leveldb": False,
        })
        self.assertFalse(result["ok"])
        self.assertIn("登录", result["message"])

    def test_device_id_requires_sixteen_digits(self):
        self.assertIsNone(
            trae._extract_device_id("iCubeAuthInfo://icube-dc:12345678"))
        self.assertIsNone(
            trae._extract_device_id("iCubeAuthInfo://icube-dc:12345678901234567"))
        self.assertEqual(
            trae._extract_device_id("iCubeAuthInfo://icube-dc:1234567890123456"),
            "1234567890123456")

    def test_import_replaces_invalid_device_id(self):
        root = os.path.join(os.environ["APPDATA"], "Trae CN")
        storage_dir = os.path.join(root, "User", "globalStorage")
        os.makedirs(storage_dir)
        token = make_jwt()
        with open(os.path.join(storage_dir, "storage.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({
                "device": "iCubeAuthInfo://icube-dc:12345678",
                "token": token,
            }, fh)

        result = trae.import_local()

        self.assertEqual(result["result"], "OK")
        store = trae.load_store()
        self.assertRegex(store["accounts"][0]["device_id"], r"^\d{16}$")
        self.assertNotEqual(store["accounts"][0]["device_id"], "12345678")


class MonitorLogTest(TraeTestBase):

    def test_monitor_empty_and_with_account(self):
        empty = trae.monitor()
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["doctor"]["result"], "NO_ACCOUNT")
        self.assertIsNone(empty["usage"])

        account = self.add()
        self._scenario["status"] = {"code": 0, "checked_in": False}
        self._scenario["remaining"] = 321
        data = trae.monitor()
        self.assertEqual(len(data["accounts"]), 1)
        self.assertEqual(data["active_id"], account["id"])
        self.assertEqual(data["history"]["today"], datetime.now()
                         .strftime("%Y-%m-%d"))

    def test_deep_monitor_does_not_deadlock(self):
        self.add()
        self._scenario["status"] = {"code": 0, "checked_in": True,
                                    "credits": 10}
        self._scenario["remaining"] = 99
        data = trae.monitor(deep=True)
        self.assertTrue(data["ok"])
        self.assertEqual(data["refresh"]["result"], "OK")
        self.assertEqual(data["accounts"][0]["last_checkin"],
                         datetime.now().strftime("%Y-%m-%d"))

    def test_log_redacts_sensitive_and_round_trips(self):
        account = self.add()
        token = trae.load_store()["accounts"][0]["token"]
        trae._sensitive.add(token)

        trae.append_log({"result": "OK", "token": token})
        log = trae.get_log()
        self.assertTrue(log["at"])
        self.assertIn("[REDACTED]", log["lines"][-1])
        self.assertNotIn(token, log["lines"][-1])

        self.assertTrue(trae.clear_log())
        self.assertEqual(trae.get_log()["lines"], [])
        self.assertIsNotNone(account["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
