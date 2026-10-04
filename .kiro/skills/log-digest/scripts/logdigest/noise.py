"""既知ノイズ辞書との照合。一致したテンプレートに理由を付け、ダイジェストの下に回す。

辞書（knowledge/known_noise.yaml）は log-knowledge skill が作る。照合規則を変えたら、
log-knowledge の noise.py（pattern_to_regex）も合わせて直すこと。
"""

from __future__ import annotations

import fnmatch
import re
from typing import Dict, List, Optional

from .templating import Template

_PLACEHOLDER_RE = re.compile(r"<[^<>\s]*>")


def _pattern_to_regex(template: str) -> "re.Pattern[str]":
    """'LDAP referral ignored: <*>' のような辞書の書き方を正規表現にする。

    <...> は任意の文字列に一致し、空白の数の違いは無視する。
    """
    parts = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(template):
        parts.append(re.escape(template[pos:m.start()]))
        parts.append(".*?")
        pos = m.end()
    parts.append(re.escape(template[pos:]))
    body = "".join(parts)
    body = re.sub(r"(\\ )+", r"\\s+", body)
    return re.compile(r"^\s*" + body + r"\s*$", re.DOTALL)


class NoiseEntry:
    def __init__(self, raw: dict, index: int):
        if not isinstance(raw, dict) or not raw.get("template"):
            raise ValueError(f"known_noise の {index + 1} 件目に template がありません")
        if not str(raw.get("reason") or "").strip():
            raise ValueError(
                f"known_noise の {index + 1} 件目（{raw['template']!r}）に reason がありません。"
                "理由のない登録はできません")
        self.template = str(raw["template"])
        self.reason = str(raw["reason"]).strip()
        versions = raw.get("versions") or ["*"]
        self.versions: List[str] = [str(v) for v in (versions if isinstance(versions, list) else [versions])]
        self.added = str(raw.get("added") or "")
        self.regex = _pattern_to_regex(self.template)

    def applies_to(self, version: Optional[str]) -> bool:
        if version is None:
            return True
        return any(fnmatch.fnmatch(version, v) for v in self.versions)

    def matches(self, template_text: str) -> bool:
        return bool(self.regex.match(template_text))


def load_entries(raw: object) -> List[NoiseEntry]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("known_noise はリスト形式（- template: ...）で書いてください")
    return [NoiseEntry(e, i) for i, e in enumerate(raw)]


def apply_noise(templates: Dict[int, Template], entries: List[NoiseEntry], version: Optional[str]) -> int:
    """一致したテンプレートに noise_reason を付け、一致したテンプレート数を返す。"""
    active = [e for e in entries if e.applies_to(version)]
    hits = 0
    for t in templates.values():
        for e in active:
            if e.matches(t.text):
                t.noise_reason = e.reason
                hits += 1
                break
    return hits
