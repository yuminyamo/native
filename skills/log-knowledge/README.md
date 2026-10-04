# log-knowledge（障害ログAI調査ガイド 案3 の実装）

障害ログAI調査ガイド（リポジトリの `docs/log-rca-ai-guide.html`）の「案3 環境をまたいで残す知見」を Agent Skill として実装したものです。
AIエージェント向けの手順は [SKILL.md](SKILL.md)、このファイルは保守する人向けの説明と残作業です。

> **現状は仮実装です。** 実際の再現環境のログを見ずに、ガイドの架空例（INC-2026-0931）を再現環境のログとみなして作っています。

## 案1・案3の分担

| | log-digest（案1） | log-knowledge（案3） |
|---|---|---|
| 扱うもの | そのチケットのログだけで完結するもの（平常期間との比較・変化点を含む） | チケットや環境をまたいで残す知見 |
| 入力 | 顧客から受け取ったログ | 再現環境のログとソースコード |
| 実行する時 | チケットごと | リリース時、再現環境のログが手に入った時 |
| 出力 | 障害対応の後に捨てる | `knowledge/` に残す |

現在の中身は既知ノイズ辞書の生成だけです。過去障害のシグネチャは、年に10件程度で顧客ごとに使い方も違い、照合が当たる見込みが小さいため後回しにしました。

## 構成

```
skills/log-knowledge/         ← 正本。ここを編集する
├── SKILL.md                  AIエージェント向けの手順と判断基準
├── README.md                 このファイル
├── requirements.txt          PyYAML
└── scripts/
    ├── log_knowledge.py      CLI の入口（log_knowledge.py <コマンド> ...）
    └── logknowledge/
        ├── parsed.py         log-digest の出力（meta.json・parsed/）を読む
        ├── noise.py          候補作り・登録・検査
        └── cli.py            noise-candidates / noise-merge / noise-validate

knowledge/                    ← skill が作って残すデータ（skills/ の外。Copilot / Kiro 用にコピーしない）
├── known_noise.yaml          既知ノイズ辞書（log-digest が既定で読む）
└── known_noise_rejected.yaml 登録しなかった候補と理由
```

## 流れ

```
再現環境のログ ─ log-digest ─ parsed/ ・meta.json（平常期間の候補の日）
                                   │
                       noise-candidates（スクリプト）: 正常期間に何度も出た WARN / ERROR を候補にする
                                   │
                       AI: 候補ごとにソースコードの出力箇所を読み、register / reject と理由を書く
                                   │
                       noise-merge（スクリプト）: 項目の検査 → known_noise.yaml / known_noise_rejected.yaml
                                   │
顧客のログ ─────────── log-digest: 一致したログを下に回す（今回だけ急増・出現したものは回さない）
```

- **log-digest とはファイル形式だけで受け渡します**（コードは共有しません）。読むのは `meta.json` の `parsed`（schema 1）と `baseline.days[].normal_candidate`、`parsed/records.jsonl.gz`、`parsed/templates.jsonl` です。log-digest の形式を変える時は `parsed.py` も直します。
- **辞書の照合規則は log-digest の `noise.py` と同じ**です（`<...>` は任意の文字列、空白の数は無視）。片方を変えたらもう片方も直します。

## 設計上の判断

人のレビューを前提にしないため、誤った登録で本当の原因が隠れないよう、次の安全策を重ねています。

- **除外ではなく、下に回すだけ（log-digest 側）。** ダイジェストに1テンプレート1行で件数を残し、log-search では通常どおり検索できます。
- **今回だけ様子が違えばノイズ扱いをやめる（log-digest 側）。** 平常期間より急増した、平常期間に無かった、ある日から出始めた、のどれかに当たれば通常のログとして扱います。
- **根拠を必須にする。** AI が作った項目は `evidence.source`（どの環境・期間）、`evidence.rate`（平常時の件数）、`evidence.code`（出力箇所）が無いと `noise-merge` が拒否します。人が後から見直す時に、何を根拠にしたかが分かります。
- **迷ったら登録しない。** SKILL.md の判断基準で、再現環境に特有のログや、顧客環境では異常の手がかりになるログを外します。
- **固定部分が短いテンプレートは登録しない。** `<*> failed` のような項目は関係のないログまで一致するため、固定部分が8文字未満なら候補にせず、辞書の検査でも拒否します。
- **バージョンで範囲を限る。** 既定は出典と同じマイナー系列（5.2.3 → `5.2.*`）です。
- **却下も残す。** 同じ候補を毎回判断し直さないように、却下した理由を `known_noise_rejected.yaml` に残します。判断を変える時は該当項目を消して候補を出し直します。
- **顧客のログからは作らない。** 顧客環境は正常だったと確認できないためです。顧客環境ごとの平常ノイズは log-digest の平常期間との比較（「変化なし」）が扱います。

## 使い方（人が試す場合）

