---
type: workflow
title: 朝ダイジェストのボタン処理
description: 朝ダイジェストの ✏️下書き・📅カレンダー登録・🗓日程候補・☑️確認済みボタンについて、mcp 側の HMAC 署名トークン（purpose・本人・期限）の発行と検証、呼ばれるツール mail_draft / calendar_event / schedule_propose / digest_ack の副作用、押し直しの二重実行防止（plugin の押下台帳と mcp の nonce の保持期限）をまとめる。
tags: [morning-digest, slack-buttons, hmac, mail-draft, calendar-event, schedule-propose, digest-ack, idempotency]
sources:
  - id: openwiki-source-6eef6c0b1f149a8267e36782
    resource: repo://infra/migrations/0025_digest_ack.sql
  - id: openwiki-source-a24133c7b43bc7a5e1ae4991
    resource: repo://infra/openclaw/caller-identity-plugin/dist/index.js
  - id: openwiki-source-e0948537ab0a1dd3b57bd3d1
    resource: repo://scripts/run_morning_digest_fargate.py
  - id: openwiki-source-5e419976c050623226fbb19e
    resource: repo://src/teamagent/adapters/digest_ack_store.py
  - id: openwiki-source-e1ac13b00972e1a14f0e2e04
    resource: repo://src/teamagent/adapters/gcalendar_client.py
  - id: openwiki-source-ccf3fdd7a33d5e9739bbaf67
    resource: repo://src/teamagent/hmac_keyring.py
  - id: openwiki-source-bdb37a42052532dadaa5a35d
    resource: repo://src/teamagent/mcp_gateway/caller_claim.py
  - id: openwiki-source-0d3fae9b930a020cd8a5b4e8
    resource: repo://src/teamagent/skills/calendar_event/skill.py
  - id: openwiki-source-111ed4b56320fde9ae4050d7
    resource: repo://src/teamagent/skills/digest_ack/skill.py
  - id: openwiki-source-184a982a6420441097ec2f18
    resource: repo://src/teamagent/skills/mail_draft/skill.py
  - id: openwiki-source-6466c22e9f2ff7bb0c76dcf0
    resource: repo://src/teamagent/skills/morning_digest/ack_token.py
  - id: openwiki-source-836b20e8c33a7d69ef68daba
    resource: repo://src/teamagent/skills/morning_digest/draft_token.py
  - id: openwiki-source-a1494bec222d692f1922a0cb
    resource: repo://src/teamagent/skills/morning_digest/event_token.py
  - id: openwiki-source-7c4e86c8d099c2810f3d0d38
    resource: repo://src/teamagent/skills/morning_digest/skill.py
  - id: openwiki-source-00e68d1a8141f269042cd1ae
    resource: repo://src/teamagent/skills/schedule_propose/skill.py
generated: { by: "claude-code", at: "2026-09-30T04:50:20.078Z" }
verified:
  - by: openwiki/0.6.1
    at: 2026-09-30T04:50:20.078Z
---

# 朝ダイジェストのボタン処理

## このページの範囲

朝ダイジェスト（[朝ダイジェスト](morning-digest.md)）の DM には、状態を変えるボタンが 4 種類ある。どのボタンも value に **mcp が発行した HMAC 署名トークン**を持ち、押されると同じ名前の MCP ツールがそのトークンを検証してから副作用を起こす。

このページは mcp 側（トークンの発行と検証・ツールの副作用・二重実行）を扱う。OpenClaw の caller-identity plugin がボタン押下を捕捉して束縛先ツールを直接呼ぶ流れ（`ACTION_BINDINGS`・本人 DM の確認・caller claim の鋳造）は [呼び出し元の証明とボタン束縛](../architecture/caller-identity-and-button-bindings.md)、鍵の世代管理は [HMAC 鍵束とローテーション](../operations/hmac-keyring-and-rotation.md) を参照。

たとえるなら、トークンは「本人の名前と有効期限が印字された引換券」。改ざんや他人の使用は券面の割り印（HMAC）で防げるが、券そのものに「使用済み」の穴は開かない。1 回きりにしているのは窓口側の台帳（後述）である。

## 4 つのボタンと呼ばれるツール

ボタンは `scripts/run_morning_digest_fargate.py` の `_reply_buttons` / `_ack_all_blocks` / `_slack_handoff_card_blocks` が組み立てる。`action_id` がそのまま呼ばれるツール名になる。

