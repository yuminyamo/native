"""時間窓の切り出しと、ERROR・例外の前後文脈ブロックの抽出。"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Set

from .parsing import Record
from .templating import Template


class Block:
    """ERROR・例外を含む連続したレコードの塊（前後の文脈付き）。"""

    def __init__(self, records: List[Record], trigger_positions: List[int]):
        self.id = ""
        self.records = records
        # records 内の位置
        self.trigger_positions = trigger_positions

    @property
    def triggers(self) -> List[Record]:
        return [self.records[i] for i in self.trigger_positions]

    def trigger_template_keys(self) -> List[int]:
        seen: List[int] = []
        for r in self.triggers:
            if r.template_key not in seen:
                seen.append(r.template_key)
        return seen


def select_window(records: List[Record], start: datetime, end: datetime) -> List[Record]:
    return [r for r in records if start <= r.ts <= end]


def build_blocks(stream: List[Record], before: int, after: int, max_records: int) -> List[Block]:
    """トリガー（ERROR 以上・スタックトレース付き）の前後 before/after 件を1つの塊にする。

    範囲が重なる・接する場合はまとめるが、1つの塊は max_records 件を超えないようにする
    （ERROR が連続して出続けると窓全体が1つの塊になってしまうため）。
    """
    blocks: List[Block] = []
    cur_start = cur_end = -1
    cur_triggers: List[int] = []

    def flush() -> None:
        if cur_start < 0:
            return
        recs = stream[cur_start:cur_end + 1]
        blocks.append(Block(recs, [i - cur_start for i in cur_triggers]))

    for i, r in enumerate(stream):
        if not r.is_trigger:
            continue
        lo = max(0, i - before)
        hi = min(len(stream) - 1, i + after)
        fits = cur_start >= 0 and (hi - cur_start + 1) <= max_records
        if cur_start >= 0 and i <= cur_end:
            # トリガー自体が今の塊の範囲内にある
            cur_triggers.append(i)
            if fits:
                cur_end = max(cur_end, hi)
            continue
        if fits and lo <= cur_end + 1:
            cur_end = hi
            cur_triggers.append(i)
            continue
        if cur_start >= 0:
            # 上限で切る場合は、前の塊と重ならないように始点をずらす
            lo = max(lo, cur_end + 1)
        flush()
        cur_start, cur_end, cur_triggers = lo, hi, [i]
    flush()
    return blocks


def select_blocks(blocks: List[Block], limit: int) -> List[Block]:
    """新しいトリガーテンプレートを含む塊だけを残し、ID（E01...）を振る。

    同じ ERROR が繰り返し出ているだけの塊は情報が増えないので省く。
    """
    seen: Set[int] = set()
    selected: List[Block] = []
    for b in blocks:
        keys = b.trigger_template_keys()
        if all(k in seen for k in keys):
            continue
        seen.update(keys)
        selected.append(b)
        if len(selected) >= limit:
            break
    width = max(2, len(str(len(selected))))
    for n, b in enumerate(selected, start=1):
        b.id = f"E{n:0{width}d}"
    return selected


def aggregate_window(window: List[Record], templates: Dict[int, Template]) -> None:
    for r in window:
        templates[r.template_key].add_window(r)
