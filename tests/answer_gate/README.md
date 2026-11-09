# 固定データの答え試験 v1

16問の架空fixtureを、既存の search / slack_search / calendar_freebusy /
mail_summary / proposal_draft の実装で処理する。計算専用スキルは存在しないため、
計算問のみ既存 BedrockClient.converse を直接呼び、scorer の Decimal で独立計算する。
道具の選択は問で固定しており、OpenClawによる道具選択の試験は含まない。

リポジトリ直下、または候補MCPイメージの `/app` で実行する。

```sh
python -m teamagent.eval.answer_gate --cases src/teamagent/eval/answer_gate/data/cases_v1.yaml --out /tmp/answer-gate.jsonl
```

既定は本物のLLM。BedrockClient.from_env と、search既存の要約・検索語展開の
クライアント生成を使用し、候補イメージのモデル設定を引き継ぐ。
業務の取得adapterと埋め込みだけをfixtureへ差し替える。
ローカルでは必ず `--fake-llm` を指定する。

```sh
python -m teamagent.eval.answer_gate --cases src/teamagent/eval/answer_gate/data/cases_v1.yaml --out /tmp/answer-gate.jsonl --fake-llm
uv run --extra dev --extra mcp --extra media pytest -q tests/answer_gate
```

各問の回答・スキル出力・道具と引数・取得先の試行・LLMモデルと使用量・副作用要求・
経過時間・全チェックの理由をJSONLへ保存する。criticalの失敗、未完走、fixture欠損で
exit 1。各問は子プロセスで処理し、120秒の期限で停止する。失敗しても残りの問を記録する。
実行器は道具を問の定義から選ぶので、外側のルーティング品質は測らない。

送信・直接投稿・進捗通知・DB接続はローカルsinkで捕捉し、要求があれば不合格にする。
モデル呼出し中以外のsocket接続も禁止する。提案本文だけの試験なのでHTML公開は無効化。
公開フラグを無視した要求はsinkで捕捉する。実行環境にpytestは不要。

`many-v1` は金庫5・Slack17・事例/案件lane3の計25候補で、適格な一意項目は5。
同じ事例を複数laneから取得する。`two-v1` と `zero-v1` は不足と0件、全角件数も問に含む。
Slack専用スキルはリンクのホスト形式を検査するため、このfixtureだけ
架空の `fixture-answer-gate.slack.com` を使う。リンク先へは接続しない。
その他の出典は `example.test`。氏名・実メール・実DM・実在の社内URLは含めない。

件数は既知の項目名と同じ一覧行／カード内のURLを対応付け、重複を不合格にする。
末尾の検索先サマリー・参考リンク欄は回答項目数に足さない。同じURLの別事例は
item_keyで区別する。表示形式や聞き返しの未知の言い換え、要約の意味の正しさは
この決定的検査だけでは保証しない。形式を増やす際はparserと負例を一緒に更新する。

今回は実読取8問、OpenClaw外側ループ、署名付き証跡、対照イメージとの比較、
費用上限、terraform_runtime_guard／便のapply接続、PPTX/PDF生成・実送信を含まない。
