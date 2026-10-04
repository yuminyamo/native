"""顧客情報のマスキング。同じ値は常に同じ記号（<USER_001> など）に置き換える。"""

from __future__ import annotations

import re
from typing import Dict, List, Match, Optional, Tuple

TOKEN_RE = re.compile(r"^<[A-Z][A-Z0-9]*_\d{3,}>$")


class MaskRule:
    def __init__(self, raw: dict):
        self.name = raw.get("name", "unnamed")
        self.token = str(raw["token"]).upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]*", self.token):
            raise ValueError(f"masking rule {self.name!r}: token は英大文字と数字にしてください: {self.token!r}")
        self.pattern = re.compile(raw["pattern"])
        self.case_insensitive = bool(raw.get("case_insensitive", False))
        self.value_groups = sorted(g for g in self.pattern.groupindex if g.startswith("value"))


class Masker:
    def __init__(self, rules: List[MaskRule]):
        self.rules = rules
        # token -> {正規化した値: 番号}
        self._maps: Dict[str, Dict[str, int]] = {}
        # token -> {正規化した値: 最初に見た元の値}
        self._originals: Dict[str, Dict[str, str]] = {}

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "Masker":
        return cls([MaskRule(r) for r in ((cfg or {}).get("rules") or [])])

    def _token_for(self, rule: MaskRule, value: str) -> str:
        key = value.lower() if rule.case_insensitive else value
        table = self._maps.setdefault(rule.token, {})
        if key not in table:
            table[key] = len(table) + 1
            self._originals.setdefault(rule.token, {})[key] = value
        return f"<{rule.token}_{table[key]:03d}>"

    def _replace(self, rule: MaskRule, m: Match) -> str:
        span: Optional[Tuple[int, int]] = None
        for g in rule.value_groups:
            if m.group(g) is not None:
                span = m.span(g)
                break
        if span is None:
            if rule.value_groups:
                return m.group(0)
            span = m.span(0)
        value = m.string[span[0]:span[1]]
        # 既に記号になっている値（前の規則で置換済み）は二重に置き換えない
        if not value or TOKEN_RE.match(value) or ("<" in value and ">" in value):
            return m.group(0)
        start, end = m.span(0)
        return m.string[start:span[0]] + self._token_for(rule, value) + m.string[span[1]:end]

    def mask(self, text: str) -> str:
        if not text:
            return text
        for rule in self.rules:
            text = rule.pattern.sub(lambda m, r=rule: self._replace(r, m), text)
        return text

    def summary(self) -> Dict[str, int]:
        """記号の種類ごとに、置き換えた値の種類数。"""
        return {token: len(table) for token, table in sorted(self._maps.items())}

    def mapping(self) -> Dict[str, Dict[str, str]]:
        """記号 → 元の値 の対応表。顧客情報を含むのでAIに渡さないこと。"""
        out: Dict[str, Dict[str, str]] = {}
        for token, table in sorted(self._maps.items()):
            originals = self._originals[token]
            out[token] = {f"<{token}_{n:03d}>": originals[key] for key, n in table.items()}
        return out
