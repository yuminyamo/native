"""ダイジェスト（Markdown）、文脈ファイル、テンプレート一覧の出力。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .baseline import APPEAR, COMPARE_ORDER, BaselineResult, fmt_day
from .extract import Block
from .parsing import WARN_SEVERITY, FileStats, Record
from .templating import Template

DIGEST_CONTEXT_FILE = "context.md"
TEMPLATES_FILE = "templates.tsv"


class Limits:
    """ダイジェストの各セクションの量。トークン上限を超えたら段階的に小さくする。"""

    def __init__(self, first_errors: int = 3, template_rows: int = 40, noise_rows: int = 20,
                 stack_traces: int = 2, stack_lines: int = 12, context_lines: int = 25,
                 msg_chars: int = 300, day_rows: int = 15, change_rows: int = 15, compare_rows: int = 20):
        self.first_errors = first_errors
        self.template_rows = template_rows
        self.noise_rows = noise_rows
        self.stack_traces = stack_traces
        self.stack_lines = stack_lines
        self.context_lines = context_lines
        self.msg_chars = msg_chars
        self.day_rows = day_rows
        self.change_rows = change_rows
        self.compare_rows = compare_rows

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
            # 平常時との比較は原因に近い情報なので、テンプレート表より後に削る
            cur.day_rows = min(cur.day_rows, max(5, rows // 3))
            cur.change_rows = min(cur.change_rows, max(8, rows // 2))
            cur.compare_rows = min(cur.compare_rows, max(10, rows // 2))
            steps.append(cur)
        return steps

    def copy(self) -> "Limits":
        return Limits(self.first_errors, self.template_rows, self.noise_rows, self.stack_traces,
                      self.stack_lines, self.context_lines, self.msg_chars, self.day_rows,
                      self.change_rows, self.compare_rows)


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
        self.stream: List[Record] = []  # 時間窓内・既知ノイズ（下に回すもの）を省いたもの
        self.templates: Dict[int, Template] = {}
        self.blocks_all: List[Block] = []
        self.blocks: List[Block] = []
        self.noise_file: Optional[str] = None
        self.baseline: Optional[BaselineResult] = None
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


def _fmt_expected(x: float) -> str:
    """平常期間の件数（換算値）。小さい値だけ小数1桁にする。"""
    if x == 0:
        return "0"
    if x < 10 and abs(x - round(x)) > 0.05:
        return f"{x:.1f}"
    return f"{round(x):,}"


def fmt_period(start: datetime, end: datetime) -> str:
    """'09-23（水）10:10〜11:10'（日付をまたぐ場合は終わりにも日付を付ける）。"""
    tail = "%H:%M" if start.date() == end.date() else "%m-%d %H:%M"
    return f"{fmt_day(start.date())}{start:%H:%M}〜{end.strftime(tail)}"


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
        out += self._baseline_section(lim)
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
            out += ["時間窓内に ERROR 以上・スタックトレース付きのログはありません（下に回した既知ノイズを除く）。",
                    "ERROR にならない異常（処理が黙って止まる等）の可能性があります。"
                    "時間窓を広げるか、WARN のテンプレート集計を確認してください。", ""]
            return out
        out.append("```")
        out += [self.line(r, "", lim.msg_chars) for r in triggers[: lim.first_errors]]
        out += ["```", ""]
        return out

    def _has_periods(self) -> bool:
        return bool(self.d.baseline and self.d.baseline.periods)

    def _baseline_section(self, lim: Limits) -> List[str]:
        b = self.d.baseline
        if b is None:
            return []
        out = ["## 平常時との比較", ""]
        periods = "、".join(fmt_period(p.start, p.end) for p in b.periods) or "なし"
        how = {"auto": "自動選択", "manual": "指定", "fallback": "自動選択（代わりの期間）", "none": "比較なし"}[b.selection]
        out.append(f"平常期間: {periods}（{how}: {b.reason}）")
        out.append("同じ環境の、正常に動いていたと思われる期間と比べた結果。平常期間の選び方が障害内容・問い合わせ内容と"
                   "合わなければ、log-digest を `--baseline <日付> --baseline-reason \"<理由>\"` で実行し直す。")
        out.append("")
        out += self._days_table(lim)
        out += self._change_points_table(lim)
        out += self._comparison_table(lim)
        return out

    def _days_table(self, lim: Limits) -> List[str]:
        b = self.d.baseline
        days = [s for s in b.days if s.records > 0 or s.full]
        if not days:
            return []
        base_days = {p.start.date() for p in b.periods if b.selection != "fallback"}
        days = list(reversed(days))
        shown = days[: lim.day_rows]
        shown += [s for s in days[lim.day_rows:] if s.day in base_days]
        out = [f"### 日別の状況（受け取ったログ {len(days)} 日分）", "",
               "| 日付 | 件数 | ERROR | 営業日 | 備考 |", "|---|---:|---:|---|---|"]
        for s in shown:
            notes = []
            if s.is_incident_day:
                notes.append("障害日")
            if not s.full:
                notes.append("一部の時間のみ")
            if s.day in base_days:
                notes.append("**平常期間**")
            if s.after_change:
                notes.append("変化点の後")
            if s.error_spike:
                notes.append("ERROR急増")
            if s.records == 0:
                notes.append("ログなし")
            biz = "-" if s.is_incident_day or not s.full else ("○" if s.business else "")
            out.append(f"| {fmt_day(s.day)} | {_num(s.records)} | {_num(s.errors)} | {biz} | {'、'.join(notes)} |")
        rest = [s for s in days[lim.day_rows:] if s not in shown]
        if rest:
            out.append("")
            out.append(f"…ほか {len(rest)} 日（{rest[-1].day:%m-%d}〜{rest[0].day:%m-%d}）は meta.json の baseline.days を参照。")
        return out + [""]

    def _change_points_table(self, lim: Limits) -> List[str]:
        b = self.d.baseline
        inc_day = self.d.incident.date()
        obs_days = sum(1 for s in b.days if s.observed)
        cps = [c for c in b.change_points if c.day < inc_day]
        on_inc = sum(1 for c in b.change_points if c.day == inc_day and c.kind == APPEAR)
        out = ["### 変化点（日別件数から）", ""]
        if not cps:
            out.append(f"障害日より前に、出始めた・消えたテンプレートはない（判定に使えた日 {obs_days} 日の範囲で）。")
        else:
            tpl = self.d.templates
            cps.sort(key=lambda c: (c.day, c.kind != "消失", -tpl[c.key].total_max_severity, tpl[c.key].id))
            out += ["| 日付 | 変化 | ID | レベル | 変化の前 | 変化の後 | テンプレート |",
                    "|---|---|---|---|---|---|---|"]
            for c in cps[: lim.change_rows]:
                t = tpl[c.key]
                noise = "（既知ノイズ）" if t.noise_reason and not t.noise_suspended else ""
                out.append(f"| {fmt_day(c.day)} | {c.kind} | {t.id} | {t.total_level} | "
                           f"{c.before_present}/{c.before_days}日 | {c.after_present}/{c.after_days}日 | "
                           f"{_cell(_clip(t.text, 140))}{noise} |")
            if len(cps) > lim.change_rows:
                out.append("")
                out.append(f"…ほか {len(cps) - lim.change_rows} 件。")
            out.append("")
            out.append("「n/m日」は、その期間の m 日のうち n 日に出ていたという意味。"
                       "障害の発端が変化点にあることが多い（少しずつ進む障害は障害日には原因のログが出ないことがある）。")
            if b.days and cps[0].day <= next(s.day for s in b.days if s.observed) + timedelta(days=4):
                out.append("変化点が受け取ったログの最初の方にある。それより前が平常だったかを確かめるには、さらに前のログが要る。")
        if on_inc:
            out.append(f"障害日 {fmt_day(inc_day)}に初めて出たテンプレートは {on_inc} 種類（下の比較の「障害時のみ」と重なる）。")
        return out + [""]

    def _comparison_table(self, lim: Limits) -> List[str]:
        b = self.d.baseline
        if not b.periods:
            return []
        win = (self.d.start.time(), self.d.end - self.d.start)
        if b.selection == "fallback":
            title = "障害日の時間窓より前との比較"
        elif all((p.start.time(), p.end - p.start) == win for p in b.periods):
            title = "平常期間の同じ時刻帯との比較"
        else:
            title = "平常期間との比較"
        out = [f"### {title}", ""]
        if not b.comparisons:
            return out + ["判定が付いたテンプレートはない（時間窓のログは平常期間と同じような件数）。", ""]
        tpl = self.d.templates
        order = {k: i for i, k in enumerate(COMPARE_ORDER)}
        far = datetime.max

        def sort_key(c):
            t = tpl[c.key]
            if c.kind in ("消失", "激減"):
                return (order[c.kind], -c.expected, t.id)
            return (order[c.kind], t.first or far, t.id)

        comps = sorted(b.comparisons, key=sort_key)
        label = "平常" if title.endswith("同じ時刻帯との比較") else "平常（換算）"
        out += [f"| 判定 | ID | レベル | {label} | 窓内 | 窓内初出 | テンプレート |", "|---|---|---|---:|---:|---|---|"]
        for c in comps[: lim.compare_rows]:
            t = tpl[c.key]
            level = t.level or t.total_level
            noise = ""
            if t.noise_reason:
                noise = "（既知ノイズだがノイズ扱いをやめた）" if t.noise_suspended else "（既知ノイズ）"
            out.append(f"| {c.kind} | {t.id} | {level} | {_fmt_expected(c.expected)} | {_num(c.count)} | "
                       f"{fmt_ts(t.first, self.multi_day, False)} | {_cell(_clip(t.text, 140))}{noise} |")
        if len(comps) > lim.compare_rows:
            out.append("")
            out.append(f"…ほか {len(comps) - lim.compare_rows} 件。")
        out.append("")
        out.append("平常は時間窓の長さに換算した件数。判定が付かなかったテンプレート（平常どおり）は省略。"
                   "「消失」「激減」は ERROR にならない異常（処理が黙って止まる等）の手がかりになる。")
        return out + [""]

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
                note = f"（既知ノイズだが今回は{t.noise_suspended}）" if t.noise_suspended else ""
                out.append(f"| {t.id} | {level} | {_num(t.count)} | {fmt_ts(t.first, self.multi_day, False)} | "
                           f"{_num(t.total_count)} | {fmt_ts(t.total_first, True, False)} | "
                           f"{_cell(_clip(t.text, 160))}{_cell(note)} |")
            if len(rows) > lim.template_rows:
                out.append("")
                out.append(f"…ほか {len(rows) - lim.template_rows} 種類は {TEMPLATES_FILE} を参照。")
        out.append("")
        out.append(f"窓内のテンプレートは全 {len(all_win)} 種類（うち上表の対象 {len(rows)}、下に回した既知ノイズ {noise_n}）。"
                   f"INFO 以下を含む全種類と時刻は {TEMPLATES_FILE} にある。"
                   "全期間の初出が窓より前なら、障害前から出ていたログである。")
        return out + [""]

    def _noise_section(self, lim: Limits) -> List[str]:
        noise = [t for t in self._window_templates() if t.is_noise]
        title = "## 既知ノイズ（下に回したもの）"
        if not noise:
            if self.d.noise_file:
                return [title, "", "窓内に、下に回した既知ノイズはありません。", ""]
            return []
        expected = self.d.baseline.expected if self.d.baseline else {}
        out = [title, "",
               f"{self.d.noise_file} に登録された平常時のログ。上の集計・抽出には入れていないが、"
               "log-search では通常どおり検索できる。平常期間より急増したもの・新たに出始めたものは、"
               "ノイズ扱いをやめて上の表に入れてある。", ""]
        for t in sorted(noise, key=lambda t: -t.count)[: lim.noise_rows]:
            base = f"、平常期間 {_fmt_expected(expected.get(t.key, 0.0))}件" if expected or self._has_periods() else ""
            out.append(f"- {t.id} {_clip(t.text, 120)}（窓内 {_num(t.count)}件{base}）: {t.noise_reason}")
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
               "`>>` が ERROR・例外。下に回した既知ノイズは省いた。", "", "```"]
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
                hint = f"get_context {r.line_id}" + (f"、または {DIGEST_CONTEXT_FILE} の {where}" if where else "")
                out.append(f"…（以下 {remaining} 行省略。全文は {hint} で取得可）")
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
        triggers = [r for r in d.stream if r.is_trigger]
        example = triggers[0] if triggers else (d.stream[0] if d.stream else None)
        line_id = example.line_id if example else "<ファイル:行番号>"
        tid = d.templates[example.template_key].id if example else "<テンプレートID>"
        return [
            "## 省略した情報の確かめ方",
            "",
            "このダイジェストは抽出・集計した結果であり、省略した箇所がある。省略箇所を推測で埋めず、"
            "log-search skill のツールで確かめること（全期間の全レコードを検索でき、時間窓の外も調べられる）。",
            "",
            f"- 特定の行の前後: `get_context {line_id}`",
            "- キーワード・レベル・時間範囲・ファイルで検索: `search_logs`",
            f"- テンプレートの実際の行: `template_lines {tid}`",
            f"- 件数の時間推移（いつから出ているか・消えたか）: `count_by_time {tid}`",
            "- 上記で足りない集計: `run_sql`（読み取り専用）",
            f"- ERROR・例外の前後の文脈: {DIGEST_CONTEXT_FILE}（{n_sel} 個の塊。同じ ERROR の繰り返しだけの塊 "
            f"{n_all - n_sel} 個は省略）",
            f"- 窓内・全期間の全テンプレートの件数と初出・最終時刻: {TEMPLATES_FILE}",
            "- 根拠としてログを挙げるときは、時刻と `@ファイル:行番号` を付ける",
        ]

    # ---- context.md -----------------------------------------------------

    def context(self, msg_chars: int = 1000, extra_lines: int = 60) -> str:
        d = self.d
        out = [f"# ERROR・例外の前後の文脈（{d.ticket}）", "",
               f"時間窓: {fmt_range(d.start, d.end)} / "
               f"前後 {d.context_before}件・{d.context_after}件 / 下に回した既知ノイズは省いた",
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
                "total_first", "total_last", "noise_reason", "noise_suspended", "components", "template"]
        ts = list(self.d.templates.values())
        far = datetime.max
        ts.sort(key=lambda t: (t.count == 0, t.first or far, t.total_first or far, t.id))

        def iso(x: Optional[datetime]) -> str:
            return x.isoformat(sep=" ", timespec="milliseconds") if x else ""

        rows = ["\t".join(cols)]
        for t in ts:
            comps = ",".join(f"{c or '-'}:{n}" for c, n in sorted(t.components.items(), key=lambda kv: -kv[1]))
            vals = [t.id, t.level, str(t.count), iso(t.first), iso(t.last), str(t.total_count),
                    iso(t.total_first), iso(t.total_last), t.noise_reason or "", t.noise_suspended or "",
                    comps, t.text]
            rows.append("\t".join(v.replace("\t", " ").replace("\n", " ") for v in vals))
        return "\n".join(rows) + "\n"