```bash
.venv/bin/pip install -r skills/log-digest/requirements.txt -r skills/log-knowledge/requirements.txt

# 架空ログを再現環境のログとみなす
.venv/bin/python tests/incident_fixture.py /tmp/repro/logs
.venv/bin/python skills/log-digest/scripts/log_digest.py --ticket NOISE-5.2.3 \
  --incident-time "2026-09-30 11:59" --product-version 5.2.3 --logs /tmp/repro/logs --out /tmp/repro/out
.venv/bin/python skills/log-knowledge/scripts/log_knowledge.py noise-candidates \
  --dir /tmp/repro/out --source "再現環境 PMS 5.2.3（架空）"
# /tmp/repro/out/noise_candidates.yaml に decision などを書いてから
.venv/bin/python skills/log-knowledge/scripts/log_knowledge.py noise-merge --candidates /tmp/repro/out/noise_candidates.yaml
```

## テスト

```bash
.venv/bin/python -m unittest discover -s tests
```

`tests/test_log_knowledge.py` が、架空ログを再現環境のログとみなして、候補作り → 判断の書き込み → 登録 → log-digest での利用までを通します。
変化点（9/24）より後のログが候補に入らないこと、判断の抜けや根拠の不足があると辞書を変えないこと、登録済み・却下済みが次から候補にならないことを確認します。

## 残作業

### 実際の再現環境のログで決めること

| # | 項目 | 現在の仮の実装 | 確認・調整すること |
|---|---|---|---|
| 1 | 候補の条件 | WARN 以上で、正常期間に3件以上・2時間帯以上 | 試験の長さ（数時間か数日か）に合うか。起動時に1回だけ出る警告をどう扱うか |
| 2 | 正常期間の取り方 | 人の指定、または log-digest の平常期間の候補（全日単位） | 試験の手順書やログから、正常期間を機械的に取れるか（試験開始・終了のログなど） |
| 3 | バージョンの範囲 | マイナー系列（`5.2.*`） | パッチでログの文言が変わることがあるか。国別版・案件個別版で辞書を分ける必要があるか |
| 4 | 再現環境で動かす機能 | 動かした機能のノイズだけが入る | 主要な機能の組み合わせを試験で網羅できているか |
| 5 | 再現環境と顧客ログのテンプレートの形の違い | 辞書のテンプレートを正規表現にして、顧客ログのテンプレートの文字列と照合する（`<...>` は任意の文字列） | Drain3 のテンプレートは集まったログによって一般化の度合いが変わる。顧客ログの方がより一般化された形（固定部分が少ない形）になると一致しない。実ログで一致しない例があれば、代表行（生のメッセージ）でも照合するなどを検討する |
| 6 | ソースコードの検索 | AI が `search_hint`（テンプレートの最も長い固定部分）でソースコードを検索する | 約1,000万行のコードで、検索が現実的な時間で終わるか。文言をリソースファイルやメッセージIDで管理していて、ログの文言がコードに直接書かれていない場合の探し方。同じ文言が多数の箇所にある場合の扱い |

### 運用で決めること

| # | 項目 | 内容 |
|---|---|---|
| 7 | 実行のきっかけ | リリース時に試験ログで実行する手順を、リリース作業に組み込むか |
| 8 | 古い項目の整理 | 製品の修正でログが出なくなった項目は残り続ける。新しいバージョンで候補を出した時に一致しなかった項目を一覧にする機能を足すか |
| 9 | チケットを閉じる時の記録（任意） | 原因のテンプレートが既知ノイズとして下に回されていなかったかを数行残すと、誤った登録に気づける。顧客ログの本文は残さない。仕組みは log-digest README の残作業22でまとめて決める |
| 10 | 過去障害のシグネチャ | 後回し。始める場合はこのskillに足し、`knowledge/` に置く |
| 11 | CI | `noise-validate` を CI で実行する |
| 12 | AI の判断の見直し方 | 人のレビューが無いので、誤った登録には障害時にしか気づけない。チケットを閉じる時の記録（9、log-digest README の残作業22）で原因が下に回されていた例が見つかったら、その項目を消して却下リストに理由を残し、SKILL.md の判断基準を直す。時間が取れる時に、知見のある人が辞書を抜き取りで見る運用も検討する |
| 13 | 照合規則が2か所にある | 辞書との照合（`<...>` を任意の文字列にする規則）が log-digest の `noise.py` とこのskillの `noise.py` にある（skill どうしでコードを共有しない方針のため）。ずれないよう、同じ例で両方を確かめるテストを足す |
| 14 | log-digest の `meta.json` への依存 | `baseline.days[].normal_candidate` を読んでいる。`meta.json` には形式のバージョンが無いので、log-digest 側で持たせたら（log-digest README の残作業）ここでも確かめる |
| 15 | 自動実行での利用 | 現在は VS Code 上で人が依頼する使い方を想定している。リリース作業から自動で実行する場合、AI がソースコードを読める環境で動かす必要がある |
| 16 | Windows / Copilot / Kiro での動作確認 | macOS でのみ確認済み。両ツールで skill が認識され、SKILL.md の判断基準どおりに候補を判断するかを確認する |
