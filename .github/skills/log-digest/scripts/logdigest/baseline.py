"""平常プロファイル: 同じ環境の「正常に動いていたと思われる期間」と障害時を比べる。

受け取ったログの全日について日ごとの件数を数え、次の2つを出す。

- 変化点: テンプレートごとの日別件数から「ある日から出始めた（出現）」「ある日から消えた（消失）」を見つける。
  少しずつ進む障害（容量の枯渇、設定変更の影響など）の発端を拾うため。
- 平常期間との比較: 正常だったと思われる日の、障害の時間窓と同じ時刻帯と比べる
  （障害時のみ・消失・急増・激減）。平常期間は規則で自動選択し、AI が引数で選び直せる。

どれも決定論的な規則で、しきい値は config/baseline.yaml にある。
"""

from __future__ import annotations

import bisect
import re
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Dict, List, Optional, Tuple

from .parsing import ERROR_SEVERITY, Record, parse_timestamp
from .templating import Template

WEEKDAYS = "月火水木金土日"

DEFAULTS = {
    "business_day_ratio": 0.5,
    "full_day_margin_minutes": 60,
    "change_points": {
        "min_history_days": 3,
        "appear_persist_ratio": 0.5,
        "vanish_presence_ratio": 0.8,
    },
    "selection": {
        "error_spike_ratio": 3.0,
        "error_spike_min": 10,
    },
    "compare": {
        "vanished_min_expected": 5,
        "surge_min_count": 10,
        "surge_ratio": 5.0,
        "drop_min_expected": 20,
        "drop_ratio": 0.2,
    },
    "noise": {
        "suspend_new_min_count": 5,
    },
    "fallback_min_minutes": 30,
}

# 比較の判定（表示順）
VANISHED, NEW, SURGE, DROP = "消失", "障害時のみ", "急増", "激減"
COMPARE_ORDER = [VANISHED, NEW, SURGE, DROP]
APPEAR, DISAPPEAR = "出現", "消失"


def load_config(raw: object) -> dict:
    """baseline.yaml の内容を既定値に重ねる（書かれていない項目は既定値）。"""
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if raw is None:
        return cfg
    if not isinstance(raw, dict):
        raise ValueError("baseline.yaml は「項目: 値」の形で書いてください")
    for k, v in raw.items():
        if k not in DEFAULTS:
            raise ValueError(f"baseline.yaml の {k!r} は未知の項目です")
        if isinstance(DEFAULTS[k], dict):
            if not isinstance(v, dict):
                raise ValueError(f"baseline.yaml の {k} は「項目: 値」の形で書いてください")
            unknown = set(v) - set(DEFAULTS[k])
            if unknown:
                raise ValueError(f"baseline.yaml の {k} に未知の項目があります: {', '.join(sorted(unknown))}")
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def fmt_day(d: date) -> str:
    return f"{d:%m-%d}（{WEEKDAYS[d.weekday()]}）"


class DayStat:
    """1日分の集計。"""

    def __init__(self, day: date):
        self.day = day
        self.records = 0
        self.errors = 0
        self.full = False  # ログの期間がその日全体を覆っているか
        self.is_incident_day = False
        self.business = False  # 件数から見た営業日
        self.error_spike = False
        self.after_change = False  # 障害日より前の最初の変化点以降
        self.normal_candidate = False  # 平常期間の候補（全日・ログあり・変化点より前・ERROR急増なし）

    @property
    def observed(self) -> bool:
        """変化点の判定に使う日（全日を覆う日と障害日。ログが1件も無い日は除く）。"""
        return self.records > 0 and (self.full or self.is_incident_day)

    def to_dict(self) -> dict:
        return {
            "date": self.day.isoformat(), "records": self.records, "errors": self.errors,
            "full": self.full, "incident_day": self.is_incident_day, "business": self.business,
            "error_spike": self.error_spike, "after_change": self.after_change,
            "normal_candidate": self.normal_candidate,
        }


class ChangePoint:
    def __init__(self, kind: str, day: date, key: int, before_present: int, before_days: int,
                 after_present: int, after_days: int):
        self.kind = kind
        self.day = day
        self.key = key
        self.before_present = before_present
        self.before_days = before_days
        self.after_present = after_present
        self.after_days = after_days


class Comparison:
    def __init__(self, kind: str, key: int, expected: float, count: int):
        self.kind = kind
        self.key = key
        self.expected = expected  # 平常期間の件数（時間窓の長さに換算した平均）
        self.count = count  # 時間窓内の件数


