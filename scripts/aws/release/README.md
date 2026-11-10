# 便（本番リリース）と本番 DB 作業の実行スクリプト（持ち主の Mac から写した控え）

2026-09〜11 に持ち主の Mac（`~/Documents/Claude/Artifacts/aico-inputs-20260911/`）で実際に使ったものを 2026-11-05 に写した。
**正本は `scripts/aws/release_mcp.sh` と `infra/deploy/`**。ここにあるのは「その日の便をどう発射したか」の記録で、そのまま再実行する前に中の便番号・タスク定義のリビジョン・image digest を今の値に直すこと。
社員メールのリテラルは `<actor-email>` に置き換えてある（`updated_by` などの記録用。実行時は自分のメールを入れる）。

| ファイル | 役割 |
|---|---|
| [`kit/`](kit/README.md) | 別端末向けの mcp 便一式（MFA 更新は既存スクリプトを共用、同時実行の確認・答え試験付き）。現行の CLI 差し替え経路と B9 の関係は kit の README を参照 |
| `fire_and_authorize.sh` | 便の発射（CodeBuild 起動→承認）。その後のタスク定義差し替えは正本 `infra/deploy/terraform_runtime_guard.sh` 経由（持ち主の手元にあった `apply_mcp_tds.sh` は CLI で `update-service` するため repo の規則 B9 に反し、ここには入れていない）。MFA は `mfa_refresh.sh`（36 時間） |
| `driver_oc.sh` | OpenClaw 側の便の運転役 |
| `release_r99_order.sh` / `release_r100_order.sh` | 11/05 の便の手順書（順序・確認点）。最新の実例 |
| `apply_00NN_*.sh` | 動画アーカイブの migration 0035〜0041 を本番 RDS に当てた記録（踏み台トンネル方式） |
| `bootstrap_video_library_s3_iam.sh` | connect-web の task role への IAM inline 追加。env の追加（持ち主の手元の `add_video_library_env.sh`）はタスク定義の直接登録なので入れていない。env は Terraform（`infra/terraform/fargate.tf`）で |
| `run_video_library_task.sh` | サムネ取得などを ECS run-task で単発実行 |
| `vl_*.sh` + `vl_*_2026MMDD.py` | 動画アーカイブの本番データ作業（件数確認・一括登録・Apify 補完・事例推定の手動実行）。`.sh` が run-task の包み、`.py` がタスク内で動く本体 |
| `db_readonly_query.sh` / `probe_readonly.sh` | 本番 DB の読み取り専用クエリ |
| `releases/oc*.json` + `README-template.md` | 便ごとの記録の形式 |
