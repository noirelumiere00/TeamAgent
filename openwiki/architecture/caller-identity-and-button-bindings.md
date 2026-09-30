---
type: architecture
title: 呼び出し元の証明とボタン束縛
description: OpenClaw の caller-identity plugin が Slack の送信者を署名付き claim にして MCP へ渡し、mcp_gateway の caller_claim が検証する仕組み。朝ダイジェストのボタンを ACTION_BINDINGS で 1 ツールへ束縛し、AI を通さず直接実行する流れも扱う。
tags: [openclaw, security, caller-claim, hmac, slack-buttons, mcp]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T07:51:05.076Z
sources:
  - id: openwiki-source-a24133c7b43bc7a5e1ae4991
    resource: repo://infra/openclaw/caller-identity-plugin/dist/index.js
  - id: openwiki-source-11b6c3c162105aea7c4bba90
    resource: repo://infra/openclaw/openclaw.config.json5
  - id: openwiki-source-bdb37a42052532dadaa5a35d
    resource: repo://src/teamagent/mcp_gateway/caller_claim.py
  - id: openwiki-source-1fdde611c13aba4b68a5ff42
    resource: repo://src/teamagent/mcp_gateway/server.py
  - id: openwiki-source-1727ffb2021970a3529945dd
    resource: repo://tests/test_openclaw_button_direct.py
generated: { by: "claude-code", at: "2026-09-29T07:51:05.076Z" }
---

# 呼び出し元の証明とボタン束縛

## 何を守っているか

OpenClaw（Slack 受け口）から MCP gateway への接続は bearer（`TEAMAGENT_MCP_BEARER`）で認証されるが、これは「OpenClaw というワークロード」を証明するだけで、**どの Slack 利用者の依頼か**は証明しない。モデルが引数に書く `slack_user_id` はただの申告なので、そのまま信じるとモデルの誤りやプロンプト注入で他人のメール・カレンダーに触れてしまう。

そこで 2 つの部品が組になっている。

| 部品 | 場所 | 役割 |
|---|---|---|
| caller-identity plugin | `infra/openclaw/caller-identity-plugin/dist/index.js` | Slack の受信イベント（送信者・team・会話・thread）を run に束縛し、ツール呼び出しのたびに HMAC 署名した claim を `_user_context.caller_claim` に入れる |
| caller claim 検証 | `src/teamagent/mcp_gateway/caller_claim.py` | 署名・宛先・期限・引数ハッシュ・one-use nonce を検証し、申告された `_user_context` と claim の一致を確かめる |

たとえるなら、OpenClaw の bearer は「社員証」、caller claim は「この 1 回の用件のために窓口で押した割り印」。社員証だけでは誰の用件かは分からない。

## claim の形と検証

plugin の `mintCallerClaim` は `payload.signature` の compact 形式を作る。payload は `v, iss, aud, sub, team, channel, thread, message, session_sha256, run_id, tool_call_id, tool, arguments_sha256, nonce, iat, exp` の固定フィールドで、署名は `TEAMAGENT_CALLER_CLAIM_SECRET` による HMAC-SHA256。有効期限は発行から 60 秒（plugin の `CLAIM_TTL_SECONDS`）。

mcp 側 `CallerClaimVerifier.verify` は次を順に確かめ、1 つでも外れれば `CallerClaimError` を投げる。

1. 署名が一致する（`hmac.compare_digest`）。
2. フィールド集合が契約と完全一致し、重複キーが無い。`iss=teamagent-openclaw`・`aud=teamagent-mcp`。
3. `team` が本番の `SLACK_TEAM_ID` と一致する。
4. `tool` が実際に呼ばれたツール名と一致する。
5. 期限内（最大寿命 60 秒・時計ずれ 5 秒）。
6. 申告された `_user_context` の `slack_user_id` / team / channel / thread が claim と一致する。
7. 引数全体の正準ハッシュ（`canonical_request_sha256`、Node 側と同じ正準化）が `arguments_sha256` と一致する。＝署名後に引数を書き換えると通らない。
8. nonce を replay store に記録する。本番は DynamoDB の条件付き書き込み（`attribute_not_exists`）で、ECS タスクが複数あっても同じ claim は 1 回しか通らない。DynamoDB のエラーは fail-closed。

<!-- openwiki: broken internal link [/openwiki/architecture/mcp-gateway.md] link "/openwiki/architecture/mcp-gateway.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/integrations/slack-identity-and-oauth.md] link "/openwiki/integrations/slack-identity-and-oauth.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`from_env()` は caller claim の秘密が MCP bearer と同じ値だと起動を拒否する。検証器が無いのに identity 解決や会社共有グループが有効な場合、`server._verify_caller` は `CALLER_IDENTITY_CONFIGURATION_ERROR` を返して止まる（fail-closed）。検証に失敗した依頼は `missing_verified_caller` の本人確認拒否として利用者に返る。以降の本人解決は [MCP gateway](/openwiki/architecture/mcp-gateway.md) と [Slack の本人確認](/openwiki/integrations/slack-identity-and-oauth.md) を参照。

## 通常メッセージの経路

1. `message_received` で plugin が送信者・会話を記録し、agent run に束縛する（`ingressByRun`）。DM は受信側で `DM:<U…>`、run 側で `D…` と名乗るため、「押した／送った本人の DM」に限って別名として同一視する。
2. `before_tool_call` で `signToolCall` が、run の束縛・session key・channel が一致し、同じ `run_id × tool_call_id` がまだ使われていないことを確かめてから claim を付ける。合わなければツール呼び出しを block する（理由コードをログに残す。本文・URL・識別子はログに出さない）。
3. 引数が二重に包まれて届くモデルの癖は、検査の前に決まった段数だけ剥がす（`unwrapToolArguments`）。剥がせなければ block。