class Period:
    def __init__(self, start: datetime, end: datetime, label: str):
        self.start = start
        self.end = end
        self.label = label

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


class BaselineResult:
    def __init__(self) -> None:
        self.days: List[DayStat] = []
        self.change_points: List[ChangePoint] = []
        self.first_change: Optional[date] = None  # 障害日より前の最初の変化点
        self.selection = "none"  # auto / manual / fallback / none
        self.reason = ""
        self.periods: List[Period] = []
        self.comparisons: List[Comparison] = []
        # テンプレートのキー → 平常期間の件数（時間窓の長さに換算）
        self.expected: Dict[int, float] = {}
        self.warnings: List[str] = []

    def to_dict(self) -> dict:
        return {
            "selection": self.selection,
            "reason": self.reason,
            "periods": [{"start": p.start.isoformat(sep=" "), "end": p.end.isoformat(sep=" "), "label": p.label}
                        for p in self.periods],
            "first_change": self.first_change.isoformat() if self.first_change else None,
            "change_points": len(self.change_points),
            "comparisons": {k: sum(1 for c in self.comparisons if c.kind == k) for k in COMPARE_ORDER},
            "days": [d.to_dict() for d in self.days],
        }


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time())


def daily_profile(records: List[Record], incident: datetime, cfg: dict
                  ) -> Tuple[List[DayStat], Dict[int, Dict[date, int]]]:
    """日ごとの件数と、テンプレートごとの日別件数を数える。records は時刻順。"""
    if not records:
        return [], {}
    first, last = records[0].ts, records[-1].ts
    margin = timedelta(minutes=cfg["full_day_margin_minutes"])
    days: Dict[date, DayStat] = {}
    d = first.date()
    while d <= max(last.date(), incident.date()):
        st = DayStat(d)
        st.full = first <= _day_start(d) + margin and last >= _day_start(d) + timedelta(days=1) - margin
        st.is_incident_day = d == incident.date()
        days[d] = st
        d += timedelta(days=1)
    per_template: Dict[int, Dict[date, int]] = {}
    for r in records:
        st = days[r.ts.date()]
        st.records += 1
        if r.severity >= ERROR_SEVERITY:
            st.errors += 1
        counts = per_template.setdefault(r.template_key, {})
        counts[st.day] = counts.get(st.day, 0) + 1
    out = [days[k] for k in sorted(days)]

    before = [s for s in out if s.full and s.records > 0 and s.day < incident.date()]
    if before:
        med = median(s.records for s in before)
        for s in before:
            s.business = s.records >= cfg["business_day_ratio"] * med
        busy = [s for s in before if s.business]
        if busy:
            med_err = median(s.errors for s in busy)
            sel = cfg["selection"]
            for s in before:
                s.error_spike = (s.errors >= sel["error_spike_min"]
                                 and s.errors > sel["error_spike_ratio"] * max(med_err, 1))
    return out, per_template


def find_change_points(days: List[DayStat], per_template: Dict[int, Dict[date, int]], cfg: dict
                       ) -> List[ChangePoint]:
    """日別件数から出現・消失を見つける。障害日の出現も含む（表示側で分けて扱う）。"""
    obs = [s for s in days if s.observed]
    cp_cfg = cfg["change_points"]
    min_hist = cp_cfg["min_history_days"]
    n = len(obs)
    out: List[ChangePoint] = []
    if n <= min_hist:
        return out
    for key, counts in per_template.items():
        seq = [counts.get(s.day, 0) for s in obs]
        present = [c > 0 for c in seq]
        if not any(present):
            continue
        # 出現: 最初の min_hist 日以上は0件で、ある日から出始め、その後も出ている
        k = present.index(True)
        if k >= min_hist:
            after = present[k:]
            if sum(after) >= cp_cfg["appear_persist_ratio"] * len(after):
                out.append(ChangePoint(APPEAR, obs[k].day, key, 0, k, sum(after), len(after)))
                continue
        # 消失: ある日より前は大半の日に出ていたのに、その日から最後まで0件
        last = n - 1 - present[::-1].index(True)
        k = last + 1
        missing = obs[k:]
        # 障害日だけ無い場合は、時間帯の違いで無いだけかもしれないので判定しない（平常期間との比較で扱う）
        if k >= min_hist and any(not s.is_incident_day for s in missing):
            before = present[:k]
            if sum(before) >= cp_cfg["vanish_presence_ratio"] * len(before):
                out.append(ChangePoint(DISAPPEAR, obs[k].day, key, sum(before), len(before), 0, len(missing)))
    return out


