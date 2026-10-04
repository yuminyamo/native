"""ガイドの6つのツール（get_digest, search_logs, get_context, template_lines, count_by_time, run_sql）と schema。

どれも結果をテキストで返し、返す量には config/limits.yaml の上限をかける。
CLI から呼ぶが、MCP サーバ化するときはこの関数をそのまま公開できるようにしてある。
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import duckdb

from .store import parse_time_arg

SEVERITY = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "ERROR": 4, "FATAL": 5}
LEVEL_ALIASES = {"WARNING": "WARN", "ERR": "ERROR", "CRITICAL": "FATAL"}

LINE_COLS = "seq, line_id, file, alias, lineno, ts, level, component, template_id, noise, message, extra, extra_lines"


class ToolError(Exception):
    """AIに見せるエラー（引数の誤りなど）。"""


class Limits:
    def __init__(self, raw: dict):
        self.raw = raw or {}

    def get(self, section: str, key: str, default):
        return (self.raw.get(section) or {}).get(key, default)

    def cap(self, section: str, key: str, requested: Optional[int], default_key: Optional[str],
            default: int) -> int:
        """引数の値を上限で抑える。未指定なら default_key の値（無ければ上限）。"""
        hi = int(self.get(section, key, default))
        if requested is None:
            return int(self.get(section, default_key, hi)) if default_key else hi
        return max(0, min(int(requested), hi))


# ---- 共通 ---------------------------------------------------------------

class Ctx:
    """1回のツール呼び出しで使う接続と設定。"""

    def __init__(self, con: "duckdb.DuckDBPyConnection", limits: Limits, out_dir: Path):
        self.con = con
        self.limits = limits
        self.out_dir = out_dir
        row = con.execute("SELECT incident_time, window_start, window_end, timezone FROM incident").fetchone()
        self.incident: datetime = row[0] if row and row[0] else datetime.now()
        self.window = (row[1], row[2]) if row else (None, None)
        self.timezone = (row[3] if row else None) or "?"
        self.msg_chars = int(limits.get("common", "msg_chars", 300))

    def q(self, sql: str, params: Sequence = ()) -> List[tuple]:
        return self.con.execute(sql, list(params)).fetchall()

    def time_range(self, frm: Optional[str], to: Optional[str]) -> Tuple[Optional[datetime], Optional[datetime]]:
        lo = parse_time_arg(frm, self.incident, upper=False) if frm else None
        hi = parse_time_arg(to, self.incident, upper=True) if to else None
        if lo and hi and lo >= hi:
            raise ToolError(f"--from（{lo}）が --to（{hi}）より後です")
        return lo, hi


def _clip(text: str, n: int) -> str:
    text = (text or "").replace("\t", "  ").replace("\r", "")
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


def fmt_ts(dt: Optional[datetime], millis: bool = True) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%m-%d %H:%M:%S") + (f".{dt.microsecond // 1000:03d}" if millis else "")


def fmt_range(lo: Optional[datetime], hi: Optional[datetime]) -> str:
    a = lo.strftime("%Y-%m-%d %H:%M:%S") if lo else "最初"
    b = hi.strftime("%Y-%m-%d %H:%M:%S") if hi else "最後"
    return f"{a} 〜 {b}" + ("（未満）" if hi else "")


def fmt_line(row: tuple, mark: str, msg_chars: int, show_extra: int = 0) -> List[str]:
    """LINE_COLS の1行を表示用の行にする。mark は '>> '（対象）/ ' ~ '（既知ノイズ）/ '   '。"""
    (_seq, line_id, _file, alias, _lineno, ts, level, comp, tid, _noise, msg, extra, extra_lines) = row
    comp_s = f"[{comp}] " if comp else ""
    head = f"{mark}{fmt_ts(ts)} {tid} {level:<5} {alias} {comp_s}{_clip(msg, msg_chars)}  @{line_id}"
    out = [head]
    if extra_lines:
        lines = (extra or "").split("\n")
        shown = lines[:show_extra] if show_extra > 0 else []
        out += ["      " + _clip(x, msg_chars) for x in shown]
        rest = extra_lines - len(shown)
        if rest > 0:
            if shown:
                out.append(f"      …（続き {rest} 行省略）")
            else:
                out[0] += f"  （続き {rest} 行）"
    return out


def mark_of(row: tuple) -> str:
    return " ~ " if row[9] else "   "


def _norm_level(level: str) -> int:
    up = level.strip().upper()
    up = LEVEL_ALIASES.get(up, up)
    if up not in SEVERITY:
        raise ToolError(f"レベルを解釈できません: {level!r}（{', '.join(SEVERITY)}）")
    return SEVERITY[up]


def _split_ids(values: Sequence[str]) -> List[str]:
    out: List[str] = []
    for v in values:
        for x in re.split(r"[,\s]+", v):
            x = x.strip()
            if x and x not in out:
                out.append(x.upper() if re.fullmatch(r"[tT]\d+", x) else x)
    return out


def _resolve_file(ctx: Ctx, name: str) -> str:
    """'pms-server.log' / 'server/pms-server.log' / 'pms-server'（別名）からファイルを1つに決める。"""
    files = [r[0] for r in ctx.q("SELECT file FROM files ORDER BY file")]
    if not files:
        files = [r[0] for r in ctx.q("SELECT DISTINCT file FROM logs ORDER BY file")]
    if name in files:
        return name
    aliases = {r[0]: r[1] for r in ctx.q("SELECT DISTINCT file, alias FROM logs")}
    cands = [f for f in files if f.endswith("/" + name) or f.rsplit("/", 1)[-1] == name or aliases.get(f) == name]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise ToolError(f"ファイル {name!r} がありません。ファイル: {', '.join(files)}")
    raise ToolError(f"ファイル {name!r} に該当するものが複数あります: {', '.join(cands)}。パスで指定してください")


# ---- get_digest -----------------------------------------------------------

def get_digest(out_dir: Path, limits: Limits) -> str:
    found = sorted(out_dir.glob("digest_*.md"))
    if not found:
        raise ToolError(f"{out_dir} にダイジェストがありません。log-digest skill を実行してください")
    text = found[0].read_text(encoding="utf-8")
    cap = int(limits.get("get_digest", "max_chars", 20000))
    if len(text) > cap:
        text = text[:cap].rstrip() + f"\n\n…（{len(text) - cap} 文字省略。{found[0]} を参照）\n"
    return text


# ---- search_logs ------------------------------------------------------------

def search_logs(ctx: Ctx, query: Sequence[str] = (), regex: bool = False, level: Optional[str] = None,
                frm: Optional[str] = None, to: Optional[str] = None, files: Sequence[str] = (),
                components: Sequence[str] = (), templates: Sequence[str] = (), exclude_noise: bool = False,
                message_only: bool = False, order: str = "asc", limit: Optional[int] = None) -> str:
    lim = ctx.limits
    max_lines = max(1, lim.cap("search_logs", "max_lines", limit, None, 200))
    overflow = min(max_lines, int(lim.get("search_logs", "overflow_lines", 20)))
    n_tpl = int(lim.get("search_logs", "overflow_templates", 10))

    where: List[str] = []
    params: List = []
    cond: List[str] = []
    for q in query:
        if regex:
            try:
                re.compile(q)
            except re.error as e:
                raise ToolError(f"正規表現の誤り: {q!r}: {e}") from e
            m = "regexp_matches(message, ?)"
            e = "regexp_matches(coalesce(extra, ''), ?)"
        else:
            m = "strpos(lower(message), lower(?)) > 0"
            e = "strpos(lower(coalesce(extra, '')), lower(?)) > 0"
        if message_only:
            where.append(m)
            params.append(q)
        else:
            where.append(f"({m} OR {e})")
            params += [q, q]
        cond.append(("regex " if regex else "") + repr(q))
    if level:
        sev = _norm_level(level)
        where.append("severity >= ?")
        params.append(sev)
        cond.append(f"level>={level.upper()}")
    lo, hi = ctx.time_range(frm, to)
    if lo:
        where.append("ts >= ?")
        params.append(lo)
    if hi:
        where.append("ts < ?")
        params.append(hi)
    if lo or hi:
        cond.append(fmt_range(lo, hi))
    if files:
        resolved = [_resolve_file(ctx, f) for f in files]
        where.append("file IN (" + ", ".join("?" * len(resolved)) + ")")
        params += resolved
        cond.append("file=" + ",".join(resolved))
    if components:
        where.append("lower(component) IN (" + ", ".join("?" * len(components)) + ")")
        params += [c.lower() for c in components]
        cond.append("component=" + ",".join(components))
    tids = _split_ids(templates)
    if tids:
        where.append("template_id IN (" + ", ".join("?" * len(tids)) + ")")
        params += tids
        cond.append("template=" + ",".join(tids))
    if exclude_noise:
        where.append("NOT noise")
        cond.append("既知ノイズ除外")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    direction = "DESC" if order == "desc" else "ASC"

    try:
        total = ctx.q(f"SELECT count(*) FROM logs{w}", params)[0][0]
    except duckdb.Error as e:
        raise ToolError(f"検索できません: {e}") from e
    head = f"search_logs: {' / '.join(cond) or '条件なし'} → {total:,}件"
    if order == "desc":
        head += "（新しい順）"
    out = [head]
    if total == 0:
        out.append("一致なし。キーワードを短くする、--level や時間範囲を外す、--regex を使う、などで条件を緩めること。")
        return "\n".join(out) + "\n"

    shown = total if total <= max_lines else overflow
    rows = ctx.q(f"SELECT {LINE_COLS} FROM logs{w} ORDER BY ts {direction}, seq {direction} LIMIT ?",
                 params + [shown])
    if total > max_lines:
        out.append(f"上限 {max_lines} 件を超えたため、先頭 {shown} 件だけを示す。条件を絞るか、"
                   "count_by_time / run_sql で集計すること。")
    out.append("")
    for r in rows:
        out += fmt_line(r, mark_of(r), ctx.msg_chars)

    if total > max_lines:
        brk = ctx.q(f"""
            SELECT m.template_id, m.n, m.a, m.b, t.template
            FROM (SELECT template_id, count(*) n, min(ts) a, max(ts) b FROM logs{w} GROUP BY 1) m
            LEFT JOIN templates t USING (template_id)
            ORDER BY m.n DESC, m.template_id LIMIT ?""", params + [n_tpl + 1])
        out += ["", f"テンプレート別の内訳（件数順・上位{min(n_tpl, len(brk))}）:"]
        for tid, n, a, b, text in brk[:n_tpl]:
            out.append(f"  {tid}  {n:,}件  {fmt_ts(a, False)}〜{fmt_ts(b, False)}  {_clip(text or '', 100)}")
        if len(brk) > n_tpl:
            out.append("  …ほか（run_sql で GROUP BY template_id すると全部見られる）")
    return "\n".join(out) + "\n"


# ---- get_context --------------------------------------------------------------

_LINE_ID_RE = re.compile(r"^@?(?P<file>.+):(?P<lineno>\d+)$")


def get_context(ctx: Ctx, line_id: str, before: Optional[int] = None, after: Optional[int] = None,
                merged: bool = False, extra_lines: Optional[int] = None) -> str:
    lim = ctx.limits
    nb = lim.cap("get_context", "max_before", before, "default_before", 50)
    na = lim.cap("get_context", "max_after", after, "default_after", 50)
    nx = lim.cap("get_context", "max_extra_lines", extra_lines, "default_extra_lines", 10)
    target_extra = int(lim.get("get_context", "target_extra_lines", 60))

    m = _LINE_ID_RE.match(line_id.strip())
    if not m:
        raise ToolError(f"行の指定は ファイル:行番号 の形にしてください（例: pms-server.log:4414）: {line_id!r}")
    file = _resolve_file(ctx, m.group("file"))
    lineno = int(m.group("lineno"))
    hit = ctx.q(f"SELECT {LINE_COLS} FROM logs WHERE file = ? AND lineno <= ? ORDER BY lineno DESC LIMIT 1",
                [file, lineno])
    if not hit:
        raise ToolError(f"{file}:{lineno} より前にレコードがありません（ファイル先頭のヘッダ行などは取り込んでいない）")
    target = hit[0]
    seq, t_lineno = target[0], target[4]

    if merged:
        prev = ctx.q(f"SELECT {LINE_COLS} FROM logs WHERE seq < ? ORDER BY seq DESC LIMIT ?", [seq, nb])
        nxt = ctx.q(f"SELECT {LINE_COLS} FROM logs WHERE seq > ? ORDER BY seq LIMIT ?", [seq, na])
        scope = "全ファイルを時刻順にマージ"
    else:
        prev = ctx.q(f"SELECT {LINE_COLS} FROM logs WHERE file = ? AND lineno < ? ORDER BY lineno DESC LIMIT ?",
                     [file, t_lineno, nb])
        nxt = ctx.q(f"SELECT {LINE_COLS} FROM logs WHERE file = ? AND lineno > ? ORDER BY lineno LIMIT ?",
                    [file, t_lineno, na])
        scope = "同じファイル"

    out = [f"get_context: {file}:{lineno}（前 {len(prev)} 件・後 {len(nxt)} 件、{scope}）"]
    if t_lineno != lineno:
        out.append(f"{lineno} 行目は {file}:{t_lineno} のレコードの続き行（スタックトレース等）なので、そのレコードを対象にした。")
    out.append("`>>` が対象、`~` が既知ノイズ。")
    out.append("")
    for r in reversed(prev):
        out += fmt_line(r, mark_of(r), ctx.msg_chars, nx)
    out += fmt_line(target, ">> ", ctx.msg_chars, target_extra)
    for r in nxt:
        out += fmt_line(r, mark_of(r), ctx.msg_chars, nx)
    if len(prev) == nb and nb:
        out.append(f"\n（さらに前は get_context {prev[-1][1]} --before {nb}）" if not merged
                   else f"\n（さらに前は get_context {prev[-1][1]} --merged --before {nb}）")
    if len(nxt) == na and na:
        out.append(f"（さらに後は get_context {nxt[-1][1]}{' --merged' if merged else ''} --after {na}）")
    return "\n".join(out) + "\n"


# ---- template_lines -----------------------------------------------------------

def template_lines(ctx: Ctx, template_id: str, limit: Optional[int] = None, frm: Optional[str] = None,
                   to: Optional[str] = None, sample: str = "first") -> str:
    n = max(1, ctx.limits.cap("template_lines", "max_lines", limit, None, 20))
    tid = _split_ids([template_id])[0] if template_id.strip() else ""
    info = ctx.q("""SELECT template_id, level, total_count, first_ts, last_ts, window_count, noise_reason, template
                    FROM templates WHERE template_id = ?""", [tid])
    if not info:
        raise ToolError(f"テンプレート {template_id!r} がありません（ID はダイジェストか templates.tsv の T01 など）")
    tid, level, total, first, last, wcount, noise, text = info[0]
    out = [f"template_lines: {tid} {level}  全期間 {total:,}件（{fmt_ts(first, False)}〜{fmt_ts(last, False)}）"
           f"  時間窓内 {wcount:,}件",
           f"テンプレート: {_clip(text, 500)}"]
    if noise:
        out.append(f"既知ノイズ: {noise}")

    where, params = ["template_id = ?"], [tid]
    lo, hi = ctx.time_range(frm, to)
    if lo:
        where.append("ts >= ?")
        params.append(lo)
    if hi:
        where.append("ts < ?")
        params.append(hi)
    w = " AND ".join(where)
    matched = ctx.q(f"SELECT count(*) FROM logs WHERE {w}", params)[0][0]
    if lo or hi:
        out.append(f"範囲 {fmt_range(lo, hi)} の件数: {matched:,}件")
    if sample == "last":
        rows = ctx.q(f"SELECT {LINE_COLS} FROM logs WHERE {w} ORDER BY ts DESC, seq DESC LIMIT ?", params + [n])
        rows.reverse()
        how = f"最後の {len(rows)} 件"
    elif sample == "spread" and matched > n:
        # 期間全体から等間隔に n 件取る（値の変わり方を見るため）
        rows = ctx.q(f"""
            SELECT {LINE_COLS} FROM (
                SELECT *, row_number() OVER (ORDER BY ts, seq) - 1 AS rn FROM logs WHERE {w})
            WHERE rn IN (SELECT CAST(floor(i * ? / ?) AS BIGINT) FROM range(?) r(i))
            ORDER BY ts, seq""", params + [matched, n, n])
        how = f"全体から等間隔に {len(rows)} 件"
    else:
        rows = ctx.q(f"SELECT {LINE_COLS} FROM logs WHERE {w} ORDER BY ts, seq LIMIT ?", params + [n])
        how = f"最初の {len(rows)} 件"
    out += [f"表示: {how}（{matched:,}件中）", ""]
    for r in rows:
        out += fmt_line(r, mark_of(r), ctx.msg_chars)
    return "\n".join(out) + "\n"


# ---- count_by_time ------------------------------------------------------------

_NICE_BUCKETS = [timedelta(seconds=s) for s in (
    1, 5, 10, 30, 60, 300, 600, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 7 * 86400)]
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
_BUCKET_RE = re.compile(r"^(\d+)\s*(s|sec|m|min|h|hour|d|day)s?$", re.IGNORECASE)


def parse_bucket(text: str) -> timedelta:
    m = _BUCKET_RE.match(text.strip())
    if not m:
        raise ToolError(f"区間の幅を解釈できません: {text!r}（例: 30s, 1m, 5m, 1h, 1d）")
    n, unit = int(m.group(1)), m.group(2)[0].lower()
    if n <= 0:
        raise ToolError("区間の幅は1以上にしてください")
    return timedelta(**{_UNITS[unit]: n})


def _fmt_bucket(td: timedelta) -> str:
    s = int(td.total_seconds())
    for unit, sec in (("d", 86400), ("h", 3600), ("m", 60)):
        if s % sec == 0:
            return f"{s // sec}{unit}"
    return f"{s}s"


def _floor(dt: datetime, width: timedelta) -> datetime:
    """幅の倍数の区切り（その日の0時起点）に切り下げる。1日より広い幅は日単位に切り下げる。"""
    day = datetime(dt.year, dt.month, dt.day)
    if width >= timedelta(days=1):
        return day
    return day + ((dt - day) // width) * width


def count_by_time(ctx: Ctx, templates: Sequence[str], bucket: Optional[str] = None,
                  frm: Optional[str] = None, to: Optional[str] = None) -> str:
    lim = ctx.limits
    max_buckets = int(lim.get("count_by_time", "max_buckets", 120))
    max_tpl = int(lim.get("count_by_time", "max_templates", 8))
    tids = _split_ids(templates)
    if not tids:
        raise ToolError("テンプレートIDを1つ以上指定してください（例: T07,T12）")
    if len(tids) > max_tpl:
        raise ToolError(f"テンプレートは一度に {max_tpl} 個までです")
    ph = ", ".join("?" * len(tids))
    info = {r[0]: r[1:] for r in ctx.q(
        f"SELECT template_id, level, template, first_ts, last_ts FROM templates WHERE template_id IN ({ph})", tids)}
    missing = [t for t in tids if t not in info]
    if missing:
        raise ToolError(f"テンプレートがありません: {', '.join(missing)}")

    lo, hi = ctx.time_range(frm, to)
    if lo is None or hi is None:
        # 範囲を省いたら、ログ全体の期間（消えた・出始めた時期が分かるように）
        a, b = ctx.q("SELECT min(ts), max(ts) FROM logs")[0]
        lo = lo or a
        hi = hi or (b + timedelta(microseconds=1))
    notes: List[str] = []
    span = hi - lo
    if bucket:
        width = parse_bucket(bucket)
        if span / width > max_buckets:
            fit = next((b for b in _NICE_BUCKETS if b >= width and span / b <= max_buckets), None)
            if fit is None:
                raise ToolError(f"区間が {max_buckets} 個を超えます。時間範囲を狭めてください")
            notes.append(f"{_fmt_bucket(width)} 幅では {max_buckets} 区間を超えるため {_fmt_bucket(fit)} 幅にした")
            width = fit
    else:
        width = next((b for b in _NICE_BUCKETS if span / b <= max_buckets), None)
        if width is None:
            raise ToolError(f"区間が {max_buckets} 個を超えます。時間範囲を狭めてください")
    start = _floor(lo, width)
    n_buckets = max(1, -(-(hi - start) // width))
    if n_buckets > max_buckets:  # 切り下げで1つ増えた場合
        n_buckets = max_buckets

    width_us = int(width / timedelta(microseconds=1))
    rows = ctx.q(f"""
        SELECT CAST((epoch_us(ts) - epoch_us(?::TIMESTAMP)) // ? AS BIGINT) AS b, template_id, count(*)
        FROM logs WHERE template_id IN ({ph}) AND ts >= ? AND ts < ?
        GROUP BY 1, 2""", [start, width_us] + tids + [lo, hi])
    counts: Dict[Tuple[int, str], int] = {(b, t): n for b, t, n in rows}

    label_fmt = "%m-%d" if width >= timedelta(days=1) else ("%m-%d %H:%M" if width >= timedelta(minutes=1)
                                                            else "%m-%d %H:%M:%S")
    out = [f"count_by_time: {', '.join(tids)} / {_fmt_bucket(width)} 幅 / {fmt_range(lo, hi)}"]
    out += notes
    for t in tids:
        level, text, first, last = info[t]
        out.append(f"  {t} {level}  {_clip(text, 100)}（全期間 {fmt_ts(first, False)}〜{fmt_ts(last, False)}）")
    out += ["", "\t".join(["区間の始まり"] + tids)]
    # すべて0の区間が3つ以上続くところは1行にまとめる（「消えた」「出始めた」の境目は残る）
    b = 0
    while b < n_buckets:
        e = b
        while e < n_buckets and all(counts.get((e, t), 0) == 0 for t in tids):
            e += 1
        if e - b >= 3:
            a_label = (start + b * width).strftime(label_fmt)
            z_label = (start + (e - 1) * width).strftime(label_fmt)
            out.append("\t".join([f"{a_label}〜{z_label}"] + ["0"] * len(tids)) + f"\t（{e - b} 区間すべて0）")
            b = e
            continue
        label = (start + b * width).strftime(label_fmt)
        out.append("\t".join([label] + [str(counts.get((b, t), 0)) for t in tids]))
        b += 1
    totals = [sum(n for (b, tt), n in counts.items() if tt == t) for t in tids]
    out.append("\t".join(["合計"] + [str(x) for x in totals]))
    return "\n".join(out) + "\n"


# ---- run_sql --------------------------------------------------------------

_ALLOWED = {"SELECT", "EXPLAIN"}


def run_sql(ctx: Ctx, sql: str, max_rows: Optional[int] = None, timeout: Optional[float] = None) -> str:
    lim = ctx.limits
    n = max(1, lim.cap("run_sql", "max_rows", max_rows, None, 200))
    hi_timeout = float(lim.get("run_sql", "timeout_seconds", 30))
    t_sec = hi_timeout if timeout is None else max(0.1, min(float(timeout), hi_timeout))
    cell_chars = int(lim.get("run_sql", "cell_chars", 300))

    try:
        stmts = duckdb.extract_statements(sql)
    except duckdb.Error as e:
        raise ToolError(f"SQL の構文エラー: {e}") from e
    if len(stmts) != 1:
        raise ToolError(f"SQL は1文だけにしてください（{len(stmts)} 文あります）")
    kind = getattr(stmts[0].type, "name", str(stmts[0].type))
    if kind not in _ALLOWED:
        raise ToolError(f"読み取り（SELECT）だけを実行できます（この文は {kind}）")

    timer = threading.Timer(t_sec, ctx.con.interrupt)
    timer.start()
    try:
        cur = ctx.con.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(n + 1)
    except duckdb.InterruptException as e:
        raise ToolError(f"{t_sec:g} 秒で打ち切った。WHERE で期間・テンプレートを絞るか、集計してから取り出すこと") from e
    except duckdb.Error as e:
        raise ToolError(f"SQL エラー: {e}\n（テーブルと列は schema で確認できる）") from e
    finally:
        timer.cancel()

    def cell(v) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, datetime):
            return v.isoformat(sep=" ", timespec="milliseconds")
        return _clip(str(v).replace("\n", "⏎"), cell_chars)

    more = len(rows) > n
    rows = rows[:n]
    out = [f"run_sql: {len(rows)} 行" + (f"（上限 {n} 行で打ち切り。LIMIT や集計で絞ること）" if more else ""), ""]
    out.append("\t".join(cols))
    out += ["\t".join(cell(v) for v in r) for r in rows]
    return "\n".join(out) + "\n"


# ---- schema -----------------------------------------------------------------

TABLE_DOCS = {
    "logs": "全期間の全レコード（1行目が書式に一致した行を1件とし、続く行は extra に入る）",
    "templates": "テンプレート（Drain3）ごとの件数・初出・最終時刻。ID はダイジェストと同じ",
    "incident": "チケット情報と時間窓（1行）",
    "files": "入力ファイルごとの統計",
}
COLUMN_DOCS = {
    ("logs", "seq"): "全ファイルを時刻順にマージした通し番号",
    ("logs", "line_id"): "'ファイル:行番号'。根拠として挙げる時と get_context に使う",
    ("logs", "lineno"): "元ファイルの行番号（1始まり）",
    ("logs", "ts"): "時刻（タイムゾーンをそろえ、時計のずれも補正済み）",
    ("logs", "severity"): "TRACE=0 DEBUG=1 INFO=2 WARN=3 ERROR=4 FATAL=5",
    ("logs", "noise"): "既知ノイズ辞書に一致したテンプレートなら true",
    ("logs", "message"): "1行目のメッセージ部分（時刻・レベル・コンポーネントを除く）",
    ("logs", "extra"): "続く行（スタックトレース等）を改行区切りで。無ければ NULL",
    ("logs", "extra_lines"): "続く行の数（取り込み時に切った分も含む）",
    ("templates", "level"): "全期間で最も重いレベル",
    ("templates", "window_count"): "時間窓内の件数",
    ("templates", "noise_reason"): "既知ノイズの理由（該当しなければ NULL）",
}


def schema(ctx: Ctx) -> str:
    out = [f"schema: 時刻はすべて {ctx.timezone} の naive TIMESTAMP（log-digest の表示用タイムゾーン）", ""]
    for table, doc in TABLE_DOCS.items():
        n = ctx.q(f"SELECT count(*) FROM {table}")[0][0]
        out.append(f"## {table}（{n:,} 行）: {doc}")
        for name, typ, *_ in ctx.q(f"DESCRIBE {table}"):
            d = COLUMN_DOCS.get((table, name))
            out.append(f"  {name} {typ}" + (f"  -- {d}" if d else ""))
        out.append("")
    inc = ctx.q("SELECT ticket, incident_time, window_start, window_end FROM incident")
    if inc:
        t, i, a, b = inc[0]
        out.append(f"チケット {t} / 申告時刻 {i} / 時間窓 {a} 〜 {b}")
    out += ["", "例:",
            "  SELECT ts, component, message FROM logs WHERE component = 'cleanup' ORDER BY ts DESC LIMIT 5",
            "  SELECT date_trunc('hour', ts) h, count(*) FROM logs WHERE severity >= 4 GROUP BY 1 ORDER BY 1",
            "  SELECT template_id, total_count, first_ts, template FROM templates "
            "WHERE level = 'WARN' ORDER BY first_ts"]
    return "\n".join(out) + "\n"
