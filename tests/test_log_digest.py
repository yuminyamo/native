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
from logdigest import baseline, cli  # noqa: E402
from logdigest.extract import build_blocks, select_blocks  # noqa: E402
from logdigest.noise import load_entries  # noqa: E402
from logdigest.parsing import (LogFormat, Record, decode_bytes, file_alias,  # noqa: E402
                               parse_file, parse_timestamp)
from logdigest.render import estimate_tokens  # noqa: E402
from logdigest.templating import Template  # noqa: E402

import yaml  # noqa: E402

CONFIG = REPO / "skills" / "log-digest" / "config"
JST = timezone(timedelta(hours=9))


def default_format() -> LogFormat:
    cfg = yaml.safe_load((CONFIG / "log_formats.yaml").read_text(encoding="utf-8"))
    aliases = {str(k).upper(): str(v).upper() for k, v in cfg["level_aliases"].items()}
    return LogFormat(cfg["formats"][0], cfg["defaults"], aliases)


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
        cls.stdout = cls._run(cls.out)
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
                "--symptom", "10:40頃から印刷ジョブが出力されない",
                "--product", "PMS", "--product-version", "5.2.3", "--os", "Windows Server 2019",
                "--logs", str(cls.logs), "--known-noise", str(cls.noise), "--out", str(out), *extra])
        assert rc == 0
        return buf.getvalue()

    def test_first_error_is_root_symptom(self):
        section = self.digest.split("## 最初に出た ERROR")[1].split("##")[0]
        first = [ln for ln in section.splitlines() if ln.startswith("10:")][0]
        self.assertIn("10:42:05.907", first)
        self.assertIn("Spool write failed", first)

    def test_noise_demoted_and_reported(self):
        self.assertIn("## 既知ノイズ（下に回したもの）", self.digest)
        noise = self.digest.split("## 既知ノイズ（下に回したもの）")[1]
        self.assertIn("LDAP referral ignored", noise)
        self.assertIn("窓内 12件、平常期間 12件", noise)
        before_noise = self.digest.split("## 既知ノイズ（下に回したもの）")[0]
        self.assertNotIn("LDAP referral", before_noise)
        self.assertNotIn("SNMP trap", before_noise)
        self.assertNotIn("SNMP trap", self.context)

    def test_baseline_picks_day_before_change(self):
        section = self.digest.split("## 平常時との比較")[1].split("\n## ")[0]
        # 前営業日（9/29）ではなく、変化点（9/24）より前で最も近い営業日を選ぶ
        self.assertIn("平常期間: 09-23（水）10:10〜11:10（自動選択: 09-24（木）以降に変化点がある", section)
        self.assertIn("| 09-23（水） | 1,969 | 72 | ○ | **平常期間** |", section)
        # 土日は件数が少ないので営業日にしない
        self.assertRegex(section, r"\| 09-27（日） \| [\d,]+ \| 72 \|  \|")

    def test_change_points_point_to_root_cause(self):
        section = self.digest.split("### 変化点")[1].split("###")[0]
        rows = [ln for ln in section.splitlines() if ln.startswith("| 09-")]
        self.assertIn("消失", rows[0])
        self.assertIn("Temp cleanup finished", rows[0])
        self.assertIn("8/8日 | 0/7日", rows[0])
        self.assertIn("出現", rows[1])
        self.assertIn("Temp cleanup skipped", rows[1])
        self.assertIn("09-28（月） | 出現", rows[2])
        self.assertIn("Spool usage", rows[2])
        self.assertEqual(len(rows), 3)
        self.assertIn("障害日 09-30（水）に初めて出たテンプレートは 5 種類", section)

    def test_comparison_with_baseline(self):
        section = self.digest.split("### 平常期間の同じ時刻帯との比較")[1].split("\n## ")[0]
        self.assertRegex(section, r"\| 障害時のみ \| T\d+ \| ERROR \| 0 \| 226 \| 10:42:05 \| Spool write failed")
        self.assertNotIn("LDAP", section)

    def test_meta_has_baseline(self):
        meta = json.loads((self.out / "meta.json").read_text(encoding="utf-8"))
        b = meta["baseline"]
        self.assertEqual(b["selection"], "auto")
        self.assertEqual(b["first_change"], "2026-09-24")
        days = {d["date"]: d for d in b["days"]}
        self.assertTrue(days["2026-09-23"]["normal_candidate"])
        self.assertFalse(days["2026-09-24"]["normal_candidate"])
        self.assertFalse(days["2026-09-30"]["normal_candidate"])
        self.assertFalse(days["2026-09-19"]["business"])

    def test_manual_baseline(self):
        out = Path(self.tmp.name) / "manual"
        self._run(out, "--baseline", "2026-09-29", "--baseline-reason", "問い合わせで前日は正常とあるため")
        digest = (out / "digest_INC-2026-0931.md").read_text(encoding="utf-8")
        self.assertIn("平常期間: 09-29（火）10:10〜11:10（指定: 問い合わせで前日は正常とあるため）", digest)
        # 変化点はどの平常期間を選んでも出る
        self.assertIn("Temp cleanup finished", digest.split("### 変化点")[1].split("###")[0])

    def test_manual_baseline_without_reason_warns(self):
        out = Path(self.tmp.name) / "noreason"
        stdout = self._run(out, "--baseline", "2026-09-30 08:00/2026-09-30 10:00")
        self.assertIn("--baseline-reason が無い", stdout)
        digest = (out / "digest_INC-2026-0931.md").read_text(encoding="utf-8")
        self.assertIn("### 平常期間との比較", digest)
        self.assertIn("平常（換算）", digest)

    def test_template_table_shows_trend(self):
        table = self.digest.split("## テンプレート集計")[1].split("\n## ")[0]
        row = [ln for ln in table.splitlines() if "Spool usage" in ln][0]
        # 窓内は1件だが、全期間では 09-28 から出ている
        self.assertIn("09-28 03:00:10", row)

    def test_messages_are_not_rewritten(self):
        # マスキングはログ収集の skill で済んでいる前提なので、本文はそのまま載る
        self.assertIn("host=PC-SALES-012", self.context)
        self.assertIn("Polling server 10.20.30.40:8443 ok", self.context)
        self.assertNotIn("マスキング", self.digest)

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


