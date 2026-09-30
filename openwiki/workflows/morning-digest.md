---
type: workflow
title: 朝ダイジェスト
description: EventBridge の定期タスク（一括・planner・単独利用者の 3 モード）が連携済み利用者ごとに Gmail/Calendar/Slack を読み、重要度分類と作り置き下書きを経て本人 DM に配信する流れ。digest_delivery / digest_notice による二重配信防止と、MCP ツール morning_digest として呼ばれた場合との違いを扱う。
tags: [morning-digest, eventbridge, fargate, scheduler, gmail, calendar, slack-dm, idempotency]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T07:51:05.076Z
sources:
  - id: openwiki-source-ef54f8e63a41477899e459da
    resource: repo://infra/terraform/fargate.tf
  - id: openwiki-source-7895b677ded28b43c9108a68
    resource: repo://infra/terraform/lambda/reminder_notify/handler.py
  - id: openwiki-source-e0b106f8a839c4d9c63c200a
    resource: repo://infra/terraform/morning_digest_schedule.tf
  - id: openwiki-source-e0948537ab0a1dd3b57bd3d1
    resource: repo://scripts/run_morning_digest_fargate.py
  - id: openwiki-source-191f782d59e3f21a8a61616d
    resource: repo://src/teamagent/adapters/digest_delivery_store.py
  - id: openwiki-source-d60026f328e461a6869f6af6
    resource: repo://src/teamagent/adapters/digest_notice_store.py
  - id: openwiki-source-93b533fdca207ce5b185cca6
    resource: repo://src/teamagent/adapters/scheduler_client.py
  - id: openwiki-source-a9c83b66f4cc0dda94a14e08
    resource: repo://src/teamagent/digest_user_ref.py
  - id: openwiki-source-f15be3fc0ab8c09c963801e7
    resource: repo://src/teamagent/skills/morning_digest/send_window.py
  - id: openwiki-source-7c4e86c8d099c2810f3d0d38
    resource: repo://src/teamagent/skills/morning_digest/skill.py
generated: { by: "claude-code", at: "2026-09-29T07:51:05.076Z" }
---

# 朝ダイジェスト

## 全体像

平日の朝、Google 連携済みの利用者ごとにメール・当日の予定・Slack の返信漏れをまとめ、本人の Slack DM へ Block Kit で 1 通送る。Slack のメンションから始まる他の機能と違い、**Aico の会話 run を通らない定期バッチ**として動く。

| 層 | 場所 | 責任 |
|---|---|---|
| 起動 | `infra/terraform/morning_digest_schedule.tf` | EventBridge ルール 2 本（一括・planner）が同じ ECS タスク定義を RunTask する |
| 実行・配信 | `scripts/run_morning_digest_fargate.py` | 対象者の決定、モード分岐、skill 呼び出し、Block Kit 整形、Slack DM、配信の印、予定リマインドの登録 |
| 組み立て | `src/teamagent/skills/morning_digest/skill.py` | 1 人分のメール要約・重要度・予定・Slack 返信漏れ・下書き。DM は送らない（外への副作用は Gmail の `drafts.create` だけ） |
| 印 | `src/teamagent/adapters/digest_delivery_store.py` / `digest_notice_store.py` | 「その日 1 回だけ」を DB の一意制約で保証する |
| 予約参照 | `src/teamagent/digest_user_ref.py` | 予約ペイロードに載せる不可逆の利用者参照（メールアドレスを載せない） |

たとえるなら、一括実行は「毎朝 9:30 の定期便」、planner は「朝 4 時に各人の最初のアポを見て早便を予約する配車係」、`digest_delivery` は「その日の配達済みスタンプ」。スタンプがあれば、どの便も 2 度は届けない。

## 起動の 3 モード

`_mode()` は `--mode=` 引数 → `MORNING_DIGEST_MODE` → `MORNING_DIGEST_USER_REF` の有無（あれば `single`）→ 既定 `bulk` の順で決まる。

