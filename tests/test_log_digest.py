"""log-digest skill のテスト。実行: .venv/bin/python -m unittest discover -s tests"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "log-digest" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from incident_fixture import KNOWN_NOISE_YAML, write_incident_logs  # noqa: E402
from logdigest import cli  # noqa: E402
from logdigest.extract import build_blocks, select_blocks  # noqa: E402
from logdigest.masking import Masker  # noqa: E402
from logdigest.noise import load_entries  # noqa: E402
from logdigest.parsing import (LogFormat, Record, decode_bytes, file_alias,  # noqa: E402
                               parse_file, parse_timestamp)
from logdigest.render import estimate_tokens  # noqa: E402

import yaml  # noqa: E402

CONFIG = REPO / "skills" / "log-digest" / "config"
JST = timezone(timedelta(hours=9))


def default_format() -> LogFormat:
    cfg = yaml.safe_load((CONFIG / "log_formats.yaml").read_text(encoding="utf-8"))
    aliases = {str(k).upper(): str(v).upper() for k, v in cfg["level_aliases"].items()}
    return LogFormat(cfg["formats"][0], cfg["defaults"], aliases)


def default_masker() -> Masker:
    return Masker.from_config(yaml.safe_load((CONFIG / "masking.yaml").read_text(encoding="utf-8")))


class ParsingTest(unittest.TestCase):
    def test_timestamp_variants(self):
        a = parse_timestamp("2026-09-30 10:42:05.907", None, JST)
        b = parse_timestamp("2026-09-30T01:42:05.907Z", None, JST)
        c = parse_timestamp("2026/09/30 10:42:05,907", None, JST)
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(parse_timestamp("2026-09-30 10:42:05", "%Y-%m-%d %H:%M:%S", JST).tzinfo, JST)

    def test_file_alias(self):
        self.assertEqual(file_alias("pms-server.log"), "pms-server")
        self.assertEqual(file_alias("pms-server.log.3"), "pms-server")
        self.assertEqual(file_alias("db.log.gz"), "db")

    def test_decode_cp932(self):
        text, enc = decode_bytes("ログイン成功".encode("cp932"), "auto")
        self.assertEqual((text, enc), ("ログイン成功", "cp932"))

    def test_multiline_levels_and_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.log"
            p.write_text(
                "header line\n"
                "2026-09-30 10:00:00.000 WARNING [a] first\n"
                "2026-09-30 10:00:01.000 ERROR [b] boom\n"
                "java.io.IOException: x\n"
                "\tat Foo.bar(Foo.java:1)\n"
                "2026-09-30 10:00:02.000 XYZ plain message without component\n",
                encoding="utf-8")
            recs, st = parse_file(p, "x.log", default_format(), 0, JST, clock_offset_seconds=-2)
        self.assertEqual([r.level for r in recs], ["WARN", "ERROR", "INFO"])
        self.assertEqual(recs[1].extra, ["java.io.IOException: x", "\tat Foo.bar(Foo.java:1)"])
        self.assertTrue(recs[1].has_stack and recs[1].is_trigger)
        self.assertFalse(recs[0].is_trigger)
        self.assertEqual(recs[2].component, "")
        self.assertEqual(st.unparsed_lines, 1)
        self.assertEqual(st.unknown_levels, {"XYZ": 1})
        self.assertEqual(recs[0].ts, datetime(2026, 9, 30, 9, 59, 58))


class MaskingTest(unittest.TestCase):
    def test_consistent_tokens(self):
        m = default_masker()
        a = m.mask('Job received user=tanaka host=PC-SALES-012 document="見積書.xlsx"')
        b = m.mask("ログイン成功 ユーザー：Tanaka mail=tanaka@corp-a.co.jp from 10.1.2.3")
        self.assertEqual(a, 'Job received user=<USER_001> host=<HOST_001> document="<DOC_001>"')
        self.assertEqual(b, "ログイン成功 ユーザー：<USER_001> mail=<MAIL_001> from <IP_001>")

    def test_hosts_and_paths(self):
        m = default_masker()
        out = m.mask(r"LDAP referral ignored: ldap://dc2.corp-a.local open \\FS01\share C:\Users\suzuki\a.txt")
        self.assertNotIn("corp-a", out)
        self.assertNotIn("FS01", out)
        self.assertNotIn("suzuki", out)
        # 置換済みの記号を二重に置き換えない
        self.assertEqual(m.mask("host=<HOST_001>"), "host=<HOST_001>")

    def test_mapping(self):
        m = default_masker()
        m.mask("user=tanaka")
        self.assertEqual(m.mapping(), {"USER": {"<USER_001>": "tanaka"}})


class NoiseTest(unittest.TestCase):
    def test_reason_required(self):
        with self.assertRaises(ValueError):
            load_entries([{"template": "Foo <*>"}])

    def test_match_and_versions(self):
        e = load_entries(yaml.safe_load(KNOWN_NOISE_YAML))[0]
        self.assertTrue(e.matches("LDAP referral ignored: <URL>"))
        self.assertTrue(e.matches("LDAP  referral ignored: <*>"))
        self.assertFalse(e.matches("LDAP referral failed: <URL>"))
        self.assertTrue(e.applies_to("5.2.3"))
        self.assertFalse(e.applies_to("6.0.0"))


class BlockTest(unittest.TestCase):
    def _recs(self, levels):
        out = []
        for i, lv in enumerate(levels):
            r = Record("f.log", "f", 0, i + 1, datetime(2026, 1, 1) + timedelta(seconds=i), lv, "", f"m{i}")
            r.template_key = 1 if lv == "ERROR" else 0
            out.append(r)
        return out

    def test_merge_and_cap(self):
        recs = self._recs(["INFO"] * 5 + ["ERROR"] + ["INFO"] * 2 + ["ERROR"] + ["INFO"] * 20 + ["ERROR"])
        blocks = build_blocks(recs, 2, 2, 60)
        self.assertEqual([(b.records[0].lineno, b.records[-1].lineno) for b in blocks], [(4, 11), (28, 30)])
        capped = build_blocks(recs, 2, 2, 5)
        for b in capped:
            self.assertLessEqual(len(b.records), 5)
        # 塊どうしが重ならない
        lines = [r.lineno for b in capped for r in b.records]
        self.assertEqual(len(lines), len(set(lines)))

    def test_select_skips_repeats(self):
        recs = self._recs(["ERROR", "INFO", "INFO", "INFO", "INFO", "INFO", "ERROR"])
        blocks = build_blocks(recs, 1, 1, 60)
        self.assertEqual(len(blocks), 2)
        self.assertEqual([b.id for b in select_blocks(blocks, 10)], ["E01"])


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.logs = write_incident_logs(base / "logs")
        cls.noise = base / "known_noise.yaml"
        cls.noise.write_text(KNOWN_NOISE_YAML, encoding="utf-8")
        cls.out = base / "out"
        cls.out2 = base / "out2"
        cls.mask_map = base / "secret" / "mask_map.json"
        cls.stdout = cls._run(cls.out, "--save-mask-map", str(cls.mask_map))
        cls._run(cls.out2)
        cls.digest = (cls.out / "digest_INC-2026-0931.md").read_text(encoding="utf-8")
        cls.context = (cls.out / "context.md").read_text(encoding="utf-8")
        cls.tsv = (cls.out / "templates.tsv").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _run(cls, out: Path, *extra: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.run([
                "--ticket", "INC-2026-0931", "--incident-time", "2026-09-30 10:40",
                "--symptom", "10:40頃から印刷ジョブが出力されない（user=tanaka から申告）",
                "--product", "PMS", "--product-version", "5.2.3", "--os", "Windows Server 2019",
                "--logs", str(cls.logs), "--known-noise", str(cls.noise), "--out", str(out), *extra])
        assert rc == 0
        return buf.getvalue()

    def test_first_error_is_root_symptom(self):
        section = self.digest.split("## 最初に出た ERROR")[1].split("##")[0]
        first = [ln for ln in section.splitlines() if ln.startswith("10:")][0]
        self.assertIn("10:42:05.907", first)
        self.assertIn("Spool write failed", first)

    def test_noise_excluded_and_reported(self):
        self.assertIn("## 既知ノイズとして除外", self.digest)
        self.assertIn("LDAP referral ignored", self.digest.split("## 既知ノイズとして除外")[1])
        before_noise = self.digest.split("## 既知ノイズとして除外")[0]
        self.assertNotIn("LDAP referral", before_noise)
        self.assertNotIn("SNMP trap", before_noise)
        self.assertNotIn("SNMP trap", self.context)

    def test_template_table_shows_trend(self):
        row = [ln for ln in self.digest.splitlines() if "Spool usage" in ln][0]
        # 窓内は1件だが、全期間では 09-28 から出ている
        self.assertIn("09-28 03:00:10", row)

    def test_templates_group_across_users(self):
        logins = [ln for ln in self.tsv.splitlines() if "ログイン成功" in ln]
        self.assertEqual(len(logins), 1, logins)

    def test_no_customer_data_in_outputs(self):
        secrets = ["tanaka", "suzuki", "yamada", "PC-SALES", "PC-ACC", "corp-a", "見積書", "議事録",
                   "請求書", "192.168.", "10.20.30.40"]
        for f in self.out.iterdir():
            text = f.read_text(encoding="utf-8").lower()
            for s in secrets:
                self.assertNotIn(s.lower(), text, f"{s} in {f.name}")
        mapping = json.loads(self.mask_map.read_text(encoding="utf-8"))
        self.assertIn("tanaka", mapping["USER"].values())

    def test_within_budget_and_deterministic(self):
        self.assertLessEqual(estimate_tokens(self.digest), 5000)
        for name in ("digest_INC-2026-0931.md", "context.md", "templates.tsv"):
            self.assertEqual((self.out / name).read_bytes(), (self.out2 / name).read_bytes(), name)

    def test_files_and_warnings(self):
        self.assertNotIn("README", self.digest)
        self.assertIn("auth.log | cp932", self.digest)
        meta = json.loads((self.out / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(len(meta["files"]), 4)
        self.assertIn("digest:", self.stdout)

    def test_budget_shrinks(self):
        out = Path(self.tmp.name) / "small"
        self._run(out, "--max-tokens", "1200")
        digest = (out / "digest_INC-2026-0931.md").read_text(encoding="utf-8")
        self.assertLess(estimate_tokens(digest), estimate_tokens(self.digest))
        self.assertIn("Spool write failed", digest)

    def test_empty_window_warns(self):
        out = Path(self.tmp.name) / "empty"
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.run(["--ticket", "X", "--incident-time", "2026-10-10 10:00", "--logs", str(self.logs),
                     "--out", str(out), "--no-known-noise"])
        digest = (out / "digest_X.md").read_text(encoding="utf-8")
        self.assertIn("時間窓内にログが1件もない", digest)


if __name__ == "__main__":
    unittest.main()
