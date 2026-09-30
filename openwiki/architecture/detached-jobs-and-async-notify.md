---
type: architecture
title: 長時間ジョブの切り離しと完了通知
description: OpenClaw の打ち切り時間を超える処理を MCP gateway が daemon thread に切り離し、完了・進捗・中断を Slack へ直接投稿する仕組み。detached_jobs・surface_video_followup・async_job_notify・progress_notify・direct_summary・payload_offload の役割と env フラグ。
tags: [mcp-gateway, async, slack, detach, feature-flags]
sources:
  - id: openwiki-source-49e9ec8046eee60f7a08c80f
    resource: repo://scripts/run_mcp_http_server.py
  - id: openwiki-source-83d4cf6d1ac49ea61a464553
    resource: repo://src/teamagent/mcp_gateway/async_job_notify.py
  - id: openwiki-source-50f649289b9afe67ea72aa91
    resource: repo://src/teamagent/mcp_gateway/detached_jobs.py
  - id: openwiki-source-c4efca55031904c23d0af62a
    resource: repo://src/teamagent/mcp_gateway/direct_summary.py
  - id: openwiki-source-40fc7dabde45dd10091ad30a
    resource: repo://src/teamagent/mcp_gateway/payload_offload.py
  - id: openwiki-source-cd61822f8aebcb78385ea96f
    resource: repo://src/teamagent/mcp_gateway/progress_notify.py
  - id: openwiki-source-1fdde611c13aba4b68a5ff42
    resource: repo://src/teamagent/mcp_gateway/server.py
  - id: openwiki-source-463095cdd1cf5a98312aab3c
    resource: repo://src/teamagent/mcp_gateway/surface_video_followup.py
generated: { by: "claude-code", at: "2026-09-30T04:50:20.078Z" }
verified:
  - by: openwiki/0.6.1
    at: 2026-09-30T04:50:20.078Z
---

# 長時間ジョブの切り離しと完了通知

## なぜ必要か

OpenClaw は 1 回のツール実行を約 6 分（360 秒）で打ち切る。動画分析（`video_algorithm`）は 5 本で 9 分前後、提案書生成や TikTok 取得は 40〜50 分かかることがある。打ち切られた後に MCP が完走しても結果の戻り先が無く、利用者には何も届かない。

そこで MCP gateway（`src/teamagent/mcp_gateway/server.py` の `dispatch_tool`）の中に、「結果をモデル経由で返さず、MCP 自身が Slack に投稿する」経路が何本か用意されている。すべて **env フラグで既定 OFF**、段階公開用の allowlist 付き。

| 仕組み | モジュール | 対象ツール | フラグ（既定） | 何をするか |
|---|---|---|---|---|
| 同期の切り離し | `detached_jobs.py` | `video_algorithm` | `USE_VIDEO_ALGORITHM_DETACH`（OFF） | 30 秒で終わらなければ受付文を返し、完了時に依頼元へ直接投稿 |
| 検索上位チェックの 2 段目 | `surface_video_followup.py` | `search_surface_check` | `USE_SURFACE_VIDEO_FOLLOWUP`（OFF） | 1 段目の後、上位動画を裏で分析して同じ会話に追記 |
| 非同期ジョブの完了通知 | `async_job_notify.py` | `tiktok_acquire` / `proposal_builder_submit` | `USE_ASYNC_JOB_NOTIFY`（OFF） | job_id を 30 秒間隔で status 照会し、完了を submit 元へ通知 |
| 進捗表示 | `progress_notify.py` | 重いツールだけ | `ENABLE_PROGRESS_NOTIFY`（OFF） | 実行中に「検索しています…」を出し、終わったら削除 |
| 直接投稿 | `direct_summary.py` | `search_surface_check` | `USE_DIRECT_SUMMARY_POST`（OFF） | 結果を Block Kit で DM に出し、Aico には「投稿済み」だけ返す |
| 長文退避 | `payload_offload.py` | 会社共有ナレッジ系 | `USE_PAYLOAD_OFFLOAD`（OFF） | 大きな返却を S3 に退避し、切り詰め版＋署名 URL を返す |

たとえるなら、窓口（OpenClaw）は 6 分しか待てないので、時間のかかる注文は厨房（MCP）が「できたら席までお持ちします」と番号札を渡して引き取る。

