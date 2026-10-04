"""log-digest の出力（parsed/）を DuckDB に取り込み、読み取り専用で開く。"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import duckdb

from . import __version__

# log-digest の export.py の PARSED_SCHEMA と合わせる
PARSED_SCHEMA = 1
DB_FILE = "logs.duckdb"
DEFAULT_ROOT = "log-digest-out"

RECORD_COLUMNS = {
    "seq": "BIGINT",
    "file": "VARCHAR",
    "alias": "VARCHAR",
    "lineno": "INTEGER",
    "ts": "TIMESTAMP",
    "level": "VARCHAR",
    "severity": "TINYINT",
    "component": "VARCHAR",
    "template_id": "VARCHAR",
    "message": "VARCHAR",
    "extra": "VARCHAR",
    "extra_lines": "INTEGER",
}
TEMPLATE_COLUMNS = {
    "template_id": "VARCHAR",
    "level": "VARCHAR",
    "severity": "TINYINT",
    "total_count": "BIGINT",
    "first_ts": "TIMESTAMP",
    "last_ts": "TIMESTAMP",
    "window_count": "BIGINT",
    "window_first": "TIMESTAMP",
    "window_last": "TIMESTAMP",
    "noise_reason": "VARCHAR",
    "template": "VARCHAR",
}


def safe_name(s: str) -> str:
    """log-digest の出力ディレクトリ名と同じ規則（logdigest/cli.py の _safe_name）。"""
    return re.sub(r"[^\w.-]+", "_", s).strip("_") or "ticket"


def resolve_dir(ticket: Optional[str], directory: Optional[str], root: str = DEFAULT_ROOT) -> Path:
    if directory:
        d = Path(directory)
    elif ticket:
        d = Path(root) / safe_name(ticket)
    else:
        raise ValueError("--ticket か --dir のどちらかを指定してください")
    if not (d / "meta.json").is_file():
        raise FileNotFoundError(f"log-digest の出力が見つかりません: {d}（先に log-digest skill を実行してください）")
    return d


def load_meta(d: Path) -> dict:
    return json.loads((d / "meta.json").read_text(encoding="utf-8"))


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _columns_struct(cols: dict) -> str:
    return "{" + ", ".join(f"{_sql_str(k)}: {_sql_str(v)}" for k, v in cols.items()) + "}"


def _stamp(paths: List[Path]) -> str:
    """取り込み元が変わったか判定するための値（サイズと更新時刻）。"""
    parts = []
    for p in paths:
        st = p.stat()
        parts.append(f"{p.name}:{st.st_size}:{st.st_mtime_ns}")
    return "|".join(parts)


def _stamp_file(db: Path) -> Path:
    # DB の中ではなく別ファイルに置く。同じプロセスで DB を開いたまま確認すると、DuckDB は
    # 設定の違う2つ目の接続を拒否するため
    return db.with_name(db.name + ".stamp")


def _db_stamp(db: Path) -> Optional[str]:
    try:
        return _stamp_file(db).read_text(encoding="utf-8")
    except OSError:
        return None


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def ensure_db(d: Path) -> Tuple[Path, bool]:
    """parsed/ から DB を作る（最新なら何もしない）。戻り値は (DBのパス, 作り直したか)。"""
    meta = load_meta(d)
    parsed = meta.get("parsed") or {}
    if not parsed:
        raise ValueError(f"{d / 'meta.json'} に parsed がありません。log-digest 0.2.0 以降で実行し直してください")
    if parsed.get("schema") != PARSED_SCHEMA:
        raise ValueError(f"parsed の形式（schema={parsed.get('schema')}）に対応していません。"
                         f"log-search は schema={PARSED_SCHEMA} を読みます")
    rec, tpl = d / parsed["records"], d / parsed["templates"]
    for p in (rec, tpl):
        if not p.is_file():
            raise FileNotFoundError(f"取り込み元がありません: {p}")
    stamp = _stamp([d / "meta.json", rec, tpl])
    db = d / DB_FILE
    if db.is_file() and _db_stamp(db) == stamp:
        return db, False

    # 途中で失敗しても壊れた DB が残らないように、別名で作ってから置き換える
    tmp = d / f".{DB_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        con = duckdb.connect(str(tmp))
        try:
            _build(con, meta, rec, tpl, stamp)
        finally:
            con.close()
        os.replace(tmp, db)
        # DB を置き換えてから書く（途中で止まっても、次回は作り直しになるだけ）
        _stamp_file(db).write_text(stamp, encoding="utf-8")
    finally:
        if tmp.exists():
            tmp.unlink()
    return db, True


def _build(con: "duckdb.DuckDBPyConnection", meta: dict, rec: Path, tpl: Path, stamp: str) -> None:
    con.execute(f"""
        CREATE TABLE templates AS
        SELECT * FROM read_json({_sql_str(str(tpl))}, format='newline_delimited',
                                columns={_columns_struct(TEMPLATE_COLUMNS)})
        ORDER BY template_id""")
    con.execute(f"""
        CREATE TABLE logs AS
        SELECT r.seq, r.file || ':' || r.lineno AS line_id, r.file, r.alias, r.lineno, r.ts,
               r.level, r.severity, r.component, r.template_id,
               t.noise_reason IS NOT NULL AS noise,
               r.message, r.extra, r.extra_lines
        FROM read_json({_sql_str(str(rec))}, format='newline_delimited',
                       columns={_columns_struct(RECORD_COLUMNS)}) r
        LEFT JOIN templates t USING (template_id)
        ORDER BY r.seq""")

    window = meta.get("window") or {}
    con.execute("""
        CREATE TABLE incident (ticket VARCHAR, product VARCHAR, product_version VARCHAR, os VARCHAR,
                               symptom VARCHAR, incident_time TIMESTAMP, window_start TIMESTAMP,
                               window_end TIMESTAMP, timezone VARCHAR)""")
    con.execute("INSERT INTO incident VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        meta.get("ticket"), meta.get("product"), meta.get("product_version"), meta.get("os"),
        meta.get("symptom"), _parse_dt(meta.get("incident_time")), _parse_dt(window.get("start")),
        _parse_dt(window.get("end")), meta.get("timezone")])

    con.execute("""
        CREATE TABLE files (file VARCHAR, alias VARCHAR, format VARCHAR, encoding VARCHAR, records BIGINT,
                            unparsed_lines BIGINT, first_ts TIMESTAMP, last_ts TIMESTAMP,
                            clock_offset_seconds DOUBLE)""")
    for f in meta.get("files") or []:
        # first_ts / last_ts はタイムゾーンを揃えた naive な時刻（log-digest の表示用タイムゾーン）
        con.execute("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            f.get("file"), f.get("alias"), f.get("format"), f.get("encoding"), f.get("records"),
            (f.get("unparsed_lines") or 0) + (f.get("bad_timestamps") or 0),
            _parse_dt(f.get("first_ts")), _parse_dt(f.get("last_ts")), f.get("clock_offset_seconds")])

    con.execute("CREATE TABLE _ingest (stamp VARCHAR, tool VARCHAR, built_at TIMESTAMP)")
    con.execute("INSERT INTO _ingest VALUES (?, ?, ?)",
                [stamp, f"log-search {__version__}", datetime.now().replace(microsecond=0)])


def connect_readonly(db: Path, memory_limit: str = "1GB", threads: int = 2) -> "duckdb.DuckDBPyConnection":
    """読み取り専用で開く。外部ファイルの読み書き・拡張の読み込み・設定の変更もできない。"""
    return duckdb.connect(str(db), read_only=True, config={
        "enable_external_access": False,
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
        "memory_limit": memory_limit,
        "threads": threads,
        "lock_configuration": True,
    })


def _time_re(prefix: str) -> str:
    return (rf"(?P<{prefix}h>\d{{1,2}}):(?P<{prefix}mi>\d{{2}})"
            rf"(?::(?P<{prefix}s>\d{{2}})(?:[.,](?P<{prefix}f>\d{{1,6}}))?)?")


_DATE_RE = r"(?P<date>(?:(?P<y>\d{4})[-/])?(?P<mo>\d{1,2})[-/](?P<d>\d{1,2}))"
# 日付付き（時刻は省略可）か、時刻だけ（グループ名に t_ を付けて区別する）
_TIME_ARG_RE = re.compile(rf"^\s*(?:{_DATE_RE}(?:[ T]+{_time_re('')})?|{_time_re('t_')})\s*$")


def parse_time_arg(text: str, ref: datetime, upper: bool) -> datetime:
    """--from / --to の時刻を解釈する。時刻は log-digest の表示用タイムゾーン。

    受け付ける形: '2026-09-30 10:42:05.907' / '2026-09-30 10:42' / '2026-09-30' / '09-30 10:42' / '10:42'
    年や日付を省くと ref（申告時刻）の年・日付を補う。
    upper=True（--to）のときは、書いた精度の終わりまでを含める上限（その値は含まない）を返す。
    例: '10:42' → 10:43:00 未満、'2026-09-30' → 10-01 00:00 未満。
    """
    m = _TIME_ARG_RE.match(text)
    if not m:
        raise ValueError(f"時刻を解釈できません: {text!r}（例: '2026-09-30 10:42', '09-30 10:42', '10:42'）")
    g = m.groupdict()
    if g["date"]:
        y = int(g["y"]) if g["y"] else ref.year
        base = datetime(y, int(g["mo"]), int(g["d"]))
        h, mi, s, f = g["h"], g["mi"], g["s"], g["f"]
    else:
        base = datetime(ref.year, ref.month, ref.day)
        h, mi, s, f = g["t_h"], g["t_mi"], g["t_s"], g["t_f"]
    if h is None:
        dt, unit = base, timedelta(days=1)
    else:
        dt = base.replace(hour=int(h), minute=int(mi), second=int(s or 0),
                          microsecond=int((f or "0").ljust(6, "0")))
        if s is None:
            unit = timedelta(minutes=1)
        elif f is None:
            unit = timedelta(seconds=1)
        else:
            unit = timedelta(microseconds=10 ** (6 - len(f)))
    return dt + unit if upper else dt
