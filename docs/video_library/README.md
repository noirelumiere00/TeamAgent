# 動画アーカイブ（connect.newstv.co.jp/library）の要件・計画の置き場

持ち主の Mac（`~/Documents/projects/video-structure-search/`）から 2026-11-05 に写した。運用手順の正本は `docs/v3.2/ops/video_library.md`、コードは `src/teamagent/connect_web/library.py`・`portal.py`、migration は 0035〜0041。

| ファイル | 中身 |
|---|---|
| `requirements_v0.1.md` / `.html` | 要件定義（構成×動画の相互検索。10/29） |
| `implementation_plan_v1.md` / `v2.md` | 実装計画 P1（DB＋同期）→ P2 サムネ/動画 S3 → P3 /library → P4 Aico → P5 公開 → P6 構成分析 |
| `p3_design.md` | /library の画面設計 |
| `plan_case_inference.md` + `case_policy_20261104.json` + `case_eval_report_20261104.json` | 事例（広告主・案件）の自動推定の方針と評価（Bedrock Haiku、66/66 正解・回答率 66%） |
| `requirements_addendum_url_import.md` / `implementation_plan_url_import.md` | URL 貼り付け取り込み（11/02 追加要件、PR #792 #817 #818 #825） |
| `deploy_runbook_p1_p3.md` | 初回の本番反映手順（10/30 に実施済み） |
| `history_20261029_20261105.md` | 決定と本番反映の時系列 |