<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: an unescaped angle bracket inside a label breaks rendering; rephrase the label. -->
```text
flowchart TD
  P["EventBridge planner<br/>日〜木 19:00 UTC（平日 04:00 JST）"] -->|"RunTask --mode=planner"| PL[run_planner]
  PL --> Q{"送信時刻が既定時刻より前?"}
  Q -- はい --> SCH["EventBridge Scheduler<br/>digest-{user_ref}-{YYYYMMDD}"]
  Q -- "いいえ / 未連携" --> KEEP[一括実行に残す]
  SCH -->|"at(時刻) → SQS"| L["reminder_notify Lambda<br/>kind=digest"]
  L -->|"RunTask MODE=single / USER_REF / DATE"| SG["single: 1 人分"]
  B["EventBridge 一括<br/>平日 00:30 UTC（09:30 JST）"] -->|RunTask| BK["bulk: 対象者全員"]
  SG --> C{"digest_delivery.claim"}
  BK --> C
  C -- 取れた --> R["skill.run → 整形 → 本人 DM"]
  C -- 取れない --> X[skip]
```

- **bulk**: タスク定義の既定 command。ルールの ENABLED/DISABLED は `morning_digest_rule_enabled` で決まり、runtime guard が live の状態を注入する。
- **planner**: 同じタスク定義を `containerOverrides` で `--mode=planner` にして起動する。ルールは `enable_reminders && morning_digest_personalized` のときだけ ENABLED。予約を作るだけで、ダイジェストは 1 通も送らない。稼働日は一括と揃える（毎日にすると土日に個人別配信だけが届く）。
- **single**: Scheduler の予約が SQS 経由で `reminder_notify` Lambda に届き、`kind=digest` の分岐がペイロード（32 桁 hex の `user_ref` と `YYYY-MM-DD`）を検査してから、タスクを 1 人分だけ RunTask する。RunTask の `failures[]` が空でなければ例外にして SQS に再試行させる（無音で 1 通落ちるのを防ぐ）。

どのルールのターゲットも `retry_policy`（`maximum_retry_attempts = 1`・イベント寿命 3600 秒）を持つ。再実行されても二重に送らない仕組みが下の「配信の記録」。

## 配信先の動的判定

`_resolve_target_users()` の順序:

1. `MORNING_DIGEST_USERS`（カンマ区切り）があればそれを使う。
2. 無ければ RDS の `oauth_tokens` を列挙する。このテーブルは FORCE RLS なので `SET app.user_role = 'admin'` を明示してから `user_email` だけを読む（トークン本体は読まない）。`DATABASE_URL` が無い・失敗したときは空＝誰にも送らない。
3. どちらの経路でも最後に `MORNING_DIGEST_EXCLUDE` の利用者を外す（連携を切らずに一時停止できる）。

Slack の宛先は保存しない。毎回 email → `users.lookupByEmail` → `conversations.open` で本人の IM を解決し直す。single でもペイロードに channel は無く、`resolve_user_ref` が連携済み利用者の中から hash が一致する email を探す（見つからなければ配信しない＝fail-closed）。`user_ref` は `sha256("<pepper>:digestref:<email>")` の先頭 32 hex で、pepper はタスク定義の `secrets`（Secrets Manager）から `DIGEST_USER_REF_PEPPER` として入る。※ `digest_user_ref.py` の docstring にある式（`pepper + ":" + email`）は実装と一致しない。コードが正。

## 送信時刻の決め方（planner）

`send_window.compute_send_time` が唯一の実装。

