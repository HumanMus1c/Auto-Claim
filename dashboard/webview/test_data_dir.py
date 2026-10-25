"""控制台运行期数据目录的回归测试。

这组测试保护两件事：

  - 默认目录已从 WorkBuddyDashboard 改名为 Auto-Claim\\dashboard，
    与 Trae 的 %APPDATA%\\Auto-Claim\\trae 保持同一套命名；
  - 旧目录在首次启动时自动迁移，而且迁移失败时不能把日志和窗口位置
    分叉到新旧两份里——失败就继续用旧目录，下次启动再试。
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app  # noqa: E402


class DataDirTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wbdata-")
        self._saved_local = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = self.root
        self.legacy = os.path.join(self.root, "WorkBuddyDashboard")
        self.new = os.path.join(self.root, "Auto-Claim", "dashboard")

    def tearDown(self):
        if self._saved_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._saved_local
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, path, text="x"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_default_path_is_branded(self):
        path = app._diag_dir()
        self.assertEqual(os.path.normcase(path), os.path.normcase(self.new))
        self.assertTrue(os.path.isdir(path))

    def test_legacy_dir_is_migrated(self):
        self.write(os.path.join(self.legacy, "growth.json"), '{"ok": true}')
        self.write(os.path.join(self.legacy, "dumps", "hang-1.dmp"), "dump")

        path = app._diag_dir()

        self.assertEqual(os.path.normcase(path), os.path.normcase(self.new))
        self.assertEqual(self.read(os.path.join(self.new, "growth.json")),
                         '{"ok": true}')
        self.assertEqual(self.read(os.path.join(self.new, "dumps", "hang-1.dmp")),
                         "dump")
        self.assertFalse(os.path.exists(self.legacy))

    def test_existing_new_dir_wins_and_legacy_is_left_alone(self):
        self.write(os.path.join(self.new, "run.log"), "new")
        self.write(os.path.join(self.legacy, "run.log"), "old")

        path = app._diag_dir()

        self.assertEqual(os.path.normcase(path), os.path.normcase(self.new))
        self.assertEqual(self.read(os.path.join(self.new, "run.log")), "new")
        self.assertEqual(self.read(os.path.join(self.legacy, "run.log")), "old")

    def test_copy_fallback_when_rename_is_locked(self):
        self.write(os.path.join(self.legacy, "run.log"), "old")
        real_replace = os.replace

        def replace(src, dst):
            if os.path.abspath(src) == os.path.abspath(self.legacy):
                raise OSError("locked by old instance")
            return real_replace(src, dst)

        with mock.patch.object(app.os, "replace", side_effect=replace):
            path = app._diag_dir()

        self.assertEqual(os.path.normcase(path), os.path.normcase(self.new))
        self.assertEqual(self.read(os.path.join(self.new, "run.log")), "old")
        self.assertFalse(os.path.exists(self.legacy))

    def test_failed_migration_keeps_using_legacy_dir(self):
        self.write(os.path.join(self.legacy, "run.log"), "old")

        with mock.patch.object(app.os, "replace", side_effect=OSError("locked")), \
                mock.patch.object(app.shutil, "copytree",
                                  side_effect=OSError("locked")):
            path = app._diag_dir()

        # 迁移失败时继续用旧目录，而不是把新日志写进半套新目录。
        self.assertEqual(os.path.normcase(path), os.path.normcase(self.legacy))
        self.assertFalse(os.path.isdir(self.new))


if __name__ == "__main__":
    unittest.main(verbosity=2)
