"""既知ノイズ辞書の候補作り・登録・検査。

流れ:
  1. candidates: 再現環境の正常期間のログ（log-digest の出力）から、平常時に出ている WARN / ERROR を候補にする
  2. AI が候補ごとにソースコードの出力箇所を読み、decision（register / reject）と理由を書き込む
  3. merge: 判断済みの候補を辞書（登録）と却下リストに反映する

辞書の照合規則は log-digest の noise.py と同じ（<...> は任意の文字列、空白の数は無視）。
skill どうしはコードを共有しないので、照合の関数はここにも持つ。
"""

from __future__ import annotations

import fnmatch
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

from .parsed import iter_records, load_meta, load_templates

WARN_SEVERITY = 3
REGISTER, REJECT = "register", "reject"
GENERATED_BY = "AI（log-knowledge）"

_PLACEHOLDER_RE = re.compile(r"<[^<>\s]*>")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RANGE_RE = re.compile(r"^(.+?\d{1,2}:\d{2}(?::\d{2})?)\s*/\s*(.+)$")


# ---- 照合（log-digest の noise.py と同じ規則） -----------------------------

def pattern_to_regex(template: str) -> "re.Pattern[str]":
    parts = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(template):
        parts.append(re.escape(template[pos:m.start()]))
        parts.append(".*?")
        pos = m.end()
    parts.append(re.escape(template[pos:]))
    body = re.sub(r"(\\ )+", r"\\s+", "".join(parts))
    return re.compile(r"^\s*" + body + r"\s*$", re.DOTALL)


def versions_overlap(a: Sequence[str], b: Sequence[str]) -> bool:
    """バージョン指定どうしが重なるか（どちらかの値がもう一方のパターンに一致する）。"""
    return any(fnmatch.fnmatch(x, y) or fnmatch.fnmatch(y, x) for x in a for y in b)


def literal_hint(template: str) -> str:
    """ソースコードを検索するための、テンプレートの中で最も長い固定部分。"""
    pieces = [p.strip(" :=,()[]'\"") for p in _PLACEHOLDER_RE.split(template)]
    return max(pieces, key=len) if pieces else ""


# ---- 辞書ファイルの読み書き ---------------------------------------------------

def read_list(path: Path) -> Tuple[List[str], List[dict]]:
    """先頭のコメント行と、YAML のリストを返す。ファイルが無ければ空。"""
    if not path.is_file():
        return [], []
    text = path.read_text(encoding="utf-8")
    header = []
    for line in text.splitlines():
        if line.startswith("#") or (not line.strip() and header):
            header.append(line)
        else:
            break
    while header and not header[-1].strip():
        header.pop()
    data = yaml.safe_load(text)
    if data is None:
        data = []
    if not isinstance(data, list):
        raise ValueError(f"{path} はリスト形式（- template: ...）で書いてください")
    return header, data


def write_list(path: Path, header: List[str], entries: List[dict]) -> None:
    body = yaml.safe_dump(entries, allow_unicode=True, sort_keys=False, width=1000) if entries else "[]\n"
    text = ("\n".join(header) + "\n" if header else "") + body
    _write(path, text)


