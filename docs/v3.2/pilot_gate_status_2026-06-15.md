# P1 パイロットゲート 実走検証レポート（2026-06-15）

> `docs/openclaw/golive_checklist.md` のゲートのうち、**機械検証できるもの**を live 環境
> （`teamagent-dev-mcp:5` / `teamagent-dev-openclaw:10` / RDS `teamagent-dev`）に対して実走した結果。
> 判定凡例: ✅ GO（機械検証で確認） / 🟡 要人手（人間/operator が実行） / ⏸ 保留（要追加データ）。

実行者: Claude（agent-snuggly-pike プラン Part B）／ SSM 踏み台トンネル + CloudWatch Logs Insights + DB-gated pytest。

---

## サマリ

| 区分 | 結論 |
|---|---|
| **RLS / データ隔離（#13・#16 の核）** | ✅ live RDS で DB-gated テスト 28 件全 PASS。本人行のみ可視・他人 read/update 不可・GUC未設定/空で fail-safe・documents/chunks の acl_groups intersect を実証。 |
| **検索レイテンシ（#18 の一部 / Wave1-①）** | 🟡 観測サンプルでは ~10.6s < 15s SLO だが **n=3（go-live/手動テストのみ）** で統計的 p95 ではない。パイロット相当の連続トラフィックで再測が必要。 |
| **MCP HTTP smoke / 敵対ハーネス（#15・#16 の HTTP 経路）** | 🟡 live MCP(8787) は SG ingress=OpenClaw SG のみ（設計通りの最小権限）で外部から到達不可。go-live(2026-06-12) で通過済。再走は VPC 内 operator が実行。 |
| **2人混線・1週間パイロット（#17・#18）** | 🟡 人間2名の Slack 実機操作が必須。未実施。 |
| **観測性ギャップ（新規発見）** | ⚠️ `usage_events` は旧 slack_bot 専用で本番 OpenClaw→MCP 経路を記録しない（0件）。本番 SLI は CloudWatch Logs に依存。改善候補。 |

---

## ゲート別詳細

### #13 RLS 実走検証 — ✅ GO
live RDS に対し DB-gated テスト 28 件全 PASS（合成メールは finally で cleanup・残存 0 確認）。
- `test_db_oauth_tokens_rls_blocks_other_users`: GUC 未設定→0件 / 本人(alice)→自分の行のみ / 他人(bob)行は read 0・update 0行 / 空 GUC→0件。**RLS の本人隔離が live で機能**。
- `test_db_rls_enforces_acl`: documents/chunks の `acl_groups` intersect で会社ドメイン可視・対象外不可視。
- `test_pgvector_client_rejects_invalid_app_role`: 不正 app_role を拒否。
- データ実在: `documents` 794件（gdrive 589 / slack 205）＝実ナレッジベースに対する検証。

### #16 敵対ハーネス — 🟡 一部GO（DB層）/ HTTP層は要operator
- **DB 層（outsider 漏れの本質＝RLS）は #13 で実証済**。
- `scripts/attack_mcp.py` の MCP-HTTP 経路は SG（OpenClaw SG 限定）で到達不可。go-live で通過済・再走は operator。

### #15 smoke（MCP tools/search）— 🟡 要operator
- live MCP は SG で外部不可達。go-live(2026-06-12 本番E2E実返信成功)で通過済。
- 注記: チェックリストの「tools=4」期待は **古い**。Wave1-② で `operation_log` を追加したため knowledge 系は増えている（scrape/mail/proposal_deck が出ないこと、が正しい期待）。

### #18 検索 p95 ≤15s — 🟡 観測GO・統計的には要トラフィック
CloudWatch Logs `/teamagent/dev/teamagent-mcp`（過去14日）から実測:

| event | n | p50 | p95 | max | 備考 |
|---|---|---|---|---|---|
| `bedrock_converse`（検索L2合成） | 3 | 10.4s | 10.6s | 10.6s | **cache_read_input_tokens=0（全件）** |
| `embedder_embed` | 3 | 0.5s | 0.5s | 0.5s | LocalE5 |
| `gemini_analyze_video`（重量） | 1 | 18.0s | — | 18.0s | 重量 SLO ≤5min 内 |

- 検索 end-to-end ≈ embedder(0.5s)+pgvector+rerank+L2合成(10.4–10.6s) ＝ **観測上 ~11s < 15s SLO**。
- ⚠️ **prompt caching 不発**: 全 `bedrock_converse` で `cache_read=0`。go-live テストが疎ら（>5分間隔）で cachePoint TTL 切れ。低トラフィックでは Wave1-① で期待した cache コスト削減が出ない＝**パイロットの連続トラフィック下で再評価**すべき。
- n=3 は go-live/手動テストのみ。**真の p95 はパイロット稼働後**に同クエリで再測（合否はその時確定）。

