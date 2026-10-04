"""Drain3 によるテンプレート化と集計。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig

from .parsing import Record


class Template:
    """テンプレート1種類と、その件数・時刻の集計。"""

    def __init__(self, tid: str, text: str, key: int = 0):
        self.id = tid
        self.key = key  # Drain3 のクラスタID（records の template_key と同じ）
        self.text = text
        self.total_count = 0
        self.total_first: Optional[datetime] = None
        self.total_last: Optional[datetime] = None
        self.total_max_severity = -1
        self.total_level = ""  # 全期間で最も重いレベル（level は時間窓内）
        self.count = 0  # 時間窓内
        self.first: Optional[datetime] = None
        self.last: Optional[datetime] = None
        self.max_severity = -1
        self.level = ""
        self.trigger_count = 0
        self.components: Dict[str, int] = {}
        self.noise_reason: Optional[str] = None
        # 既知ノイズでも、今回だけ様子が違う（急増・新たに出現）のでノイズ扱いをやめた理由
        self.noise_suspended: Optional[str] = None

    @property
    def is_noise(self) -> bool:
        """既知ノイズとして下に回すか。辞書に一致し、かつ今回だけ様子が違うわけではないもの。"""
        return self.noise_reason is not None and self.noise_suspended is None

    def add_total(self, r: Record) -> None:
        self.total_count += 1
        if self.total_first is None or r.ts < self.total_first:
            self.total_first = r.ts
        if self.total_last is None or r.ts > self.total_last:
            self.total_last = r.ts
        if r.severity > self.total_max_severity:
            self.total_max_severity = r.severity
            self.total_level = r.level

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
        result = miner.add_log_message(r.message.strip() or "<EMPTY>")
        cid = result["cluster_id"]
        r.template_key = cid
        if cid not in known:
            known.add(cid)
            first_seen.append(cid)

    clusters = {c.cluster_id: c for c in miner.drain.clusters}
    width = max(2, len(str(len(first_seen))))
    templates = {
        cid: Template(f"T{n:0{width}d}", clusters[cid].get_template(), cid)
        for n, cid in enumerate(first_seen, start=1)
    }
    for r in records:
        templates[r.template_key].add_total(r)
    return templates

