# 追加依頼: clientkarte の元の質問文は、入力の query ではなく署名済みの request_text で受け取る

作業場所: ~/dev/worktrees/teamagent-a3karte（前回の作業の続き・未コミット）

## なぜ
前回、ClientKarteInput に `query` を足した。しかしこれには 2 つ問題がある。
- ツールの入力の形が変わる。#820 で 42 本の入力の形を tests/mcp_gateway/tool_input_contract_mcp224.json に固定している。
- モデルが query を渡さないと、状態の行が出ない。

元の質問文を、モデルを通さずに渡す仕組みがすでにある。
- infra/openclaw/caller-identity-plugin/dist/index.js の `mintCallerClaim`（6150 行付近）: proposal_builder / proposal_builder_submit のときだけ `request_text`（利用者の本文）を署名つきの _user_context に入れている。
- src/teamagent/mcp_gateway/server.py の 700 行付近: 同じ 2 本のツールで、DM（channel_id が D 始まり）かつ 4000 字以下のときだけ、`request_metadata["request_text"]` に入れている。
- スキルは `ctx.metadata.get("request_text", "")` で読む（proposal_builder/skill.py 889 行）。

## 直すこと
1. ClientKarteInput から `query` を外す（schema を origin/dev と同じに戻す）。
2. skill.py は `ctx.metadata.get("request_text", "")` を asks_status に渡す。
3. server.py の対象ツールに `clientkarte` を足す（DM 限定・4000 字以下の条件は同じ）。
4. プラグインの mintCallerClaim の条件にも `clientkarte` を足す。dist を直接直す（src は無い）。infra/openclaw/caller-identity-plugin/test/ に、clientkarte で request_text が入ることと、ほかのツール（search など）では入らないことのテストを足す。
5. 前回のテスト (a)〜(d) は、request_text を metadata で渡す形に直す。

## 合格条件（全部緑）
uv run --extra dev --extra mcp pytest -q tests/skills/clientkarte tests/mcp_gateway
（plugin の node テスト: infra/openclaw/caller-identity-plugin で package.json の test スクリプト）
uv run --extra dev --extra mcp ruff check src/teamagent/skills/clientkarte src/teamagent/mcp_gateway tests/skills/clientkarte
uv run --extra dev --extra mcp ruff format --check src/teamagent/skills/clientkarte src/teamagent/mcp_gateway tests/skills/clientkarte
uv run --extra dev --extra mcp mypy src/teamagent/skills/clientkarte src/teamagent/mcp_gateway

## やらないこと
- git commit・push、AWS 呼び出し、.env・secrets の読み取り。ほかのツールへの request_text の拡大。
