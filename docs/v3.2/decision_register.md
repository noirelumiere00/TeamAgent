# 意思決定レジストリ（12論点・暫定決定の管理台帳）

> 元仕様の「12論点の暫定決定を台帳化し Sprint レトロで更新」（spec_vs_current matrix その他#13）に対応。
> 各決定の **現値 / 根拠 / 決定日 / 状態** を一覧化。状態が変わったら本表を更新する（レトロ cycle）。

最終更新: 2026-06-16

| # | 論点 | 現決定 | 根拠 | 決定日 | 状態 |
|---|---|---|---|---|---|
| 1 | 実行基盤 | **OpenClaw(Fargate) 外殻 + teamagent-mcp 金庫 + worker EC2**（Lambda/SQS/API GW サーバーレス疎結合は不採用） | 6/12 本番 E2E 実返信 3.3s で go-live（mossy §S3） | 2026-06-12 | 確定・稼働中 |
| 2 | Embedding | **LocalE5（multilingual-e5-large・1024次元）継続**（Titan v2/Cohere は不採用） | e5 で本番要件充足・Embedder Protocol で差替口は確保 | 2026-06 | 確定（将来差替可） |
| 3 | Rerank | **Cohere Rerank v3.5（Bedrock 東京）採用** | gold set top-1 20%→52%→64% を実証 | 2026-05 | 確定・稼働中 |
| 4 | RLS / アクセスモデル | **会社共有モデル（acl_groups intersect・FORCE RLS）**＋per-user OAuth（mail/workspace） | RLS 28 tests live PASS（pilot_gate_status） | 2026-06 | 確定・稼働中 |
| 5 | Scrape 実行場所 | **MCP 金庫内（Puppeteer/yt-dlp/ffmpeg）・OpenClaw は native exec 不使用** | §L・url_guard SSRF 防御・attack_mcp 回帰 | 2026-06 | 確定 |
| 6 | ClawHub（外部プラグイン） | **禁止**（公式プラグイン digest pin のみ） | ClawHavoc 供給網リスク回避（security_audit §4） | 2026-06 | 確定 |
| 7 | OAuth クライアント | 連携(web)用と共有(desktop)用を **分離**（`CONNECT_GOOGLE_CLIENT_*`） | main ワークストリーム commit e7ca482 | 2026-06 | 確定 |
| 8 | LLM 2段運用 | **外側=Haiku 4.5（速い/安い）／重い L2 合成=Sonnet 4.6** | openclaw.config.json5 models・§T1 | 2026-06 | 確定・稼働中 |
| 9 | 検索 latency SLO | **中量 p95 ≤ 15s**（当初「3秒」から現実化） | `docs/v3.2/slo_v1.md` §2・実機 10.6s 観測 | 2026-06-15 | 確定（パイロット実測で再確認） |
| 10 | proposal_deck 露出 | **既定 OFF（`USE_PROPOSAL_DECK_TOOLS`）＋OpenClaw exclude** | FMT テンプレ未 provision・write 階層（commit 49a5a04） | 2026-06-16 | 暫定（P2 で本番化判断） |
| 11 | メール機能 | **dev に合流済だが全 `USE_MAIL_*` 既定 OFF＋OpenClaw `mail_*` exclude** | dev↔main union（merge 9d776b5）・本番挙動不変 | 2026-06-16 | 暫定（10名展開は人手ゲート） |
| 12 | ログ形式 | **structlog JSON（`STRUCTLOG_FORMAT=json`）**＝CloudWatch metric filter バインド | mcp:6 で JSON 化稼働（commit 40afded） | 2026-06-16 | 確定・稼働中 |

## 更新 cycle
- Sprint レトロ / 大きな方式変更時に本表を更新。
- 「暫定」項目（10/11）は P2/パイロット後に再判定。
- 関連: `spec_vs_current_full_matrix_2026-06-15.md`・`ops/risk_register.md`・`~/.claude/plans/agent-snuggly-pike.md`。