def _parse_point(text: str, tz) -> datetime:
    t = text.strip()
    if re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{1,2}:\d{2}", t):
        t += ":00"
    return parse_timestamp(t, None, tz).astimezone(tz).replace(tzinfo=None)


# 範囲の指定: 始まりは時刻まで書き、その後の「/」で区切る（日付に / を使う書き方とも区別できる）
_RANGE_RE = re.compile(r"^(.+?\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?)\s*/\s*(.+)$")
_DATE_ONLY_RE = re.compile(r"^(\d{4})[-/](\d{2})[-/](\d{2})$")


def parse_baseline_specs(specs: List[str], window_start: datetime, window_end: datetime, tz) -> List[Period]:
    """--baseline の値を期間にする。

    - '2026-09-23'                         その日の、時間窓と同じ時刻帯
    - '2026-09-30 08:00/2026-09-30 10:00'  任意の範囲（件数は時間窓の長さに換算して比べる）
    """
    out: List[Period] = []
    offset = window_start - _day_start(window_start.date())
    length = window_end - window_start
    for spec in specs:
        s = spec.strip()
        m = _DATE_ONLY_RE.match(s)
        if m:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            start = _day_start(d) + offset
            out.append(Period(start, start + length, fmt_day(d)))
            continue
        m = _RANGE_RE.match(s)
        if not m:
            raise ValueError(f"--baseline は日付（2026-09-23）か範囲（2026-09-30 08:00/2026-09-30 10:00）で"
                             f"指定してください: {spec!r}")
        start, end = _parse_point(m.group(1), tz), _parse_point(m.group(2), tz)
        if end <= start:
            raise ValueError(f"--baseline の範囲の終わりが始まりより前です: {spec!r}")
        out.append(Period(start, end, f"{start:%m-%d %H:%M}〜{end:%m-%d %H:%M}"))
    return out


def _count_in(ts_list: List[datetime], keys: List[int], start: datetime, end: datetime) -> Dict[int, int]:
    lo = bisect.bisect_left(ts_list, start)
    hi = bisect.bisect_right(ts_list, end)
    out: Dict[int, int] = {}
    for k in keys[lo:hi]:
        out[k] = out.get(k, 0) + 1
    return out


def expected_counts(records: List[Record], periods: List[Period], window_start: datetime,
                    window_end: datetime) -> Dict[int, float]:
    """平常期間のテンプレート別件数を、時間窓の長さに換算して返す（期間が複数なら平均）。"""
    if not periods:
        return {}
    ts_list = [r.ts for r in records]
    keys = [r.template_key for r in records]
    window_min = (window_end - window_start).total_seconds() / 60
    expected: Dict[int, float] = {}
    for p in periods:
        scale = window_min / p.minutes if p.minutes > 0 else 0.0
        for k, n in _count_in(ts_list, keys, p.start, p.end).items():
            expected[k] = expected.get(k, 0.0) + n * scale / len(periods)
    return expected


def classify(templates: Dict[int, Template], expected: Dict[int, float], cfg: dict) -> List[Comparison]:
    """平常期間から予想される件数と時間窓の件数を比べ、判定が付いたものを返す。"""
    c = cfg["compare"]
    out: List[Comparison] = []
    for key, t in templates.items():
        e = expected.get(key, 0.0)
        n = t.count
        if n == 0 and e == 0:
            continue
        kind = None
        if e == 0 and n > 0:
            kind = NEW
        elif n == 0 and e >= c["vanished_min_expected"]:
            kind = VANISHED
        elif n >= c["surge_min_count"] and n >= c["surge_ratio"] * max(e, 1.0):
            kind = SURGE
        elif e >= c["drop_min_expected"] and n <= c["drop_ratio"] * e:
            kind = DROP
        if kind:
            out.append(Comparison(kind, key, e, n))
    return out


