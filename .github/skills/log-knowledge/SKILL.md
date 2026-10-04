---
name: log-knowledge
description: 再現環境（検証環境）の正常期間のログとソースコードから、既知ノイズ辞書（knowledge/known_noise.yaml）を根拠付きで作る・更新する。新しいバージョンのリリース時や、再現環境のログが手に入った時に使う。障害チケットの調査そのものには使わない（log-digest / log-search を使う）。Use when building or updating the known-noise dictionary from reproduction-environment logs (known noise, 既知ノイズ, ログ知見).
---

# log-knowledge: 既知ノイズ辞書を作る

既知ノイズ辞書は、平常時から出ている WARN / ERROR の一覧である。log-digest がこれを読み、一致したログをダイジェストの下に回す。
**人のレビューは無い前提**なので、登録してよいかは AI が下の判断基準で決め、理由と根拠（出力箇所）を必ず残す。

| ファイル | 内容 |
|---|---|
| `knowledge/known_noise.yaml` | 既知ノイズ辞書（log-digest が既定で読む） |
| `knowledge/known_noise_rejected.yaml` | 登録しなかった候補と理由（次から同じバージョンでは候補にしない） |

どちらも `noise-merge` だけで更新する（直接編集しない）。`knowledge/` はワークスペースのルートにある。

## 1. 準備

- log-digest skill の準備（venv と requirements）が済んでいること。このskillの `requirements.txt` も同じ venv に入れる。`<skill>` はこの SKILL.md があるディレクトリ。
- **再現環境のログ**と、**正常に動いていた期間**（試験に合格した期間、障害を再現する前の期間など）を人から受け取る。
  期間が分からない場合は、log-digest が変化点から選んだ日を使う（3で確認する）。
- **製品のソースコード**がワークスペースで読めること。読めない場合は登録できないので、その旨を人に伝えて止める。

## 2. 再現環境のログを log-digest にかける

```bash
.venv/bin/python <log-digest>/scripts/log_digest.py \
  --ticket NOISE-5.2.3-202610 --incident-time "<ログの最後の時刻>" \
  --product PMS --product-version 5.2.3 --logs path/to/repro-logs
```

`<log-digest>` は log-digest skill のディレクトリ（このskillと同じ階層）。`--ticket` は任意の名前でよい。
`--incident-time` にログの最後の時刻を渡すと、それより前で変化点・ERROR の急増が無い日を「平常期間の候補」として meta.json に書く。

## 3. 候補を出す

```bash
.venv/bin/python <skill>/scripts/log_knowledge.py noise-candidates \
  --dir log-digest-out/NOISE-5.2.3-202610 \
  --source "再現環境 PMS 5.2.3 リリース試験 2026-09-16〜09-23" \
  --normal "2026-09-16 09:00/2026-09-23 18:00"
```

- `--source` は根拠として辞書に残る。環境・バージョン・期間が分かるように書く。
- `--normal` は正常だと分かっている期間（日付 `2026-09-16`、または範囲 `開始/終了`、複数可）。省略すると log-digest が選んだ日を使う。その場合は出力の「正常期間」に**障害を再現した期間が含まれていないか**を確かめ、含まれていれば `--normal` で指定し直す。
- 登録するバージョンは既定で同じマイナー系列（5.2.3 なら `5.2.*`）。変える時は `--versions`。
- 出力は `<--dir>/noise_candidates.yaml`。正常期間に何度も出ていた WARN / ERROR が候補になる。登録済み・却下済み・件数が少ないものは `skipped` に理由付きで並ぶ。

## 4. 候補ごとに判断する（ここが AI の仕事）

候補ごとに、`search_hint`（テンプレートの固定部分）でソースコードを検索し、ログを出している箇所と前後の処理を読む。
次の **すべて** を満たす時だけ `decision: register` にする。

1. **出力箇所を特定できた。** 同じ文言の箇所が複数あれば、すべて読む。
2. **ログを出した後も処理が正常に続き、結果に影響しない。** 例: 代わりの手段で成功する、再試行で成功する、使っていない任意機能について知らせるだけ。
3. **顧客環境でも平常時に出うる。** 再現環境に特有の条件（監視サーバが無い、試用ライセンス、試験用の設定、意図的に起こした障害）で出ているものは登録しない。
4. **顧客環境で出たら設定ミスや異常の手がかりになるものではない。**

**迷ったら登録しない。** 誤って登録すると、本当の原因が下に回されて見落とされる。登録しなければ、ダイジェストに少し余計な行が残るだけで済む。

書き込む項目:

| decision | 書く項目 |
|---|---|
| `register` | `reason`: 影響がないと言える理由（処理がどう続くかを具体的に）。`code`: 出力箇所（`ファイル:行`、複数なら `,` 区切り） |
| `reject` | `reject_reason`: 登録しない理由（どの基準を満たさないか） |

## 5. 辞書に反映する

```bash
.venv/bin/python <skill>/scripts/log_knowledge.py noise-merge --candidates log-digest-out/NOISE-5.2.3-202610/noise_candidates.yaml
.venv/bin/python <skill>/scripts/log_knowledge.py noise-validate
```

- 判断していない候補や、項目の足りない候補があると反映しない（エラーの内容に従って直す）。今回は判断を見送る候補がある時だけ `--allow-undecided` を付ける。
- 終わったら、登録した項目と却下した項目を一覧にして人に報告する。git で管理している場合は `knowledge/` の変更をコミットする。

## 守ること

- **登録の根拠はソースコードで確かめる。** ログの文言だけを見て「影響なさそう」と判断しない。
- 辞書のファイルを直接編集しない（`noise-merge` が根拠の有無や重複を検査する）。
- 顧客から受け取ったログからは辞書を作らない。顧客環境は「正常だった」と確認できないため（少しずつ進んでいた障害のログを平常と誤認するおそれがある）。顧客環境ごとの平常ノイズは、log-digest の平常期間との比較が扱う。
