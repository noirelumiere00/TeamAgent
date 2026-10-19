# 利用者の目で確かめる Aico 回帰評価

Claude が小俣さん本人の Aico DM に自分の Slack の道具で送受信し、会話を JSON に記録する。
Python はそのファイルだけを採点する。Slack を叩かず、トークンを扱わず、ネットワークにも接続しない。
オフラインのテスト合格は Aico 本番の品質合格を意味しない。

## 記録して採点、95点まで反復する

1. Claude が `scenarios.json` の全13シナリオを実行する。`{client_a}` などの実名は手元で割り当てる。
   `campaign_a` は終了済みで提案書・動画別結果が確認できる施策、`campaign_b` は比較用。
   小俣さん本人の Aico DM のみを使い、各シナリオは独立した会話、複数発話は同じ会話で順次送る。
2. 送受信・添付・request_id・取得できたログを、下記の JSON 配列として便ごとに保存する。
   全発話への最終回答が揃うまで記録する。評価ボタン専用メッセージは Block Kit の構造から識別する。
   本文に「評価」という語があるだけで除外しない。
3. Claude がシナリオの全会話・添付・一次資料を確認して `judge` を記録する。
   `good` は依頼に答え根拠と成果物が揃う、`partial` は重要な一部不足、`bad` は依頼に答えない。
   重大違反は下記のコードで必ず明示する。単なる `bad` は重大違反の指定にはならない。
   `core.JUDGE_PROMPT` は10-06全DM判定原文未確認の暫定基準。原文確認後に差し替える。
4. 採点する（実名差し込み用の `scenarios.local.json` は採点時には不要）。

   ```bash
   python -m scripts.user_eval score \
     --recorded scripts/user_eval/results/batch-1/recorded.json \
     --scenarios scripts/user_eval/scenarios.json \
     --out scripts/user_eval/results/batch-1
   ```

5. `report.md` 最後の「直すべきこと」を Codex への修正指示の材料にする。
   ファイル候補はローカルコードの grep（rg）による**推測**。本文・ログ・実装で原因を確かめて修正する。
6. Claude が全シナリオを再実行して新しい便へ保存し、前回と比較する。全体95点以上になるまで反復する。

   ```bash
   python -m scripts.user_eval score \
     --recorded scripts/user_eval/results/batch-2/recorded.json \
     --scenarios scripts/user_eval/scenarios.json \
     --out scripts/user_eval/results/batch-2 \
     --previous scripts/user_eval/results/batch-1/results.jsonl
   ```

実名・会話・結果はコミットしない。既定の `results/` と `scenarios.local.json` は gitignore 済み。
任意の場所に記録した場合はその場所も管理する。
CLI の成功終了はファイル生成成功の意味。95点到達はレポートの全体点で確認する。

## JSON の形

1ファイルに複数シナリオの配列。IDは一意で、シナリオ定義の `id` と対応する。
`ts` は非負で有限の秒数（Slack の ts 文字列・JSON 数値の両方可）。`sent` は本人の送信のみ。
`received` は取得した会話。`author`・`files`・`is_feedback_buttons` を必須とし、添付なしは `[]`。
`request_ids` は必須配列（不明なら `[]`）、`logs`・`judge` は任意。

```json
[
  {
    "scenario_id": "named_proposal",
    "sent": [{"ts": "1792000000.000001", "text": "取引先の提案書PDFを出して"}],
    "received": [
      {
        "ts": "1792000030.000002",
        "author": "aico",
        "text": "資料を添付しました。",
        "files": [{"name": "proposal.pdf", "mimetype": "application/pdf"}],
        "is_feedback_buttons": false
      }
    ],
    "request_ids": ["request-example"],
    "logs": [],
    "judge": {"verdict": "good", "reason": "対象の提案書が実際に届き内容も依頼に合う", "critical_violations": []}
  }
]
```

必要な追加情報:

- ファイルの `url` は任意。本文URLとともにリンクチェックに使う。
- 受信の `citations: [{url, author_id, is_aico_dm}]` は任意の出典情報。
  自己引用禁止チェックでは Slack リンクの著者が不明なら不合格。自己DMを出典とした記録があれば0点。
  最小の記録だけから著者は推測しない。