- 送信時刻 = clamp(当日最初の「時刻つき」予定の開始 − 60 分（5 分単位に切り下げ）, 下限 06:00, 上限 `MORNING_DIGEST_DEFAULT_TIME`（既定 09:30）)。
- 終日予定は計算から外す。時刻つき予定が無い日は既定時刻。
- planner は「予定なし」「既定時刻に張り付いた」人には**予約を作らない**。一括実行が拾うので 1 通は必ず出るし、予約を作ると土曜に DM が出たり、一括と同時刻に 1 人 1 タスクが余分に立ったりする。
- 下限 06:00 に張り付いた回は、single 実行の事例ブリーフ節に 1 行添える。判定は予約ペイロードに載せず、発火側が同じ純関数で計算し直す（`_early_notice`）。
- planner 後の予定変更には追随しない。祝日判定もしない。対象日は `MORNING_DIGEST_DATE` で上書きでき、planner と配信の両方が同じ `_digest_day()` を使う。

## 1 人分の組み立て（MorningDigestSkill.run）

`ctx.metadata["user_email"]` が必須で、トークンが無ければ `PermissionError`（runner 側では `skipped`）。各節は独立した try で囲まれ、失敗は `out.errors` に積んで他の節は配信する。

1. **メール**: `(in:inbox OR is:starred) newer_than:{lookback_days}d -category:promotions -category:social`（既定 3 日・最大 30 件）をスレッド単位に重複排除し（最大 25）、最新メッセージをアンカーにする。
   - 差出人区分は `IMPORTANT_SENDERS` に当たれば `vip`、`DIGEST_INTERNAL_DOMAIN` なら `internal`、それ以外 `external`。
   - **本人宛の判定**（`_is_addressed_to`）: To ヘッダに本人のアドレスが直接あるときだけ真。CC のみ・メーリングリスト宛（To がリスト）は偽。✏️ 下書きボタンのトークンは本人宛・一斉配信でない・HMAC 鍵が有効、のすべてを満たすときだけ発行する。
   - **重要度**: Bedrock（`BEDROCK_MODEL_ID`）へ本文を `MORNING_DIGEST_TRIAGE_BATCH`（既定 8）件ずつ渡し、固定 JSON 配列で `importance`（high/medium/low）・要約・期限・依頼・次の一手・確定 MTG 日時・日程打診の有無を得る。本文は資料として扱い、境界トークンを無害化する。結果は LLM に複写させた `id` で結合し、位置では結合しない。id が合わない要素は要約なし、バッチの失敗は `medium` に落とす。1 件も一致しないバッチは `error` ログになり、アラームが鳴る。最後に重要度順に並べ替える。
2. **カレンダー**: JST の当日 0:00 起点の窓。取得上限は 20 件（`MORNING_DIGEST_BRIEF` が ON なら 100 件）。
3. **事例ブリーフ**: `MORNING_DIGEST_BRIEF` が ON のときだけ呼ぶ（既定 OFF）。
4. **Slack 返信漏れ**: provider が渡されたときだけ走査し、走査できたかどうか（`slack_unread_scanned`）を別に持つ。見ていないのに「なし」とは表示しない。
5. **下書き**（次節）と、☑️ 確認済み用の一括トークン。

<!-- openwiki: broken internal link [/openwiki/workflows/digest-buttons.md] link "/openwiki/workflows/digest-buttons.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/integrations/google-oauth-and-token-store.md] link "/openwiki/integrations/google-oauth-and-token-store.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
各ボタンのトークンと押下後の処理は [朝ダイジェストのボタン処理](/openwiki/workflows/digest-buttons.md)、Google トークンの保管と更新は [Google OAuth とトークン保管](/openwiki/integrations/google-oauth-and-token-store.md) を参照。

## 作り置き下書き

`DRAFT_ON_DEMAND_ONLY` が偽なら `_create_drafts` が朝のうちに Gmail 下書きを作る（定期タスクのタスク定義は `false`）。