### 連携依頼・動画 URL の多層防御

同じ plugin には、モデルがツールを呼ばずに自作回答する事故への対策もある。

- **連携（oauth_connect）**: 層1 = 短い連携依頼（正規化後 12 文字以下の「連携」「接続」など）を検出したら、モデルを通さず `oauth_connect` を直接呼んで返す。層2 = `before_agent_finalize` で「ツール 0 回×短い連携依頼」なら再パスを要求。層3 = それでも 0 回なら定型文に置き換える。
- **動画 URL**: 層2 だけを置く（分析内容や引数はモデルが決める必要があるため、層1・層3 は無い）。

## 朝ダイジェストのボタン束縛（ACTION_BINDINGS）

朝ダイジェストの 4 種のボタン（✏️ `mail_draft`・📅 `calendar_event`・🗓 `schedule_propose`・☑️ `digest_ack`）は `ACTION_BINDINGS` 表で「action_id → 呼べる 1 ツール」に固定されている。各行が持つ項目は次のとおり。

| 項目 | 意味 |
|---|---|
| `tool` | その押下で署名してよい唯一のツール |
| `tokenParam` | 押下時に捕捉した value（mcp が発行した署名付きトークン）で上書きする引数名（`draft_token` / `event_token` / `schedule_token` / `ack_token`） |
| `maxLength` | value の上限（各ツール入力 schema の max_length と一致。`mail_draft` だけ 160） |
| `tokenTypes` | トークン payload の `typ`（例: 📅 は `event`、☑️ は `ack` / `ackall` / `unack`）。`mail_draft` は形だけを見る |
| `outsideAction` | メッセージ由来の run でそのツールが呼ばれたとき。`deny` = 署名しない、`blank_token` = 署名するがトークン引数を空にする（`calendar_event` は自由文でも予定登録できるため） |
| `resultLink` / `undoToken` / `pendingText` / `texts` | 直接実行したときの返答文面（リンク表示名、☑️ の取り消しボタン、処理中の一時表示、失敗時の文） |

表は起動時に自己検査され、1 対 1 でない（同じツールが 2 行ある等）と plugin は起動に失敗する。

### 直接実行の流れ

本番は `heartbeat.every: "0m"` のため、押下の後に AI の run が起きない。そこで plugin がボタン押下を捕捉した時点で `{handled: true}` を返し、自分で束縛先ツールを呼ぶ（`executeButtonAction`）。

```text
Slack ボタン押下
  └─ interactive handler（action_id ごとに登録）
       ├─ value の形・上限・typ を検査（合わなければ「最新のダイジェストから押して」）
       ├─ 押された会話が押した本人の DM か Slack に確認（違えば本人 DM へ案内だけ）
       ├─ ✏️・🗓 は本人だけに見える「作っています」を一時表示
       ├─ mintCallerClaim（nonce は押下の指紋から HMAC で決定）→ MCP へ tools/call
       └─ 結果（ツールの message）を押した本人の DM へ投稿
```

失敗時の扱いは「MCP がツールを受け取ったか」で分かれる。

- **受け取る前に失敗**: 何も実行されていない（nonce も未消費）ので押下台帳から外し、「もう一度押して」と返す。
- **受け取った後に途切れた**: 実行されたか分からないので台帳は外さず、「入っていなければ頼み直して」と返す。同じ押下はプラグイン台帳と MCP の one-use nonce の両方で止まるため、二重登録にならない。
- MCP が `CALLER_IDENTITY_REJECTED` を返した場合も「分からない」扱い（nonce 再生と本人確認失敗を応答から区別できないため）。

<!-- openwiki: broken internal link [/openwiki/workflows/digest-buttons.md] link "/openwiki/workflows/digest-buttons.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/operations/hmac-keyring-and-rotation.md] link "/openwiki/operations/hmac-keyring-and-rotation.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
MCP 側のトークン検証（HMAC・purpose・本人・期限・one-use nonce）は plugin の束縛とは独立に残っている＝二重の守り。トークンそのものの発行と検証は [朝ダイジェストのボタン処理](/openwiki/workflows/digest-buttons.md)、鍵の世代管理は [HMAC 鍵束とローテーション](/openwiki/operations/hmac-keyring-and-rotation.md) を参照。

## 変更するときの注意

- 新しいボタンを足すときは、`ACTION_BINDINGS` への行追加、OpenClaw の `toolFilter.include` と `effective-tool-scope.json` への登録、`maxLength` とツール schema の一致を揃える（runtime contract テストがこれらを突き合わせる）。
- 上流 OpenClaw（2026.7.1）は system event の文字列を 160 字で切る。長いトークンはモデル経由では壊れるので、ボタン由来の値は必ず捕捉済みの完全な value で上書きする設計になっている。
- 秘密の値はログにも wiki にも出さない。plugin のログは形・段数・理由コードだけを出す方針（G7）。

## テスト

- `tests/test_openclaw_action_bindings.py`: 束縛表が 1 対 1 で全 namespace を登録すること、📅 が DM で 1 回だけ完全なトークンで走ること、他ツールを認可できないこと、同じ件名の予定 2 行を取り違えないこと、他人の DM では使えないこと等。
- `tests/test_openclaw_button_direct.py`: 本物の `dist/index.js` と本物の HTTP MCP（`scripts/run_mcp_http_server.py` の `build_app`）を立て、Slack だけを本番の失敗の形で偽装して、二重押し・再起動後の再押下・MCP 停止・時間切れなどを端から端まで確かめる。
- `tests/caller_claim_testkit.py`: テストで claim を鋳造する補助。