class BaselineUnitTest(unittest.TestCase):
    CFG = baseline.load_config(yaml.safe_load((CONFIG / "baseline.yaml").read_text(encoding="utf-8")))

    @staticmethod
    def _records(spec):
        """spec: [(datetime, template_key, level)] → 時刻順の Record"""
        out = []
        for i, (ts, key, level) in enumerate(sorted(spec)):
            r = Record("f.log", "f", 0, i + 1, ts, level, "", f"m{key}")
            r.template_key = key
            out.append(r)
        return out

    def _days(self, n, start=datetime(2026, 9, 1)):
        return [start + timedelta(days=i) for i in range(n)]

    def test_config_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            baseline.load_config({"compare": {"surge_ration": 3}})
        with self.assertRaises(ValueError):
            baseline.load_config({"typo": 1})

    def test_parse_specs(self):
        ws, we = datetime(2026, 9, 30, 10, 10), datetime(2026, 9, 30, 11, 10)
        day, rng = baseline.parse_baseline_specs(["2026-09-23", "2026/09/30 08:00/2026/09/30 09:30"], ws, we, JST)
        self.assertEqual((day.start, day.end), (datetime(2026, 9, 23, 10, 10), datetime(2026, 9, 23, 11, 10)))
        self.assertEqual((rng.start, rng.end), (datetime(2026, 9, 30, 8, 0), datetime(2026, 9, 30, 9, 30)))
        for bad in ["yesterday", "2026-09-30 10:00/2026-09-30 09:00"]:
            with self.assertRaises(ValueError):
                baseline.parse_baseline_specs([bad], ws, we, JST)

    def test_change_points_need_history_and_persistence(self):
        spec = []
        for i, d in enumerate(self._days(10)):
            spec.append((d + timedelta(hours=1), 1, "INFO"))        # 毎日出る（変化なし）
            spec.append((d + timedelta(hours=23, minutes=30), 1, "INFO"))
            if i < 6:
                spec.append((d + timedelta(hours=3), 2, "INFO"))    # 7日目から消える
            if i >= 6:
                spec.append((d + timedelta(hours=3), 3, "WARN"))    # 7日目から出る
            if i == 4:
                spec.append((d + timedelta(hours=5), 4, "ERROR"))   # 1回だけ（出現にしない）
            if i == 1:
                spec.append((d + timedelta(hours=5), 5, "WARN"))    # 2日目から出る（履歴が短いので判定しない）
                spec.append((d + timedelta(days=1, hours=5), 5, "WARN"))
        recs = self._records(spec)
        incident = datetime(2026, 9, 10, 12, 0)
        days, per = baseline.daily_profile(recs, incident, self.CFG)
        cps = {(c.key, c.kind): c for c in baseline.find_change_points(days, per, self.CFG)}
        self.assertEqual(set(cps), {(2, "消失"), (3, "出現")})
        self.assertEqual(cps[(2, "消失")].day, datetime(2026, 9, 7).date())
        self.assertEqual((cps[(3, "出現")].before_days, cps[(3, "出現")].after_present), (6, 4))

    def test_vanish_only_on_incident_day_is_left_to_comparison(self):
        spec = [(d + timedelta(hours=h), 1, "INFO") for d in self._days(6) for h in (1, 23)]
        spec += [(d + timedelta(hours=22), 2, "INFO") for d in self._days(5)]  # 障害日だけ無い（夜の処理）
        recs = self._records(spec)
        days, per = baseline.daily_profile(recs, datetime(2026, 9, 6, 12), self.CFG)
        self.assertEqual(baseline.find_change_points(days, per, self.CFG), [])

    def test_noise_suspended_on_surge(self):
        spec = [(datetime(2026, 9, d, 10, m), 1, "WARN") for d in (1, 2, 3, 4) for m in (0, 30)]
        spec += [(datetime(2026, 9, d, h), 9, "INFO") for d in (1, 2, 3, 4) for h in (0, 23)]
        spec += [(datetime(2026, 9, 5, 10, m), 1, "WARN") for m in range(0, 50, 2)]
        recs = self._records(spec)
        tpl = {1: Template("T01", "noise <*>", 1), 9: Template("T02", "other", 9)}
        tpl[1].noise_reason = "平常時から出る"
        ws, we = datetime(2026, 9, 5, 9, 30), datetime(2026, 9, 5, 10, 30)
        for r in recs:
            if ws <= r.ts <= we:
                tpl[r.template_key].add_window(r)
        res = baseline.build(recs, tpl, datetime(2026, 9, 5, 10), ws, we, self.CFG)
        self.assertEqual(res.selection, "auto")
        self.assertEqual([p.start for p in res.periods], [datetime(2026, 9, 4, 9, 30)])
        self.assertEqual([(c.kind, c.key) for c in res.comparisons], [("急増", 1)])
        self.assertFalse(tpl[1].is_noise)
        self.assertIn("2 件から 16 件に急増", tpl[1].noise_suspended)


if __name__ == "__main__":
    unittest.main()
