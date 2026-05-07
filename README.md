# TeamAgent v2.0 — 構想・要件・アーキテクチャ ドキュメント集

営業16名 + マネージャー向け Slack AI エージェント「TeamAgent」の v2.0 ドキュメント一式。

## ドキュメント一覧

| 文書 | ファイル | 役割 | 主な読者 |
|---|---|---|---|
| 構想・概要書 | [`teamagent_overview_v2.0.html`](teamagent_overview_v2.0.html) | 5機能 / 5+1データ層 / 4共通基盤の俯瞰 | 営業・推進担当 |
| 実装計画書 | [`teamagent_implementation_plan_v2.0.html`](teamagent_implementation_plan_v2.0.html) | 6ヶ月Phase別コスト + スケジュール + 月次ダッシュボード | FDE |
| 要件定義書 | [`teamagent_requirements_v2.0.html`](teamagent_requirements_v2.0.html) | FR 42件 + NFR 25件 + その他37件 = 計104件 | FDE・QA |
| アーキテクチャ図書 | [`teamagent_architecture_v2.0.html`](teamagent_architecture_v2.0.html) | C4 model 12章 + 21図解 | FDE・実装担当 |

## 5機能（Phase 順）

```
Phase 1：機能④ 動画 URL 分析（1ヶ月）
Phase 2：機能② Mail ワークフロー（1.5ヶ月）
Phase 2.5：機能⑤ 営業進捗サマリー（0.5ヶ月、② に内包）
Phase 3：機能③ 提案コンテンツ生成（2ヶ月）
Phase 4：機能① Slack 自動サジェスト（1ヶ月）
合計：約 6ヶ月
```

## 主要数値（v2.0・あくまで設計時点の見積もり）

- **開発期間**：6ヶ月想定
- **コスト試算**：開発期間 ¥473K〜¥634K / 本番月次 ¥95K〜¥106K（実測ではなく Claude による試算なので参考値）
- **AWS 構成**：db.r7g.large + Lambda + EventBridge + Fargate + CloudWatch

## 技術スタック

- **共通基盤**：Claude Agent SDK / Gmail OAuth / Slack Bolt / pgvector
- **データ層**：Gmail / Slack / Google Drive / 動画分析（VSEO + dpro + Buzzmiru）/ Gemini Deep Research / X
- **インフラ**：AWS（Lambda / Fargate / RDS PostgreSQL pgvector / EventBridge / Secrets Manager / CloudWatch）

## 履歴

- v1.0（2026-04-27）：初版
- v1.1〜1.4（2026-04-28）：図解強化、Mail Agent 章、Gemini DR 移行方針
- v1.5（2026-05-01）：ナレッジソース統合
- **v2.0（2026-05-07）：5機能 / 5データ層 / 4共通基盤 に再構成。Slack UI モック・フロー図・月次ダッシュボード追加。要件定義書・アーキテクチャ図書を別冊化。**

## 注意事項

- 本書のコスト試算・効果試算は Claude による参考見積もりであり、実測値ではない
- 顧客名・案件名は ACME / Beta社 / Gamma健食 等のダミー名

---

最終更新：2026年5月7日
