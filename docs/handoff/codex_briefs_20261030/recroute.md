あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
本番の実機確認（11/02）: DM で「伊藤ハムの提案書に似た過去の提案書を探して」と頼むと、外側ループは recommend（類似する提案書・議事録・営業 FB の推薦・A9 で開けたばかり）ではなく proposal_draft（提案の骨子づくり）を呼び、骨子を返した。期待は「似た過去の提案書の一覧（資料名・時期・リンク）」。
やること:
1. infra/openclaw/SOUL.md の振り分けと、recommend・proposal_draft の各ツールの description（src/teamagent/skills/recommend・proposal）を見直し、「似た・類似の・近い・参考になる 過去の提案書／資料を探して」は recommend、「提案（の骨子・たたき台）を作って」は proposal_draft、と明確に分ける。どちらとも取れるときは recommend を優先して資料を見せ、骨子は求められてから。
2. SOUL の字数上限（契約テスト）に収める。振り分けの例文の固定テスト（既存の routing のテスト tests/skills/test_routing_descriptions_catalog.py などの流儀）に、この 2 例を足す。
3. `uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/infra tests/skills tests/scripts` と plugin の node テスト・ruff。
禁止: git commit・push、AWS の実行、.env・secrets の読み取り。差分は小さく。最後に変更点・テスト結果を短く。