def build(records: List[Record], templates: Dict[int, Template], incident: datetime,
          window_start: datetime, window_end: datetime, cfg: dict,
          manual: Optional[List[Period]] = None, manual_reason: Optional[str] = None,
          disabled: bool = False) -> BaselineResult:
    res = BaselineResult()
    res.days, per_template = daily_profile(records, incident, cfg)
    res.change_points = find_change_points(res.days, per_template, cfg)
    before_incident = [cp.day for cp in res.change_points if cp.day < incident.date()]
    res.first_change = min(before_incident) if before_incident else None
    for s in res.days:
        if s.day < incident.date() and s.full and s.records > 0:
            s.after_change = res.first_change is not None and s.day >= res.first_change
            s.normal_candidate = not s.after_change and not s.error_spike

    if disabled:
        res.selection = "none"
        res.reason = "--baseline none の指定により比較しない"
    elif manual:
        res.selection = "manual"
        res.periods = manual
        res.reason = manual_reason or "理由の記載なし"
        if not manual_reason:
            res.warnings.append("--baseline を指定したが --baseline-reason が無い。選んだ理由を残すこと")
        first, last = (records[0].ts, records[-1].ts) if records else (None, None)
        for p in manual:
            if first is None or p.end < first or p.start > last:
                res.warnings.append(f"指定した平常期間 {p.label} にログが無い")
            elif p.start < window_end and window_start < p.end:
                res.warnings.append(f"指定した平常期間 {p.label} が障害の時間窓と重なっている")
    else:
        _auto_select(res, records, incident, window_start, window_end, cfg)

    res.expected = expected_counts(records, res.periods, window_start, window_end)
    res.comparisons = classify(templates, res.expected, cfg) if res.periods else []
    _suspend_noise(res, templates, cfg)
    return res


def _auto_select(res: BaselineResult, records: List[Record], incident: datetime,
                 window_start: datetime, window_end: datetime, cfg: dict) -> None:
    offset = window_start - _day_start(incident.date())
    length = window_end - window_start
    candidates = [s for s in res.days if s.business and s.day < incident.date()]
    clean = [s for s in candidates if s.normal_candidate]
    if clean:
        pick = clean[-1]
        res.selection = "auto"
        if res.first_change and pick.day < res.first_change:
            why = f"{fmt_day(res.first_change)}以降に変化点があるため、その前で最も近い営業日"
        else:
            why = "障害日より前で最も近い営業日（変化点・ERROR の急増なし）"
        skipped = [s for s in candidates if s.day > pick.day and s.error_spike and not s.after_change]
        if skipped:
            why += "。ERROR が急増していた " + "、".join(fmt_day(s.day) for s in skipped) + " は除いた"
        res.reason = why
        start = _day_start(pick.day) + offset
        res.periods = [Period(start, start + length, fmt_day(pick.day))]
        return
    if candidates:
        pick = candidates[-1]
        res.selection = "auto"
        res.reason = ("変化点やERRORの急増がない営業日が見つからないため、障害日より前で最も近い営業日を使った。"
                      "この日も平常ではない可能性がある")
        res.warnings.append("平常と言える日が受け取ったログの中に無い。変化点より前の日を含むログ"
                            "（さらに前の期間）を依頼すること")
        start = _day_start(pick.day) + offset
        res.periods = [Period(start, start + length, fmt_day(pick.day))]
        return
    # 障害日のログしか無い: 同じ日の時間窓より前を使う（時刻帯が違うので目安）
    if records:
        start = max(records[0].ts, _day_start(incident.date()))
        end = window_start
        if (end - start).total_seconds() / 60 >= cfg["fallback_min_minutes"]:
            res.selection = "fallback"
            res.reason = ("障害日より前の日のログが無いため、障害日の時間窓より前を使った。"
                          "時刻帯が違うので件数の比較は目安")
            res.periods = [Period(start, end, f"{start:%m-%d %H:%M}〜{end:%H:%M}")]
            res.warnings.append("障害日より前の日のログが無い。少なくとも前営業日のログを依頼すること")
            return
    res.selection = "none"
    res.reason = "比較に使える平常期間がない"
    res.warnings.append("平常期間との比較ができない。少なくとも前営業日のログを依頼すること")


def _suspend_noise(res: BaselineResult, templates: Dict[int, Template], cfg: dict) -> None:
    """既知ノイズでも、今回だけ様子が違うものはノイズ扱いをやめる（下に回さない）。"""
    min_new = cfg["noise"]["suspend_new_min_count"]
    for c in res.comparisons:
        t = templates[c.key]
        if t.noise_reason is None:
            continue
        if c.kind == SURGE:
            t.noise_suspended = f"平常期間の {c.expected:.0f} 件から {c.count} 件に急増"
        elif c.kind == NEW and c.count >= min_new:
            t.noise_suspended = f"平常期間には無く、時間窓に {c.count} 件"
    for cp in res.change_points:
        t = templates[cp.key]
        if t.noise_reason is not None and cp.kind == APPEAR and not t.noise_suspended:
            t.noise_suspended = f"{fmt_day(cp.day)}から出始めた"
