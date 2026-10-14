# ルーティング評価ハーネス（OpenClaw 外側ループ）

`tests/routing/catalog_routing_corpus.jsonl` の期待ラベル付き発話（84 件）を、**本番と同じ OpenClaw 2026.7.1
が Bedrock へ送る要求**に差し込んで Haiku 4.5 で実行し、ツール選択・引数・聞き返し・拒否を採点する。

ツール定義やモデルを変えたとき（例: #491 のスキーマ圧縮、A3 の説明文の英語化）に、
「トークンは減ったがツールの選び方が悪くなっていないか」を同じ物差しで比べるためのもの。

- 採点（`scoring.py`）は決定的で、`tests/scripts/test_routing_eval_scoring.py` が CI で固定する
- LLM を呼ぶ実行（`run_routing_eval.py`）は**手動専用・課金あり**。CI では回さない
- 本番の設定・デプロイには一切触れない。ツールは実行しない（MCP は代役）

## しくみ

```
dump_tools.py ──(コミットごとのツール定義 JSON)──┐
                                                  ▼
capture.sh ── OpenClaw 公式イメージ（本番と同じ digest）＋同版の slack / amazon-bedrock plugin
   │           本番の openclaw.config.json5（Bedrock と MCP の宛先だけ代役へ）・SOUL.md ほか
   │           slack_dm_driver.mjs が Slack DM 1 通と同じ形で OpenClaw の受信処理を呼ぶ
   ▼
capture_stubs.py ── Bedrock の代役が「送ろうとした要求」を保存（推論しない）
   ▼
make_template.py ── 要求を雛形に（発話の位置に目印・ツール定義の改変が無いことを検査）
   ▼
run_routing_eval.py ── 雛形に 84 件の発話を差し込み、本物の Bedrock（Haiku 4.5）で最初のターンだけ実行
   ▼
report.py ── 指標・必須条件・ケース単位の差
```

本番の要求との違い（意図して固定・省略しているもの）:

| 項目 | 扱い |
|---|---|
| Slack の受信 | `@openclaw/slack` 2026.7.1 の受信処理と同じ SDK 関数で組む。Slack API には接続しない |
| 送信者 | 実在しない ID `U0ROUTEVAL1`・名前「評価ユーザー」（実利用者の ID・会話は使わない） |
| 日時 | 評価の発話は 2026-10-01 01:00 UTC（木曜 10:00 JST）に固定 |
| 初めての DM の Slack 履歴 | 本番は記録が無いとき Slack の DM 履歴を `dmHistoryLimit` 件前置きするが、ここでは 0 件 |
| 本人特定 plugin | 外す。プロンプトもツール定義も変えない plugin だが、「連携」の短文を決定論で `oauth_connect` に回す層1・層2 は評価に乗らない（モデル単体の判断を測る） |
| App Home のパッチ | 当てない（Home タブの文面だけでプロンプトに無関係） |
| system の Runtime 行の sessionId | 取り出すたびに変わる UUID を固定値に置換（コミット間で system を完全一致させるため） |
| ツールの実行 | しない。最初の assistant ターン（どのツールを・どの引数で呼ぶか／呼ばずに何と答えるか）だけを見る |

Slack 経路とローカル経路（`openclaw agent --local`）の要求を `diff_templates.py` で比べると、違いは
system の「Conversation Context / Inbound Context」節（channel=slack・chat_type=direct・mrkdwn の書式）と
Slack DM の一文・承認ボタンの一文・Runtime 行、user 側の「Conversation info」ブロックだけで、
ツール定義・推論設定・キャッシュ位置は同一（2026-10-01 確認）。

## 評価群

| group | 中身 |
|---|---|
| `new_dm` | 新規 DM の 1 通目 |
| `prior_search` | 同じ DM で「あいさつ」「架空の会社の資料検索（search を 1 回呼んで答えた）」の 2 往復の後（`history_scenarios.json`）。本番の `dmHistoryLimit: 2` と同じ件数。前置きの応答も OpenClaw 自身が履歴として整形したもの |

## 手順