## detached_jobs: video_algorithm の切り離し

1. `dispatch_tool` は `video_algorithm` を専用 daemon thread で開始する（OpenClaw 側の CancelledError はジョブに伝えない）。
2. `VIDEO_ALGORITHM_DETACH_AFTER_S`（既定 30 秒、5〜240 に丸め）以内に終われば通常どおり同期で返す。キャッシュヒット・入力エラーはここで返る。
3. 超えたら `status=running` の受付 payload を返し、ジョブは走り続ける。完了したら**署名検証済み caller claim の channel_id / thread_ts**（`destination_from_claim`）へ `slack_summary` を投稿する。モデルが書いた宛先は使わない。

主な制約:

- `VIDEO_ALGORITHM_DETACH_ALLOWED_EMAILS` が**空なら誰にも適用しない**（`skills/_shared/rollout.py` の「空＝全員許可」とは逆なので注意）。
- `VIDEO_ALGORITHM_DETACH_DM_ONLY`（既定 1）で 1 対 1 DM（`D…`）だけに限定。
- 同時実行は `VIDEO_ALGORITHM_MAX_BACKGROUND`（既定 2、全利用者合計）。超えた分は優先度付きの順番待ち（最大 10 件、`DEFAULT_MAX_QUEUED`）。満杯なら quota を使う前に「混み合っています」。同期に戻さないのは、戻すと 360 秒の打ち切りが再発するため。
- 二重依頼は「検証済み slack_user_id＋正規化 query」のキーでプロセス内登録簿（`REGISTRY`）が止める。
- 登録簿の項目は開始から 45 分（`STALE_AFTER_S`）を超えると次の依頼時に掃除される。
- 利用者向けの文に job_id・error_code・S3 URL・ツール名などの内部語を出さない（SOUL.md の禁止語）。

### 再デプロイ時の中断通知

`scripts/run_mcp_http_server.py` は SIGTERM を受けると、接続の片付けより先に `notify_interrupted`（予算 20 秒、ECS の stopTimeout 30 秒以内）を呼ぶ。登録簿に closing の印を立て、処理中ジョブの宛先へ「システム更新で中断」を送る。以降の新しい依頼には受付文の代わりに中断文を返し、順番待ちのジョブは開始しない。

登録簿は**プロセス内**なので、タスクが入れ替わると処理中の状態は引き継がれない（中断通知で利用者に知らせる設計）。

## surface_video_followup: 検索上位チェックの 2 段目

`search_surface_check`（約 2 分で結論を返す）の `skill.run` が返った直後に `maybe_schedule` が呼ばれる。対象なら、同じ上位 N 本（`SURFACE_VIDEO_FOLLOWUP_MAX_VIDEOS`、既定 5）を `video_algorithm` の分析エンジンで裏で分析するジョブを **同じ `REGISTRY`** に登録し、1 段目の `slack_summary` に予告を 1 行足す。

- 同時実行枠と待ち行列は `video_algorithm` の切り離しと共有する（別枠にすると同時に 4 本走り、メモリと Gemini の 429 を踏むため）。明示依頼（`PRIORITY_EXPLICIT`）が自動の 2 段目（`PRIORITY_AUTO`）より先に始まる。
- 月間の動画分析上限の残りを `VideoQuotaStore.peek_remaining` で消費せずに読み、0 本なら予告の代わりに「上限に達しているため分析しません」を足して登録しない。
- 同じ人・同じ KW・同じ URL 集合の成功結果は 24 時間プロセス内 TTL キャッシュで使い回す（再デプロイで消える）。

## async_job_notify: submit/status 型ジョブの完了通知

`tiktok_acquire` と `proposal_builder_submit` は即座に `job_id` を返し、実処理は別の場所（SQS→Fargate、MCP 内 thread）で進む。`USE_ASYNC_JOB_NOTIFY` が ON なら、`_schedule_async_job_notice` が daemon thread を 1 本立て、30 秒後から 30 秒間隔で対応する status skill（`tiktok_acquire_status` / `proposal_builder_status`）を呼ぶ。`done` / `failed` になったら submit 元の会話へ通知する。`tiktok_acquire` は 1 回の実行時間に収まらない依頼を複数のジョブに分けて `job_ids` で返すことがあるため、`job_id` と `job_ids` の全ジョブ（重複除去）にそれぞれ見張りを付ける。先頭だけを見張ると残りの完了が届かない。

