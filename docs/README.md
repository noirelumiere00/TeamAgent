# TeamAgent ドキュメント目次

設計・要件・実装・運用ドキュメントの索引。**現行実装を知りたい場合は「現行実装ドキュメント」から読むこと**（v3.0 以前は旧設計の歴史文書）。

---

## 現行実装ドキュメント

| 文書 | パス | 役割 |
|---|---|---|
| 📘 リポジトリ概観 | [../README.md](../README.md) | 現行アーキテクチャ（OpenClaw + MCP Gateway + Bedrock）の一次説明 |
| 🏗 アーキテクチャ & フロー | [v3.2/architecture_and_flows.md](v3.2/architecture_and_flows.md) | 全体像・データフロー（※Slack 面の記述は OpenClaw 化以前の部分が残る） |
| 📚 システムリファレンス | [v3.2/system_reference.md](v3.2/system_reference.md) | v3.2 系の全体リファレンス（※オーケストレーター記述は一部 stale） |
| 🔬 仕様×現状マトリクス | [v3.2/spec_vs_current_full_matrix_2026-06-15.md](v3.2/spec_vs_current_full_matrix_2026-06-15.md) | 元仕様と実装の突合（2026-06-15 時点） |
| 📈 SLO | [v3.2/slo_v1.md](v3.2/slo_v1.md) | パイロット SLO v1 |
| 🚀 OpenClaw デプロイ | [openclaw/deploy_runbook.md](openclaw/deploy_runbook.md) | OpenClaw の正準デプロイ手順（署名リリース鎖） |
| ✅ go-live チェックリスト | [openclaw/golive_checklist.md](openclaw/golive_checklist.md) | 本番投入判定 |
| 🛡 敵対ハーネス | [openclaw/adversarial_harness_runbook.md](openclaw/adversarial_harness_runbook.md) | なりすまし/RLS 越権の実証手順 |
| 🔐 セキュリティ監査 | [security/security_audit_2026-06-12.md](security/security_audit_2026-06-12.md) | OpenClaw + teamagent-mcp の監査記録 |
| 🔑 HMAC ローテ契約 | [security/hmac_rotation_contract.md](security/hmac_rotation_contract.md) | 鍵ローテーションの契約 |
| 🧰 Runbooks | [runbooks/](runbooks/) | provenance bootstrap / rollback drill / ingest 障害 ほか |

## 将来設計（Future / 未実装）

| 文書 | パス | 役割 |
|---|---|---|
| 🧭 **Hermes 段階導入 ADR** | [architecture/hermes_migration_design.md](architecture/hermes_migration_design.md) | Hermes Agent の dark deployment からの段階導入設計（Security 不変条件・敵対審査反映） |
| 🗺 Hermes 実装計画 | [architecture/hermes_implementation_plan.md](architecture/hermes_implementation_plan.md) | PR 分割（PR2〜PR8 + PR-R）・テスト戦略 |

## 旧設計（歴史文書 — 現実装の説明としては読まない）

### v3.0（2026-05・「Claude Agent SDK 常駐 + Slack Bolt」時代の構想）

構想の主要 3 文書は [archive/v3.0_superseded/](archive/v3.0_superseded/) へ移動済み:
[概要](archive/v3.0_superseded/teamagent_overview_v3.0.html) / [実装計画](archive/v3.0_superseded/teamagent_implementation_plan_v3.0.html) / [MVA 仕様](archive/v3.0_superseded/teamagent_mva_spec_v1.html)

[v3.0/](v3.0/) に現存するもの:

| 文書 | パス |
|---|---|
| 要件定義書 | [v3.0/teamagent_requirements_v3.0.html](v3.0/teamagent_requirements_v3.0.html) |
| アーキテクチャ図書 | [v3.0/teamagent_architecture_v3.0.html](v3.0/teamagent_architecture_v3.0.html) |
| 機能④ 確定仕様書 | [v3.0/teamagent_kinou4_spec_v1.html](v3.0/teamagent_kinou4_spec_v1.html) |
| 機能③ Phase 0〜E 仕様書 | [v3.0/teamagent_phase0E_spec_v1.html](v3.0/teamagent_phase0E_spec_v1.html) |
| Personal AI Coach 要件 | [v3.0/teamagent_personal_ai_coach_requirements_v3.0.html](v3.0/teamagent_personal_ai_coach_requirements_v3.0.html) |
| 部門別効果まとめ | [v3.0/teamagent_dept_effect_summary_v1.html](v3.0/teamagent_dept_effect_summary_v1.html) |
| Claude Code セミナー | [v3.0/teamagent_claudecode_seminar_v1.html](v3.0/teamagent_claudecode_seminar_v1.html) |
| 監査役懸念精査レポート | [v3.0/teamagent_監査役懸念精査レポート_v1.html](v3.0/teamagent_監査役懸念精査レポート_v1.html) |
| データ収集実現性リサーチ | [v3.0/teamagent_懸念2_データ収集実現性_深掘りリサーチ_v1.html](v3.0/teamagent_懸念2_データ収集実現性_深掘りリサーチ_v1.html) |
| IG スクレイピングリサーチ | [v3.0/teamagent_懸念2_複合リサーチ_IGスクレイピング調査_v2.html](v3.0/teamagent_懸念2_複合リサーチ_IGスクレイピング調査_v2.html) |

### v3.1 / それ以前

| 版 | 場所 | 備考 |
|---|---|---|
| v3.1 | [v3.1/](v3.1/) | 設計訂正ノート（OpenClaw 互換性の検証記録）ほか |
| v2.1 | [archive/v2.1/](archive/v2.1/) | 旧 README（README.v2.md）もここへ移設 |
| v2.0 | [archive/v2.0/](archive/v2.0/) | IBM レビュー前 |
| v1.5 | [archive/v1.5/](archive/v1.5/) | 初期構想 |

## その他

| 文書 | パス |
|---|---|
| PoC 記録（orchestrator 基盤選定など） | [poc/](poc/) |
| 次期要件メモ | [next/](next/)（※「claude-agent-sdk 厳密 pin」等の記述は 2026-07-17 の置換以前のもの） |
| 引き継ぎ | [handoff/](handoff/) |
| AiLa 自走ループ | [aila_loop/](aila_loop/) |

---

## 読む順序の推奨

### 新規参加エンジニア
1. [../README.md](../README.md)（現行アーキテクチャ）
2. [../CLAUDE.md](../CLAUDE.md)（開発ルール・地雷）
3. [openclaw/deploy_runbook.md](openclaw/deploy_runbook.md) + [security/security_audit_2026-06-12.md](security/security_audit_2026-06-12.md)
4. [architecture/hermes_migration_design.md](architecture/hermes_migration_design.md)（将来方向）

### 経営層 / 監査役
1. [../README.md](../README.md)
2. [v3.0/teamagent_監査役懸念精査レポート_v1.html](v3.0/teamagent_監査役懸念精査レポート_v1.html)（当時の精査記録）
3. [security/security_audit_2026-06-12.md](security/security_audit_2026-06-12.md)

---

最終更新: 2026-08-18（現行/将来/旧設計の三分冊化・dead link 全修正）