- 対象は `importance` が `MORNING_DIGEST_DRAFT_IMPORTANCE`（既定 `high`）に入り、かつ To に本人がいるスレッド。日程打診は 🗓 `schedule_propose` の担当なので除外する。
- 既存下書きのあるスレッドには作らず `has_draft` だけ立てる（`MORNING_DIGEST_DEDUPE_DRAFTS` 既定 ON）。毎日走っても二重にならない。
- 上限は**作成数**で数える（`MORNING_DIGEST_MAX_DRAFTS`・入力 schema は 0〜10・タスク定義は 5）。候補が途中で脱落しても下位の候補で埋まる。
- Reply-All（`MORNING_DIGEST_REPLY_ALL` 既定 ON）で、スレッド履歴と案件の決定事項を文脈に入れる。一斉配信・自動配信には作らない。
- Gmail には `drafts.create` しか呼ばない。送信はしない（G4）。

<!-- openwiki: broken internal link [/openwiki/workflows/mail-tools.md] link "/openwiki/workflows/mail-tools.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
真のときは `list_drafts` との照合で `has_draft` を埋めるだけで、作成は ✏️ ボタンから `mail_draft` が行う（[メール系ツール](/openwiki/workflows/mail-tools.md)）。※ skill.py の「4. 下書き」のコメントは「既定（オンデマンド）」と書いているが、コードの既定値は偽で、定期タスクも作り置きで動いている。

## 配信の記録（digest_delivery / digest_notice）

`_process_user` の順序:

1. store があれば `DigestDeliveryStore.claim(email, day, origin)`。`INSERT ... ON CONFLICT (user_email, digest_date) DO NOTHING` の rowcount が 1 のときだけ送る。例外・不正な入力は偽＝**送らない**（fail-closed）。`origin` は `scheduled`（single）か `bulk`。
2. `skill.run` → 整形（`MORNING_DIGEST_COMPACT` で密度優先の描画）→ Slack DM。
3. skill の失敗、整形・配信の例外、Slack が受け付けなかった場合は `release` で印を消し、後続の経路が再挑戦できるようにする。
4. 配信に成功し `MORNING_DIGEST_REMINDERS` が ON なら、当日の予定の開始 N 分前リマインドを Scheduler に登録する（失敗しても配信の成否は変えない）。

store は `MORNING_DIGEST_PERSONALIZED` が ON のときだけ作られる。OFF なら claim を 1 度も呼ばない。タスク定義は `enable_reminders` が偽のときこのフラグを強制的に偽にする（予約を作れないのに一括だけが claim する状態を作らない）。

| テーブル | migration | 主キー | 用途 |
|---|---|---|---|
| `digest_delivery` | `0026` | `(user_email, digest_date)` | その日のダイジェスト本文を送る権利。`origin` は CHECK で `scheduled` / `bulk` に限定 |
| `digest_notice` | `0027` | `(user_email, notice_kind, notice_date)` | お知らせ系 DM の送信権。現状の種類は `calendar_unlinked` だけ |

<!-- openwiki: broken internal link [/openwiki/data/rls-and-app-role.md] link "/openwiki/data/rls-and-app-role.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
どちらも FORCE RLS で本人行だけを `teamagent_app` ロール＋`app.user_email` GUC で読み書きし（[RLS と実行ロール](/openwiki/data/rls-and-app-role.md)）、claim と同じトランザクションで期限切れ（14 日）の本人行を掃除する。

`digest_notice` は planner が使う。カレンダー未連携の人へ、**月曜だけ**・`MORNING_DIGEST_BRIEF` が ON のときに 1 行の DM を出す（認可 URL は貼らず「この DM で『連携』と送って」へ誘導する）。claim は送信の前に取る。planner が再試行で再実行されても 2 通目は出ない。`digest_delivery` に相乗りしないのは、お知らせが先に印を取るとその人のその日のダイジェストが消えるため（しかも `origin` の CHECK で INSERT できない）。

## MCP 経由で呼ばれた場合との違い