```bash
# 0) 比べるコミットを detach で置く
git worktree add --detach /tmp/src_A <commit A>
git worktree add --detach /tmp/src_B <commit B>

# 1) ツール定義（同じ機能フラグで固定。公開停止の影響を測るときだけ --disable を使う）
#    いまの本番に合わせるなら flags_mcp157.json（2026-10-14 の本番 TD）。#491 前後の比較は flags_mcp132.json
uv run python scripts/routing_eval/dump_tools.py --src /tmp/src_A/src --flags scripts/routing_eval/flags_mcp157.json --out runs/tools_A.json
uv run python scripts/routing_eval/dump_tools.py --src /tmp/src_B/src --flags scripts/routing_eval/flags_mcp157.json --out runs/tools_B.json

# 2) 雛形（docker が要る。AWS の資格情報は使わない。1 本 20 秒ほど）
scripts/routing_eval/capture.sh /tmp/src_A runs/tools_A.json runs/cap_A_new
scripts/routing_eval/capture.sh /tmp/src_A runs/tools_A.json runs/cap_A_hist prior_search
scripts/routing_eval/capture.sh /tmp/src_B runs/tools_B.json runs/cap_B_new
scripts/routing_eval/capture.sh /tmp/src_B runs/tools_B.json runs/cap_B_hist prior_search

# 3) 比べる条件が「ツール定義の差だけ」になっているか確かめる（system・messages が identical=True）
uv run python scripts/routing_eval/diff_templates.py runs/cap_A_new/template.json runs/cap_B_new/template.json

# 4) 実行（課金あり。各ケース 3 回・variant を交互に）
uv run python scripts/routing_eval/run_routing_eval.py --variant A=runs/cap_A_new/template.json --variant B=runs/cap_B_new/template.json --runs 3 --out runs/results_new.jsonl
uv run python scripts/routing_eval/run_routing_eval.py --variant A=runs/cap_A_hist/template.json --variant B=runs/cap_B_hist/template.json --runs 3 --out runs/results_hist.jsonl

# 5) 集計（--template を付けると保存済みの本文から今の採点規則で採点し直す。課金なし）
uv run python scripts/routing_eval/report.py runs/results_new.jsonl runs/results_hist.jsonl --a A --b B \
  --template A=runs/cap_A_new/template.json --template B=runs/cap_B_new/template.json --md runs/report.md
```

`runs/` は手元の作業場所（コミットしない）。

### 費用と本番への配慮

- 1 回あたりの入力は約 9.5 万〜13 万トークン。プロンプトキャッシュが効くので、2 回目以降の入力の大半はキャッシュ読み
  （84 件 × 3 回 × 2 variant × 2 group ≒ 1,000 回で十数ドル）
- 本番 Aico と同じアカウントの枠（Haiku 4.5 は毎分 500 万トークン）を使うので、既定で毎分 150 万トークンに絞る（`--max-tpm`）
- キャッシュが 6 回続けて効かない／費用の見積りが `--max-usd` を超えたら止まる
- 途中で止まっても同じ `--out` で再実行すれば未実行の分だけ回る

## 採点

コーパス 1 行の `expect` / `alt_ok` / `forbid` / `arg_rules` を、`tests/routing/README.md` のシミュ手順と同じ 3 段で見る。

| 記録する項目 | 意味 |
|---|---|
| `selected_tool` / `called_tools` | 最初に呼んだツール／同じターンで呼んだ全部 |
| `arg_names` | 呼び出しごとの引数名（`_user_context` を除く） |
| `no_tool` / `response_kind` | ツールを呼ばなかったか／本文の分類（`refusal` 拒否・`ask_back` 聞き返し・`text` その他。日本語の定型句による近似） |
| `false_refusal` | 期待がツールなのに拒否した |
| `forbid_violation` / `arg_violation` | 禁止ツールを呼んだ／引数の契約違反（その引数を持つツールの呼び出しだけ検査） |
| `leaked_internal_names` | 本文にツール名や `teamagent__` が出た（SOUL の禁止語。参考指標で合否に入れない） |
| `usage` / `latency_ms` / `wall_ms` | Bedrock の使用量（キャッシュ読み書きを含む）・モデルの処理時間・往復時間 |

必須条件（`report.py` は b 側が満たさないと終了コード 1）:

- 凍結済みの本番障害ケース（`tests/skills/test_routing_descriptions_catalog.py` の `R4_REQUIRED_IDS`）が全回合格
- 禁止ツールの呼び出し 0・引数の契約違反 0・API エラー 0

本番で呼べないツールを期待する既知の行（同ファイルの `CORPUS_ROWS_EXPECTING_UNWIRED_TOOLS`）は母数から外して別に数える。

さらに `run_routing_eval.py` は、期待ツール（か `alt_ok`）が**どれかの variant の雛形に出ていない行**を実行前に外し、
外した行を毎回表示する（機能フラグで外れたツールを期待する行は、何を選んでも必ず落ちるため。課金もしない）。
固定の一覧はフラグを変えると古くなるので、こちらは実際に評価する雛形から判定する。凍結済みの本番障害行が外れる場合は止まる。

### 読み方の注意

コーパスは「name と description だけで 1 本選ぶルーター」の手動シミュ用に作られたため、
URL が途中で切れている・商材名が無いといった**情報不足の発話でもツールを選ぶのが正解**になっている行がある。
本番と同じ条件の Aico はこうした発話で聞き返すことが多く、合格率の絶対値は低めに出る。
コミットやモデルの**比較**には同じ条件で効くので使えるが、絶対値を品質の目標にするならコーパスの期待値を見直すこと。
