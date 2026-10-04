"""tools/sync_skills.py のテスト。コピー先（.github/skills, .kiro/skills）が最新かも確認する。"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import sync_skills  # noqa: E402


class SyncSkillsTest(unittest.TestCase):
    def test_skills_are_valid(self):
        for skill in sync_skills.find_skills():
            self.assertEqual(sync_skills.validate(skill), [], skill.name)

    def test_copies_are_up_to_date(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = sync_skills.main(["--check"])
        self.assertEqual(rc, 0, "skills/ を変更したら python tools/sync_skills.py を実行すること\n" + out.getvalue())


if __name__ == "__main__":
    unittest.main()
