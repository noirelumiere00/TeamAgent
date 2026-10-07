# 動画分析の切り離し（video_algorithm detach）有効化 Runbook

作成: 2026-09-28。対象: `USE_VIDEO_ALGORITHM_DETACH` ほか 6 つの env（mcp の TD）。
コード: `src/teamagent/mcp_gateway/detached_jobs.py`・`server.dispatch_tool`・
`scripts/run_mcp_http_server.py`。**この Runbook 自体は apply しない。**

## 何が変わるか

- 動画分析が `VIDEO_ALGORITHM_DETACH_AFTER_S`（既定 30 秒）を超えたら、mcp は受付文を返して
  分析を続け、完了したら依頼元の会話（署名検証済み claim の channel_id・thread_ts）へ直接投稿する。
  OpenClaw の約 6 分（360 秒）の打ち切りで結果が消える問題への対策。
- 同時実行は `VIDEO_ALGORITHM_MAX_BACKGROUND`（既定 2・全利用者の合計）まで。超えた分は
  **順番待ち**（受付文「順番待ちです。始まり次第分析し、終わったらこの会話にお届けします」）。
  待っている間は quota を使わない。待ちも 10 件で満杯なら「混み合っています」を返す（quota 未使用）。
- 再デプロイ（SIGTERM）では、処理中・順番待ちの宛先へ「システム更新で中断」を送る。
  その後に届いた依頼には受付文ではなく中断文を返す。

## env と tfvars 変数

| env | tfvars 変数 | 既定（今と同じ） | 第 1 段階 |
|---|---|---|---|
| `USE_VIDEO_ALGORITHM_DETACH` | `use_video_algorithm_detach` | `0` | `1` |
| `VIDEO_ALGORITHM_DETACH_ALLOWED_EMAILS` | （退役・fargate.tf が `"*"` を直接焼く） | `"*"`（2026-10-06 全員開放） | 小俣さん本人のみ（済） |
| `VIDEO_ALGORITHM_DETACH_DM_ONLY` | `video_algorithm_detach_dm_only` | `1` | `1` |
| `VIDEO_ALGORITHM_DETACH_AFTER_S` | `video_algorithm_detach_after_s` | `30` | `30` |
| `VIDEO_ALGORITHM_MAX_BACKGROUND` | `video_algorithm_max_background` | `2` | `2` |
| `VIDEO_ALGORITHM_CACHE_LEASE_SECONDS` | `video_algorithm_cache_lease_seconds` | `1800` | `600`（裁定待ち） |

## 有効化の手順

1. mcp便で本コードが本番に入っていることを確かめる（フラグ OFF のままなら挙動は今と同じ）。
2. mcp の TD の env を上の「第 1 段階」の値に差し替えて、サービスを更新する（小俣さん実行）。
3. **同じ値を activation 版 tfvars（正本）へ必ず追記する:**
   `~/dev/worktrees/teamagent-activation/infra/terraform/terraform.tfvars`
   - 理由: `infra/deploy/terraform_runtime_guard.sh` の live→tfvars 導出は、この 6 変数を
     列挙していない（guard は凍結対象なので今回は変えていない）。tfvars に無いと、guard 経由の
     terraform apply（次の mcp便など）で既定（OFF・allowlist 空・LEASE 1800）に黙って戻る。
   - 値を変えたとき（DM_ONLY を外す・OFF に戻す）も毎回同じく追記する。
   - allowlist だけは 2026-10-06 の全員開放で tfvars から外した（fargate.tf が `"*"` を直接焼く）。
     絞り直すときは fargate.tf の値を変える PR を出す。
4. 小俣さんの DM で実機確認: 30 秒で受付文が返る／完了が同じ会話に届く／
   2 本同時の後の 3 本目が「順番待ち」になり、後から届く。

## 戻し方

- TD の env で `USE_VIDEO_ALGORITHM_DETACH=0` に戻し、tfvars（正本）も `"0"` に戻す。
