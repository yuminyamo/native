"""ログファイルの読み込み、1行のレコード分解、時刻の正規化。"""

from __future__ import annotations

import codecs
import fnmatch
import gzip
import re
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

SEVERITY = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "ERROR": 4, "FATAL": 5}
ERROR_SEVERITY = SEVERITY["ERROR"]
WARN_SEVERITY = SEVERITY["WARN"]

# 1レコードに付ける続き行の上限（メモリ保護）。超えた分は件数だけ数える
MAX_EXTRA_LINES = 300

# スタックトレースらしい続き行（Java / .NET / Python）
STACK_LINE_RE = re.compile(
    r"^\s+at\s+\S"
    r"|^\s*Caused by:"
    r"|^Traceback \(most recent call last\)"
    r'|^\s+File "'
    r"|^\s*\.\.\. \d+ more"
    r"|^\s*---> "
    r"|^[\w.$]+(?:Exception|Error)\b"
)

_ISO_TS_RE = re.compile(
    r"^(\d{4})[-/](\d{2})[-/](\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
    r"(?:[.,](\d{1,9}))?\s*(Z|[+-]\d{2}:?\d{2})?$"
)
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


class Record:
    """ログ1件。1行目と、それに続く行（スタックトレース等）をまとめて持つ。"""

    __slots__ = (
        "file", "alias", "file_order", "lineno", "ts", "level", "severity",
        "component", "message", "extra", "extra_dropped", "template_key",
    )

    def __init__(self, file: str, alias: str, file_order: int, lineno: int, ts: datetime,
                 level: str, component: str, message: str):
        self.file = file
        self.alias = alias
        self.file_order = file_order
        self.lineno = lineno
        self.ts = ts
        self.level = level
        self.severity = SEVERITY.get(level, SEVERITY["INFO"])
        self.component = component
        self.message = message
        self.extra: List[str] = []
        self.extra_dropped = 0
        self.template_key: Optional[int] = None

    @property
    def line_id(self) -> str:
        return f"{self.file}:{self.lineno}"

    @property
    def has_stack(self) -> bool:
        return any(STACK_LINE_RE.match(line) for line in self.extra[:20])

    @property
    def is_trigger(self) -> bool:
        """ERROR 以上、またはスタックトレースを伴うレコード。"""
        return self.severity >= ERROR_SEVERITY or self.has_stack

    def sort_key(self) -> Tuple[datetime, int, int]:
        return (self.ts, self.file_order, self.lineno)


class FileStats:
    def __init__(self, file: str, alias: str, fmt_name: str):
        self.file = file
        self.alias = alias
        self.format = fmt_name
        self.encoding = ""
        self.total_lines = 0
        self.records = 0
        self.continuation_lines = 0
        self.unparsed_lines = 0
        self.bad_timestamps = 0
        self.unknown_levels: Dict[str, int] = {}
        self.first_ts: Optional[datetime] = None
        self.last_ts: Optional[datetime] = None
        self.clock_offset_seconds = 0.0
        self.window_records = 0

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "alias": self.alias,
            "format": self.format,
            "encoding": self.encoding,
            "total_lines": self.total_lines,
            "records": self.records,
            "continuation_lines": self.continuation_lines,
            "unparsed_lines": self.unparsed_lines,
            "bad_timestamps": self.bad_timestamps,
            "unknown_levels": self.unknown_levels,
            "first_ts": self.first_ts.isoformat() if self.first_ts else None,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "clock_offset_seconds": self.clock_offset_seconds,
            "window_records": self.window_records,
        }


def parse_tz(spec: str) -> tzinfo:
    """'+09:00' / 'UTC' / 'Z' / IANA名（'Asia/Tokyo'）を tzinfo にする。"""
    s = str(spec).strip()
    if s.upper() in ("UTC", "Z", "GMT"):
        return timezone.utc
    m = _OFFSET_RE.match(s)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(s)
    except Exception as e:  # noqa: BLE001 - zoneinfo の例外型は環境で異なる
        raise ValueError(f"タイムゾーンを解釈できません: {spec!r}") from e


def parse_timestamp(text: str, ts_format: Optional[str], default_tz: tzinfo) -> datetime:
    """時刻文字列をタイムゾーン付き datetime にする。解釈できなければ ValueError。"""
    if ts_format:
        dt = datetime.strptime(text.strip(), ts_format)
    else:
        m = _ISO_TS_RE.match(text.strip())
        if not m:
            raise ValueError(text)
        y, mo, d, h, mi, s, frac, off = m.groups()
        micro = int((frac or "0")[:6].ljust(6, "0"))
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), micro)
        if off:
            dt = dt.replace(tzinfo=parse_tz(off))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt


def file_alias(name: str) -> str:
    """'pms-server.log.1' → 'pms-server'。表示用の短い名前。"""
    base = name
    if base.lower().endswith(".gz"):
        base = base[:-3]
    base = re.sub(r"\.\d+$", "", base)
    base = re.sub(r"\.(log|txt)$", "", base, flags=re.IGNORECASE)
    return base or name


