"""Drain3 によるテンプレート化と集計。"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig

from .parsing import Record

# 顧客情報のマスク記号 <USER_001> は、テンプレート化では <USER> として扱う。
# 番号のまま渡すと、ユーザーごとに別のテンプレートに分かれてしまう。
_NUMBERED_TOKEN_RE = re.compile(r"<([A-Z][A-Z0-9]*)_\d{3,}>")


class Template:
    """テンプレート1種類と、その件数・時刻の集計。"""

    def __init__(self, tid: str, text: str):
        self.id = tid
        self.text = text
        self.total_count = 0
        self.total_first: Optional[datetime] = None
        self.total_last: Optional[datetime] = None
        self.count = 0  # 時間窓内
        self.first: Optional[datetime] = None
        self.last: Optional[datetime] = None
        self.max_severity = -1
        self.level = ""
        self.trigger_count = 0
        self.components: Dict[str, int] = {}
        self.noise_reason: Optional[str] = None

    @property
    def is_noise(self) -> bool:
        return self.noise_reason is not None

    def add_total(self, r: Record) -> None:
        self.total_count += 1
        if self.total_first is None or r.ts < self.total_first:
            self.total_first = r.ts
        if self.total_last is None or r.ts > self.total_last:
            self.total_last = r.ts

    def add_window(self, r: Record) -> None:
        self.count += 1
        if self.first is None or r.ts < self.first:
            self.first = r.ts
        if self.last is None or r.ts > self.last:
            self.last = r.ts
        if r.severity > self.max_severity:
            self.max_severity = r.severity
            self.level = r.level
        if r.is_trigger:
            self.trigger_count += 1
        self.components[r.component] = self.components.get(r.component, 0) + 1


def load_miner(ini_path: Path) -> TemplateMiner:
    if not ini_path.is_file():
        raise FileNotFoundError(f"Drain3 の設定ファイルが見つかりません: {ini_path}")
    config = TemplateMinerConfig()
    config.load(str(ini_path))
    return TemplateMiner(config=config)


def mine_templates(records: List[Record], ini_path: Path) -> Dict[int, Template]:
    """records（時刻順）をテンプレート化する。

    各 record.template_key に Drain3 のクラスタIDを入れ、クラスタID → Template を返す。
    テンプレートIDは初出順に T01, T02 ... と振り直す（同じ入力なら常に同じIDになる）。
    """
    miner = load_miner(ini_path)
    first_seen: List[int] = []
    known = set()
    for r in records:
        message = _NUMBERED_TOKEN_RE.sub(r"<\1>", r.message.strip()) or "<EMPTY>"
        result = miner.add_log_message(message)
        cid = result["cluster_id"]
        r.template_key = cid
        if cid not in known:
            known.add(cid)
            first_seen.append(cid)

    clusters = {c.cluster_id: c for c in miner.drain.clusters}
    width = max(2, len(str(len(first_seen))))
    templates = {
        cid: Template(f"T{n:0{width}d}", clusters[cid].get_template())
        for n, cid in enumerate(first_seen, start=1)
    }
    for r in records:
        templates[r.template_key].add_total(r)
    return templates

