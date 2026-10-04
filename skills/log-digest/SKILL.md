---
name: log-digest
description: 障害チケットで受領したログ一式（server / agent / db / auth など、数万〜数十万行）から、AIが最初に読む数千トークンのダイジェストを作る。障害の一次調査でログを調べる時は、生ログを丸ごと読まずに必ずこのskillを最初に使う。Use when investigating an incident ticket with attached log files (log digest, root cause analysis, 障害調査, ログ解析).
---

# log-digest: 障害ログのダイジェスト生成

ログを丸ごと読まずに、スクリプト（LLMを使わない決定論的な処理）で次の資料を作ってから読む。

| 出力 | 内容 |
|---|---|
| `digest_<チケット>.md` | 最初に読む要約。最初の ERROR、テンプレート集計（件数・初出時刻）、既知ノイズ、ERROR前後の抜粋、代表スタックトレース |
| `context.md` | ERROR・例外の前後の文脈（塊 E01, E02 …）。行番号付き |
| `templates.tsv` | 時間窓内・全期間の全テンプレートの件数と初出・最終時刻 |
| `meta.json` | 実行条件、入力ファイルの統計、設定ファイルのハッシュ |
| `parsed/` | 全期間の全レコード（log-search skill が DuckDB に取り込んで検索する。直接読まない） |

## 1. 準備（初回のみ）

Python 3.9 以上が必要（動作確認は 3.9.6 で実施済み）。`python3 --version`（Windows は `py -3 --version`）で確認する。

ワークスペースのルートに venv を作り、このskillの `requirements.txt` を入れる。`<skill>` はこの SKILL.md があるディレクトリ。

```bash
# macOS / Linux
python3 -m venv .venv
.venv/bin/pip install -r <skill>/requirements.txt
```

```powershell
# Windows
py -3 -m venv .venv
.venv\Scripts\pip install -r <skill>\requirements.txt
```

## 2. チケットから引数を決める

| 引数 | 必須 | チケットのどこから取るか |
|---|---|---|
| `--ticket` | ○ | チケットID |
| `--incident-time` | ○ | 申告された発生時刻（`"2026-09-30 10:40"` の形）。タイムゾーンは `--tz`（既定 +09:00） |
| `--logs` | ○ | 受領したログのディレクトリまたはファイル（複数可、`.gz` 可） |
| `--symptom` |  | 症状の要約1行 |
| `--product` / `--product-version` |  | 製品名とバージョン。バージョンは既知ノイズ辞書の照合に使うので分かれば必ず渡す |
| `--os` |  | OS |

時計のずれが分かっている場合は `--clock-offset print-agent=-12` のようにファイル名（拡張子なし）ごとに秒数で補正する。

## 3. 実行

```bash
.venv/bin/python <skill>/scripts/log_digest.py \
  --ticket INC-2026-0931 --incident-time "2026-09-30 10:40" \
  --symptom "10:40頃から一部ユーザーの印刷ジョブが出力されない" \
  --product PMS --product-version 5.2.3 --os "Windows Server 2019" \
  --logs path/to/INC-2026-0931/logs
```

Windows では `.venv\Scripts\python <skill>\scripts\log_digest.py ...`。出力先は既定で `log-digest-out/<チケット>/`（`--out` で変更可）。

標準出力の `warning:` 行は必ず確認する。書式に合わない行が多い、時間窓にログが無い、などの入力側の問題が出る。

## 4. 読み方

1. `digest_<チケット>.md` だけを最初に読む。
2. 「最初に出た ERROR」と、テンプレート集計の**初出時刻**を重視する。件数が多いものは結果（二次症状）であることが多い。
3. 「全期間初出」が時間窓より前のテンプレートは、障害の前から出ていたログである（平常時からのノイズ、または徐々に進んでいた異常）。
4. 足りない情報は **log-search skill** のツールで取りに行く（時間窓の外も、特定の行の前後も、件数の推移も調べられる）。
   - 特定の行の前後: `get_context ファイル:行番号`
   - キーワード・時間範囲での検索: `search_logs`
   - テンプレートの実際の行・件数の推移: `template_lines` / `count_by_time`
   - ERROR前後の文脈の一覧は `context.md`、全テンプレートの一覧は `templates.tsv` にもある
5. ダイジェストに「省略」と書かれた箇所を推測で埋めない。

## 5. 守ること

- **生ログを直接読まない。** 数十万行を読むとコンテキストを使い切り、重要な行が埋もれる。行の確認は log-search の `get_context` で行う。
- 結論には根拠のログを `時刻` と `@ファイル:行番号` 付きで挙げる（例: `10:42:05.907 @pms-server.log:4414`）。
- ERROR にならない異常（処理が黙って止まる等）はダイジェストに出にくい。ERROR が見当たらない時は WARN のテンプレートと、全期間初出・件数の変化を確認し、その旨を結論に書く。

## 設定ファイル（`<skill>/config/`）

| ファイル | 内容 |
|---|---|
| `log_formats.yaml` | ログ書式（正規表現）、文字コード、タイムゾーン、レベル表記のゆれ |
| `drain3.ini` | テンプレート化（Drain3）の設定と、変わる値（数値・パス・ID）の置き換え規則 |
| `known_noise.yaml` | 既知ノイズ辞書。平常時から出るログを理由付きで登録する |

書式に合わない行が多いと警告が出たら、`log_formats.yaml` を直すよう人に報告する（skill の設定は勝手に変更しない）。