`morning_digest` は orchestrator の factory で ToolSpec としても登録され（`USE_MORNING_DIGEST_TOOL`）、MCP ランタイムのタスク定義では有効になっている。skill は同じだが、周りの振る舞いは別物になる。

| 観点 | 定期タスク（本スクリプト） | MCP ツール |
|---|---|---|
<!-- openwiki: broken internal link [/openwiki/architecture/mcp-gateway.md] link "/openwiki/architecture/mcp-gateway.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| 起動 | EventBridge ルール / Lambda の RunTask | Slack の依頼 → OpenClaw → [MCP gateway](/openwiki/architecture/mcp-gateway.md) |
| 本人 | 対象者リストの email を metadata に入れる | caller claim で検証した Slack 利用者から resolver が解いた email |
| 下書き | `DRAFT_ON_DEMAND_ONLY=false`：高重要に作り置き（最大 5） | `DRAFT_ON_DEMAND_ONLY=true`：作らず、既存下書きの照合だけ |
| 配信 | runner が Block Kit に整形して本人 DM へ投稿 | 構造化結果をモデルへ返すだけ。DM 投稿・`digest_delivery`・リマインド登録は無い |
| Slack 返信漏れ | `MORNING_DIGEST_SLACK_UNREAD` が真のときだけ provider を渡す | factory が常に provider を渡す（provider 自体が fail-open） |
| 結果の扱い | — | 個人機微のため payload offload（署名 URL への退避）と進捗通知の対象外 |

## 失敗時の挙動と運用

<!-- openwiki: broken internal link [/openwiki/operations/hmac-keyring-and-rotation.md] link "/openwiki/operations/hmac-keyring-and-rotation.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- 起動時に `require_runtime_startup` が `mail_action` の HMAC 鍵束の durable state を確かめ、合わなければ起動しない（[HMAC 鍵束とローテーション](/openwiki/operations/hmac-keyring-and-rotation.md)）。鍵が不正なときは skill 側もボタンのトークンを発行しない。
- 1 人の失敗は封じ込め、全体は止めない。最後に `{"users","delivered","skipped","errors"}` の件数だけをログに出す。メールアドレス・件名・本文はログに出さない（マスクか件数だけ）。
- 専用ロググループ（保持 30 日）なので、`level=error` を既存の `ErrorCount` metric へ流す filter と、triage 不発（`matched=0`）を 1 件から鳴らす専用 alarm をこの tf で定義している。
- `MORNING_DIGEST_CONCURRENCY` が 1 より大きいとスレッドプールで並列に回し、Bedrock クライアントを先に作って共有する。
- 利用者ごとのトークン更新には connect（web 型）の OAuth クライアント ID / secret が要り、欠けるとメール・予定の収集が全件 0 になる。
- 手動検証は output `morning_digest_task_definition_arn` を使って run-task する。

## 代表的なテスト

- `tests/scripts/test_digest_personalized_delivery.py`: 送信済みの人は skill を走らせずに skip、配信失敗で印を戻す、planner の終日除外・1 人 1 予約・既定時刻なら予約しない・一括と cron の稼働日一致、未連携のお知らせは月曜 1 回・印が取れなければ送らない、pepper を secrets で注入する。
- `tests/scripts/test_reminder_notify_digest_branch.py`: `kind=digest` が 1 人分のタスクを起動し channel を運ばないこと、不正ペイロードは何もしないこと、RunTask 失敗は例外にすること。
- `tests/scripts/test_run_morning_digest_fargate.py`: Block Kit の描画、除外リスト、`oauth_tokens` 列挙で admin GUC を立てること。
- `tests/skills/test_morning_digest.py`: triage を id で結合すること、全件不一致で error、未連携で fail-closed、高重要だけに下書き、一斉配信に下書きボタンを出さないこと。
- `tests/adapters/test_digest_delivery_store.py`・`tests/adapters/test_digest_notice_store.py`・`tests/skills/pre_meeting_brief/test_send_window.py`。