def decode_bytes(data: bytes, encoding: str) -> Tuple[str, str]:
    """バイト列を文字列にする。戻り値は (文字列, 実際に使ったエンコーディング)。"""
    if encoding and encoding != "auto":
        return data.decode(encoding, errors="replace"), encoding
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16", errors="replace"), "utf-16"
    for enc in ("utf-8-sig", "cp932"):
        try:
            return data.decode(enc), ("utf-8" if enc == "utf-8-sig" else enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(置換あり)"


def read_text(path: Path, encoding: str) -> Tuple[str, str]:
    if path.suffix.lower() == ".gz":
        with gzip.open(path, "rb") as f:
            data = f.read()
    else:
        data = path.read_bytes()
    return decode_bytes(data, encoding)


class LogFormat:
    def __init__(self, raw: dict, defaults: dict, level_aliases: Dict[str, str]):
        self.name = raw.get("name", "unnamed")
        self.files: List[str] = list(raw.get("files") or ["*"])
        patterns = raw.get("patterns") or ([raw["pattern"]] if raw.get("pattern") else [])
        if not patterns:
            raise ValueError(f"log format {self.name!r} に patterns がありません")
        self.patterns = [re.compile(p) for p in patterns]
        for p in self.patterns:
            missing = {"ts", "level", "message"} - set(p.groupindex)
            if missing:
                raise ValueError(f"log format {self.name!r} の pattern に {sorted(missing)} グループがありません")
        self.ts_format: Optional[str] = raw.get("ts_format")
        self.timezone = parse_tz(raw.get("timezone", defaults.get("timezone", "+09:00")))
        self.encoding: str = raw.get("encoding", defaults.get("encoding", "auto"))
        self.clock_offset_seconds = float(raw.get("clock_offset_seconds", 0) or 0)
        self.level_aliases = level_aliases

    def matches_name(self, name: str) -> bool:
        lname = name.lower()
        return any(fnmatch.fnmatch(lname, g.lower()) for g in self.files)

    def normalize_level(self, level: str) -> Optional[str]:
        up = level.upper()
        up = self.level_aliases.get(up, up)
        return up if up in SEVERITY else None


def load_formats(cfg: dict) -> List[LogFormat]:
    defaults = cfg.get("defaults") or {}
    aliases = {str(k).upper(): str(v).upper() for k, v in (cfg.get("level_aliases") or {}).items()}
    formats = [LogFormat(f, defaults, aliases) for f in (cfg.get("formats") or [])]
    if not formats:
        raise ValueError("log_formats に formats が1つもありません")
    return formats


def collect_files(inputs: Sequence[str], formats: List[LogFormat]) -> List[Tuple[Path, str, LogFormat]]:
    """入力パス（ファイルまたはディレクトリ）から (実パス, 表示用相対パス, 書式) の一覧を作る。

    ディレクトリは再帰的にたどり、書式の files に一致するファイルだけを読む。
    明示的に指定したファイルは、一致する書式が無ければ先頭の書式で読む。
    """
    found: List[Tuple[Path, str, LogFormat]] = []
    seen = set()
    for inp in inputs:
        p = Path(inp)
        if p.is_dir():
            for f in sorted(x for x in p.rglob("*") if x.is_file()):
                rel = f.relative_to(p)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                fmt = next((fm for fm in formats if fm.matches_name(f.name)), None)
                if fmt is None or f.resolve() in seen:
                    continue
                seen.add(f.resolve())
                found.append((f, rel.as_posix(), fmt))
        elif p.is_file():
            if p.resolve() in seen:
                continue
            seen.add(p.resolve())
            fmt = next((fm for fm in formats if fm.matches_name(p.name)), formats[0])
            found.append((p, p.name, fmt))
        else:
            raise FileNotFoundError(f"ログが見つかりません: {inp}")
    return found


def parse_file(path: Path, rel: str, fmt: LogFormat, file_order: int, out_tz: tzinfo,
               clock_offset_seconds: Optional[float] = None) -> Tuple[List[Record], FileStats]:
    """1ファイルを読み、レコードの一覧と統計を返す。時刻は out_tz の naive datetime にそろえる。"""
    alias = file_alias(path.name)
    stats = FileStats(rel, alias, fmt.name)
    text, stats.encoding = read_text(path, fmt.encoding)
    offset = fmt.clock_offset_seconds if clock_offset_seconds is None else clock_offset_seconds
    stats.clock_offset_seconds = offset
    shift = timedelta(seconds=offset)

    records: List[Record] = []
    current: Optional[Record] = None
    lineno = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = None
        for pat in fmt.patterns:
            m = pat.match(line)
            if m:
                break
        if m:
            try:
                ts = parse_timestamp(m.group("ts"), fmt.ts_format, fmt.timezone)
            except ValueError:
                stats.bad_timestamps += 1
                m = None
        if m:
            raw_level = m.group("level")
            level = fmt.normalize_level(raw_level)
            if level is None:
                stats.unknown_levels[raw_level] = stats.unknown_levels.get(raw_level, 0) + 1
                level = "INFO"
            component = (m.groupdict().get("component") or "").strip()
            local = (ts.astimezone(out_tz) + shift).replace(tzinfo=None)
            current = Record(rel, alias, file_order, lineno, local, level, component,
                             m.group("message").rstrip())
            records.append(current)
            continue
        if not line.strip():
            continue
        if current is not None:
            stats.continuation_lines += 1
            if len(current.extra) < MAX_EXTRA_LINES:
                current.extra.append(line.rstrip())
            else:
                current.extra_dropped += 1
        else:
            stats.unparsed_lines += 1
    stats.total_lines = lineno
    stats.records = len(records)
    if records:
        stats.first_ts = min(r.ts for r in records)
        stats.last_ts = max(r.ts for r in records)
    return records, stats
