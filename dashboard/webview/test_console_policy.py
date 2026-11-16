"""签到流程只由计划任务托管：失败自愈默认不唤起图形控制台。"""
import os
import unittest
from unittest import mock

import app


def _without_switches():
    return {k: v for k, v in os.environ.items()
            if k not in ("WORKBUDDY_SURFACE_CONSOLE", "WORKBUDDY_NO_SURFACE")}


class ConsoleSurfacePolicyTest(unittest.TestCase):

    def test_default_never_surfaces_console(self):
        """无环境变量时也必须跳过，换机器/重装系统不能回到旧行为。"""
        with mock.patch.dict(os.environ, _without_switches(), clear=True):
            self.assertEqual(app._surface_console("workbuddy"), "skipped")
            self.assertEqual(app._surface_console("trae"), "skipped")

    def test_legacy_no_surface_still_skips(self):
        with mock.patch.dict(os.environ, _without_switches(), clear=True):
            os.environ["WORKBUDDY_NO_SURFACE"] = "1"
            self.assertEqual(app._surface_console("workbuddy"), "skipped")

    def test_explicit_opt_in_launches_console(self):
        with mock.patch.dict(os.environ, _without_switches(), clear=True), \
             mock.patch.object(app, "_console_command",
                               return_value=(["console"], os.getcwd())), \
             mock.patch.object(app.subprocess, "Popen") as popen:
            os.environ["WORKBUDDY_SURFACE_CONSOLE"] = "1"
            self.assertEqual(app._surface_console("workbuddy"), "launched")
            self.assertEqual(popen.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
