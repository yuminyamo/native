"""ダイジェスト（Markdown）、文脈ファイル、テンプレート一覧の出力。"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, List, Optional

from .extract import Block
from .parsing import WARN_SEVERITY, FileStats, Record
from .templating import Template

DIGEST_CONTEXT_FILE = "context.md"
TEMPLATES_FILE = "templates.tsv"


class Limits:
    """ダイジェストの各セクションの量。トークン上限を超えたら段階的に小さくする。"""

    def __init__(self, first_errors: int = 3, template_rows: int = 40, noise_rows: int = 20,
                 stack_traces: int = 2, stack_lines: int = 12, context_lines: int = 25,
                 msg_chars: int = 300):
        self.first_errors = first_errors
        self.template_rows = template_rows
        self.noise_rows = noise_rows
        self.stack_traces = stack_traces
        self.stack_lines = stack_lines
        self.context_lines = context_lines
        self.msg_chars = msg_chars

    def shrink_steps(self) -> List["Limits"]:
        """少しずつ小さくした設定の列。先頭ほど優先度の低い情報から削る。"""
        steps = []
        cur = self.copy()
        for ctx, rows, stack, msg in [(15, 40, 12, 300), (8, 30, 10, 240), (8, 20, 8, 200),
                                      (0, 15, 6, 160), (0, 10, 4, 120)]:
            cur = cur.copy()
            cur.context_lines = min(cur.context_lines, ctx)
            cur.template_rows = min(cur.template_rows, rows)
            cur.noise_rows = min(cur.noise_rows, rows // 2)
            cur.stack_lines = min(cur.stack_lines, stack)
            cur.msg_chars = min(cur.msg_chars, msg)
            steps.append(cur)
        return steps

    def copy(self) -> "Limits":
        return Limits(self.first_errors, self.template_rows, self.noise_rows, self.stack_traces,
                      self.stack_lines, self.context_lines, self.msg_chars)


class DigestInput:
    """ダイジェストを書くのに必要な材料一式。"""

    def __init__(self) -> None:
        self.ticket = ""
        self.product: Optional[str] = None
        self.version: Optional[str] = None
        self.os: Optional[str] = None
        self.symptom: Optional[str] = None
        self.tz_label = ""
        self.incident: Optional[datetime] = None
        self.start: Optional[datetime] = None
        self.end: Optional[datetime] = None
        self.before_minutes = 0.0
        self.after_minutes = 0.0
        self.context_before = 5
        self.context_after = 5
        self.files: List[FileStats] = []
        self.total_records = 0
        self.window_records = 0
        self.stream: List[Record] = []  # 時間窓内・既知ノイズ除外済み
        self.templates: Dict[int, Template] = {}
        self.blocks_all: List[Block] = []
        self.blocks: List[Block] = []
        self.noise_file: Optional[str] = None
        self.warnings: List[str] = []


def estimate_tokens(text: str) -> int:
    """トークン数のおおまかな見積もり（ASCII 3.5文字で1、非ASCII 1文字で1）。"""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return math.ceil(ascii_chars / 3.5 + (len(text) - ascii_chars))


def fmt_ts(dt: Optional[datetime], with_date: bool = False, millis: bool = True) -> str:
    if dt is None:
        return "-"
    base = dt.strftime("%m-%d %H:%M:%S" if with_date else "%H:%M:%S")
    return base + (f".{dt.microsecond // 1000:03d}" if millis else "")


def _clip(text: str, n: int) -> str:
    text = text.replace("\t", "  ")
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _num(n: int) -> str:
    return f"{n:,}"


def fmt_range(start: datetime, end: datetime) -> str:
    """'2026-09-30 10:10〜11:10'（日付が同じなら終わりの日付を省く）。"""
    tail = "%H:%M" if start.date() == end.date() else "%Y-%m-%d %H:%M"
    return f"{start:%Y-%m-%d %H:%M}〜{end.strftime(tail)}"


class Renderer:
    def __init__(self, d: DigestInput):
        self.d = d
        self.multi_day = d.start is not None and d.end is not None and d.start.date() != d.end.date()

    def tid(self, r: Record) -> str:
        return self.d.templates[r.template_key].id

    def line(self, r: Record, mark: str, msg_chars: int, with_id: bool = True) -> str:
        comp = f"[{r.component}] " if r.component else ""
        s = (f"{mark}{fmt_ts(r.ts, self.multi_day)} {self.tid(r)} {r.level:<5} {r.alias} "
             f"{comp}{_clip(r.message, msg_chars)}")
        if with_id:
            s += f"  @{r.line_id}"
        return s

    # ---- digest -------------------------------------------------------

    def digest(self, lim: Limits) -> str:
        d = self.d
        out: List[str] = ["# 障害ログダイジェスト", ""]
        head = [f"チケット: {d.ticket}"]
        if d.product or d.version:
            head.append(f"製品: {' '.join(x for x in (d.product, d.version) if x)}")
        if d.os:
            head.append(f"OS: {d.os}")
        out.append(" / ".join(head))
        if d.symptom:
            out.append(f"症状: {d.symptom}")
        out.append(f"申告時刻: {d.incident:%Y-%m-%d %H:%M}（{d.tz_label}）")
        out.append(f"時間窓: {fmt_range(d.start, d.end)}"
                   f"（申告時刻 -{d.before_minutes:g}分 / +{d.after_minutes:g}分）")
        names = ", ".join(f.file for f in d.files)
        out.append(f"対象: {names}（窓内 {_num(d.window_records)}行 / 全期間 {_num(d.total_records)}行）")
        out.append("")

        if d.warnings:
            out += ["## 注意", ""] + [f"- {w}" for w in d.warnings] + [""]

        out += self._files_section()
        out += self._first_errors_section(lim)
        out += self._template_section(lim)
        out += self._noise_section(lim)
        out += self._context_section(lim)
        out += self._stack_section(lim)
        out += self._howto_section()
        return "\n".join(out).rstrip() + "\n"

    def _files_section(self) -> List[str]:
        out = ["## 入力ファイル", "",
               "| ファイル | 文字コード | 全期間の範囲 | レコード | 窓内 | 未解釈行 | 時刻補正 |",
               "|---|---|---|---:|---:|---:|---:|"]
        for f in self.d.files:
            rng = f"{fmt_ts(f.first_ts, True, False)}〜{fmt_ts(f.last_ts, True, False)}"
            offset = f"{f.clock_offset_seconds:+g}秒" if f.clock_offset_seconds else "-"
            out.append(f"| {_cell(f.file)} | {f.encoding} | {rng} | {_num(f.records)} | "
                       f"{_num(f.window_records)} | {_num(f.unparsed_lines + f.bad_timestamps)} | {offset} |")
        return out + [""]

    def _first_errors_section(self, lim: Limits) -> List[str]:
        triggers = [r for r in self.d.stream if r.is_trigger]
        out = [f"## 最初に出た ERROR・例外（時刻順・先頭{lim.first_errors}件）", ""]
        if not triggers:
            out += ["時間窓内に ERROR 以上・スタックトレース付きのログはありません（既知ノイズを除く）。",
                    "ERROR にならない異常（処理が黙って止まる等）の可能性があります。"
                    "時間窓を広げるか、WARN のテンプレート集計を確認してください。", ""]
            return out
        out.append("```")
        out += [self.line(r, "", lim.msg_chars) for r in triggers[: lim.first_errors]]
        out += ["```", ""]
        return out

    def _window_templates(self) -> List[Template]:
        ts = [t for t in self.d.templates.values() if t.count > 0]
        return sorted(ts, key=lambda t: (t.first, t.id))

    def _template_section(self, lim: Limits) -> List[str]:
        all_win = self._window_templates()
        rows = [t for t in all_win
                if not t.is_noise and (t.max_severity >= WARN_SEVERITY or t.trigger_count > 0)]
        noise_n = sum(1 for t in all_win if t.is_noise)
        out = ["## テンプレート集計（ERROR/WARN・例外、窓内の初出順）", ""]
        if not rows:
            out += ["該当なし。", ""]
        else:
            out += ["| ID | レベル | 窓内件数 | 窓内初出 | 全期間件数 | 全期間初出 | テンプレート |",
                    "|---|---|---:|---|---:|---|---|"]
            for t in rows[: lim.template_rows]:
                level = t.level + ("+例外" if t.trigger_count and t.level not in ("ERROR", "FATAL") else "")
                out.append(f"| {t.id} | {level} | {_num(t.count)} | {fmt_ts(t.first, self.multi_day, False)} | "
                           f"{_num(t.total_count)} | {fmt_ts(t.total_first, True, False)} | "
                           f"{_cell(_clip(t.text, 160))} |")
            if len(rows) > lim.template_rows:
                out.append("")
                out.append(f"…ほか {len(rows) - lim.template_rows} 種類は {TEMPLATES_FILE} を参照。")
        out.append("")
        out.append(f"窓内のテンプレートは全 {len(all_win)} 種類（うち上表の対象 {len(rows)}、既知ノイズ {noise_n}）。"
                   f"INFO 以下を含む全種類と時刻は {TEMPLATES_FILE} にある。"
                   "全期間の初出が窓より前なら、障害前から出ていたログである。")
        return out + [""]

    def _noise_section(self, lim: Limits) -> List[str]:
        noise = [t for t in self._window_templates() if t.is_noise]
        if not noise:
            if self.d.noise_file:
                return ["## 既知ノイズとして除外", "", "窓内に既知ノイズ辞書と一致するログはありません。", ""]
            return []
        out = [f"## 既知ノイズとして除外（{self.d.noise_file} による）", ""]
        for t in sorted(noise, key=lambda t: -t.count)[: lim.noise_rows]:
            out.append(f"- {t.id} {_clip(t.text, 120)}（窓内 {_num(t.count)}件）: {t.noise_reason}")
        if len(noise) > lim.noise_rows:
            out.append(f"- …ほか {len(noise) - lim.noise_rows} 種類（{TEMPLATES_FILE} の noise_reason 列）")
        return out + [""]

    def _context_section(self, lim: Limits) -> List[str]:
        if lim.context_lines <= 0 or not self.d.blocks:
            return []
        b = self.d.blocks[0]
        first = b.trigger_positions[0]
        lo = max(0, first - lim.context_lines // 3)
        hi = min(len(b.records), lo + lim.context_lines)
        trig = set(b.trigger_positions)
        out = [f"## 最初の ERROR の前後（{b.id}、{DIGEST_CONTEXT_FILE} から抜粋）", "",
               "`>>` が ERROR・例外。既知ノイズは除外済み。", "", "```"]
        for i in range(lo, hi):
            r = b.records[i]
            s = self.line(r, ">> " if i in trig else "   ", lim.msg_chars, with_id=False)
            if r.extra:
                s += f"  （続き {len(r.extra) + r.extra_dropped} 行）"
            out.append(s)
        out.append("```")
        rest = len(b.records) - (hi - lo)
        if rest > 0:
            out.append(f"この塊の残り {rest} 件と行番号は {DIGEST_CONTEXT_FILE} の {b.id} を参照。")
        return out + [""]

    def _stack_section(self, lim: Limits) -> List[str]:
        if lim.stack_traces <= 0:
            return []
        picked: List[Record] = []
        seen = set()
        for r in self.d.stream:
            if r.has_stack and r.template_key not in seen:
                seen.add(r.template_key)
                picked.append(r)
                if len(picked) >= lim.stack_traces:
                    break
        if not picked:
            return []
        out = ["## 代表スタックトレース（テンプレートごとに最初の1件）", ""]
        for r in picked:
            t = self.d.templates[r.template_key]
            n = self._count_template_stacks(r.template_key)
            out.append(f"### {t.id} {fmt_ts(r.ts, self.multi_day)} {r.alias} @{r.line_id}"
                       f"（同じテンプレートでスタック付き {n} 件）")
            out += ["", "```", _clip(r.message, lim.msg_chars)]
            out += [_clip(x, lim.msg_chars) for x in r.extra[: lim.stack_lines]]
            remaining = len(r.extra) - lim.stack_lines + r.extra_dropped
            if remaining > 0:
                where = self._block_of(r)
                hint = f"{DIGEST_CONTEXT_FILE} の {where}" if where else f"元ファイル {r.line_id} 以降"
                out.append(f"…（以下 {remaining} 行省略。全文は {hint} を参照）")
            out += ["```", ""]
        return out

    def _count_template_stacks(self, key: int) -> int:
        return sum(1 for r in self.d.stream if r.template_key == key and r.has_stack)

    def _block_of(self, r: Record) -> Optional[str]:
        for b in self.d.blocks:
            if any(x is r for x in b.records):
                return b.id
        return None

    def _howto_section(self) -> List[str]:
        d = self.d
        n_all, n_sel = len(d.blocks_all), len(d.blocks)
        return [
            "## 省略した情報の確かめ方",
            "",
            "このダイジェストは抽出・集計した結果であり、省略した箇所がある。省略箇所を推測で埋めず、次で確かめること。",
            "",
            f"- ERROR・例外の前後の文脈: {DIGEST_CONTEXT_FILE}（{n_sel} 個の塊。同じ ERROR の繰り返しだけの塊 "
            f"{n_all - n_sel} 個は省略）",
            f"- 窓内・全期間の全テンプレートの件数と初出・最終時刻: {TEMPLATES_FILE}",
            "- 時間窓の外: `--window-minutes`（または `--before-minutes` / `--after-minutes`）を広げて再実行する",
            "- 根拠の行そのもの: 生ログの `@ファイル:行番号` の前後に範囲を絞って読む（全文は読まない）",
            "- 根拠としてログを挙げるときは、時刻と `@ファイル:行番号` を付ける",
        ]

    # ---- context.md -----------------------------------------------------

    def context(self, msg_chars: int = 1000, extra_lines: int = 60) -> str:
        d = self.d
        out = [f"# ERROR・例外の前後の文脈（{d.ticket}）", "",
               f"時間窓: {fmt_range(d.start, d.end)} / "
               f"前後 {d.context_before}件・{d.context_after}件 / 既知ノイズは除外済み",
               f"抽出した塊 {len(d.blocks_all)} 個のうち、新しい ERROR テンプレートを含む {len(d.blocks)} 個を載せる。",
               "書式: `時刻 テンプレートID レベル ファイル [コンポーネント] メッセージ @ファイル:行番号`、"
               "`>>` が ERROR・例外。", ""]
        if not d.blocks:
            out.append("時間窓内に ERROR・例外はありません。")
        for b in d.blocks:
            ids = ", ".join(d.templates[k].id for k in b.trigger_template_keys())
            out += [f"## {b.id} {fmt_ts(b.records[0].ts, self.multi_day)}〜{fmt_ts(b.records[-1].ts, self.multi_day)}"
                    f"（トリガー: {ids}）", "", "```"]
            trig = set(b.trigger_positions)
            for i, r in enumerate(b.records):
                out.append(self.line(r, ">> " if i in trig else "   ", msg_chars))
                for x in r.extra[:extra_lines]:
                    out.append("      " + _clip(x, msg_chars))
                rest = len(r.extra) - extra_lines + r.extra_dropped
                if rest > 0:
                    out.append(f"      …（以下 {rest} 行省略。元ファイル {r.line_id} 以降）")
            out += ["```", ""]
        return "\n".join(out).rstrip() + "\n"

    # ---- templates.tsv --------------------------------------------------

    def templates_tsv(self) -> str:
        cols = ["id", "level", "window_count", "window_first", "window_last", "total_count",
                "total_first", "total_last", "noise_reason", "components", "template"]
        ts = list(self.d.templates.values())
        far = datetime.max
        ts.sort(key=lambda t: (t.count == 0, t.first or far, t.total_first or far, t.id))

        def iso(x: Optional[datetime]) -> str:
            return x.isoformat(sep=" ", timespec="milliseconds") if x else ""

        rows = ["\t".join(cols)]
        for t in ts:
            comps = ",".join(f"{c or '-'}:{n}" for c, n in sorted(t.components.items(), key=lambda kv: -kv[1]))
            vals = [t.id, t.level, str(t.count), iso(t.first), iso(t.last), str(t.total_count),
                    iso(t.total_first), iso(t.total_last), t.noise_reason or "", comps, t.text]
            rows.append("\t".join(v.replace("\t", " ").replace("\n", " ") for v in vals))
        return "\n".join(rows) + "\n"