- `critical_violations` はシナリオ直下または `judge` 内の任意配列。両方の和集合を使う。
  コードは `fabricated_numbers`（作り話の数字）、`other_company_numbers`（他社の数字）、
  `completed_as_ongoing`（終了済みを進行中）、`own_answer_as_source`（自分の過去回答を根拠）。
  Claude が一次資料と照合し、理由を `judge.reason` に書く。未知コードは入力エラー。
  捏造・他社混入を正規表現だけでは確定できないため、記録する側の確認が必要。
- シナリオ定義に `critical_patterns: [正規表現]` を指定できる。
  終了済み施策のシナリオでは進行中表現を重大違反にする。引用や否定でも一致し得るため、
  その場合は一次資料と記録をレビューし、必要ならシナリオの正規表現を調整する。

## 点数と出力

決定的チェックは70点を `checks` の各 `weight` 比で配分する。省略は1（すべて省略なら均等）。
重みは正の有限数。合格なら配点全額、不合格なら0点。
末尾の `llm_judge` は固定30点で、`good=30`・`partial=15`・`bad=0`。
判定未記録は未評価として0/30点（最大70点）。`llm_judge` の `weight` は使用しない。
記録の判定を優先し、プログラムからは `score_recorded(..., judge=...)` で `core.Judge` を差し込める。
判定には評価ボタン・本人受信発話・送信前の履歴を除いた会話を渡す。例外は判定失敗として減点する。
重大違反は決定的チェック・LLMが全合格でもそのシナリオを0点にする。

全体点は全シナリオ点の平均、小数1桁。未記録シナリオも0点で含める。
未知ID・ID重複・不正形式は入力エラー。送信数不一致・発話の未回答は記録不完備でシナリオ0点。
本人発話・評価ボタン・最初の送信より古い履歴はチェック・応答時間から除外する。
応答時間は各送信から次の送信までの最初の非空Aico応答を計測し、最も遅い発話を使う。
途中通知と最終成果物を区別するフィールドは最小記録にないため、内容の完了性は Claude の判定で確認する。

- `results.jsonl`: 1シナリオ1行。点・配点・判定・減点理由・原記録・ログ・前回点・差を保存。
- `report.md`: 全体点、95点到達、前回全体差、シナリオ別点と減点理由（各1行）、最後に「直すべきこと」。
  前回のシナリオ集合が異なる場合は全体差を比較不可とし、共通IDだけ個別比較する。
  同じIDでもシナリオ定義・判定基準を変えた場合は同条件の差ではないため、同じ基準で再評価する。
  旧形式の点数なし `results.jsonl` は比較入力に使えない。

## オフライン検証

```bash
.venv/bin/python -m pytest -q -p no:warnings tests/scripts/test_user_eval.py tests/scripts/test_user_eval_score.py
.venv/bin/ruff check scripts/user_eval tests/scripts/test_user_eval*.py
.venv/bin/mypy --explicit-package-bases scripts/user_eval tests/scripts/test_user_eval*.py
.venv/bin/python -m scripts.user_eval.run_mutations
```

変異実行は一時コピーだけを変更する。従来の8チェック常時合格化・DMガード・評価ボタン・待機期限・
判定に加え、記録の除外・形式検証・点数配分・partial・重大違反・未回答・前回差・平均・欠測を壊す。
KILLED は pytest の assertion が失敗して検出した意味。収集エラーは成功に数えない。
完全な失敗出力を `results/mutations.txt` に保存する。

従来の `core.Runner` と `fakes.py` はオフライン契約テスト用として残している。
`python -m scripts.user_eval --fake --output scripts/user_eval/results/demo` で偽物デモを生成できる。
本番の送受信は Claude の道具で行い、Python 用 Slack アダプターやトークンは用意しない。

## 発話にツールや探す場所のヒントを入れない

「社内の Slack で」「Drive を検索して」のように、使うツールや探す場所を発話に書かない。
社員が普段言う言い方で聞き、Aico が自分で正しいツールを選べるかを見る（2026-10-15 の指摘）。
欲しい物（提案書 PDF・資料）を言うのは普段の依頼なのでよい。
