# log-search（障害ログAI調査ガイド 案2 の実装）

障害ログAI調査ガイド（リポジトリの `docs/log-rca-ai-guide.html`）の「案2 ログ検索ツール」を Agent Skill として実装したものです。
AIエージェント向けの手順は [SKILL.md](SKILL.md)、このファイルは保守する人向けの説明と残作業です。

> **現状は仮実装です。** 実際のログ例を見ずに、ガイドの架空例（INC-2026-0931）に合わせて作っています。
> 実ログで調整が必要な箇所は「[残作業](#残作業)」にまとめました。

ガイドの「既存パイプラインへの組み込み方」にある最初の段階（skills ＋ コマンドラインのスクリプト）です。MCP サーバ化はしていません。

## 構成

```
skills/log-search/            ← 正本。ここを編集する
├── SKILL.md                  AIエージェント向けの手順（Copilot / Kiro 共通の形式）
├── README.md                 このファイル
├── requirements.txt          duckdb, PyYAML
├── config/
│   └── limits.yaml           各ツールが返す量の上限                         【仮】
└── scripts/
    ├── log_search.py         CLI の入口（log_search.py <ツール名> ...）
    └── logsearch/
        ├── store.py          parsed/ → DuckDB の取り込み、読み取り専用の接続、--from/--to の解釈
        ├── tools.py          6つのツールと schema（結果をテキストで返す関数）
        └── cli.py            引数の解釈、呼び出し記録（tool_calls.jsonl）
```

## log-digest との関係

```
ログ一式 ─ log-digest ─┬─ digest_<チケット>.md / context.md / templates.tsv / meta.json   （案1）
                       └─ parsed/records.jsonl.gz, parsed/templates.jsonl                  （全期間の全レコード）
                                     │ 最初のツール呼び出しで取り込む（取り込み元が変われば作り直す）
                                     ▼
                       logs.duckdb ─ log-search の各ツール                                  （案2）
```

- **パースとテンプレート化は log-digest だけが行います。** log-search は結果を取り込むだけなので、時刻の正規化・時計のずれの補正・テンプレートID（T07 など）・`ファイル:行番号` がダイジェストと必ず一致します。
- skill どうしはコードを共有していません。受け渡しは `parsed/` のファイル形式（`meta.json` の `parsed.schema`、現在 1）だけです。
  形式を変える時は log-digest の `export.py` の `PARSED_SCHEMA` と、log-search の `store.py` の `PARSED_SCHEMA` / 列定義を両方直します。
- ガイドでは Parquet 経由としていましたが、log-digest に duckdb への依存を持ち込まないよう JSON Lines（gzip）にしました。DuckDB の `read_json` でそのまま取り込めます。
- DB（`logs.duckdb`、取り込み元の判定用に `logs.duckdb.stamp`）と呼び出し記録（`tool_calls.jsonl`）は `log-digest-out/<チケット>/` に置きます（`.gitignore` 済み）。障害1件ごとに1ファイルなので、不要になったらディレクトリごと消せます。

## DB のテーブル

| テーブル | 内容 |
|---|---|
| `logs` | 全期間の全レコード。`seq`（全ファイルを時刻順にマージした通し番号）、`line_id`（`ファイル:行番号`）、`ts`、`level` / `severity`、`component`、`template_id`、`noise`、`message`、`extra`（続き行）など |
| `templates` | テンプレートごとの全期間・時間窓内の件数と初出・最終時刻、既知ノイズの理由 |
| `incident` | チケット情報と時間窓（1行） |
| `files` | 入力ファイルごとの統計 |

列の説明は `log_search.py schema --ticket <チケット>` で出ます（AIが `run_sql` の前に読む想定）。

## 設計上の判断

- **返す量に必ず上限を付ける。** ガイドの表の値を `config/limits.yaml` に置き、引数でそれより大きくはできないようにしました。上限を超えた時は、切ったこと・全体の件数・絞り方を必ず書きます（`search_logs` はテンプレート別の内訳も付けます）。
- **`run_sql` は3重に守る。** ① SELECT（と EXPLAIN）1文だけを受け付ける、② DB を読み取り専用で開く、③ DuckDB の `enable_external_access=false` で外部ファイルの読み書きと拡張の読み込みを止め、`lock_configuration` で SQL から設定を戻せないようにする。加えて時間（30秒で `interrupt`）・行数・メモリ（1GB）に上限があります。
- **`get_context` は既定で同じファイルの前後。** 生ログを開いて前後を見るのと同じ感覚で使えるようにしました。`--merged` で全ファイルを時刻順にマージした前後を見られます。続き行（スタックトレース）の行番号を指定した場合は、その行を含むレコードを対象にします。
- **`count_by_time` はすべて0の区間も出す（3つ以上続く所は1行にまとめる）。** 「いつもの完了ログが消えた」ことに気づけるようにするためです。区間が120を超える幅を指定された場合は、超えない幅に広げて、そのことを書きます。
- **`--to` は書いた精度の終わりまで含む。** ガイドの例 `to="2026-09-30 10:42"` で 10:42:05 の行が含まれるよう、`10:42` は 10:42:59.999 まで、`2026-09-30` はその日の終わりまでとしました。
- **ツールの呼び出しを記録する。** ガイド段階2の「AIが何回ツールを呼んだか、どの証拠で結論を出したか」を見直すため、引数・成否・出力の行数と文字数・所要時間を `tool_calls.jsonl` に1行ずつ残します。出力そのものは残しません。
- **結果はテキストで返す関数にした。** MCP サーバにする時は `tools.py` の関数をそのまま公開できます。

## 使い方（人が試す場合）

```bash
.venv/bin/pip install -r skills/log-digest/requirements.txt -r skills/log-search/requirements.txt

.venv/bin/python tests/incident_fixture.py /tmp/inc/logs
.venv/bin/python skills/log-digest/scripts/log_digest.py \
  --ticket INC-2026-0931 --incident-time "2026-09-30 10:40" --product-version 5.2.3 \
  --logs /tmp/inc/logs --known-noise /tmp/inc/known_noise_example.yaml

.venv/bin/python skills/log-search/scripts/log_search.py search_logs --ticket INC-2026-0931 \
  -q "Spool usage" --level WARN --from 2026-09-27 --to "2026-09-30 10:42"
.venv/bin/python skills/log-search/scripts/log_search.py run_sql --ticket INC-2026-0931 \
  "SELECT ts, component, message FROM logs WHERE component = 'cleanup' ORDER BY ts DESC LIMIT 5"
```

## テスト

```bash
.venv/bin/python -m unittest discover -s tests
```

`tests/test_log_search.py` が架空障害のログから log-digest → log-search を通して実行し、ガイドの調査例（容量の推移を時間窓の外まで遡る、cleanup のスキップを SQL で見つける）が再現できること、
上限・読み取り専用・タイムアウトが効くこと、取り込み元が変わったら DB を作り直すことなどを確認します。

## 性能の目安

架空の40万行（1ファイル）で、取り込みを含む最初の呼び出しが約0.5秒、2回目以降は1回約0.1秒。DB は約9MB、`parsed/records.jsonl.gz` は約6MB（MacBook、Python 3.9、duckdb 1.4.5）。

## 残作業

### 実際のログ例を見て決める必要があるもの

| # | 項目 | 現在の仮の実装 | 実ログで確認・調整すること |
|---|---|---|---|
| 1 | **返す量の上限**（`limits.yaml`） | ガイドの表の値（検索200行・超えたら20行、前後50件、20行、120区間、SQL 200行・30秒）、メッセージは300文字で切る | 実ログの1行の長さ（長いSQL文・XMLを1行に出すログなど）で、1回の結果が何トークンになるか。実際の障害で使いながら「上限で切られて手がかりを見落とした」「多すぎてコンテキストを圧迫した」例を集めて調整する |
| 2 | **`line_id` の形**（`ファイル:行番号`） | 受領ディレクトリからの相対パス（`/` 区切り）と行番号。別名（拡張子なし）やファイル名だけでも指定できる | ローテーション後のファイル（`pms-server.log.1` など）や、サーバごとのサブディレクトリに同名ファイルがある構成で、AIが迷わず指定できるか |
| 3 | **追跡に使うID** | `search_logs -q J-20391` の部分一致だけ | 実際のジョブID・セッションID・トランザクションIDの形。相関IDがあるなら、IDで全ファイルを横断する専用ツール（`trace_id` など）を足すか（案4と合わせて検討） |
| 4 | **よく使う調べ方の専用ツール化** | ガイドの6つと `schema` | 実際の障害で使いながら、`run_sql` に頼った問い合わせを `tool_calls.jsonl` から集め、繰り返し出る形を専用ツールにする（例: 任意の2期間の件数比較。平常期間との比較と変化点は log-digest のダイジェストに既にある） |
| 5 | **タイムゾーンの扱い** | すべて log-digest の表示用タイムゾーンの naive な時刻 | 海外拠点のログで、チケットの申告時刻・ログの時刻・AIへの指示がそれぞれどのタイムゾーンで書かれるか |
| 6 | **性能・サイズ** | 40万行で最初の呼び出し約0.5秒 | 実際の受領ログの最大サイズ（数百万行以上になるか）。`records.jsonl.gz` と `logs.duckdb` が二重に場所を取るので、大きい場合は取り込み後に jsonl を消す運用にするか |
| 7 | **続き行の上限** | log-digest が1レコードにつき300行まで保持（それ以上は件数だけ） | 長大なスタックトレースやダンプを出すログがあるか。あるなら上限を上げるか、続き行も別テーブルに入れる |

### 運用・組み込みで決めること

| # | 項目 | 内容 |
|---|---|---|
| 8 | MCP サーバ化 | 自動の一次調査と VS Code の両方から同じツールを使いたくなったら、`tools.py` の関数を MCP サーバとして公開する。その場合もサーバは読み取り専用・上限付きにし、チケットごとの DB を開く |
| 9 | 既存の一次調査パイプラインへの組み込み | 自動の一次調査からどう呼ぶか（log-digest の実行 → ダイジェストを入力に → 足りない時に log-search）。チケットへの書き戻しに根拠の `@ファイル:行番号` を含める形式 |
| 10 | 根拠の機械的な照合 | ガイド「注意点」の「引用された行が実在するかを機械的に照合する」。AIの結論に含まれる `@ファイル:行番号` と時刻・本文を `logs` テーブルと突き合わせるスクリプトを作る |
| 11 | 呼び出し記録の見直し | 評価データは用意しないので、実際の障害の `tool_calls.jsonl` を集計し、ガイドの「見る指標」（原因への到達、根拠の正しさ、入力トークン量など）とあわせて見直す。必要なら出力の要約も記録する（顧客ログの本文は残さない） |
| 12 | 同時実行 | 同じチケットで複数のプロセスが同時に最初の呼び出しをすると、それぞれ DB を作って最後のものが残る（結果は同じ）。Windows で DB を開いている間に作り直しが起きると置き換えに失敗する可能性がある。実運用で問題になるか確認する |
| 13 | Windows / Copilot / Kiro での動作確認 | macOS でのみ確認済み。DuckDB の Windows 版、パスの区切り、コンソールの文字コード、両ツールで skill が認識されるかを確認する |

### 他の案との接続（範囲外として未実装）

- **平常期間との比較**: 同じ環境の正常だった期間との比較と変化点は、log-digest がダイジェストに載せます（同じチケットのログで完結するため）。ダイジェストに無い期間どうしを比べたい時は `count_by_time` か `run_sql` を使います。
- **既知ノイズ**: `noise` 列は、log-digest が今回「下に回した」既知ノイズだけが true です。辞書に登録されていても、今回だけ急増・出現したものは false になります。
- **案3 過去障害のシグネチャ**: 後回しにしています（log-knowledge skill の README 参照）。
- **案4 構造化ログ**: JSON Lines のログに相関IDなどの項目が増えたら、`logs` テーブルに列を足し、IDで横断するツールを作ります。
