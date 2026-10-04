"""コマンドライン: log_knowledge.py <コマンド> ...

  noise-candidates  再現環境の正常期間のログ（log-digest の出力）から、既知ノイズの候補を出す
  noise-merge       AI が判断を書き込んだ候補を、既知ノイズ辞書と却下リストに反映する
  noise-validate    既知ノイズ辞書を検査する
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

import yaml

from . import __version__
from .noise import (auto_periods, build_candidates, default_versions, merge, parse_periods, read_list,
                    validate_dictionary, write_candidates, write_list)
from .parsed import load_meta

# 辞書はワークスペースの knowledge/ に置く（log-digest は同じ場所を既定で読む）
KNOWLEDGE_DIR = Path("knowledge")
DEFAULT_DICT = KNOWLEDGE_DIR / "known_noise.yaml"
DEFAULT_REJECTED = KNOWLEDGE_DIR / "known_noise_rejected.yaml"
CANDIDATES_FILE = "noise_candidates.yaml"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="log_knowledge.py",
                                description="障害対応をまたいで残すログの知見（既知ノイズ辞書）を作る。")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<コマンド>")
    sub.required = True

    sp = sub.add_parser("noise-candidates", help="再現環境の正常期間のログから既知ノイズの候補を出す")
    sp.add_argument("--dir", required=True, help="再現環境のログを log-digest にかけた出力ディレクトリ")
    sp.add_argument("--source", required=True,
                    help="根拠として残すログの出どころ（例: '再現環境 PMS 5.2.3 リリース試験 2026-09-16〜09-23'）")
    sp.add_argument("--normal", action="append", default=[], metavar="DATE|START/END",
                    help="正常だと分かっている期間。日付（'2026-09-16'）か範囲（'2026-09-16 09:00/2026-09-18 18:00'）。"
                         "複数可。省略すると log-digest が平常期間の候補とした日を使う")
    sp.add_argument("--versions", nargs="+", help="登録する製品バージョン（既定: 5.2.3 なら 5.2.*）")
    sp.add_argument("--dict", type=Path, default=DEFAULT_DICT, help=f"既知ノイズ辞書（既定: {DEFAULT_DICT.as_posix()}）")
    sp.add_argument("--rejected", type=Path, default=DEFAULT_REJECTED,
                    help=f"却下リスト（既定: {DEFAULT_REJECTED.as_posix()}）")
    sp.add_argument("--min-count", type=int, default=3, help="候補にする最小件数（既定 3）")
    sp.add_argument("--min-hours", type=int, default=2, help="候補にする最小の時間帯数（1時間単位、既定 2）")
    sp.add_argument("--out", type=Path, help=f"候補ファイル（既定: <--dir>/{CANDIDATES_FILE}）")

    sp = sub.add_parser("noise-merge", help="判断を書き込んだ候補を辞書と却下リストに反映する")
    sp.add_argument("--candidates", type=Path, required=True, help="noise-candidates が作った候補ファイル")
    sp.add_argument("--dict", type=Path, default=DEFAULT_DICT)
    sp.add_argument("--rejected", type=Path, default=DEFAULT_REJECTED)
    sp.add_argument("--allow-undecided", action="store_true", help="判断していない候補は今回は見送る")
    sp.add_argument("--today", help=argparse.SUPPRESS)

    sp = sub.add_parser("noise-validate", help="既知ノイズ辞書を検査する")
    sp.add_argument("--dict", type=Path, default=DEFAULT_DICT)
    return p


def cmd_candidates(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    meta = load_meta(d)
    if args.normal:
        periods = parse_periods(args.normal)
        how = "指定"
    else:
        periods = auto_periods(meta)
        how = "log-digest の平常期間の候補"
        if not periods:
            raise ValueError("log-digest が平常期間の候補とした日がありません。--normal で正常だと分かっている期間を指定してください")
    versions = args.versions or default_versions(meta.get("product_version"))
    if not versions:
        raise ValueError("製品バージョンが分かりません。log-digest に --product-version を付けるか、--versions を指定してください")
    _, dictionary = read_list(args.dict)
    _, rejected = read_list(args.rejected)
    data = build_candidates(d, periods, versions, args.source, dictionary, rejected, args.min_count, args.min_hours)
    out = args.out or d / CANDIDATES_FILE
    write_candidates(out, data)
    print(f"candidates: {out}")
    print(f"正常期間（{how}）: {', '.join(data['normal_periods'])}（計 {data['normal_hours']} 時間）")
    print(f"バージョン: {', '.join(versions)}")
    print(f"候補 {len(data['candidates'])} 件 / 対象外 {len(data['skipped'])} 件")
    for c in data["candidates"]:
        print(f"  - {c['level']:<5} {c['template']}  （{c['rate']}、検索語: {c['search_hint']!r}）")
    if not args.normal:
        print("warning:   正常期間を規則で選んだ。再現環境で障害を再現した期間が含まれていないか、"
              "上の正常期間を確認すること")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    cand = yaml.safe_load(args.candidates.read_text(encoding="utf-8")) or {}
    dict_header, dictionary = read_list(args.dict)
    rej_header, rejected = read_list(args.rejected)
    today = date.fromisoformat(args.today) if args.today else date.today()
    res = merge(cand, dictionary, rejected, today, args.allow_undecided)
    if res.errors:
        for e in res.errors:
            print(f"error: {e}", file=sys.stderr)
        print("辞書は変更していない", file=sys.stderr)
        return 1
    write_list(args.dict, dict_header, dictionary)
    write_list(args.rejected, rej_header, rejected)
    print(f"登録 {len(res.registered)} 件 → {args.dict}")
    for e in res.registered:
        print(f"  + {e['template']}: {e['reason']}（{e['evidence']['code']}）")
    print(f"却下 {len(res.rejected)} 件 → {args.rejected}")
    for e in res.rejected:
        print(f"  - {e['template']}: {e['reject_reason']}")
    if res.undecided:
        print(f"見送り {len(res.undecided)} 件（判断していない）")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    _, entries = read_list(args.dict)
    errors = validate_dictionary(entries)
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    if not errors:
        print(f"ok: {args.dict}（{len(entries)} 件）")
    return 1 if errors else 0


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return {"noise-candidates": cmd_candidates, "noise-merge": cmd_merge,
            "noise-validate": cmd_validate}[args.command](args)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        sys.exit(run())
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
