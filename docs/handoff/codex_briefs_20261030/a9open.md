あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
目的（計画 A9・小俣さんの決定「基本的には開いて大丈夫・全員に」）: 本番で OFF のツールのうち次の 6 本を全員に開ける。
  tiktok_comment_mining（USE_TIKTOK_COMMENT_TOOLS）・operation_log（USE_OPERATION_LOG_TOOLS）・mail_to_internal_context（USE_MAIL_LINK_TOOL）・workspace_search（USE_WORKSPACE_TOOLS）・recommend（USE_RECOMMEND_SKILL）・knowledge_search_url（USE_KNOWLEDGE_SEARCH_URL_TOOL）
開けないもの（理由つきで残す）: proposal_campaign・proposal_deck（資料生成は休止中）・morning_digest・digest_ack（別 AI の担当）・video_approval（動画チェックツールと重複）・mail_constraints（同意の設計が先）。
監査表: docs/audits/2026-10-30-production-capabilities.md。SOUL と factory の食い違いを検出するテスト: tests/infra/test_soul_capabilities.py（固定 env のフィクスチャ tests/infra/fixtures/）。
やること:
1. 6 本それぞれについて、開ける前に確かめること（必要な連携・費用の上限・外部 API・既知の不具合・テストの有無）をコードから確認し、開けると危ないものがあれば理由を書いて外す（無理に開けない）。
2. infra/openclaw/SOUL.md の「準備中」一覧から開ける分を外し、振り分け（どんな依頼でどのツールを呼ぶか）を 1〜2 行ずつ足す。字数上限（契約テスト）に収める。
3. フィクスチャの env を「開けた後の本番」にそろえ、test_soul_capabilities を通す。
4. 本番で変える env の一覧（KEY=VALUE）を docs/audits/2026-11-02-open-off-tools.md にまとめる（MCP の env と、OC 側に要る設定があればそれも）。env の変更自体は Claude が便で行うので、ここではコードと文書だけ。
5. `uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/infra tests/skills tests/orchestrator` と plugin の node テスト・ruff。
禁止: git commit・push、AWS の実行、.env・secrets の読み取り。差分は小さく。最後に、開けた／外したツールと理由・env の一覧・テスト結果を短く。
