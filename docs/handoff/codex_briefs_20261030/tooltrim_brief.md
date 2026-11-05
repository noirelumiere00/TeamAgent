# 依頼: MCP のツール定義を短くして、会話の固定部分を減らす（A4・挙動は変えない）

作業場所: ~/dev/worktrees/teamagent-tooltrim（branch fix/tool-defs-trim-20261104・origin/dev c6b73b33 起点）

## 背景（11-04 の実測）
- Aico（OpenClaw・Haiku 4.5・上限 200k）は 1 回の呼び出しごとに固定部分を送っている。Bedrock の CacheRead の最大は 10/28 に 107k、11/04 に 115k と増えている。会話上限・一通ずれ・混線（全 DM 判定で 59 件）の根本原因。
- 11-04 に本番と同じフラグ（scripts/routing_eval/flags_mcp224.json＝本番 TD rev224 の USE_*）で tools/list を書き出し、Bedrock CountTokens で数えた結果:
  - ツール定義 42 本 = **86,985 tokens**（JSON 56,273 字・1 字あたり約 1.55 tokens。日本語と JSON の記号が重い）
  - SOUL.md = 21,535 tokens
  - 10/30 の作業（文字数÷3.5 の見積り・22k）は実測と大きくずれていたので、その数字は使わないこと。
- ツールごとの大きさ（description 字数・inputSchema 字数）: ~/Documents/Claude/Artifacts/handoff-20261030/codex/tooltrim_sizes.txt。上位は proposal_builder_submit（schema 6,642 字）・digest_settings（3,233）・omiyage_report_submit・search・mail_reply・search_surface_check・knowledge_deliver・mail_draft・video_capture・slack_summary・tiktok_acquire。
- 書き出しは ~/Documents/Claude/Artifacts/handoff-20261030/codex/dump_tools_ctxtrim.py を使う:
  `uv run --extra dev --extra mcp python <dump_tools_ctxtrim.py> --src src --flags scripts/routing_eval/flags_mcp224.json --out /tmp/tools_after.json`

## やること
1. **ツール定義の JSON（description＋inputSchema）を合計 56,273 字から 36,000 字以下にする**（約 -36%・約 3 万トークン減の見込み）。上位 15 本から順に。
   - inputSchema の各引数の description は、モデルが値を選ぶのに要る最小限（1 文）にする。例・経緯・社内の事情・同じ説明の繰り返しは削る。
   - ツールの description に書いてあることを引数の description で繰り返さない。逆も同じ。
   - 大きな enum・nested object・$defs の重複は、型と制約を保ったまま短くできるなら短くする（pydantic の title や不要な default の露出など）。
   - SOUL.md に既にある利用者向けの規則（言い回し・案内文）をツール定義から削る。
2. **ツール名・引数名・型・required・enum の値は変えない**（呼び出しの互換）。どのツールを選ぶかの手がかりになる語（「一覧提示」など、tests/skills/test_routing_descriptions_catalog.py などの routing 系テストが求める語）は残す。
3. before/after の字数を出すテストかスクリプトを 1 つ（10/30 の measure_context の型でよい。ただし「÷3.5 でトークン」は書かない。字数だけ）。合計 36,000 字以下を assert する契約テストを足すと、今後ツールを足したときに増えたことに気づける。
4. 次が全部緑:
   uv run --extra dev --extra mcp pytest -q -n auto tests/skills tests/mcp_gateway tests/orchestrator tests/infra
   uv run --extra dev --extra mcp ruff check src tests
   uv run --extra dev --extra mcp ruff format --check src tests
   uv run --extra dev --extra mcp mypy src/teamagent/skills src/teamagent/mcp_gateway

## やらないこと
- ツールを外す・フラグを変える（それは小俣さんの判断）。SOUL.md の変更。DB・取込・LLM 呼び出しの変更。
- git commit・push、AWS・Bedrock の実呼び出し、.env・secrets の読み取り。

最後に: 合計字数の before/after、ツールごとの上位 15 本の before/after、挙動に影響しうる変更（あれば）を短く。
