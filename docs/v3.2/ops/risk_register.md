# リスク台帳（R16-R19・Sprint レトロ更新）

> spec_vs_current matrix その他#14「R16-R19 のリスク台帳化と Sprint レトロ更新」に対応。
> 各リスクの **status / mitigation / owner** を記載。レトロ時に status を更新する。

最終更新: 2026-07-10（R16: trivy CI 必須化を反映）

| ID | リスク | status | mitigation（実装/運用） | owner |
|---|---|---|---|---|
| **R16** | 依存 CVE（OpenClaw/MCP SDK/OS）の見落とし | 緩和中 | OpenClaw base を digest pin（2026.6.5）・MCP SDK CVE-2026-52869/52870 対応で 1.27.2 固定・GHSA 手動確認（2026-06-09 実施）・bandit/gitleaks CI 常設。**trivy fs（uv.lock 依存 CVE）＋ config（IaC misconfig）を CI 必須化（2026-07-10・CRITICAL/HIGH で fail・例外は `.trivyignore` に理由/期限付き）**。残: image スキャン（ECR scan_on_push の gate 接続 or CodeBuild post_build）は別チケット | 小俣 / DevOps |
| **R17** | ClawHub 等 外部プラグイン供給網汚染（ClawHavoc） | 回避済 | 外部 Skill マーケットを**使わない方針**＝公式プラグインのみ digest pin。`tools.exec:deny`/`fs.workspaceOnly`/toolFilter で攻撃面縮小 | OpenClaw maintainer |
| **R18** | 露出トークン（Slack bot/app・Google・MCP bearer 等）の失効/漏洩 | 緩和中 | `docs/v3.2/ops/secrets_rotation_policy.md` に 9 secret の rotation 手順・gate チェックリスト整備。**Slack 3本の実 rotation はパイロット前ゲート（人手・未実行）** | Slack admin / 小俣 |
| **R19** | LLM/インフラ単一障害（Bedrock/RDS/Slack 障害） | 緩和中 | 緊急停止スイッチ（`USE_OPENCLAW_FRONTEND` flag で ~1分ロールバック・desired_count=0 で旧Bot復帰）・MCP rollback=mcp:5。**RDS スナップ復旧/LLM 主副切替の DR 訓練は Sprint14（未実施）**・副経路 runbook 化（監視#26）も未 | 小俣 / DevOps |

## 関連する未クローズ（Batch E）
- トークン rotation 実行（R18）／DR 訓練（R19・監視#14,#29）／CVE 運用工数実測（R16・監視#35）／副経路 Anthropic 直 API runbook（監視#26）。
- いずれも手順は整備済（secrets_rotation_policy / bundled_deploy / deploy_runbook）＝**実行は人手/組織タスク**。

## 更新 cycle
- Sprint レトロで status（回避済/緩和中/未対応）を更新。
- 関連: `decision_register.md`・`security/security_audit_2026-06-12.md`・`pilot_gate_status_2026-06-15.md`。
