"""ガイドの架空障害 INC-2026-0931 を模したログ一式を生成する（テスト・動作確認用）。

真の原因: 一時ファイル削除タスクが 9/24 から無効 → スプール領域が埋まり 9/30 10:42:05 に容量不足。
ノイズ:   LDAP referral ignored（平常時から WARN）、SNMP trap send failed（平常時から ERROR）。
二次症状: 10:42 以降の DB 接続待ち。

ファイルごとに書式・タイムゾーン・文字コードをわざと変えている。
- pms-server.log  : 2026-09-30 10:42:05.907 ERROR [spool] ...（+09:00、UTF-8、先頭にヘッダ行）
- print-agent.log : 2026-09-30T01:42:06.120Z ERROR [agent] ...（UTC）
- db.log          : 2026/09/30 10:42:06,004 WARNING [pool] ...（カンマ区切りミリ秒、WARNING 表記）
- auth.log        : CP932 で日本語を含む

python tests/incident_fixture.py <出力ディレクトリ> で単体でも生成できる。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

JST = timezone(timedelta(hours=9))
START = datetime(2026, 9, 28, 0, 0, 0)
INCIDENT = datetime(2026, 9, 30, 10, 42, 5, 907000)
END = datetime(2026, 9, 30, 12, 0, 0)

USERS = [("tanaka", "PC-SALES-012"), ("suzuki", "PC-SALES-020"), ("yamada", "PC-ACC-003")]
DOCS = ["見積書_A社.xlsx", "議事録0930.docx", "請求書.pdf"]


def _jst(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def _pms_server() -> List[str]:
    events: List[Tuple[datetime, str]] = []

    def add(dt: datetime, level: str, comp: str, msg: str, extra: Tuple[str, ...] = ()) -> None:
        line = f"{_jst(dt)} {level:<5} [{comp}] {msg}"
        events.append((dt, "\n".join((line,) + extra)))

    add(START, "INFO", "main", "Font cache rebuild skipped")
    t = START
    n = 0
    while t < END:
        # 平常時から出ている警告とエラー（既知ノイズ）
        if t.minute % 5 == 0:
            add(t + timedelta(seconds=3, microseconds=551000), "WARN", "ldap",
                f"LDAP referral ignored: ldap://dc{2 + (t.minute // 5) % 2}.corp-a.local")
        if t.minute % 20 == 7:
            add(t + timedelta(seconds=1), "ERROR", "snmp", "SNMP trap send failed: 192.168.10.5 timeout")
        # 毎日 3時の定期処理: 削除タスクは無効、使用率が単調増加
        if t.hour == 3 and t.minute == 0:
            day = (t - START).days
            add(t + timedelta(seconds=1), "WARN", "cleanup", "Temp cleanup skipped: scheduled task disabled")
            add(t + timedelta(seconds=10), "WARN", "spool", f"Spool usage {[81, 88, 95][day]}% on D:")
        # 印刷ジョブ（2分ごと）。障害後は失敗する
        if t.minute % 2 == 0:
            n += 1
            job = f"J-{20000 + n}"
            user, host = USERS[n % 3]
            dt = t + timedelta(seconds=n % 50, microseconds=112000)
            add(dt, "INFO", f"job-worker-{n % 4}",
                f'Job {job} received user={user} host={host} document="{DOCS[n % 3]}"')
            if dt < INCIDENT:
                add(dt + timedelta(seconds=4), "INFO", f"job-worker-{n % 4}",
                    f"Job {job} completed (pages={n % 7 + 1})")
        t += timedelta(minutes=1)

    add(datetime(2026, 9, 30, 10, 20, 44), "WARN", "spool", "Spool usage 99% on D:")
    add(datetime(2026, 9, 30, 10, 41, 58, 112000), "INFO", "job-worker-3", "Job J-20388 completed (pages=4)")

    # 障害: 10:42:05.907 から書き込み失敗とジョブ中断が続く。DB接続待ちは二次症状
    stack = ("java.io.IOException: There is not enough space on the disk",
             "\tat jp.example.pms.spool.SpoolWriter.write(SpoolWriter.java:212)",
             "\tat jp.example.pms.job.JobProcessor.spool(JobProcessor.java:88)",
             "\tat jp.example.pms.job.JobProcessor.run(JobProcessor.java:41)",
             "\tat java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1149)",
             "\tat java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:624)",
             "\tat java.lang.Thread.run(Thread.java:750)")
    t = INCIDENT
    k = 0
    while t < END:
        job = f"J-{20391 + k}"
        add(t, "ERROR", "spool", f"Spool write failed: disk quota exceeded on D:\\PMS\\spool\\{job}.tmp")
        add(t + timedelta(milliseconds=3), "ERROR", f"job-worker-{k % 4}",
            f"Job {job} aborted: IOException at SpoolWriter.write(SpoolWriter.java:212)", stack)
        add(t + timedelta(milliseconds=97), "WARN", "db",
            f"Connection pool wait {1000 + (k * 37) % 900} ms (active=50/50)")
        k += 1
        t += timedelta(seconds=7, milliseconds=423)

    events.sort(key=lambda e: e[0])
    return ["=== PMS Server 5.2.3 log started ==="] + [e[1] for e in events]


def _print_agent() -> List[str]:
    lines = []
    t = START
    while t < END:
        utc = (t.replace(tzinfo=JST)).astimezone(timezone.utc)
        if t.minute % 10 == 0:
            lines.append(f"{utc:%Y-%m-%dT%H:%M:%S}.000Z INFO [agent] Polling server 10.20.30.40:8443 ok")
        t += timedelta(minutes=1)
    for i in range(3):
        dt = (INCIDENT + timedelta(seconds=1 + i * 30)).replace(tzinfo=JST).astimezone(timezone.utc)
        lines.append(f"{dt:%Y-%m-%dT%H:%M:%S}.120Z ERROR [agent] Job J-{20391 + i} not received from server "
                     f"(status=503) host=PC-SALES-012")
    lines.sort()
    return lines


def _db() -> List[str]:
    lines = []
    t = datetime(2026, 9, 30, 10, 42, 6, 4000)
    for i in range(40):
        lines.append(f"{t:%Y/%m/%d %H:%M:%S},{t.microsecond // 1000:03d} WARNING [pool] "
                     f"Connection pool exhausted, waiting (active=50/50)")
        t += timedelta(seconds=30)
    return ["2026/09/28 00:00:00,000 INFO [db] Database started"] + lines


def _auth() -> List[str]:
    lines = []
    t = START
    while t < END:
        if t.minute == 15:
            user, host = USERS[t.hour % 3]
            lines.append(f"{_jst(t)} INFO [auth] ログイン成功 ユーザー：{user} host={host} "
                         f"mail={user}@corp-a.co.jp")
        t += timedelta(minutes=1)
    return lines


def write_incident_logs(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pms-server.log").write_text("\n".join(_pms_server()) + "\n", encoding="utf-8")
    (out_dir / "print-agent.log").write_text("\n".join(_print_agent()) + "\n", encoding="utf-8")
    (out_dir / "db.log").write_text("\n".join(_db()) + "\n", encoding="utf-8")
    (out_dir / "auth.log").write_bytes(("\r\n".join(_auth()) + "\r\n").encode("cp932"))
    (out_dir / "README.txt.bak").write_text("ログ以外のファイル（読み飛ばされること）", encoding="utf-8")
    return out_dir


KNOWN_NOISE_YAML = """\
- template: "LDAP referral ignored: <*>"
  versions: ["5.1.*", "5.2.*"]
  reason: "AD多段構成の環境では常時出力される。動作への影響なし"
  added: 2026-05 保守チーム
- template: "SNMP trap send failed: <*>"
  versions: ["*"]
  reason: "監視サーバ未設定の環境で常時出力される"
  added: 2026-06 保守チーム
"""

if __name__ == "__main__":
    dest = write_incident_logs(Path(sys.argv[1] if len(sys.argv) > 1 else "incident-logs"))
    (dest.parent / "known_noise_example.yaml").write_text(KNOWN_NOISE_YAML, encoding="utf-8")
    print(dest)