| ボタン | ツール（action_id） | value のトークン | ツールの引数（schema 上限） | 描画される条件 |
|---|---|---|---|---|
| ✏️ 下書きを作成 | `mail_draft` | draft トークン | `draft_token`（400） | 本人が To にいる・一斉配信でない・そのスレッドに下書きがまだ無い |
| 📅 {日時} に登録 | `calendar_event` | event トークン | `event_token`（500） | 確定した未来の会議を抽出・To に本人・`MORNING_DIGEST_CALENDAR_BUTTON=1` |
| 🗓 日程候補を提案 | `schedule_propose` | draft トークン（✏️ と同じ値） | `schedule_token`（400） | `scheduling_request`・draft トークンあり・`MORNING_DIGEST_SCHEDULE_BUTTON=1` |
| ☑️ 確認済みにする／☑️ 全部確認した／↩︎ 取り消す | `digest_ack` | ack トークン | `ack_token`（2000） | `MORNING_DIGEST_ACK_FILTER=1`（トークン発行）かつ `MORNING_DIGEST_ACK_BUTTON=1`（描画） |

ツール側も `USE_MAIL_DRAFT_TOOL` / `USE_CALENDAR_EVENT_TOOL` / `USE_SCHEDULE_PROPOSE_TOOL` / `USE_DIGEST_ACK_TOOL` がすべて既定 OFF で、ON のときだけ登録される（`src/teamagent/orchestrator/factory.py`、[ツール登録と機能フラグ](../architecture/tool-registry-and-feature-flags.md)）。📅・🗓・☑️ の描画フラグは「押下先ツールが本番で有効になってから ON」にする運用で、順序を誤ると押しても反応しないボタンになる。

## トークンの発行

発行は朝ダイジェストの生成時（`MorningDigestSkill`）で、`src/teamagent/skills/morning_digest/` の 3 モジュールが担う。

| 種類 | モジュール | HMAC purpose | payload |
|---|---|---|---|
| draft | `draft_token.py` | `teamagent.mail-action.draft` | `v=2, typ=draft, t`（thread_id）`, o, e` |
| event | `event_token.py` | `teamagent.mail-action.event` | `v=2, typ=event, s`（開始）`, n`（終了）`, l`（件名）`, o, e` |
| ack | `ack_token.py` | `teamagent.mail-action.ack` | `v=2, typ∈{ack, ackall, unack}, n`（ハッシュ済み項目鍵の配列）`, o, e` |

共通の形:

- 文字列は `base64url(JSON payload) + "." + base64url(署名)`。署名は HMAC-SHA256 の先頭 16 バイト。
- `o` は所有者ハッシュ `sha256("owner:" + 小文字化した email)` の先頭 16 hex。`e` は発行時刻＋TTL の失効時刻（epoch 秒）。
- 署名は `HmacKeyring.sign` が **purpose と payload を長さ付きで連結した文**に対して行う（`_domain_separated_message`）。purpose が違えば同じ payload でも署名が一致しないので、draft トークンを ack や event として使うことはできない（テストで相互検証不可を固定）。
- 署名に使う鍵は `MAIL_ACTION_HMAC_SECRET`（発行用の唯一の主鍵）。ローテーション中の旧鍵は検証専用。
- TTL は `load_mail_action_token_ttl_s`。`MAIL_ACTION_TTL_S` が無ければ上限の 24 時間、あれば ASCII 10 進の 1〜86400 のみ有効で、不正値なら `None` を返し発行自体が止まる（＝ボタンが出ない）。↩︎ 取り消し用の `unack` だけは明示的に 1 時間。
- どこかで失敗すると encode は例外を投げずに `None` を返し、その行のボタンは描画されない（fail-closed）。

種類ごとの注意:

- **event**: 件名入りで value 上限 500 字（`EVENT_TOKEN_MAX_LENGTH`、ツール schema と plugin の束縛表で同じ値）。JSON を `ensure_ascii=False` で書き、収まらなければ件名を末尾から削る。件名を空にしても超えるなら発行しない。
- **ack**: 生の Gmail thread_id や Slack channel は載せず、`DigestAckStore.item_key` による 16 hex の鍵と新着判定の基準値（anchor）だけを載せる。「全部確認した」は最大 30 件・1900 バイトまでで、超えると一括ボタンだけ出ない。
- **draft / event は秘匿ではない**。payload は base64url なので復号すれば thread_id や日時・件名が読める。HMAC が守るのは改ざん・鋳造・他人使用の防止で、本人の DM にだけ置かれる前提である。`draft_token.py` 冒頭の説明は「生の thread_id を value に出さない」と読めるが、実際は符号化されているだけ（`event_token.py` と `ack_token.py` の説明は秘匿でないことを明記している）。

