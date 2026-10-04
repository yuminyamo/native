"""log-search skill のテスト。実行: .venv/bin/python -m unittest discover -s tests"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "log-digest" / "scripts"))
sys.path.insert(0, str(REPO / "skills" / "log-search" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402
from incident_fixture import KNOWN_NOISE_YAML, write_incident_logs  # noqa: E402
from logdigest import cli as digest_cli  # noqa: E402
from logsearch import cli as search_cli  # noqa: E402
from logsearch.store import connect_readonly, ensure_db, parse_time_arg  # noqa: E402
from logsearch.tools import (Ctx, Limits, ToolError, count_by_time, get_context, get_digest,  # noqa: E402
                             run_sql, search_logs, template_lines)

LIMITS = Limits(yaml.safe_load((REPO / "skills" / "log-search" / "config" / "limits.yaml").read_text(encoding="utf-8")))


class TimeArgTest(unittest.TestCase):
    REF = datetime(2026, 9, 30, 10, 40)

    def test_forms(self):
        self.assertEqual(parse_time_arg("2026-09-27", self.REF, False), datetime(2026, 9, 27))
        self.assertEqual(parse_time_arg("2026-09-27", self.REF, True), datetime(2026, 9, 28))
        self.assertEqual(parse_time_arg("09-28 03:00", self.REF, False), datetime(2026, 9, 28, 3, 0))
        self.assertEqual(parse_time_arg("10:42", self.REF, True), datetime(2026, 9, 30, 10, 43))
        self.assertEqual(parse_time_arg("2026/09/30T10:42:05.907", self.REF, True),
                         datetime(2026, 9, 30, 10, 42, 5, 908000))
        with self.assertRaises(ValueError):
            parse_time_arg("yesterday", self.REF, False)


class LogSearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        logs = write_incident_logs(base / "logs")
        noise = base / "known_noise.yaml"
        noise.write_text(KNOWN_NOISE_YAML, encoding="utf-8")
        cls.out = base / "out"
        with redirect_stdout(io.StringIO()):
            rc = digest_cli.run(["--ticket", "INC-2026-0931", "--incident-time", "2026-09-30 10:40",
                                 "--product-version", "5.2.3", "--logs", str(logs),
                                 "--known-noise", str(noise), "--out", str(cls.out)])
        assert rc == 0
        cls.db, cls.built = ensure_db(cls.out)
        cls.con = connect_readonly(cls.db)
        cls.ctx = Ctx(cls.con, LIMITS, cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def _tid(self, text: str) -> str:
        return self.con.execute("SELECT template_id FROM templates WHERE template LIKE ?", [f"%{text}%"]).fetchone()[0]

    def _search(self, *argv: str) -> str:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = search_cli.run([*argv, "--dir", str(self.out)])
        return f"{rc}\n{out.getvalue()}{err.getvalue()}"

    # ---- 取り込み ----

    def test_ingest_matches_digest(self):
        self.assertTrue(self.built)
        meta = json.loads((self.out / "meta.json").read_text(encoding="utf-8"))
        n = self.con.execute("SELECT count(*) FROM logs").fetchone()[0]
        self.assertEqual(n, meta["records"]["total"])
        # ダイジェストに載っている行ID・テンプレートIDがそのまま DB にある
        digest = (self.out / "digest_INC-2026-0931.md").read_text(encoding="utf-8")
        row = self.con.execute("SELECT line_id, template_id FROM logs WHERE severity >= 4 AND NOT noise "
                               "ORDER BY seq LIMIT 1").fetchone()
        self.assertIn(f"@{row[0]}", digest)
        self.assertIn(f"get_context {row[0]}", digest)
        self.assertIn(f"| {row[1]} | ERROR", digest)
        self.assertTrue(self.con.execute("SELECT bool_and(noise) FROM logs WHERE message LIKE 'LDAP referral%'")
                        .fetchone()[0])

    def test_rebuild_only_when_source_changes(self):
        _, built = ensure_db(self.out)
        self.assertFalse(built)
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            copy = Path(tmp) / "out"
            shutil.copytree(self.out, copy, ignore=shutil.ignore_patterns("logs.duckdb"))
            _, built = ensure_db(copy)
            self.assertTrue(built)
            tpl = copy / "parsed" / "templates.jsonl"
            st = tpl.stat()
            os.utime(tpl, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
            _, built = ensure_db(copy)
            self.assertTrue(built)
            self.assertEqual([p.name for p in copy.glob(".logs.duckdb.*")], [])

    # ---- ガイドの調査例 ----

    def test_guide_example_spool_usage_outside_window(self):
        out = search_logs(self.ctx, ["Spool usage"], level="WARN", frm="2026-09-27", to="2026-09-30 10:42")
        self.assertIn("→ 4件", out)
        for pct in ("81%", "88%", "95%", "99%"):
            self.assertIn(pct, out)
        self.assertIn("09-28 03:00:10.000", out)
        self.assertIn("@pms-server.log:", out)

    def test_guide_example_cleanup_sql(self):
        out = run_sql(self.ctx, "SELECT ts, component, message FROM logs WHERE component = 'cleanup' "
                                "ORDER BY ts DESC LIMIT 5")
        self.assertIn("run_sql: 3 行", out)
        self.assertIn("2026-09-28 03:00:01.000\tcleanup\tTemp cleanup skipped: scheduled task disabled", out)

    # ---- 上限 ----

    def test_search_overflow(self):
        out = search_logs(self.ctx, ["Spool write failed"])
        self.assertIn("→ 630件", out)
        self.assertIn("先頭 20 件", out)
        self.assertEqual(sum(1 for ln in out.splitlines() if ln.startswith("   09-30")), 20)
        self.assertIn("テンプレート別の内訳", out)
        # 引数で上限を超えられない
        out = search_logs(self.ctx, ["Spool write failed"], limit=10_000)
        self.assertIn("上限 200 件", out)

    def test_search_filters(self):
        out = search_logs(self.ctx, ["J-20391"])
        # ファイルをまたいで同じジョブを追える（server の失敗・中断、agent の未受信）
        self.assertIn("@pms-server.log:", out)
        self.assertIn("@print-agent.log:", out)
        only_agent = search_logs(self.ctx, ["J-20391"], files=["print-agent"])
        self.assertNotIn("@pms-server.log:", only_agent)
        # 続き行（スタックトレース）も検索する
        self.assertIn("→ 630件", search_logs(self.ctx, ["SpoolWriter.java:212"], message_only=False))
        self.assertIn("一致なし", search_logs(self.ctx, ["not enough space"], message_only=True))
        self.assertNotIn("LDAP", search_logs(self.ctx, [], level="WARN", frm="10:40", to="10:41",
                                             exclude_noise=True))
        out = search_logs(self.ctx, [r"usage 9\d%"], regex=True)
        self.assertIn("→ 2件", out)

    def test_get_context(self):
        first = self.con.execute("SELECT file, lineno FROM logs WHERE message LIKE 'Spool write failed%' "
                                 "ORDER BY seq LIMIT 1").fetchone()
        out = get_context(self.ctx, f"@{first[0]}:{first[1]}", before=3, after=3)
        lines = out.splitlines()
        target = [ln for ln in lines if ln.startswith(">> ")]
        self.assertEqual(len(target), 1)
        self.assertIn("Spool write failed", target[0])
        self.assertIn("前 3 件・後 3 件、同じファイル", lines[0])
        # 続き行の行番号を指定すると、そのレコードが対象になる
        out = get_context(self.ctx, f"{first[0]}:{first[1] + 2}", before=0, after=0)
        self.assertIn("続き行", out)
        self.assertIn("IOException: There is not enough space", out)
        # 全ファイルのマージでは agent / db の行も前後に入る
        merged = get_context(self.ctx, f"pms-server:{first[1]}", before=5, after=5, merged=True)
        self.assertIn("@print-agent.log:", merged)
        # 上限
        big = get_context(self.ctx, f"pms-server.log:{first[1]}", before=500, after=500)
        self.assertIn("前 50 件・後 50 件", big)
        with self.assertRaises(ToolError):
            get_context(self.ctx, "nofile.log:3")
        with self.assertRaises(ToolError):
            get_context(self.ctx, "pms-server.log")

    def test_template_lines(self):
        tid = self._tid("Spool usage")
        out = template_lines(self.ctx, tid.lower(), sample="spread", limit=3)
        self.assertIn(f"template_lines: {tid} WARN  全期間 4件", out)
        self.assertIn("等間隔に 3 件", out)
        self.assertIn("81%", out)
        noise = template_lines(self.ctx, self._tid("LDAP referral"))
        self.assertIn("既知ノイズ: AD多段構成", noise)
        self.assertLessEqual(sum(1 for ln in noise.splitlines() if "@pms-server.log:" in ln), 20)
        with self.assertRaises(ToolError):
            template_lines(self.ctx, "T999")

    def test_count_by_time(self):
        spool = self._tid("Spool write failed")
        out = count_by_time(self.ctx, [spool], bucket="1m", frm="10:40", to="10:44")
        rows = dict(ln.split("\t")[:2] for ln in out.splitlines() if ln.startswith("09-30 10:4"))
        self.assertEqual(rows["09-30 10:40"], "0")
        self.assertEqual(rows["09-30 10:42"], "8")
        # 全期間・1分幅は120区間を超えるので広げる。0が続く区間はまとめる
        wide = count_by_time(self.ctx, [spool], bucket="1m")
        self.assertIn("120 区間を超えるため", wide)
        self.assertIn("すべて0", wide)
        body = [ln for ln in wide.splitlines() if ln[:2].isdigit()]
        self.assertLessEqual(len(body), 120)
        self.assertIn("合計\t630", wide)
        with self.assertRaises(ToolError):
            count_by_time(self.ctx, ["T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08", "T09"])

    # ---- run_sql の安全性 ----

    def test_run_sql_is_read_only(self):
        for sql in ("INSERT INTO logs SELECT * FROM logs", "DELETE FROM logs", "SELECT 1; SELECT 2",
                    "COPY logs TO 'x.csv'", "CREATE TABLE x AS SELECT 1", "SET enable_external_access = true"):
            with self.assertRaises(ToolError, msg=sql):
                run_sql(self.ctx, sql)
        with self.assertRaises(ToolError):
            run_sql(self.ctx, f"SELECT * FROM read_csv('{self.out / 'templates.tsv'}')")
        self.assertEqual(self.con.execute("SELECT count(*) FROM logs").fetchone()[0] > 0, True)

    def test_run_sql_limits(self):
        out = run_sql(self.ctx, "SELECT seq FROM logs")
        self.assertIn("上限 200 行で打ち切り", out)
        self.assertEqual(len(out.splitlines()), 2 + 1 + 200)
        with self.assertRaises(ToolError) as cm:
            run_sql(self.ctx, "SELECT sum(hash(range)) FROM range(5000000000)", timeout=0.3)
        self.assertIn("打ち切った", str(cm.exception))

    # ---- CLI ----

    def test_cli_and_call_log(self):
        log = self.out / "tool_calls.jsonl"
        before = log.read_text(encoding="utf-8").count("\n") if log.exists() else 0
        res = self._search("search_logs", "-q", "Spool usage", "--level", "WARN")
        self.assertTrue(res.startswith("0\n"), res)
        res = self._search("get_digest")
        self.assertIn("# 障害ログダイジェスト", res)
        res = self._search("run_sql", "DROP TABLE logs")
        self.assertTrue(res.startswith("2\n"), res)
        self.assertIn("error:", res)
        entries = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()[before:]]
        self.assertEqual([e["tool"] for e in entries], ["search_logs", "get_digest", "run_sql"])
        self.assertEqual([e["status"] for e in entries], ["ok", "ok", "error"])
        self.assertEqual(entries[0]["args"]["query"], ["Spool usage"])

    def test_get_digest_cap(self):
        small = Limits({"get_digest": {"max_chars": 100}})
        out = get_digest(self.out, small)
        self.assertIn("文字省略", out)

    def test_missing_output(self):
        res = self._search("schema")
        self.assertIn("logs（", res)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = search_cli.run(["schema", "--ticket", "NOPE", "--root", self.tmp.name])
        self.assertEqual(rc, 2)
        self.assertIn("log-digest", err.getvalue())


if __name__ == "__main__":
    unittest.main()
