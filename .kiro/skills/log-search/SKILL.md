---
name: log-search
description: 障害ログの検索ツール。log-digest のダイジェストで足りない情報（時間窓の外、特定の行の前後、同じジョブIDの他のログ、件数の推移など）を、全期間の全ログを入れた DuckDB から必要な分だけ取り出す。生ログを直接読む・grep する代わりに必ずこれを使う。Use when the log digest is not enough during incident log investigation (search logs, get context, count by time, SQL, 障害調査, ログ検索).
---

# log-search: 障害ログの検索ツール

log-digest skill が作ったダイジェストを入口にし、足りない部分をこのツールで少しずつ取りに行く。
ツールは全期間の全レコード（log-digest の `parsed/`）を DuckDB に入れて検索する。どのツールも返す量に上限がある。

## 1. 準備

- 先に **log-digest skill を実行しておく**（出力: `log-digest-out/<チケット>/`）。DB（`logs.duckdb`）は最初のツール呼び出しで自動で作られる。
- このskillの `requirements.txt` を、log-digest と同じ venv に入れる（初回のみ）。`<skill>` はこの SKILL.md があるディレクトリ。

```bash
.venv/bin/pip install -r <skill>/requirements.txt          # macOS / Linux
.venv\Scripts\pip install -r <skill>\requirements.txt      # Windows
```

## 2. 呼び出し方

```bash
.venv/bin/python <skill>/scripts/log_search.py <ツール> --ticket INC-2026-0931 [引数...]
```

Windows では `.venv\Scripts\python <skill>\scripts\log_search.py ...`。log-digest を `--out` 付きで実行した場合は `--ticket` の代わりに `--dir <出力ディレクトリ>` を使う。

| ツール | 用途 | 主な引数 | 返す上限 |
|---|---|---|---|
| `get_digest` | 調査の入口。案1のダイジェストを返す | なし | 約5,000トークン |
| `search_logs` | キーワード・レベル・時間範囲・ファイルで検索 | `-q` `--level` `--from` `--to` `--file` `--component` `--template` | 200行。超えたら件数・先頭20行・テンプレート別内訳 |
| `get_context` | 特定の行の前後を見る | `ファイル:行番号` `--before` `--after` `--merged` | 前後それぞれ50件 |
| `template_lines` | テンプレートIDの実際の行を見る | `T07` `--sample first/last/spread` | 20行 |
| `count_by_time` | テンプレートごとの件数の時間推移 | `T07 T12` `--bucket` `--from` `--to` | 120区間、8テンプレート |
| `run_sql` | 上で足りない時の読み取り専用SQL | `"SELECT ..."` | 200行・30秒 |
| `schema` | `run_sql` 用のテーブル・列の説明 | なし | |

引数の詳細は `<ツール> --help` で確認できる。

- **時刻**はすべて log-digest の表示用タイムゾーン（ダイジェストの「申告時刻」の括弧内）。`--from` / `--to` は `'2026-09-30 10:42'` `'09-30 10:42'` `'10:42'`（日付を省くと申告日）`'2026-09-27'` の形。`--to` は書いた精度の終わりまで含む（`10:42` なら 10:42:59.999 まで）。
- **`--level WARN`** は WARN 以上（ERROR・FATAL も含む）。
- **`-q`** は大文字小文字を区別しない部分一致で、スタックトレース等の続き行も検索する。複数指定するとすべてを含む行。
- **出力の1行**は `時刻 テンプレートID レベル ファイル別名 [コンポーネント] メッセージ  @ファイル:行番号`。行頭の `>>` は `get_context` の対象、`~` は既知ノイズ。

## 3. 調査の進め方

1. ダイジェストを読み、仮説を1つ立てる（読んでいなければ `get_digest`）。
2. 仮説を確かめる・崩すのに必要な最小の問い合わせを1つ選ぶ。
3. 結果を見て仮説を更新し、2に戻る。根拠がそろったら結論を書く。

ガイドの架空障害での例:

```bash
# 容量不足はいつから進んでいたか（時間窓の外を調べる）
log_search.py search_logs --ticket INC-2026-0931 -q "Spool usage" --level WARN --from 2026-09-27 --to "2026-09-30 10:42"
# 毎日3時に動く処理は何をしていたか
log_search.py run_sql --ticket INC-2026-0931 "SELECT ts, component, message FROM logs WHERE component = 'cleanup' ORDER BY ts DESC LIMIT 5"
# 最初の ERROR の前後を、全ファイルを時刻順に並べて見る
log_search.py get_context --ticket INC-2026-0931 pms-server.log:4414 --merged
# DB接続待ち（T15）は ERROR（T12）より前からあったか
log_search.py count_by_time --ticket INC-2026-0931 T12 T15 --bucket 1m --from 10:30 --to 10:50
```

よく使う調べ方:

| 知りたいこと | ツール |
|---|---|
| この警告は障害の前から出ていたか | `count_by_time <ID>`（範囲を省くとログ全体の期間）。または `template_lines <ID>` の全期間の件数と初出 |
| いつもの完了ログが途中で消えていないか（ERROR にならない異常） | `count_by_time <完了ログのID>` で0になった区間を探す |
| 同じジョブ・ユーザー・ホストの他のログ | `search_logs -q J-20391`（ファイルをまたいで探せる） |
| サーバとエージェントの因果関係 | `get_context <行> --merged` |
| 値（使用率・待ち時間など）の変わり方 | `template_lines <ID> --sample spread` |

## 4. 守ること

- **生ログを直接読んだり grep したりしない。** 必要な行はこのツールで取る。
- 一致が多すぎて切られた時は、推測で補わず、条件（時間範囲・レベル・ファイル・テンプレート）を絞って取り直すか、`count_by_time` / `run_sql` で集計する。
- 結論の根拠には、ツールが返した行の `時刻` と `@ファイル:行番号` をそのまま挙げる（例: `09-28 03:00:10 @pms-server.log:230`）。ツールの結果に無い行を根拠にしない。
- 原因と二次症状を区別する。件数の多さではなく、初出時刻の早さと、時間の流れで説明できるかで判断する。
- 既知ノイズ（`~` の行）は平常時から出ているログ。今回だけ急増・出現したものは log-digest がノイズ扱いをやめているので `~` は付かない。`~` の行も原因でないと決めつけず、時間の流れと合わなければ検討する。
- `run_sql` は読み取り専用で、外部ファイルの読み書きもできない。エラーが出たら `schema` で列名を確かめる。
- ツールの呼び出しは `log-digest-out/<チケット>/tool_calls.jsonl` に記録される（調査の振り返り用）。