発行できる状態かは `scripts/check_mail_action_button_key.py` で確かめられる。鍵束と TTL が読めるかを理由コードだけの JSON で出し（鍵の値・環境変数名は出さない）、発行できれば終了コード 0、できなければ 2。鍵が壊れていても実行時は「機能 OFF」と区別がつかない（どのローダーも `None` を返すだけ）ため、その切り分け用に置かれている。

## 検証（ツールの入口）

どのツールも `run()` の冒頭で同じ順に確かめる。

1. `ctx.metadata["user_email"]` が無ければ `PermissionError`（本人限定・fail-closed）。この email は mcp gateway が caller claim で検証した Slack 利用者から解決したもので、モデルが書いた引数ではない（[MCP gateway](../architecture/mcp-gateway.md)）。
2. `decode_*_token(token, user_email)` が次をすべて満たしたときだけ中身を返す。
   - 鍵束が読める。形式が `payload.署名` で、`v` と `typ` がその種類のもの。
   - その種類の purpose で署名が一致する（`HmacKeyring.verify` は有効な全鍵と定数時間で比較する）。
   - `e` が現在時刻より後（境界ちょうどは失効）。
   - `o` が押した本人の email から計算した所有者ハッシュと一致する。他人のトークンは本人確認に通っても失効扱いになる。
   - ack だけは追加で、フィールド集合の完全一致・base64url が正規の表現であること・項目数（`ack` は 1 件、`ackall` / `unack` は 1 件以上）・項目の形を検査する。
3. `None` なら `error="expired"` と「このボタンは無効です（期限切れ/不正）。最新のダイジェストから操作してください。」を返し、何もしない。

draft / event は、旧形式（`v` と `typ` が無い payload）をローテーション期間の旧鍵（`verify_legacy_previous`）でだけ受ける。ack は新しい機能なので旧形式の分岐を持たない。

`calendar_event` は自由文でも予定を登録できるツールだが、`event_token` が空でなければ**必ずボタン経路**になり、壊れたトークンを自由文の引数へ落とすことはない（署名済みの日時がモデル由来の値に置き換わる経路を作らない）。

トークン自体には nonce が無い。期限内であれば、検証は何度でも通る。1 回きりにする仕組みはツールの外側にある（次の次の節）。

## ツールごとの副作用

| ツール | 外部への書き込み | 冪等性と失敗時 |
|---|---|---|
| `mail_draft` | Gmail の下書き 1 件（`drafts.create` のみ。送信はしない）。本文は LLM が書き、既定では Reply-All で CC を付ける（`MORNING_DIGEST_REPLY_ALL`） | スレッドに下書きがあれば `already`（`MORNING_DIGEST_DEDUPE_DRAFTS`、既定 ON）。本人が To にいなければ `not_addressed`。作れたときだけ日次上限（1 人 10 件）を消費 |
| `calendar_event` | 本人の primary カレンダーに予定 1 件。`sendUpdates="none"` 固定・attendees を受け取らない | event_id を「所有者×開始×終了」から導出（`stable_event_id(kind="confirm")`）。Google の 409 を「登録済み」として返す。`calendar.events` スコープが無ければ `reauth_needed` |
| `schedule_propose` | 候補日入りの返信下書き 1 件（LLM を使わない決定的な本文）＋候補ごとの仮予定（`tentative`・`transparent`＝自分の空き枠を潰さない） | 下書きは `mail_draft` と同じ重複検査で、既にあれば `already` を返しホールドも作らない。ホールドは `kind="hold"` の event_id で 409 を成功扱い。書き込みスコープが無い旧連携は下書きだけ作る |
| `digest_ack` | 本人の `digest_ack` 行だけ（Google・Slack API は呼ばない） | `ack` / `ackall` は UPSERT（保持 30 日）、`unack` は DELETE。書けた件数が 0 なら成功文を返さない（`store_failed`） |

補足:

- **`mail_draft`** の本体は `MorningDigestSkill.generate_draft_for_thread`。スレッドを取り直し、最新メッセージを返信の基準にする。日次上限はプロセス内の辞書で数えるため、ECS タスクが複数あれば全体では上限×タスク数まで通りうる（コードのコメントも「暴走の頭打ち」としか保証していない）。詳しくは [メール系ツール](mail-tools.md)。
- **`calendar_event`** の event_id は件名を含まないので、同じ日時なら翌日のダイジェストから押しても同じ id になり二重登録にならない。代わりに、UI から手動で消した予定を同じボタンで入れ直そうとしても 409（「登録済み」）になる。
- **`schedule_propose`** は、本人カレンダーの freebusy（現在から 9 日）から `find_slots` で空き枠を出す。freebusy の API 障害は「空き枠なし」と区別して `freebusy_failed` を返す。ホールド作成の失敗は下書きの成功を取り消さない。
- **`digest_ack`** は PostgreSQL の `digest_ack`（migration 0025）へ `teamagent_app` ロールと `app.user_email` で接続し、RLS（`FORCE ROW LEVEL SECURITY`）で本人行に限って読み書きする（[RLS と実行ロール](../data/rls-and-app-role.md)）。ack に成功すると 1 時間有効の `unack` トークンを発行し、plugin が押した本人の DM への結果投稿に「↩︎ 取り消す」ボタン（同じ `digest_ack`）として添える。朝ダイジェストは、確認済みでもその後に新着があれば（anchor が進んでいれば）再び表示する。確認状態の読み取りに失敗したときは 1 件も隠さない（fail-open。書き込みの失敗は件数 0 で伝える）。

