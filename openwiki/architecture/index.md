# ファイル

- [呼び出し元の証明とボタン束縛](caller-identity-and-button-bindings.md) - OpenClaw の caller-identity plugin が Slack の送信者を署名付き claim にして MCP へ渡し、mcp_gateway の caller_claim が検証する仕組み。朝ダイジェストのボタンを ACTION_BINDINGS で 1 ツールへ束縛し、AI を通さず直接実行する流れも扱う。
- [長時間ジョブの切り離しと完了通知](detached-jobs-and-async-notify.md) - OpenClaw の打ち切り時間を超える処理を MCP gateway が daemon thread に切り離し、完了・進捗・中断を Slack へ直接投稿する仕組み。detached_jobs・surface_video_followup・async_job_notify・progress_notify・direct_summary・payload_offload の役割と env フラグ。
- [本人メモ（Hermes 覚える係）](hermes-personal-memory.md) - 1 対 1 DM の発話から「本人に合う返事のためのメモ」を学習する仕組み。MCP 側の personal_memory（gate・buffer・learner・service・guard）、専用ロールと RLS の 3 表、TLS 必須の Hermes 学習サービス（hermes_runtime）の役割と安全策。
- [3層分離と Skill の契約](layering-and-skill-contract.md) - src/teamagent の skills / adapters / runtime（と orchestrator・mcp_gateway・ingest）の依存方向を import-linter がどう強制しているか、BaseSkill・SkillRegistry・SkillContext・Pydantic I/O の契約、prompt ファイルの読み込み、structlog の JSON 出力。
- [MCP gateway（ツール実行サーバ）](mcp-gateway.md) - OpenClaw から streamable-http で呼ばれる TeamAgent MCP サーバの起動条件と、1 回のツール呼び出しを caller claim 検証・Slack 本人解決・RLS メタデータ組み立て・入力検証・skill 実行・usage 記録・返却前処理へ流す dispatch_tool の流れ。
- [OpenClaw ゲートウェイ（Slack 受け口）](openclaw-gateway.md) - Aico の Slack 受け口である OpenClaw の設定（Socket Mode・dmPolicy/allowFrom・Haiku モデル・native ツールの封鎖・MCP 接続と toolFilter）、起動時の entrypoint 検査、SOUL/IDENTITY の seed、CI の不変条件チェックと effective-tool-scope。
- [オーケストレータ（bounded tool loop）](orchestrator.md) - anthropic の AsyncAnthropicBedrock で既存 Skill をツールとして回す自前の上限付き tool loop（sdk_runner.run_sdk_agent）と、それを 1 ツールとして公開する run_agent（USE_AGENT_ORCHESTRATOR）、ToolSpec・decider/loop・忠実性チェック・評価の役割と現在の公開状態。
- [全体構成](overview.md) - Aico（TeamAgent）の実行時構成。Slack → OpenClaw（受け口・Haiku）→ MCP gateway（Skill 実体・秘密を持つ側）→ skills → adapters → AWS / Google / Slack の流れと、ECS サービス・スケジュールタスク・使い捨て Fargate・Lambda・Hermes・停止中の旧 EC2 worker の責任範囲。
- [ツール登録と機能フラグ](tool-registry-and-feature-flags.md) - MCP のツール群を決める factory.build_production_tools の USE_* env フラグ（既定 OFF）、検索ノブの一元解決、OpenClaw toolFilter と effective-tool-scope、terraform の MCP task env への配線までの「4 段ゲート」と、フラグを増やす・変えるときの注意。