- 見張りは 60 分で打ち切る。実測の所要（40〜50 分帯）より短いと毎回「まだ完了していません」の誤報を出したうえで完了通知を落としていたため、15 分から延長された。
- poll 用の `SkillContext` には `ASYNC_JOB_POLL_METADATA_KEY` の印が付き、status skill は課金を伴う補完（Apify）をこの経路では起こさない。
- 通知の失敗は呼び出し元へ伝播させない。

## progress_notify: 実行中の進捗表示

`ENABLE_PROGRESS_NOTIFY` が ON のとき、`search` など「重い」と登録されたツールだけ、実行前に進捗メッセージを投稿し、終了後に `chat.delete` で消す（`chat.update` だと最終回答と二重になるため）。宛先は `_user_context.channel_id` 優先、無ければ本人 DM。Slack 往復は 2.5 秒で打ち切り、失敗してもツール実行は止めない（fail-open / fail-fast）。

メール・カルテ・朝ダイジェストなど個人機微のツールは対象に**入れない**。公開チャンネルに「この人がメール操作中」と出てしまうため。

## direct_summary: 検索上位チェックの直接投稿

モデル（Haiku）が MCP の文面を自分の見出しで組み直し、詳細レポートの URL を落とす事故への対策。`USE_DIRECT_SUMMARY_POST` が ON で、allowlist（`DIRECT_SUMMARY_POST_ALLOWED_EMAILS`、**空なら誰にも適用しない**）内・署名検証済み・1 対 1 DM なら、MCP が Block Kit で DM に結果を出し、Aico には中身の無い「投稿済み」を返す。投稿に失敗したら通常の返却に戻す（結果が消えないことを優先）。usage 記録の**後**に置かれているので、費用記録は投稿の成否に関係なく残る。

## payload_offload: 長文の S3 退避

返却 JSON が閾値（既定 10,000 文字）を超えたら、全文を非公開 S3 に退避（署名 URL・7 日）し、構造を保ったまま長い文字列だけを切り詰め、`offloaded` / `full_url` を付ける。URL 系フィールドは切らない。

- **allowlist 方式**: 対象は `search`・`clientkarte`・`knowledge_deliver`・`proposal_draft`・`proposal_review`・`tiktok_search`・`video_analysis`・`video_algorithm` だけ。メールや朝ダイジェストのような本人限定データを署名 URL にすると RLS をバイパスする漏洩経路になるため、足してはいけない。
- `USE_PAYLOAD_OFFLOAD` に加え、会社共有モード（`TEAMAGENT_SHARED_COMPANY_DOMAINS` 設定時）でしか発動しない。
- S3 退避に失敗したら切り詰めもせず原文を返す（fail-open）。

## 返却前ミドルウェアの順序

`dispatch_tool` の最後は次の順で固定されている（順序契約）。

1. `surface_video_followup.maybe_schedule`（2 段目の登録と予告）
2. `_schedule_async_job_notice`（完了通知の登録）
3. usage 記録（`mcp_tool_usage` ログと `usage_events`）
4. `direct_summary`（投稿済みならここで返る）
5. `_relay_fields`（skill の `mcp_relay_fields` で返す欄を絞る）
6. `payload_offload.maybe_offload`（**リンク注入より先**。逆だと注入した URL ごと切り詰められる）
7. `search` にだけ Web UI リンクを注入

詳細は [MCP gateway](mcp-gateway.md)。ジョブ本体は [提案書・資料生成ジョブ](../workflows/proposal-jobs.md) と [動画・TikTok 分析](../workflows/video-and-tiktok-analysis.md) を参照。

## テスト

`tests/mcp_gateway/` に機能ごとのテストがある: `test_video_algorithm_detach.py`、`test_surface_video_followup.py`、`test_async_job_notify.py`、`test_async_job_poll_ctx.py`、`test_progress_notify.py`、`test_direct_summary.py`、`test_payload_offload_report.py`、`test_slack_blocks_delivery.py`。`tests/test_payload_offload.py` も長文退避の純関数を固定する。
