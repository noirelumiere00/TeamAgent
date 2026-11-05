あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
背景: 全 DM 判定で「長い作業の完了・失敗が届かない／状態を偽る」が 31 件。長いジョブ（提案書・お土産資料・TikTok 取得・動画分析・X 調査・Web 調査・ingest など detached/long job の全種類）で、完了・失敗・時間切れ・再起動での中断のどれでも、依頼した本人へ必ず 1 回だけ知らせが届くようにしたい。既存の仕組み: src/teamagent/mcp_gateway/async_job_notify.py・detached_jobs.py・ProposalJobStore・長いジョブの通知（#548 P4）。
やること:
1. ジョブ種別ごとに、終わり方（done/failed/incomplete/timeout/stale＝heartbeat 切れ/再起動で孤立）× 知らせが届く経路の表を作り、届かない組み合わせをファイル:行で特定する。
2. 届かない組み合わせを、既存の通知の流れに乗せて直す（新しい仕組みは作らない）。heartbeat が切れて孤立したジョブを「失敗」として本人に知らせる掃除が無ければ、既存の stale 判定（PROPOSAL_JOB_STALE_SECONDS 等）を使って足す。二重送信しない（dedup lock を使う）。
3. 「状態を偽る」対策: 照会（status）で、実際に動いていないジョブを「作成中」と答えない。
4. テスト: 各組み合わせのフェイクで「本人に 1 回だけ届く」こと。`uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/mcp_gateway tests/skills tests/adapters` 相当と ruff・mypy（変更ファイル）。
禁止: git commit・push、AWS の実行、.env・secrets の読み取り。差分は小さく。最後に表・直した点・テスト結果を短く。