def _write(path: Path, text: str) -> None:
    """改行は OS によらず LF にする（git の差分が出ないように）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _sort_key(e: dict) -> Tuple[str, str]:
    return (",".join(e.get("versions") or ["*"]), str(e.get("template", "")))


def validate_dictionary(entries: List[dict]) -> List[str]:
    """辞書の検査。log-digest が読める形か、AI が作った項目に根拠があるか、重複が無いか。"""
    errors = []
    seen = set()
    for i, e in enumerate(entries, start=1):
        where = f"{i} 件目"
        if not isinstance(e, dict) or not str(e.get("template") or "").strip():
            errors.append(f"{where}: template がありません")
            continue
        where = f"{i} 件目（{e['template']!r}）"
        if not str(e.get("reason") or "").strip():
            errors.append(f"{where}: reason がありません")
        versions = e.get("versions") or ["*"]
        if not isinstance(versions, list):
            errors.append(f"{where}: versions はリストで書いてください")
            versions = [str(versions)]
        if e.get("generated_by") == GENERATED_BY:
            ev = e.get("evidence") or {}
            for k in ("source", "rate", "code"):
                if not str(ev.get(k) or "").strip():
                    errors.append(f"{where}: evidence.{k} がありません（AI が作った項目は根拠が必須）")
        if len(literal_hint(e["template"])) < 8:
            errors.append(f"{where}: 固定部分が短すぎ、関係のないログまで一致するおそれがある")
        key = (e["template"], tuple(versions))
        if key in seen:
            errors.append(f"{where}: 同じ template と versions の項目が重複している")
        seen.add(key)
    return errors


def matching_entry(template: str, version_patterns: Sequence[str], entries: List[dict]) -> Optional[dict]:
    for e in entries:
        if not isinstance(e, dict) or not e.get("template"):
            continue
        if not versions_overlap(version_patterns, e.get("versions") or ["*"]):
            continue
        if pattern_to_regex(str(e["template"])).match(template) or e["template"] == template:
            return e
    return None


# ---- 候補作り -----------------------------------------------------------------

class Period:
    def __init__(self, start: datetime, end: datetime):
        self.start = start
        self.end = end

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    def label(self) -> str:
        return f"{self.start:%Y-%m-%d %H:%M}〜{self.end:%Y-%m-%d %H:%M}"


def _parse_dt(text: str) -> datetime:
    t = text.strip().replace("/", "-").replace("T", " ")
    return datetime.fromisoformat(t)


def parse_periods(specs: Sequence[str]) -> List[Period]:
    """'2026-09-16'（その日の全体）または '2026-09-16 09:00/2026-09-18 18:00'。"""
    out = []
    for spec in specs:
        s = spec.strip()
        if _DATE_RE.match(s):
            d = datetime.fromisoformat(s)
            out.append(Period(d, d + timedelta(days=1)))
            continue
        m = _RANGE_RE.match(s)
        if not m:
            raise ValueError(f"--normal は日付（2026-09-16）か範囲（2026-09-16 09:00/2026-09-18 18:00）で"
                             f"指定してください: {spec!r}")
        start, end = _parse_dt(m.group(1)), _parse_dt(m.group(2))
        if end <= start:
            raise ValueError(f"--normal の範囲の終わりが始まりより前です: {spec!r}")
        out.append(Period(start, end))
    return out


def auto_periods(meta: dict) -> List[Period]:
    """log-digest が「平常期間の候補」とした日（変化点より前・ERROR の急増なし・全日あり）。"""
    days = ((meta.get("baseline") or {}).get("days")) or []
    out = []
    for d in days:
        if d.get("normal_candidate"):
            start = datetime.fromisoformat(d["date"])
            out.append(Period(start, start + timedelta(days=1)))
    return out


def default_versions(product_version: Optional[str]) -> Optional[List[str]]:
    """'5.2.3' → ['5.2.*']（同じマイナーバージョンの系列）。"""
    if not product_version:
        return None
    parts = product_version.split(".")
    if len(parts) >= 3:
        return [".".join(parts[:2]) + ".*"]
    return [product_version]


def _clip(periods: List[Period], meta: dict) -> List[Period]:
    """ログが無い時間を正常期間の長さに数えないよう、ログの期間で切る。"""
    firsts = [datetime.fromisoformat(f["first_ts"]) for f in meta.get("files") or [] if f.get("first_ts")]
    lasts = [datetime.fromisoformat(f["last_ts"]) for f in meta.get("files") or [] if f.get("last_ts")]
    if not firsts:
        return periods
    lo, hi = min(firsts), max(lasts)
    out: List[Period] = []
    for p in periods:
        s, e = max(p.start, lo), min(p.end, hi)
        if e <= s:
            continue
        if out and s <= out[-1].end:
            # 続いている・重なっている期間は1つにまとめる
            out[-1].end = max(out[-1].end, e)
        else:
            out.append(Period(s, e))
    return out


def build_candidates(log_dir: Path, periods: List[Period], versions: List[str], source: str,
                     dictionary: List[dict], rejected: List[dict], min_count: int, min_hours: int) -> dict:
    meta = load_meta(log_dir)
    periods = _clip(sorted(periods, key=lambda p: p.start), meta)
    if not periods:
        raise ValueError("正常期間にログがありません（--normal の指定か、log-digest の平常期間の候補を確認してください）")
    templates = load_templates(log_dir, meta)
    starts = [p.start for p in periods]

    def in_periods(ts: datetime) -> bool:
        for p in periods:
            if p.start <= ts < p.end:
                return True
            if ts < p.start:
                break
        return False

    stats: Dict[str, dict] = {}
    for r in iter_records(log_dir, meta):
        ts = r["ts"]
        if ts is None or ts < starts[0] or not in_periods(ts):
            continue
        s = stats.get(r["template_id"])
        if s is None:
            s = stats[r["template_id"]] = {"count": 0, "severity": -1, "level": "", "hours": set(), "days": set(),
                                           "sample": None, "components": {}}
        s["count"] += 1
        if (r.get("severity") or 0) > s["severity"]:
            s["severity"], s["level"] = r.get("severity") or 0, r.get("level") or ""
        s["hours"].add(ts.replace(minute=0, second=0, microsecond=0))
        s["days"].add(ts.date())
        comp = r.get("component") or ""
        s["components"][comp] = s["components"].get(comp, 0) + 1
        if s["sample"] is None:
            s["sample"] = (f"{ts:%Y-%m-%d %H:%M:%S} @{r['file']}:{r['lineno']} "
                           + (f"[{comp}] " if comp else "") + str(r.get("message") or "")[:300])

    total_hours = sum(p.hours for p in periods)
    candidates, skipped = [], []
    for tid in sorted(stats, key=lambda t: (-stats[t]["severity"], -stats[t]["count"], t)):
        s = stats[tid]
        if s["severity"] < WARN_SEVERITY:
            continue
        text = templates[tid]["template"]
        hint = literal_hint(text)
        reg = matching_entry(text, versions, dictionary)
        if reg is not None:
            skipped.append({"template": text, "why": f"登録済み（{reg['template']}）"})
            continue
        rej = matching_entry(text, versions, rejected)
        if rej is not None:
            skipped.append({"template": text, "why": f"以前に却下（{rej.get('reject_reason', '')}）"})
            continue
        if s["count"] < min_count or len(s["hours"]) < min_hours:
            skipped.append({"template": text, "why": f"件数が少ない（{s['count']}件・{len(s['hours'])}時間帯）。"
                                                     "平常時から出ているとは言い切れない"})
            continue
        if len(hint) < 8:
            skipped.append({"template": text, "why": "固定部分が短すぎ、関係のないログまで一致するおそれがある"})
            continue
        rate = s["count"] / total_hours if total_hours else 0.0
        comps = ", ".join(c or "-" for c, _ in sorted(s["components"].items(), key=lambda kv: -kv[1])[:3])
        candidates.append({
            "template": text,
            "level": s["level"],
            "component": comps,
            "search_hint": hint,
            "sample": s["sample"],
            "count": s["count"],
            "rate": f"1時間あたり約{rate:.1f}件（{len(s['days'])}日・{len(s['hours'])}時間帯で計{s['count']}件）",
            "decision": "",
            "reason": "",
            "code": "",
            "reject_reason": "",
        })
    return {
        "source": source,
        "versions": versions,
        "log_dir": str(log_dir),
        "product_version": meta.get("product_version"),
        "normal_periods": [p.label() for p in periods],
        "normal_hours": round(total_hours, 1),
        "candidates": candidates,
        "skipped": skipped,
    }


CANDIDATES_HEADER = """\
# 既知ノイズの候補（log-knowledge noise-candidates が作成）
#
# 候補ごとに、ソースコードでログの出力箇所を探して読み、次を書き込む（SKILL.md の判断基準を参照）。
#   decision:      register（既知ノイズとして登録）/ reject（登録しない）
#   reason:        register の時。影響がないと言える理由（例: 「referral を無視して次のDCに問い合わせを続ける。認証の結果には影響しない」）
#   code:          register の時。出力箇所（ファイル:行）
#   reject_reason: reject の時。登録しない理由
# 書き終えたら noise-merge で辞書に反映する。
"""


def write_candidates(path: Path, data: dict) -> None:
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)
    _write(path, CANDIDATES_HEADER + body)


# ---- 登録 ---------------------------------------------------------------------

class MergeResult:
    def __init__(self) -> None:
        self.registered: List[dict] = []
        self.rejected: List[dict] = []
        self.undecided: List[str] = []
        self.errors: List[str] = []


def merge(cand: dict, dictionary: List[dict], rejected: List[dict], today: date,
          allow_undecided: bool = False) -> MergeResult:
    """判断済みの候補を、dictionary（登録）と rejected（却下）に反映する。リストはその場で書き換える。"""
    res = MergeResult()
    versions = cand.get("versions") or []
    source = str(cand.get("source") or "").strip()
    if not versions:
        res.errors.append("候補ファイルに versions がありません")
    if not source:
        res.errors.append("候補ファイルに source（どの環境・期間のログか）がありません")
    for i, c in enumerate(cand.get("candidates") or [], start=1):
        where = f"{i} 件目（{c.get('template')!r}）"
        decision = str(c.get("decision") or "").strip().lower()
        if decision == REGISTER:
            for k in ("reason", "code"):
                if not str(c.get(k) or "").strip():
                    res.errors.append(f"{where}: register には {k} が必要です")
        elif decision == REJECT:
            if not str(c.get("reject_reason") or "").strip():
                res.errors.append(f"{where}: reject には reject_reason が必要です")
        elif decision:
            res.errors.append(f"{where}: decision は register か reject です（{decision!r}）")
        else:
            res.undecided.append(str(c.get("template")))
    if res.undecided and not allow_undecided:
        res.errors.append(f"判断していない候補が {len(res.undecided)} 件あります（すべて判断するか、"
                          "--allow-undecided で今回は見送る）")
    if res.errors:
        return res

    def drop(entries: List[dict], template: str) -> None:
        entries[:] = [e for e in entries
                      if not (e.get("template") == template and list(e.get("versions") or ["*"]) == list(versions))]

    for c in cand.get("candidates") or []:
        decision = str(c.get("decision") or "").strip().lower()
        t = c["template"]
        if decision == REGISTER:
            entry = {
                "template": t,
                "versions": list(versions),
                "reason": str(c["reason"]).strip(),
                "evidence": {"source": source, "rate": c.get("rate", ""), "code": str(c["code"]).strip()},
                "generated_by": GENERATED_BY,
                "added": today.isoformat(),
            }
            drop(dictionary, t)
            drop(rejected, t)
            dictionary.append(entry)
            res.registered.append(entry)
        elif decision == REJECT:
            entry = {
                "template": t,
                "versions": list(versions),
                "reject_reason": str(c["reject_reason"]).strip(),
                "source": source,
                "added": today.isoformat(),
            }
            drop(rejected, t)
            rejected.append(entry)
            res.rejected.append(entry)
    dictionary.sort(key=_sort_key)
    rejected.sort(key=_sort_key)
    res.errors = validate_dictionary(dictionary)
    return res
