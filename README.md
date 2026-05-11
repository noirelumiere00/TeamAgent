# TeamAgent v2.1 — 構想・要件・アーキテクチャ ドキュメント集

営業16名 + マネージャー向け Slack AI エージェント「TeamAgent」のドキュメント一式。

## 最新版（v2.1）ドキュメント一覧

| 文書 | ファイル | 役割 | 主な読者 |
|---|---|---|---|
| 🎯 **上司共有用サマリー** | [`teamagent_summary_v2.1.html`](teamagent_summary_v2.1.html) | 1〜2ページの経営層向けサマリー（機能 + Slack具体例 + 削減効果） | 経営層・上司 |
| ✋ **Phase 0〜E 全工程仕様書** | [`teamagent_phase0E_spec_v1.html`](teamagent_phase0E_spec_v1.html) | 機能③ の Phase 0〜E 詳細仕様（営業確認用・26質問項目つき） | 営業・推進担当 |
| 構想・概要書 | [`teamagent_overview_v2.1.html`](teamagent_overview_v2.1.html) | 5機能 / 6データ層 / 4共通基盤の俯瞰 | 営業・推進担当 |
| 実装計画書 | [`teamagent_implementation_plan_v2.1.html`](teamagent_implementation_plan_v2.1.html) | 8.5ヶ月 17 Sprint 反復型計画 + コスト + 月次ダッシュボード | FDE |
| 要件定義書 | [`teamagent_requirements_v2.1.html`](teamagent_requirements_v2.1.html) | FR 40件 + NFR 25件 + その他37件 = 計102件、Sprint 別 DoD | FDE・QA |
| アーキテクチャ図書 | [`teamagent_architecture_v2.1.html`](teamagent_architecture_v2.1.html) | C4 model 12章 + 14図解、機能① 再構成版 | FDE・実装担当 |

## 履歴版（参考）

| 版 | ファイル | 備考 |
|---|---|---|
| v2.0 | [`teamagent_overview_v2.0.html`](teamagent_overview_v2.0.html) など | IBM レビュー前の初版。v2.1 と並列して参照可能 |
| v1.5 | [`teamagent_overview_v1.5.html`](teamagent_overview_v1.5.html) | 5機能整理前の旧構想 |

## 5機能（Phase 順）

```
Phase 1（M1〜M2）：機能④ 動画 URL 分析（S1+S2+S3 = 1.5ヶ月）
Phase 2（M2〜M4）：機能② Mail ワークフロー（S4+S5+S6+S7 = 2ヶ月）
Phase 2.5（M4 並行）：機能⑤ 営業進捗サマリー（S8 = 0.75ヶ月）
Phase 3（M4〜M7）：機能③ 提案コンテンツ生成（S9〜S14 = 3ヶ月）
Phase 4（M7〜M9）：機能① Slack 自動サジェスト（S15+S16+S17 = 1.5ヶ月）
M9：S-Final（統合検証）
合計：約 8.5ヶ月（17 Sprint × 2週間）
```

## 主要数値（v2.1・参考見積もり）

- **開発期間**：8.5ヶ月想定（17 Sprint 反復型）
- **Sprint 内工数**：実装 55% / テスト 25% / 修正・安定化 20%
- **コスト試算**：開発期間 ¥664K〜¥874K / 本番月次 ¥95K〜¥106K
- **AWS 構成**：db.r7g.large + Lambda + EventBridge + Fargate + CloudWatch

## 技術スタック

- **共通基盤**：Claude Agent SDK / Gmail OAuth / Slack Bolt / pgvector
- **データ層**：Gmail / Slack / Google Drive / 動画分析（VSEO + dpro + Buzzmiru）/ Gemini Deep Research / X
- **インフラ**：AWS（Lambda / Fargate / RDS PostgreSQL pgvector / EventBridge / Secrets Manager / CloudWatch）

## v2.0 → v2.1 の主要変更点

IBM シニアエンジニアからのレビューを反映：

1. **機能① の起動方式を変更**：Slack 受動監視 → `@TeamAgent` 直接メンション。プライバシー懸念を軽減し、API コスト 70%削減、開発工数 62.5%削減
2. **機能①/③ の意図ルーター追加**：`@TeamAgent` で軽い事例検索 / 重い提案書生成 のどちらかを判定
3. **17 Sprint 反復型に再構成**：各機能を 2〜3 Sprint に分解、各 Sprint 末に Champion 検証 + FB 反映サイクル
4. **テスト工程を明示**：単体・結合・E2E・受入・性能・カオス・セキュリティ・回帰・監査ログテストを Phase 別に配置
5. **期間延長**：6ヶ月 → 8.5ヶ月（テスト+安定化期間を正規化、+¥190〜240Kの追加投資）

## 履歴

- v1.0（2026-04-27）：初版
- v1.1〜1.4（2026-04-28）：図解強化、Mail Agent 章、Gemini DR 移行方針
- v1.5（2026-05-01）：ナレッジソース統合
- v2.0（2026-05-07）：5機能 / 5データ層 / 4共通基盤 に再構成。Slack UI モック・フロー図・月次ダッシュボード追加。要件定義書・アーキテクチャ図書を別冊化
- v2.1（2026-05-08）：IBM レビュー反映。機能① 再構成 + 17 Sprint 化 + テスト工程明示 + 上司共有用サマリー新設
- **Phase 0〜E 全工程仕様書 v1（2026-05-11）：機能③ の Phase 0〜E 詳細仕様を別冊化。営業確認用に26質問項目つき。DR は統合派採用（Phase B サブエージェント、Pattern Bravo+ 4経路、Secrets Manager プロンプト管理）**

## 注意事項

- 本書のコスト試算・効果試算は Claude による参考見積もりであり、実測値ではない
- 顧客名・案件名は ACME / Beta社 / Gamma健食 等のダミー名

---

最終更新：2026年5月8日
