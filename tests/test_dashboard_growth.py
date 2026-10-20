# -*- coding: utf-8 -*-
"""成长中心快照差分测试。

只测纯函数 _growth_changes()：它决定“数据变动”日志长什么样，
不碰网络、不读写用户目录，跑起来没有副作用。
"""

import importlib.util
import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