## 押し直しと二重実行

同じボタンの押し直しは次の段で止める。

1. **plugin の押下台帳**（`buttonPressLedger`）: 押下の指紋（押した人・team・会話・メッセージ ts・thread・`action_id`・value の SHA-256）をキーに、24 時間＋10 分保持する。OpenClaw プロセスのメモリ上の `Map` なので、再起動で消える。上限は 5000 件で、超えると古いものから捨てる。台帳にある押下の押し直しは実行せず、押した本人にだけ状態に応じた 1 行を一時表示で返す（詳細は [呼び出し元の証明とボタン束縛](../architecture/caller-identity-and-button-bindings.md)）。
2. **mcp の caller claim nonce**: 押下の claim の nonce は指紋から HMAC で決まるため、同じ押下は同じ nonce になる。mcp は DynamoDB へ `attribute_not_exists(nonce)` の条件付き書き込みで記録し、2 回目を `CALLER_IDENTITY_REJECTED` で拒否する。記録の保持期限（TTL 属性 `expires_at`）は「消費した時刻＋25 時間」（`CALLER_CLAIM_REPLAY_RETENTION_SECONDS`）で、ボタントークンの最長寿命 24 時間より長い。そのため OpenClaw の再起動などで 1 段目が消えても、トークンが有効な間の押し直しは 2 段目で止まり、TTL で項目が消えるのはトークンが失効した後になる。
3. **ツール側の冪等性**: 上の表のとおり（予定・ホールドは固定 id、下書きは既存下書きの検査、ack は UPSERT）。

2026-09-29 の #476 までは、2 段目の `expires_at` に claim の `exp`（発行から最長 60 秒）が入っていた。TTL の削除時刻は保証されないため、再起動で 1 段目が消えた後に DynamoDB の項目が先に削除されると、期限内のボタンの押し直しでツールがもう一度実行されうる状態だった（`mail_draft` と `schedule_propose` は、本人が下書きを送信・削除した後なら下書きがもう一度作られる）。現在の保持期限はこれを塞ぐための値。

### 旧 worker の経路

旧 Socket Mode worker（`src/teamagent/runtime/slack_bot.py`）にも同じ `action_id` のハンドラ（`@app.action("mail_draft")` など）が残っている。こちらは押した Slack 利用者から email を引き、トークンの検証と処理をプロセス内で直接行う（`calendar_event` は `CalendarEventSkill` をそのまま呼ぶ）。caller claim も plugin の押下台帳も通らない。この経路が動く構成では、押し直しを止めるのはトークンの期限とツール側の冪等性だけになる。

## 変更するときの注意

- トークンの上限を変えるときは、ツール schema の `max_length`、朝ダイジェストの schema、plugin の `ACTION_BINDINGS.maxLength` を揃える（📅 の 500 はテストで 3 か所の一致を固定している）。
- 新しい種類のトークンには専用の `HMAC_PURPOSE_*` を足し、既存の purpose を使い回さない。
- 生の ID・件名・本文・トークンの値はログに出さない。どのツールも理由コードと件数だけを記録する方針。

## テスト

- `tests/skills/test_draft_token.py`・`tests/skills/test_ack_token.py`: 往復、purpose の相互検証不可、所有者違い、失効境界、改ざん、鍵なし時の fail-closed、TTL 設定の不正値、`unack` の 1 時間。
- `tests/skills/calendar_event/`・`tests/skills/schedule_propose/`・`tests/skills/mail_draft/`・`tests/skills/test_digest_ack.py`: 各ツールの副作用・冪等・失敗文。
- `tests/adapters/test_digest_ack_store.py`: 項目鍵の決定性（email 正規化込み）と、DB 接続障害時に読み取り・掃除は fail-open、ack / unack は 0 件を返すこと。
- `tests/test_openclaw_action_bindings.py`・`tests/test_openclaw_button_direct.py`: plugin の束縛と直接実行を本物の MCP サーバまで通した e2e。