### #17 2人同時混線テスト — 🟡 要人手
専用 ch で2名同時質問→各スレッドに各人の回答（混線なし）を人間が確認。未実施。

### #1 ゲート①承認 — 🟡 要人手
OpenClaw(Node) 本番持込の組織承認署名。go-live 実機検証は完了（memory）だが最終署名は人間タスク。

---

## 新規発見・改善候補

1. **観測性ギャップ（→ readout で一次対応済・2026-06-16）**: `usage_events` テーブルは旧 `slack_bot.handle_app_mention` のみが書き込み、本番経路（Slack→OpenClaw→MCP）は書かない。よって本番 SLI（p95/エラー率/コスト）の一次ソースは現状 CloudWatch Logs の構造化ログのみ。`slo_v1.md` §5 の「未実装: 月次可用性集計」と整合。
   - **対応（実装済）**: `scripts/pilot_health.py` を新設。CloudWatch Logs `/teamagent/dev/teamagent-mcp` から検索 p95 / エラー率 / 1検索コスト p50 / cache_hit 比を集計し SLO（p95≤15s・error≤1%・cost p50≤$0.02）と照合して GO/NO-GO を出す。**パイロット中は毎日 `python scripts/pilot_health.py --hours 24` を実行**して #18 の日次ゲートを読む。実走確認済（過去14日: bedrock_converse p95=10.6s / cost_p50=$0.011 / cache_hit 0% を再現・OVERALL GO）。
   - 恒久対応案（別タスク）: MCP server 側に usage 記録を足す or 本 readout を日次 cron 化してダッシュボード/Slack 通知に。
2. **CloudWatch アラーム空振り（→ 実装済・2026-06-16 検証で2経路に切り分け）**: 既存 metric filter は全て JSON セレクタ（`{ $.cost_usd = * }` 等）だが、`structlog.configure()` が未実装で全サービスが **console 形式**ログを出していたためバインドせず、アラームが発火していなかった。一次確認で2経路に切り分け:
   - **MCP 経路（本番 Slack→OpenClaw→MCP・Python・`/teamagent/dev/teamagent-mcp`＝1.46MB 生存）**: **実装済**＝`observability/logging_config.py` ＋ entrypoint で `configure_logging()` ＋ `fargate.tf`/手動 register で `STRUCTLOG_FORMAT=json`（commit `40afded`）。**まとめてデプロイ（`bundled_deploy_2026-06-16.md`）で MCP 再ビルド+ECS roll すれば MCP アラーム（McpCostUSD/McpToolError/McpIdentitySpoofRejected）が発火する。**
   - **app 経路（worker EC2 旧 slack_bot・`/teamagent/dev`）**: **0 bytes＝ログが CloudWatch に未到達**（EC2 に CloudWatch agent 無し）。`aws_cloudwatch_log_group.app` は `lambda_iam.tf` に実在（「未定義」説は誤り）。よって app 系アラーム（BedrockCostUSD/SkillLatencyMs/ErrorCount）は「形式」以前に「未到達」で死＝**EC2 log 送出の導入が必要な別タスク**（本番経路ではないため優先度低）。
3. **prompt caching が低トラフィックで不発**: cachePoint TTL（~5分）より検索間隔が長いと cache_read=0。パイロットで連続利用されれば改善する見込みだが、SLO の「1検索コスト ≤$0.02」はキャッシュ前提なので、パイロット中に `pilot_health.py` の cache_hit 比で妥当性を確認。

---

## パイロット開始前に残る人手ゲート（このレポートでクローズできないもの）
- [ ] #1 ゲート①承認の最終署名
- [ ] #15 smoke / #16 attack_mcp の HTTP 経路を VPC 内 operator が再走（go-live 済だが現イメージ mcp:5/openclaw:10 で念のため）
- [ ] #17 2人混線テスト（人間2名・Slack 実機）
- [ ] #18 1週間パイロット観測（p95/エラー/コスト/RLS越権0 を毎日確認）
- [ ] `OPS_SLACK_WEBHOOK` 実値投入（未投入なら ingest 失敗は journalctl 手検知）

## 機械検証でクローズしたもの
- [x] #13 RLS データ隔離（live RDS・28 tests PASS）
- [x] #16 の核（RLS outsider 漏れ防止・データ層）
- [x] #18 検索レイテンシの観測（~10.6s < 15s・ただし要パイロット再測）
- [x] Wave2-⑤ `connector_state` テーブルの本番存在確認（0行・pipeline 未cursor化は設計通り）
