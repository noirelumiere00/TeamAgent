# TeamAgent ドキュメント目次

すべての設計・要件・実装ドキュメントの一覧。

---

## v3.0（最新）

### 主要設計ドキュメント

| 文書 | パス | 役割 | 主な読者 |
|---|---|---|---|
| 📘 **構想・概要書** | [v3.0/overview_v3.0.html](v3.0/overview_v3.0.html) | プロダクト全体像と思想 | 経営層・営業・推進担当 |
| 📋 **要件定義書** | [v3.0/requirements_v3.0.html](v3.0/requirements_v3.0.html) | FR / NFR / DR の網羅的定義 | FDE・QA |
| 🏗 **アーキテクチャ図書** | [v3.0/architecture_v3.0.html](v3.0/architecture_v3.0.html) | C4 model + Skill Registry 設計 | FDE・実装担当 |
| 🗓 **実装計画書** | [v3.0/implementation_plan_v3.0.html](v3.0/implementation_plan_v3.0.html) | 17 Sprint ロードマップ・コスト・リスク | FDE・PM |
| 🚀 **MVA 仕様書 v1** | [v3.0/mva_spec_v1.html](v3.0/mva_spec_v1.html) | サーバー来た日に実装開始できる Day-0 仕様 | FDE（実装担当） |

### Skill 別仕様書（別冊）

| 文書 | パス | 役割 |
|---|---|---|
| 🎬 **機能④ 確定仕様書** | [v3.0/kinou4_spec_v1.html](v3.0/kinou4_spec_v1.html) | 動画ナレッジ分析 Skill（VSEO吸収・Gemini採用） |
| 📝 **機能③ Phase 0〜E 仕様書** | [v3.0/phase0E_spec_v1.html](v3.0/phase0E_spec_v1.html) | 提案コンテンツ生成 Skill の詳細工程 |
| 🤖 **機能⑥ Personal AI Coach 要件** | [v3.0/personal_ai_coach_requirements_v3.0.html](v3.0/personal_ai_coach_requirements_v3.0.html) | 社内版 NoimosAI 構想 |

### 関連資料

| 文書 | パス | 役割 |
|---|---|---|
| 📊 **AI 効率化スライド v2** | [v3.0/ai_capability_for_sales_slides_v2.html](v3.0/ai_capability_for_sales_slides_v2.html) | 営業部長向け 23 枚スライド（4 層モデル+実演デモ+Agent化メリット） |
| 📄 **部門別効果まとめ（ペラ3）** | [v3.0/dept_effect_summary_v1.html](v3.0/dept_effect_summary_v1.html) | 営業/制作/アカウントの効果を A4×3 ページ |
| 🎓 **Claude Code 営業向けセミナー** | [v3.0/claudecode_seminar_v1.html](v3.0/claudecode_seminar_v1.html) | 個人 PC で AI を始めるセミナー教材 |
| 🔍 **監査役懸念精査レポート** | [v3.0/監査役懸念精査レポート_v1.html](v3.0/監査役懸念精査レポート_v1.html) | 監査役4懸念への精査結果 |
| 🔍 **データ収集実現性 深掘りリサーチ** | [v3.0/懸念2_データ収集実現性_深掘りリサーチ_v1.html](v3.0/懸念2_データ収集実現性_深掘りリサーチ_v1.html) | データ収集の事例リサーチ |
| 🔍 **IGスクレイピング複合リサーチ v2** | [v3.0/懸念2_複合リサーチ_IGスクレイピング調査_v2.html](v3.0/懸念2_複合リサーチ_IGスクレイピング調査_v2.html) | IG スクレイピングのリスク評価 |
| 💬 **子会社エンジニアインタビューガイド** | [v3.0/subsidiary_interview_guide_v1.md](v3.0/subsidiary_interview_guide_v1.md) | 120人運用ノウハウ吸い上げ用 |

---

## 履歴版（archive）

過去バージョンは [archive/](archive/) に格納。

| 版 | 場所 | 備考 |
|---|---|---|
| v2.2 | [archive/v2.2/](archive/v2.2/) | 整合性 QA 反映版（v3.0 直前） |
| v2.1 | [archive/v2.1/](archive/v2.1/) | IBM レビュー反映 |
| v2.0 | [archive/v2.0/](archive/v2.0/) | 5機能再構成版 |
| v1.5 | [archive/v1.5/](archive/v1.5/) | ナレッジソース統合版 |

---

## 読む順序の推奨

### 経営層 / 監査役
1. `v3.0/overview_v3.0.html`（概要書）
2. `v3.0/監査役懸念精査レポート_v1.html`
3. `v3.0/dept_effect_summary_v1.html`（部門別効果ペラ3）

### 実装担当 / FDE
1. `v3.0/mva_spec_v1.html`（**最重要**）
2. `v3.0/architecture_v3.0.html`
3. `v3.0/requirements_v3.0.html`
4. `v3.0/implementation_plan_v3.0.html`
5. `v3.0/kinou4_spec_v1.html`（機能④ 着手時）

### 営業 / 推進担当
1. `v3.0/overview_v3.0.html`
2. `v3.0/claudecode_seminar_v1.html`（個人 Claude Code 活用）
3. `v3.0/ai_capability_for_sales_slides_v2.html`（部内共有用）

---

最終更新：2026 年 5 月 19 日
