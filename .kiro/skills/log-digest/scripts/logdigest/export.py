"""パース済みの全レコードとテンプレートを parsed/ に書き出す（案2 log-search の取り込み元）。

形式は JSON Lines。log-search がこれを DuckDB に取り込む。項目を変えたら PARSED_SCHEMA を上げ、
log-search 側（skills/log-search/scripts/logsearch/store.py）も合わせて直すこと。
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .parsing import Record
from .templating import Template

PARSED_SCHEMA = 1
PARSED_DIR = "parsed"
RECORDS_FILE = "records.jsonl.gz"
TEMPLATES_FILE = "templates.jsonl"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(sep=" ", timespec="microseconds") if dt else None


def write_parsed(out_dir: Path, records: List[Record], templates: Dict[int, Template]) -> dict:
    """records（時刻順にマージ済み）と templates を書き出し、meta.json に載せる情報を返す。

    同じ入力からは同じバイト列になるよう、gzip のヘッダに時刻・ファイル名を入れない。
    """
    d = out_dir / PARSED_DIR
    d.mkdir(parents=True, exist_ok=True)
    with open(d / RECORDS_FILE, "wb") as raw, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        for seq, r in enumerate(records):
            row = {
                "seq": seq,
                "file": r.file,
                "alias": r.alias,
                "lineno": r.lineno,
                "ts": _iso(r.ts),
                "level": r.level,
                "severity": r.severity,
                "component": r.component,
                "template_id": templates[r.template_key].id,
                "message": r.message,
                "extra": "\n".join(r.extra) if r.extra else None,
                "extra_lines": len(r.extra) + r.extra_dropped,
            }
            gz.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))

    with open(d / TEMPLATES_FILE, "w", encoding="utf-8", newline="\n") as f:
        for t in sorted(templates.values(), key=lambda t: t.id):
            row = {
                "template_id": t.id,
                "level": t.total_level,
                "severity": t.total_max_severity,
                "total_count": t.total_count,
                "first_ts": _iso(t.total_first),
                "last_ts": _iso(t.total_last),
                "window_count": t.count,
                "window_first": _iso(t.first),
                "window_last": _iso(t.last),
                # 今回ノイズ扱いをやめたもの（急増・出現）は、log-search でも通常のログとして扱う
                "noise_reason": t.noise_reason if t.is_noise else None,
                "template": t.text,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {
        "schema": PARSED_SCHEMA,
        "records": f"{PARSED_DIR}/{RECORDS_FILE}",
        "templates": f"{PARSED_DIR}/{TEMPLATES_FILE}",
    }
