"""log-knowledge skill のテスト。実行: .venv/bin/python -m unittest discover -s tests"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "log-digest" / "scripts"))
sys.path.insert(0, str(REPO / "skills" / "log-knowledge" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from incident_fixture import write_incident_logs  # noqa: E402
from logdigest import cli as digest_cli  # noqa: E402
from logknowledge import cli  # noqa: E402
from logknowledge.noise import (default_versions, literal_hint, parse_periods,  # noqa: E402
                                validate_dictionary)


def _run(*argv: str):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.run(list(argv))
    return rc, out.getvalue() + err.getvalue()


class UnitTest(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(default_versions("5.2.3"), ["5.2.*"])
        self.assertEqual(default_versions("6.0"), ["6.0"])
        self.assertIsNone(default_versions(None))
        self.assertEqual(literal_hint("Job <JOBID> aborted: IOException at <*>"), "aborted: IOException at")
        day, rng = parse_periods(["2026-09-16", "2026/09/16 09:00/2026/09/18 18:00"])
        self.assertEqual((day.hours, rng.hours), (24, 57))
        with self.assertRaises(ValueError):
            parse_periods(["last week"])

    def test_validate_dictionary(self):
        ok = {"template": "LDAP referral ignored: <*>", "versions": ["5.2.*"], "reason": "影響なし",
              "generated_by": "AI（log-knowledge）",
              "evidence": {"source": "再現環境", "rate": "1時間あたり12件", "code": "a.java:1"}}
        self.assertEqual(validate_dictionary([ok]), [])
        no_code = dict(ok, evidence={"source": "再現環境", "rate": "x"})
        self.assertIn("evidence.code", validate_dictionary([no_code])[0])
        self.assertIn("固定部分が短すぎ", validate_dictionary([dict(ok, template="<*> failed")])[0])
        self.assertIn("重複", validate_dictionary([ok, ok])[0])
        self.assertIn("reason", validate_dictionary([{"template": "Something happened <*>"}])[0])


class NoiseFlowTest(unittest.TestCase):
    """架空ログを再現環境のログとみなし、候補 → AI の判断 → 登録 → log-digest で利用、を通す。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        logs = write_incident_logs(base / "logs")
        cls.repro = base / "repro"
        with redirect_stdout(io.StringIO()):
            # 再現環境のログ: 最後の時刻を「障害時刻」にして log-digest にかける（平常期間の候補を出させる）
            assert digest_cli.run(["--ticket", "NOISE-5.2.3", "--incident-time", "2026-09-30 11:59",
                                   "--product-version", "5.2.3", "--no-known-noise",
                                   "--logs", str(logs), "--out", str(cls.repro)]) == 0
        cls.logs = logs
        cls.kdir = base / "knowledge"
        cls.kdir.mkdir()
        for name in ("known_noise.yaml", "known_noise_rejected.yaml"):
            shutil.copy(REPO / "knowledge" / name, cls.kdir / name)
        cls.dict = cls.kdir / "known_noise.yaml"
        cls.rejected = cls.kdir / "known_noise_rejected.yaml"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _files(self):
        return ["--dict", str(self.dict), "--rejected", str(self.rejected)]

    def _candidates(self, *extra: str, out_file: str = "noise_candidates.yaml"):
        path = self.repro / out_file
        rc, out = _run("noise-candidates", "--dir", str(self.repro), "--source", "再現環境 PMS 5.2.3（架空）",
                       *self._files(), "--out", str(path), *extra)
        self.assertEqual(rc, 0, out)
        return yaml.safe_load(path.read_text(encoding="utf-8")), out

    def test_flow(self):
        data, out = self._candidates()
        # 変化点（9/24）より前の日だけを正常期間にする。9/24 からの「Temp cleanup skipped」は候補にならない
        self.assertEqual(data["normal_periods"], ["2026-09-16 00:00〜2026-09-24 00:00"])
        self.assertIn("再現環境で障害を再現した期間が含まれていないか", out)
        self.assertEqual(data["versions"], ["5.2.*"])
        templates = [c["template"] for c in data["candidates"]]
        self.assertEqual(templates, ["SNMP trap send failed: <IP> timeout", "LDAP referral ignored: <URL>"])
        self.assertEqual(data["candidates"][1]["search_hint"], "LDAP referral ignored")
        self.assertIn("1時間あたり約12.0件", data["candidates"][1]["rate"])

        # 判断していない候補があると反映しない
        cand_file = self.repro / "noise_candidates.yaml"
        before = self.dict.read_text(encoding="utf-8")
        rc, out = _run("noise-merge", "--candidates", str(cand_file), *self._files())
        self.assertEqual(rc, 1)
        self.assertIn("判断していない候補が 2 件", out)
        self.assertEqual(self.dict.read_text(encoding="utf-8"), before)

        # register に code が無いと反映しない
        data["candidates"][1].update(decision="register", reason="referral を無視して処理を続ける。影響なし")
        data["candidates"][0].update(decision="reject", reject_reason="再現環境に特有（監視サーバなし）")
        cand_file.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        rc, out = _run("noise-merge", "--candidates", str(cand_file), *self._files())
        self.assertEqual(rc, 1)
        self.assertIn("register には code が必要", out)

        data["candidates"][1]["code"] = "auth/LdapClient.java:340"
        cand_file.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        rc, out = _run("noise-merge", "--candidates", str(cand_file), *self._files(), "--today", "2026-10-05")
        self.assertEqual(rc, 0, out)
        text = self.dict.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 既知ノイズ辞書"))  # 先頭のコメントを残す
        entries = yaml.safe_load(text)
        self.assertEqual(entries, [{
            "template": "LDAP referral ignored: <URL>", "versions": ["5.2.*"],
            "reason": "referral を無視して処理を続ける。影響なし",
            "evidence": {"source": "再現環境 PMS 5.2.3（架空）",
                         "rate": data["candidates"][1]["rate"], "code": "auth/LdapClient.java:340"},
            "generated_by": "AI（log-knowledge）", "added": "2026-10-05"}])
        rejected = yaml.safe_load(self.rejected.read_text(encoding="utf-8"))
        self.assertEqual([e["template"] for e in rejected], ["SNMP trap send failed: <IP> timeout"])
        self.assertEqual(_run("noise-validate", "--dict", str(self.dict))[0], 0)

        # 登録済み・却下済みは次から候補にしない
        again, _ = self._candidates()
        self.assertEqual(again["candidates"], [])
        self.assertEqual(len(again["skipped"]), 2)

        # log-digest が生成した辞書を読み、LDAP は下に回し、SNMP は通常のログとして扱う
        out_dir = Path(self.tmp.name) / "digest"
        with redirect_stdout(io.StringIO()):
            assert digest_cli.run(["--ticket", "INC-2026-0931", "--incident-time", "2026-09-30 10:40",
                                   "--product-version", "5.2.3", "--logs", str(self.logs),
                                   "--known-noise", str(self.dict), "--out", str(out_dir)]) == 0
        digest = (out_dir / "digest_INC-2026-0931.md").read_text(encoding="utf-8")
        noise = digest.split("## 既知ノイズ（下に回したもの）")[1].split("\n## ")[0]
        self.assertIn("LDAP referral ignored", noise)
        self.assertNotIn("SNMP", noise)

    def test_explicit_normal_period_and_versions(self):
        data, out = self._candidates("--normal", "2026-09-16 09:00/2026-09-16 12:00", "--versions", "5.2.3",
                                     out_file="explicit.yaml")
        self.assertEqual(data["normal_periods"], ["2026-09-16 09:00〜2026-09-16 12:00"])
        self.assertEqual(data["versions"], ["5.2.3"])
        self.assertNotIn("規則で選んだ", out)
        # 3時間では SNMP（20分ごと）も LDAP（5分ごと）も件数・時間帯の条件を満たす
        self.assertEqual(len(data["candidates"]) + len(data["skipped"]), 2)

    def test_missing_dir(self):
        with self.assertRaises(FileNotFoundError):
            cli.run(["noise-candidates", "--dir", str(Path(self.tmp.name) / "none"), "--source", "x"])


if __name__ == "__main__":
    unittest.main()
