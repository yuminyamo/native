"""コマンドライン: log_search.py <ツール名> --ticket <チケット> ...

ツール名はガイドの名前（get_digest, search_logs, get_context, template_lines, count_by_time, run_sql）と同じ。
呼び出しのたびに、出力ディレクトリの tool_calls.jsonl に記録を1行足す（AIが何を調べたかを後で見直すため）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import yaml

from . import __version__
from .store import DEFAULT_ROOT, connect_readonly, ensure_db, resolve_dir
from .tools import (Ctx, Limits, ToolError, count_by_time, get_context, get_digest, run_sql, schema,
                    search_logs, template_lines)

SKILL_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = SKILL_DIR / "config"
CALL_LOG = "tool_calls.jsonl"


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("対象")
    g.add_argument("--ticket", help="チケットID。log-digest の出力 <root>/<チケット>/ を読む")
    g.add_argument("--dir", help="log-digest の出力ディレクトリを直接指定する（--out を変えて実行した場合）")
    g.add_argument("--root", default=DEFAULT_ROOT, help=f"log-digest の出力の親ディレクトリ（既定: {DEFAULT_ROOT}）")
    g.add_argument("--limits", type=Path, default=CONFIG_DIR / "limits.yaml", help=argparse.SUPPRESS)
    g.add_argument("--no-call-log", action="store_true", help=f"{CALL_LOG} に記録しない")

    p = argparse.ArgumentParser(
        prog="log_search.py",
        description="log-digest の出力（全期間の全レコード）を DuckDB で検索する。返す量には上限がある。")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="tool", metavar="<ツール>")
    sub.required = True

    sp = sub.add_parser("get_digest", parents=[common], help="案1のダイジェストを返す（調査の入口）")

    sp = sub.add_parser("search_logs", parents=[common],
                        help="キーワード・レベル・時間範囲・ファイルで検索（一致が多いと件数と先頭だけ）")
    sp.add_argument("--query", "-q", action="append", default=[],
                    help="含む文字列（大文字小文字を区別しない）。複数指定はすべて含むもの。続き行（スタックトレース）も検索する")
    sp.add_argument("--regex", action="store_true", help="--query を正規表現として扱う")
    sp.add_argument("--message-only", action="store_true", help="続き行は検索しない")
    sp.add_argument("--level", help="このレベル以上（INFO / WARN / ERROR / FATAL）")
    sp.add_argument("--from", dest="frm", help="開始時刻（この時刻を含む）。例: '2026-09-27', '09-30 10:20', '10:20'")
    sp.add_argument("--to", help="終了時刻（書いた精度の終わりまで含む。'10:42' は 10:42:59.999 まで）")
    sp.add_argument("--file", action="append", default=[], help="ファイル名または別名（例: pms-server）。複数可")
    sp.add_argument("--component", action="append", default=[], help="コンポーネント（[spool] の spool）。複数可")
    sp.add_argument("--template", action="append", default=[], help="テンプレートID（T07 など）。複数可")
    sp.add_argument("--exclude-noise", action="store_true", help="既知ノイズを除く")
    sp.add_argument("--order", choices=["asc", "desc"], default="asc", help="desc で新しい順")
    sp.add_argument("--limit", type=int, help="一致した時に全件返す上限（limits.yaml の値より大きくはできない）")

    sp = sub.add_parser("get_context", parents=[common], help="特定の行の前後を見る")
    sp.add_argument("line_id", help="ファイル:行番号（ダイジェストの @pms-server.log:4414 の部分）")
    sp.add_argument("--before", type=int, help="前の件数（既定 10、最大 50）")
    sp.add_argument("--after", type=int, help="後の件数（既定 10、最大 50）")
    sp.add_argument("--merged", action="store_true", help="同じファイルではなく、全ファイルを時刻順にマージした前後を見る")
    sp.add_argument("--extra-lines", type=int, help="前後の行に続く行（スタックトレース等）を何行出すか（既定 3）")

    sp = sub.add_parser("template_lines", parents=[common], help="テンプレートIDの実際の行を見る")
    sp.add_argument("template_id", help="T07 など")
    sp.add_argument("--limit", type=int, help="件数（最大 20）")
    sp.add_argument("--from", dest="frm")
    sp.add_argument("--to")
    sp.add_argument("--sample", choices=["first", "last", "spread"], default="first",
                    help="first=最初から、last=最後の分、spread=期間全体から等間隔に")

    sp = sub.add_parser("count_by_time", parents=[common], help="テンプレートごとの件数の時間推移")
    sp.add_argument("template_ids", nargs="+", help="テンプレートID（T07 T12 または T07,T12）。最大 8")
    sp.add_argument("--bucket", help="区間の幅（30s, 1m, 5m, 1h, 1d など）。省略時は 120 区間以内で自動")
    sp.add_argument("--from", dest="frm", help="省略時はログ全体の最初から")
    sp.add_argument("--to", help="省略時はログ全体の最後まで")

    sp = sub.add_parser("run_sql", parents=[common], help="読み取り専用の SQL（200行・30秒まで）")
    sp.add_argument("sql", help="SELECT 文1つ。テーブルは schema で確認する")
    sp.add_argument("--max-rows", type=int)
    sp.add_argument("--timeout", type=float)

    sub.add_parser("schema", parents=[common], help="run_sql 用のテーブル・列の説明")
    return p


def _dispatch(args: argparse.Namespace, out_dir: Path, limits: Limits) -> str:
    if args.tool == "get_digest":
        return get_digest(out_dir, limits)
    db, _built = ensure_db(out_dir)
    con = connect_readonly(db, str(limits.get("common", "memory_limit", "1GB")),
                           int(limits.get("common", "threads", 2)))
    try:
        ctx = Ctx(con, limits, out_dir)
        if args.tool == "search_logs":
            return search_logs(ctx, args.query, args.regex, args.level, args.frm, args.to, args.file,
                               args.component, args.template, args.exclude_noise, args.message_only,
                               args.order, args.limit)
        if args.tool == "get_context":
            return get_context(ctx, args.line_id, args.before, args.after, args.merged, args.extra_lines)
        if args.tool == "template_lines":
            return template_lines(ctx, args.template_id, args.limit, args.frm, args.to, args.sample)
        if args.tool == "count_by_time":
            return count_by_time(ctx, args.template_ids, args.bucket, args.frm, args.to)
        if args.tool == "run_sql":
            return run_sql(ctx, args.sql, args.max_rows, args.timeout)
        if args.tool == "schema":
            return schema(ctx)
        raise ToolError(f"未知のツール: {args.tool}")
    finally:
        con.close()


_LOG_SKIP = {"tool", "ticket", "dir", "root", "limits", "no_call_log"}


def _record_call(out_dir: Path, args: argparse.Namespace, status: str, output: str, elapsed: float) -> None:
    entry = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": args.tool,
        "args": {k: v for k, v in vars(args).items() if k not in _LOG_SKIP and v not in (None, [], False)},
        "status": status,
        "output_lines": output.count("\n"),
        "output_chars": len(output),
        "elapsed_ms": int(elapsed * 1000),
    }
    try:
        with open(out_dir / CALL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # 記録できなくても検索結果は返す


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        out_dir = resolve_dir(args.ticket, args.dir, args.root)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    with open(args.limits, encoding="utf-8") as f:
        limits = Limits(yaml.safe_load(f) or {})

    started = time.monotonic()
    try:
        output = _dispatch(args, out_dir, limits)
        status, rc = "ok", 0
    except (ToolError, ValueError, FileNotFoundError) as e:
        output = f"error: {e}\n"
        status, rc = "error", 2
    if not args.no_call_log:
        _record_call(out_dir, args, status, output, time.monotonic() - started)
    (sys.stdout if rc == 0 else sys.stderr).write(output)
    return rc


def main() -> None:
    # Windows のコンソール（cp932）で表せない文字があっても落ちないようにする
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    sys.exit(run())
