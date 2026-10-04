"""コマンドライン: ログ一式 → digest_<チケット>.md / context.md / templates.tsv / meta.json"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from . import __version__
from .extract import aggregate_window, build_blocks, select_blocks, select_window
from .noise import apply_noise, load_entries
from .parsing import (collect_files, file_alias, load_formats, parse_file, parse_timestamp,
                      parse_tz)
from .render import (DIGEST_CONTEXT_FILE, TEMPLATES_FILE, DigestInput, Limits, Renderer,
                     estimate_tokens)
from .templating import mine_templates

SKILL_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = SKILL_DIR / "config"

# 未解釈行がこの割合を超えたら書式設定の見直しを促す
UNPARSED_WARN_RATIO = 0.05


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="log_digest.py",
        description="障害ログ一式から、AIが最初に読むダイジェスト（数千トークン）を作る。LLMは使わない。")
    g = p.add_argument_group("チケット情報")
    g.add_argument("--ticket", required=True, help="チケットID（例: INC-2026-0931）")
    g.add_argument("--incident-time", required=True,
                   help="申告された発生時刻（例: '2026-09-30 10:40'）。--tz のタイムゾーンで解釈する")
    g.add_argument("--symptom", help="症状の要約（1行）")
    g.add_argument("--product", help="製品名（例: PMS）")
    g.add_argument("--product-version", help="製品バージョン（例: 5.2.3）。既知ノイズ辞書の照合に使う")
    g.add_argument("--os", dest="os_name", help="OS（例: 'Windows Server 2019'）")

    g = p.add_argument_group("入力")
    g.add_argument("--logs", nargs="+", required=True, help="ログファイルまたはディレクトリ（複数可、.gz 可）")
    g.add_argument("--tz", help="表示と申告時刻のタイムゾーン（既定: log_formats.yaml の defaults.timezone）")
    g.add_argument("--clock-offset", action="append", default=[], metavar="ALIAS=SECONDS",
                   help="ファイルごとの時計のずれを補正する秒数（例: print-agent=-12）。複数指定可")

    g = p.add_argument_group("時間窓と抽出")
    g.add_argument("--window-minutes", type=float, default=30.0, help="申告時刻の前後何分を見るか（既定 30）")
    g.add_argument("--before-minutes", type=float, help="申告時刻の前だけ別に指定する")
    g.add_argument("--after-minutes", type=float, help="申告時刻の後だけ別に指定する")
    g.add_argument("--context-lines", type=int, default=5, help="ERROR の前後に付ける件数（既定 5）")
    g.add_argument("--max-block-records", type=int, default=60, help="文脈の塊1個の最大件数（既定 60）")
    g.add_argument("--max-blocks", type=int, default=30, help="context.md に載せる塊の最大数（既定 30）")
    g.add_argument("--first-errors", type=int, default=3, help="ダイジェストに載せる最初の ERROR の件数（既定 3）")
    g.add_argument("--max-tokens", type=int, default=5000, help="ダイジェストの目標トークン数（既定 5000）")

    g = p.add_argument_group("設定と出力")
    g.add_argument("--out", help="出力ディレクトリ（既定: ./log-digest-out/<チケット>）")
    g.add_argument("--formats", type=Path, default=CONFIG_DIR / "log_formats.yaml")
    g.add_argument("--drain-config", type=Path, default=CONFIG_DIR / "drain3.ini")
    g.add_argument("--known-noise", type=Path, default=CONFIG_DIR / "known_noise.yaml")
    g.add_argument("--no-known-noise", action="store_true", help="既知ノイズ辞書を使わない")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _load_yaml(path: Path) -> object:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_offsets(specs: List[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for s in specs:
        alias, sep, sec = s.partition("=")
        if not sep:
            raise ValueError(f"--clock-offset は ALIAS=SECONDS の形で指定してください: {s!r}")
        out[alias.strip()] = float(sec)
    return out


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w.-]+", "_", s).strip("_") or "ticket"


def _fmt_offset(tz) -> str:
    off = datetime(2000, 1, 1, tzinfo=tz).utcoffset() or timedelta(0)
    total = int(off.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    return f"{sign}{abs(total) // 60:02d}:{abs(total) % 60:02d}"


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    formats_cfg = _load_yaml(args.formats) or {}
    formats = load_formats(formats_cfg)
    tz_spec = args.tz or (formats_cfg.get("defaults") or {}).get("timezone", "+09:00")
    out_tz = parse_tz(tz_spec)
    noise_entries = []
    noise_file = None
    if not args.no_known_noise and args.known_noise.is_file():
        noise_entries = load_entries(_load_yaml(args.known_noise))
        noise_file = args.known_noise.name
    offsets = _parse_offsets(args.clock_offset)

    incident = parse_timestamp(args.incident_time if args.incident_time.count(":") >= 2
                               else args.incident_time + ":00", None, out_tz)
    incident = incident.astimezone(out_tz).replace(tzinfo=None)
    before = args.before_minutes if args.before_minutes is not None else args.window_minutes
    after = args.after_minutes if args.after_minutes is not None else args.window_minutes
    start = incident - timedelta(minutes=before)
    end = incident + timedelta(minutes=after)

    warnings: List[str] = []

    # 1. 収集と時刻の正規化
    files = collect_files(args.logs, formats)
    if not files:
        raise SystemExit("読み込めるログファイルがありません（log_formats.yaml の files を確認してください）")
    records = []
    stats_list = []
    for order, (path, rel, fmt) in enumerate(files):
        recs, st = parse_file(path, rel, fmt, order, out_tz, offsets.get(file_alias(path.name)))
        records.extend(recs)
        stats_list.append(st)
    unused = set(offsets) - {s.alias for s in stats_list}
    if unused:
        warnings.append(f"--clock-offset の {', '.join(sorted(unused))} に一致するファイルがありません")

    # 2〜3. マージ（時刻順に1本へ）
    records.sort(key=lambda r: r.sort_key())

    # 5. テンプレート化（全期間で行い、窓の外での出現状況も分かるようにする）
    templates = mine_templates(records, args.drain_config)
    apply_noise(templates, noise_entries, args.product_version)
    if noise_entries and not args.product_version:
        warnings.append("--product-version が未指定のため、既知ノイズ辞書の全エントリをバージョンに関係なく適用した")

    # 2. 時間窓の切り出し
    window = select_window(records, start, end)
    aggregate_window(window, templates)
    for st in stats_list:
        st.window_records = sum(1 for r in window if r.file == st.file)
    stream = [r for r in window if not templates[r.template_key].is_noise]

    # 4. エラー抽出と前後の文脈
    blocks_all = build_blocks(stream, args.context_lines, args.context_lines, args.max_block_records)
    blocks = select_blocks(blocks_all, args.max_blocks)

    # 入力の品質に関する注意
    for st in stats_list:
        if st.records == 0:
            warnings.append(f"{st.file}: 書式に一致する行が1行もない。log_formats.yaml の pattern を確認すること")
            continue
        bad = st.unparsed_lines + st.bad_timestamps
        if st.total_lines and bad / st.total_lines > UNPARSED_WARN_RATIO:
            warnings.append(f"{st.file}: 解釈できない行が {bad:,} 行（{bad / st.total_lines:.0%}）ある")
        if st.unknown_levels:
            lv = ", ".join(f"{k}({v})" for k, v in sorted(st.unknown_levels.items()))
            warnings.append(f"{st.file}: 未知のレベル表記を INFO として扱った: {lv}")
        if st.encoding not in ("utf-8", "ascii"):
            warnings.append(f"{st.file}: 文字コード {st.encoding} として読んだ")
        if st.clock_offset_seconds:
            warnings.append(f"{st.file}: 時計のずれを {st.clock_offset_seconds:+g} 秒補正した")
        if st.last_ts and st.first_ts and (st.last_ts < start or st.first_ts > end):
            warnings.append(f"{st.file}: ログの期間（{st.first_ts:%m-%d %H:%M}〜{st.last_ts:%m-%d %H:%M}）が"
                            "時間窓と重ならない")
    if not window:
        warnings.append("時間窓内にログが1件もない。申告時刻・タイムゾーン（--tz）・時計のずれを確認すること")

    d = DigestInput()
    d.ticket = args.ticket
    d.product, d.version, d.os, d.symptom = args.product, args.product_version, args.os_name, args.symptom
    d.tz_label = tz_spec if tz_spec.startswith(("+", "-")) else f"{tz_spec}, {_fmt_offset(out_tz)}"
    d.incident, d.start, d.end = incident, start, end
    d.before_minutes, d.after_minutes = before, after
    d.context_before = d.context_after = args.context_lines
    d.files = stats_list
    d.total_records = len(records)
    d.window_records = len(window)
    d.stream = stream
    d.templates = templates
    d.blocks_all, d.blocks = blocks_all, blocks
    d.noise_file = noise_file
    d.warnings = warnings

    renderer = Renderer(d)
    limits = Limits(first_errors=args.first_errors)
    digest = renderer.digest(limits)
    tokens = estimate_tokens(digest)
    for step in limits.shrink_steps():
        if tokens <= args.max_tokens:
            break
        limits = step
        digest = renderer.digest(limits)
        tokens = estimate_tokens(digest)
    if tokens > args.max_tokens:
        # d.warnings と同じリストなので、描き直すと注意欄に載る
        warnings.append(f"ダイジェストが目標 {args.max_tokens} トークンを超えた（見積もり {tokens}）")
        digest = renderer.digest(limits)
        tokens = estimate_tokens(digest)

    out_dir = Path(args.out) if args.out else Path("log-digest-out") / _safe_name(args.ticket)
    out_dir.mkdir(parents=True, exist_ok=True)
    digest_path = out_dir / f"digest_{_safe_name(args.ticket)}.md"
    digest_path.write_text(digest, encoding="utf-8")
    (out_dir / DIGEST_CONTEXT_FILE).write_text(renderer.context(), encoding="utf-8")
    (out_dir / TEMPLATES_FILE).write_text(renderer.templates_tsv(), encoding="utf-8")

    try:
        from importlib.metadata import version as pkg_version
        drain3_version = pkg_version("drain3")
    except Exception:  # noqa: BLE001
        drain3_version = "unknown"
    config_files = [args.formats, args.drain_config] + ([args.known_noise] if noise_file else [])
    meta = {
        "tool": f"log-digest {__version__}",
        "drain3": drain3_version,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ticket": args.ticket,
        "incident_time": incident.isoformat(sep=" "),
        "timezone": tz_spec,
        "window": {"start": start.isoformat(sep=" "), "end": end.isoformat(sep=" ")},
        "config": {p.name: {"path": str(p), "sha256": _sha256(p)} for p in config_files},
        "files": [s.to_dict() for s in stats_list],
        "records": {"total": len(records), "window": len(window), "window_after_noise": len(stream)},
        "templates": {"total": len(templates), "window": sum(1 for t in templates.values() if t.count)},
        "blocks": {"extracted": len(blocks_all), "written": len(blocks)},
        "digest_tokens_estimate": tokens,
        "warnings": warnings,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"digest:    {digest_path}  (約 {tokens} トークン)")
    print(f"context:   {out_dir / DIGEST_CONTEXT_FILE}  (塊 {len(blocks)}/{len(blocks_all)})")
    print(f"templates: {out_dir / TEMPLATES_FILE}")
    print(f"records:   全期間 {len(records):,} / 窓内 {len(window):,} / ノイズ除外後 {len(stream):,}")
    for w in warnings:
        print(f"warning:   {w}")
    return 0


def main() -> None:
    # Windows のコンソール（cp932）で表せない文字があっても落ちないようにする
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        sys.exit(run())
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
