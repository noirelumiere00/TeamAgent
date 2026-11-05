あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。

対象: src/teamagent/skills/media_case_search/distill.py（自社メディアの TikTok 投稿 1 本ごとに、キャプション＋サムネ/スライド画像から「ブランド・施策・業種」などを JSON で抜き出す。今は Gemini 3.5 Flash・thinking minimal 固定）と scripts/ingest_media_cases.py。

やりたいこと: 抜き出しのモデルを env で選べるようにし、同じ投稿で比べられるようにする。
1. env `MEDIA_CASE_MODEL` を追加。値は `gemini-3.5-flash`（既定・今と同じ挙動）、`gemini-3.6-flash`、`bedrock-haiku`（Bedrock の jp.anthropic.claude-haiku-4-5-20251001-v1:0。既存の src/teamagent/adapters/bedrock_client.py を使い、画像入力に対応しているか確認。無ければ最小の追加で対応）。費用の見積り・CostGuard の精算・EvidenceMismatch などの検証は、どのモデルでも同じ経路を通す。単価表は既存のものを使う。
2. scripts/ に比較用の小さなスクリプトを足す: 入力は既存の取り込み結果（Apify のデータセット ID か、ローカルの JSONL）から N 本（既定 50）を決まった順で選び、指定した複数モデルで抜き出し、項目ごとの一致率（ブランド・施策名・業種）・失敗数・1 本あたり費用・所要時間を JSON と短い表で出す。DB には書かない（--commit なし）。
3. テスト: env の切り替え（既定が変わらないこと）、Bedrock 経路のフェイク（本番の失敗の仕方＝ThrottlingException・ServiceUnavailable・JSON でない答え を再現）での再試行と失敗の記録、比較スクリプトの集計。`uv run --extra dev --extra mcp pytest -q tests/skills/media_case_search tests/scripts` と ruff・mypy（変更ファイル）を通す。

禁止: git commit・push、AWS・Gemini・Bedrock の実呼び出し、.env・secrets の読み取り。差分は小さく保つ。
最後に、変更点・テスト結果・本番で比較を流すときのコマンド例を短くまとめる。
