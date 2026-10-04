"""log-digest の出力（meta.json と parsed/）を読む。

skill どうしはコードを共有せず、ファイル形式だけで受け渡す。形式は log-digest の export.py（PARSED_SCHEMA）。
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator

# log-digest の export.py の PARSED_SCHEMA と合わせる
PARSED_SCHEMA = 1


def load_meta(d: Path) -> dict:
    p = d / "meta.json"
    if not p.is_file():
        raise FileNotFoundError(f"log-digest の出力が見つかりません: {d}（先に log-digest skill を実行してください）")
    meta = json.loads(p.read_text(encoding="utf-8"))
    parsed = meta.get("parsed") or {}
    if parsed.get("schema") != PARSED_SCHEMA:
        raise ValueError(f"parsed の形式（schema={parsed.get('schema')}）に対応していません。"
                         f"log-knowledge は schema={PARSED_SCHEMA} を読みます")
    return meta


def load_templates(d: Path, meta: dict) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    with open(d / meta["parsed"]["templates"], encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                out[row["template_id"]] = row
    return out


def iter_records(d: Path, meta: dict) -> Iterator[dict]:
    """全レコードを1件ずつ返す（メモリに全部は載せない）。ts は datetime にする。"""
    with gzip.open(d / meta["parsed"]["records"], "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            row["ts"] = datetime.fromisoformat(row["ts"]) if row.get("ts") else None
            yield row
