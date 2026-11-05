あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
背景: 社内 Slack AI（OpenClaw 外側ループ・Haiku 4.5・context 200k）で「会話上限で止まる・一通ずれ・混線」が全 DM 判定の 33 件。9 月の調査で、1 回の呼び出しの固定部分（SOUL.md などの bootstrap・ツール一覧の JSON schema・plugin が足す文）が約 127k トークンあり、それが真因と判明（memory: project_oc_dm_context_overflow）。
やること:
1. 今の固定部分を測る: infra/openclaw/SOUL.md・HEARTBEAT.md・IDENTITY.md の文字数、MCP が公開するツールの説明と inputSchema（src/teamagent/orchestrator/factory.py と各 skill の schema）を本番 ON のツール（環境変数 USE_* が 1/true のもの。一覧は ~/Documents/Claude/Artifacts/tfvars-rebuild-20261030/live_config.json の teamagent-dev-mcp の env）で組み立て、トークン概算（文字数÷3.5 程度でよい）を種類別に出す。
2. 削れるものを、効果の大きい順に提案し、安全なものから実装する: ツール説明の重複・長すぎる description・巨大な enum/例・使われていない引数・SOUL の重複規則。挙動（どのツールを選ぶか）を変えないことを優先し、変える恐れのある削減は提案だけにする。
3. 既存の契約テスト（SOUL の字数上限・ツール一覧の契約・effective-tool-scope.json など）が通ること。削減量を before/after で測るテストかスクリプトを 1 つ足す。
4. `uv run --extra dev --extra mcp pytest -q -n auto tests/orchestrator tests/mcp_gateway tests/infra tests/openclaw` 相当と ruff・mypy（変更ファイル）。
禁止: git commit・push、AWS・Bedrock の実呼び出し、.env・secrets の読み取り。最後に before/after のトークン概算・変更点・挙動への影響の見立てを短くまとめる。
