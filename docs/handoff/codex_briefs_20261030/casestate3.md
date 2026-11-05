あなたは teamagent リポジトリの開発者。作業場所はこの worktree（PR2 #752 のブランチの上に積んだ feat/case-state-pr3-20261030）。日本語で報告する。
docs/case_state_plan_20261030.md と docs/case_state_pr2_20261030.md（PR3 への引き継ぎ）に従い、PR3「正本から描く」を実装する: 案件の状態・日付・出典を、全回答面（通常検索・fanout・ライブ Slack・直接回答・事例の答え）でコードの固定描画にし、モデルが状態を言い換え・再作文して「提案中」を「受注・実績」にしないようにする。
- PR2 のフラグ CASE_STATE_EVIDENCE_ALLOWED_EMAILS（空＝誰も）の内側だけで動かし、OFF の挙動は変えない。
- 回答の最終文に状態の語（受注・実績・完了・失注）が、根拠束に無いのに現れたら除去／「不明」に置換するコードの検査を入れる（指示文だけに頼らない＝#601→#603 の教訓）。
- テストは先に失敗するもの（モデルが「受注」と書き足した答えが出てしまう再現）から。`uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/skills/search tests/ingest tests/mcp_gateway` と ruff・mypy（変更ファイル）を通す。
禁止: git commit・push、AWS・DB の実接続、.env・secrets の読み取り。顧客名を書かない。差分は小さく。最後に変更点（ファイル:行）・テスト結果・残る懸念を短くまとめる。
