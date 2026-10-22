# -*- coding: utf-8 -*-
"""成长中心快照差分测试。

只测纯函数 _growth_changes()：它决定“数据变动”日志长什么样，
不碰网络、不读写用户目录，跑起来没有副作用。
"""

import importlib.util
import os
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "dashboard", "webview", "app.py")

spec = importlib.util.spec_from_file_location("dashboard_growth_app", APP)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def snapshot():
    return {
        "tasks": [
            {"code": "t1", "title": "签到任务", "status": "not_accepted",
             "current": 0, "target": 1},
            {"code": "t2", "title": "浏览任务", "status": "claimed",
             "current": 3, "target": 3},
        ],
        "travel": {"state": "idle", "location": None, "daily_limit_reached": False},
        "streak": {
            "days": 9,
            "makeup_cards": {"balance": 0},
            "makeup_candidates": [],
        },
        "redeem": {"tiers": [
            {"tier": "7d", "label": "7 天", "status": "available"},
            {"tier": "14d", "label": "14 天", "status": "locked"},
            {"tier": "28d", "label": "28 天", "status": "locked"},
        ]},
        "lottery": {"balance": 0},
        "energy": {"balance": 8},
        "buddy": {"affordable": 0, "cost_per_open": 10},
    }


class GrowthChangesTest(unittest.TestCase):

    def test_first_snapshot_stays_quiet(self):
        self.assertEqual(app._growth_changes({}, snapshot()), [])

    def test_unchanged_snapshot_stays_quiet(self):
        self.assertEqual(app._growth_changes(snapshot(), snapshot()), [])

    def test_reports_growth_center_changes(self):
        old = snapshot()
        new = snapshot()
        new["tasks"][0].update(status="accepted", current=1)
        new["travel"].update(state="traveling", location="大阪",
                             daily_limit_reached=True)
        new["streak"].update(days=10, makeup_cards={"balance": 1},
                             makeup_candidates=["2026-10-19"])
        new["redeem"]["tiers"][0]["status"] = "claimed"
        new["lottery"]["balance"] = 1
        new["energy"]["balance"] = 10
        new["buddy"]["affordable"] = 1

        lines = app._growth_changes(old, new)
        text = "\n".join(lines)
        for keyword in ("签到任务", "待接单 → 进行中",
                        "Buddy 旅行：待命 → 旅行中（大阪）",
                        "连签天数：9 → 10 天",
                        "补登卡：0 → 1 张",
                        "连登兑换 7 天：可兑换 → 已兑换",
                        "抽奖机会：0 → 1 次",
                        "能量：8 → 10",
                        "Buddy 盲盒可开：0 → 1 次"):
            self.assertIn(keyword, text)

    def test_same_label_status_switch_is_not_noise(self):
        """accepted 和 in_progress 都显示“进行中”，不该产生一行空变化。"""
        old = snapshot()
        new = snapshot()
        old["tasks"][0]["status"] = "accepted"
        new["tasks"][0]["status"] = "in_progress"
        self.assertEqual(app._growth_changes(old, new), [])


class SigninBudgetResetTest(unittest.TestCase):
    """回归：长驻进程的旧预算时钟会让人误以为“凭据处理超时”。

    signin 的时间预算记在模块全局。仪表盘进程里上一次刷新的时钟超过 420 秒
    后，_budget_left() 为负，resolve_session() 起解密子进程前就会拒绝并抛
    HELPER_TIMEOUT；如果重置预算写在读凭据之后，这个状态会一直粘住。这里用
    假模块锁住顺序：必须先 _start_budget()，再 resolve_session()。
    """

    def _fake(self, calls):
        fake = types.SimpleNamespace(
            DEFAULT_ENDPOINT="https://example.test",
            find_auth_file=lambda: (__file__, [__file__]),
            load_session_retry=lambda path: {"auth": {}, "account": {}},
            build_headers=lambda session: {},
        )

        def start_budget(*args, **kwargs):
            fake.budget_started = True
            calls.append("budget")

        def resolve_session(session):
            calls.append("resolve")
            self.assertTrue(getattr(fake, "budget_started", False),
                            "resolve_session 在预算重置之前被调用")
            return {"auth": {"endpoint": "https://example.test"},
                    "account": {"uid": "u"}}

        fake._start_budget = start_budget
        fake.resolve_session = resolve_session
        return fake

    def test_growth_resets_budget_before_credentials(self):
        calls = []
        fake = self._fake(calls)
        with mock.patch.object(app, "_load_signin", return_value=fake), \
                mock.patch.object(app, "_growth_snapshot", return_value={"tasks": []}), \
                mock.patch.object(app, "_load_growth_store", return_value={"data": {}}), \
                mock.patch.object(app, "_save_growth_store"):
            result = app.Api.growth(None)
        self.assertTrue(result["ok"], result)
        self.assertEqual(calls[:2], ["budget", "resolve"])

    def test_tasks_resets_budget_before_credentials(self):
        calls = []
        fake = self._fake(calls)
        fake.get = lambda url, headers: (200, {"tasks": []})
        fake.dig = lambda body, key: body.get(key)
        with mock.patch.object(app, "_load_signin", return_value=fake):
            result = app.Api.tasks(None)
        self.assertTrue(result["ok"], result)
        self.assertEqual(calls[:2], ["budget", "resolve"])


if __name__ == "__main__":
    unittest.main()
