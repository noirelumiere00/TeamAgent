# ADR: Hermes Agent 段階導入設計（hermes_migration_design）

- Status: **Accepted for docs（PR1）— 実装は PR ごとに個別承認**
- 作成日: 2026-08-18（Session 1 監査 + Session 2 再検証・敵対審査を反映した補正版）
- 改訂履歴: **2026-09-25 v1 本人メモ（決裁者承認）** — Hermes を「覚える係」に限定し、機械検査後の自動反映、記録つき管理者閲覧、1 対 1 DM 限定、凍結・削除を裁定
- 基準: dev @ 95d45a8。本文中の file:line は同 commit 時点。**2026-09-25 改訂分（§9・§10・§10b・§21 ほか v1 の記述）の file:line は dev @ a8f2416 時点**。同じ PR で改訂した文書どうしは行番号でなく見出しで参照する
- 番号の読み方: **v1（DM 本人メモ）の工程は M0〜M11**。旧番号 `PR1 / PR2-A0 / PR2-A1 / PR2-B / PR3〜PR8 / PR-R` は v2（汎用 Hermes）の番号で、v1 の工程には使わない
- 関連: [hermes_implementation_plan.md](hermes_implementation_plan.md)（PR 分割・テスト戦略）

---

## 1. Executive Summary

> **v1（DM 本人メモ）の注記**: 本節から §8、§11〜§18 の `run_hermes_agent`・delegated claim・Hermes の MCP client 利用・既存 ECS task role での実行は v2（汎用 Hermes）の記述である。v1 の Hermes は別タスク・最小 IAM・MCP へ届かない「覚える係」で、境界は §10b を正とする。

TeamAgent は現在、**Slack → OpenClaw（外殻）→ TeamAgent MCP Gateway（信頼境界）→ Skill Registry → 会社データ/API → AWS Bedrock Claude** という構成で本番稼働している。本 ADR は、この構成を**一切壊さず**、Hermes Agent（NousResearch 製・**OpenClaw からの移行を公式サポートする** agent runtime）を **`run_hermes_agent` という dark な MCP tool** として境界の内側に段階導入する設計を定める。

位置づけを一文で言うと:

> **TeamAgent を Hermes で作り直すのではない。TeamAgent が自前で持っている Agent Runtime 部分（`orchestrator/sdk_runner.py` の bounded tool loop）に、Hermes という選択肢を追加する。** Security / MCP / Skills / RAG はすべて既存のまま残す。

比較対象も「Claude Agent SDK vs Hermes」ではなく「**TeamAgent 自前 agent loop vs Hermes runtime**」である（Claude Agent SDK は 2026-07-17 の `6589e79` で `AsyncAnthropicBedrock` による自前実装へ置換済み。§2）。

## 2. Current State（一次情報で検証済みの事実）

- Slack 前面 = OpenClaw 2026.7.1（Socket Mode・Haiku 4.5 外側ループ・tools profile minimal・exec/fs/browser 封鎖・digest 固定イメージ）
- OpenClaw → teamagent-mcp は streamable-http :8787 + Bearer + **one-use HMAC caller claim**（`mcp_gateway/caller_claim.py`・16 フィールド厳密契約・TTL 60s・DynamoDB conditional PutItem で replay 拒否）
- MCP Gateway（`mcp_gateway/server.py`）が identity 解決・RLS・fail-closed・監査の単一境界
- L1 Skill 42 クラス / factory 最大 40 ToolSpec / OpenClaw へは toolFilter.include で 35 本公開
- **L2 `run_agent` = anthropic Python client（`AsyncAnthropicBedrock`）による自前 bounded tool loop**（`orchestrator/sdk_runner.py`）。`USE_AGENT_ORCHESTRATOR` で dark（既定 OFF・OpenClaw include 外）。
  - Claude Agent SDK は `6589e79`（2026-07-17 "split core and media runtimes"）で置換済み。core イメージでは禁止依存として能動ブロック（`Dockerfile.teamagent-mcp:156-158`・`tests/infra/test_dockerfile_teamagent_mcp.py:205`）。`requirements-worker.lock:344` に残る `claude-agent-sdk==0.2.87` は EC2 worker 向けの未使用残骸で、解消は既存 draft PR#263 が対応
  - `sdk_runner.py` / `run_sdk_agent` / `SdkAgentResult` という命名は SDK 時代の残り（実体は Bedrock client）
- データ層: RDS PostgreSQL + pgvector（RLS fail-closed）・S3・Slack/Drive/Sheets ingest・per-user Google OAuth TokenStore（KMS EncryptionContext=user_email）
- **会社共有モード（§G/§U）の実挙動**: `TEAMAGENT_SHARED_COMPANY_DOMAINS` 設定時も resolver は必須で、`metadata.user_email` には **resolver が解決した本人の実 email が入り `identity_verified=True`**（`server.py:478-484`）。「user_email=None」は §U 以前の記述（`identity.py:100-105` の docstring は stale）。この事実は §7 のツールポリシー設計の前提になる

## 3. Problems（現構成の限界）

| # | 問題 | 根拠 |
|---|---|---|
| P1 | **経験が蓄積されない**: OpenClaw セッションは会話履歴 20 件制限。ユーザーの好み・作業パターン・過去の修正が毎回失われる | `openclaw.config.json5` `messages.groupChat.historyLimit: 20` |
| P2 | **Planning 能力の天井**: 外側ループは Haiku の軽量 tool 選択。多段計画は自前 loop（8 turn 上限）どまりで、これも dark | `agent_config.py` / `sdk_runner.py` |
| P3 | **Personal 化の器が無い**: identity は解決されるが、それを使う「本人専用の agent 状態」を持つ層が存在しない | profile store 該当なし |
| P4 | **Specialist 分化の器が無い**: 全ツールが flat な ToolSpec リストで、役割別の memory / 最小権限 subset を持てない | `factory.build_production_tools()` |

## 4. Why Hermes

- **OpenClaw からの移行を公式サポートする** agent runtime（upstream に設定・Memory・Skills 等の import 機能と "Migrating from OpenClaw" ガイド）＝移行リスクが小さい選択肢。※公式に確認できるのは移行機能の提供までで、「直系後継」といった系譜は公式の主張ではないため本 ADR では表現しない
- 必要な 3 能力が標準装備: (a) MCP client（TeamAgent 境界をそのまま使える） (b) agent-curated Memory（MEMORY.md/USER.md・FTS5 session search） (c) Skills（agentskills.io 標準）
- AWS Bedrock を **native provider** としてサポート（**boto3 / IAM 認証**）→ 既存 ECS task role で動き、新しい API key・外部送信先が増えない。具体的 transport は版により異なる（Claude 系を AnthropicBedrock 経路・非 Claude 系を bedrock_converse 経路に分ける実装がある）ため **Hermes runtime 実装に追従**し、本 ADR では固定しない
- 自前 agent loop（意図的にミニマル）と比較して、memory/skills/session という「経験の器」を最初から持つ

## 5. Why not Big Bang

- 現本番は 16 名パイロットの生命線（Slack Bot 停止は営業業務直撃）
- OpenClaw 側には署名リリース鎖・契約テスト・SOUL/config/scope 台帳が焼き込まれており、置換は「イメージ 1 個の差し替え」ではなくリリース基盤全体の再認証
- 2026-07-31 の OpenClaw 載せ替えで実証済みの教訓「health check はすり抜ける・実 Slack 1 往復まで完了と言えない」
- よって **追加 → 併走 → 比較 → 縮退判断** の順でしか進めない

## 6. Target Architecture

> **2026-09-25 v1 の優先境界**: 下図は後続の汎用 Hermes 構想を含む。最初に提供する「DM 本人メモ v1」では Hermes は学習専用であり、`run_hermes_agent`、callback、delegated claim、MCP client は使わない。v1 の実効構成は §10b の図を正とする。

```
Slack (Socket Mode)
   ↓
OpenClaw（Edge / Slack shell / fast path — 現行のまま）
   ↓ streamable-http :8787 + Bearer + one-use HMAC caller claim
TeamAgent MCP Gateway（Security Authority — 不変）
   │ server-resolved identity（既存 _verify_caller → _resolve_metadata）
   ▼
run_hermes_agent（新 MCP tool・USE_HERMES_ORCHESTRATOR gate・OC toolFilter 非公開 = dark）
   ↓ Gateway→Hermes: 専用SG + TLS + TEAMAGENT_HERMES_INGRESS_BEARER
Hermes Runtime（ECS・Personal Agent / Memory / Specialist）
   │ short-lived delegated session claim
   │ + per-call MAC（K_session）
   │ + arguments binding（既存水準から退行させない）
   ▼
Hermes 専用 Callback Boundary（/mcp とは別 route・別 bearer・別鍵）
   ├ Identity 再解決（server-side resolver・claim は照合値）
   ├ server-side tool policy（allowlist ∩ claim ∩ feature flag・denylist 優先）
   ├ call budget / nonce / replay（専用 DynamoDB で線形化）
   └ RLS metadata 再構築（build_rls_metadata 経由のみ）
          ↓
   Existing Skills → Company Data（RDS+pgvector / S3 / GWS / Slack / Connect RAG）
          ↓
   AWS Bedrock Claude（Haiku=routing / Sonnet=synthesis）
```

**やってはいけない形（本設計で明示的に禁止）**: `Hermes → 既存 MCP bearer → /mcp`。
`toolFilter.include` は **OpenClaw クライアント側の設定**であり、サーバ側は factory 登録の有無でしか絞っていない。「OpenClaw だけが 8787 に到達できる」という SG 前提の上に成り立つ第 2 ゲートなので、Hermes に既存 bearer を渡すとこのゲートが丸ごと消える（factory 登録済みの mail_draft 等が呼べてしまう）。

| レイヤ | 役割 | 持つもの | 持たないもの |
|---|---|---|---|
| OpenClaw | Edge / Slack shell / fast path | Slack tokens・MCP bearer・claim 署名鍵 | 会社データ・DB・Google token |
| MCP Gateway | **Security Authority** | RLS・identity・claim 検証・監査・tool policy | — |
| Hermes（DM 本人メモ v1） | 学習係（本家 memory tool で清書） | ingress bearer・TLS 鍵 | MCP・RDS/pgvector・DynamoDB・Slack・OAuth token・既存 bearer・callback/delegated claim |
| Hermes（後続） | Personal Agent / Memory / Experience | 本人スコープ memory・短命 session claim + K_session | RDS/pgvector・OAuth token・Slack token・既存 MCP bearer |
| Bedrock Claude | Intelligence | — | — |

## 7. Security Boundary — Delegated Session Claim 設計（敵対審査反映版）

**v1 適用外**: DM 本人メモ v1 は MCP への戻り経路を持たず、delegated claim、callback、`HMAC_PURPOSE_HERMES_DELEGATION`、K_session、専用 DynamoDB を実装しない。これらは v2（汎用 Hermes）で再レビューしてから導入する。v1 の M0〜M11 には含まれない。v1 の Hermes task role は jp. Haiku 推論プロファイルへの `bedrock:InvokeModel` と logs だけを Allow し、secretsmanager・kms・rds・dynamodb・s3 は明示 Deny とする。execution role が読める secret は ingress bearer と TLS 鍵の 2 本だけである。

### 7.1 既存 caller claim から**退行させない**不変条件

既存機構（`caller_claim.py`）の強度: 16 フィールド厳密契約（過不足拒否・重複キー拒否）・HMAC-SHA256（署名検証が parse より前）・TTL 60s（verifier 構築時上限 300s）・one-use nonce（DynamoDB conditional PutItem が認可の線形化点・resolver より前）・`arguments_sha256` による「1 claim = 1 tool の 1 引数の 1 回」束縛・bearer と claim 鍵の相互排他。

delegated 経路でも次を維持する:

1. **request binding（arguments_sha256）を退行させない**（§7.3 の per-call MAC で実現）
2. **one-use / 予算の線形化点は DynamoDB conditional write**（§7.4）
3. **既存 `CallerClaimVerifier` の契約（60s/300s・16 field）には一切触れない** — delegated 用は**別 verifier クラス**
4. Hermes 申告の email/groups/role/profile_id は**認可に使わない**（照合値のみ）
5. `user_role` は常にサーバ導出 `"member"`（`build_rls_metadata` の単一変換点を delegated 経路にも強制）

### 7.2 Delegated Session Claim（session capability）

`run_hermes_agent` 受理時（既存 `_verify_caller` → `_resolve_metadata` を通過した後）に Gateway が mint し、セッション開始時に一度だけ Hermes へ渡す。

```jsonc
{
  "v": 1,
  "iss": "teamagent-mcp-delegator",          // 既存 "teamagent-openclaw" と分離
  "aud": "teamagent-hermes-callback",        // 既存 "teamagent-mcp" と分離
  "sub": "<principal_id = team_id:slack_user_id>", // stable principal（§8）。email は入れない
  "profile_id": "<HMAC(profile_salt, principal_id)>", // サーバ導出のみ
  "session_id": "<uuid>",                     // 1 run_hermes_agent = 1 session
  "parent_request_id": "<request_id>",        // trace 貫通
  "allowed_tools": ["search", "clientkarte", …], // サーバ側 policy の縮小コピー（§7.5）
  "max_calls": 8,
  "iat": …, "exp": …,                         // exp - iat ≤ 300s（同期 session の上限）
  "absolute_deadline": …,                     // ≤ exp（v1 では exp と同値）。将来 renew を導入しても越えられない絶対上限
  "nonce": "<22char b64url>"                  // session 一意性
}
```

- 署名鍵は既存 caller claim 鍵と**別**。裸の env ではなく `hmac_keyring.py` に `HMAC_PURPOSE_HERMES_DELEGATION` として追加（purpose 重複拒否・verifier-first rotation を継承）。相互排他チェックは「hermes 鍵 ≠ caller 鍵 ≠ MCP bearer ≠ hermes ingress bearer ≠ hermes callback bearer」の 5 値へ拡張
- **renew は v1 では実装しない**。したがって**同期 Hermes session は実質最大 300 秒**であり、`absolute_deadline` は v1 では `exp` と同値（独立した長い deadline は意味を持たないため置かない。将来 renew を導入した場合にのみ「renew でも越えられない絶対上限」として独立の意味を持つ）。**それを超える長時間処理は claim を延命するのではなく、既存の async submit/status tool（`proposal_builder_submit` 等）へ委譲する** — Agent の同期 session は 5 分以内・長仕事は非同期 tool へ、が既存 TeamAgent 思想（§20）と整合する基本形。将来 renew の必須条件（Gateway mint・resolver 再実行・absolute_deadline 不可越・max_calls 非リセット）は §24 に記録

### 7.3 per-call MAC（session_mac_key 方式）— request binding の復元

「台帳に対称鍵を保存」ではなく **KDF 導出**方式を採る（Hermes が MAC を生成でき、かつ DynamoDB に秘密鍵を置かない）:

```
K_session = HKDF-SHA256(
    master_key = hmac_keyring[HMAC_PURPOSE_HERMES_DELEGATION],
    info       = session_id || nonce(jti) || sub
)
```

- Gateway は claim mint 時に K_session を導出し、**セッション開始時に一度だけ** Hermes へ渡す（claim 本体には入れない）
- Gateway 側は master key から**再導出**できるため、K_session を保存しない
- 各 callback で Hermes は最低限次を MAC する:

```
call_mac = HMAC-SHA256(K_session,
    session_id || tool_name || arguments_sha256 || call_nonce || parent_request_id)
```

- `arguments_sha256` は既存 `canonical_request_sha256`（型タグ付き canonical 化・クロス言語決定的）を流用する。ただし delegated 経路では `_user_context` を Hermes に作らせないため、sanitize 分岐を分けて実装する
- Gateway は K_session を再導出して検証 → **「1 callback = 1 tool の 1 引数の 1 回」が既存 caller claim と同水準で復元**される
- `call_nonce` は per-callback one-use（§7.4 の台帳で consume）

### 7.4 DynamoDB は「秘密の保管庫」ではなく「線形化点」

専用テーブル `hermes-session-state`（既存 `mcp-caller-claim-nonces` とは**別テーブル・別 IAM statement**。既存テーブルの PutItem-only 不変性を守るため UpdateItem 権限を混ぜない）:

| 保持するもの | 用途 |
|---|---|
| session state（sub / allowed_tools のサーバ側正本 / policy version） | claim 単体を真実源にしない（confused deputy 対策） |
| absolute_deadline | 期限の非延長性 |
| remaining_calls | calls < max の判定 + increment（**実行前**・下記 transaction 内） |
| consumed call_nonce | per-callback one-use（下記 transaction 内） |
| budget（cost / wall-clock 累計） | §19 |

**単一の認可線形化点（PR3 実装要件）**: 「call_nonce 未使用 ∧ calls < max_calls ∧ deadline 未超過 ∧ budget 内」の判定と「nonce consume + call count increment」は、**可能な限り 1 回の DynamoDB `TransactWriteItems`** で原子的に行い、全部成立した時だけ skill を実行する。write を nonce Put と calls Update に分割すると「replay 攻撃が拒否されつつ call budget だけを削る」DoS 余地が生まれるため、分割 write は不可。

障害時は**全て fail-closed**（既存 replay store と同じ裁定: 「予算台帳が壊れている時に skill を実行しない」）。SSE / PITR / TTL / deletion_protection は既存 nonce テーブルと同水準。

### 7.5 Tool Policy — intersection と denylist

```
effective_tools =
      server_policy_allowlist(policy_version)   # サーバ側コード/env が真実源
    ∩ claim.allowed_tools                       # 縮小方向にのみ作用
    ∩ feature_enabled_tools                     # USE_* フラグ
    − hard_denylist                             # 常に最優先
```

- **Hermes の希望 tool list は authority にしない**。claim の allowed_tools 自体、mint 時にサーバ側 policy から導出する（リクエスト由来値からは作らない — OpenClaw の Haiku が広げられてしまうため）
- **恒久 deny（policy version に依らず不変）**: `run_hermes_agent` / `run_agent`（再帰・meta-tool）
- **Hermes v1 hard deny（恒久ではなく policy-versioned）**: per-user OAuth 系 = `mail_summary / mail_followup / mail_to_internal_context / mail_reply / mail_draft / calendar_event / calendar_freebusy / schedule_propose / morning_digest / oauth_connect / slack_summary / attachment_assist / video_capture / workspace_search / knowledge_deliver`
  - 理由: §2 のとおり会社共有モードでも `metadata.user_email` は実 email であり、mail_* 系 skill は現状 `identity_verified` を見ていない（`mail_summary/skill.py:101-106` 等）。claim 漏洩＝「他人の受信箱への窓」になる攻撃価値の跳ね上がりを v1 では構造で遮断する
  - **将来の解禁パス（Personal AI Secretary 構想との整合）**: `hermes_tool_policy_version` を上げることで明示的に解禁できる。前提条件 = ①Personal Profile / per-user OAuth 境界の完成（PR5 以降） ②mail_*/calendar_* 系 G1 ゲートの「`user_email` **かつ** `identity_verified is True`」への強化（`attachment_assist/skill.py:153-155` と同形） ③delegated 経路の `identity_verified` の扱いの再裁定。**default deny は維持**し、解禁は常に明示的な policy version 変更 + レビューで行う
- **Hermes v1 allowlist（会社共有 read-only + 生成系）**: `search / clientkarte / proposal_draft / proposal_review / proposal_builder_submit / proposal_builder_status / web_research`

### 7.6 Callback Boundary の分離

- callback は `/mcp` とは**別 ASGI route**（例 `/hermes/callback`）+ **別 bearer**（`TEAMAGENT_HERMES_CALLBACK_BEARER`）+ 別 aud/iss/鍵 + **専用の縮小 tool マップ**（`by_name` 全体を渡さない）
- 既存 `BearerAuthMiddleware` は単一 prefix 前提のため、route×token の対応表型へ拡張（既存 route の挙動は不変）
- callback route では `_user_context.caller_claim` フィールドを**受け付けない**（既存 claim との混同・格上げ防止）
- ネットワーク: Hermes SG は MCP SG からのみ ingress、MCP callback への到達も SG で Hermes SG のみに限定（bearer と二重）

## 8. Identity — resolver 再実行と cache

```
claim.sub（principal_id = team_id:slack_user_id・stable principal）
  → slack_user_id を取り出し server-side Identity Resolver（既存 SlackClient.resolve_identity）
  → ResolvedIdentity {email, groups, is_member}
  → build_rls_metadata()（唯一の変換点・role=member 固定）
  → RLS GUC
```

- **Identity の主キーは stable principal**（`team_id + ":" + slack_user_id`、将来的には社内 immutable user id）。**email は Resolver 由来の「属性」としてのみ扱い、主キーにしない** — email を Personal Memory / profile のキーにすると、改姓・ドメイン変更・アカウント移行で「旧 email → Memory A / 新 email → Memory B」に分裂する事故が起きる。RLS / per-user OAuth が email を要求する箇所へは、毎回 resolver が返した現在の email を流す
- **claim 内の値は authority にしない**（sub は resolver 結果との一致照合のみ。不一致は fail-closed）
- `build_rls_metadata` に **email 文字列を渡す実装は禁止**（str 分岐は `is_member` を検査しない＝退職者・ゲスト降格・stranger を検出できない）。必ず `ResolvedIdentity` を渡す
- **ただし「claim を信用しない」と「毎 tool call で Slack API を叩く」は別問題**。既存 resolver のプロセス内 TTL cache（成功/失敗とも 60s）をそのまま利用してよく、100 人展開を見据えて**短 TTL の server-side cache（上限 60s・失効イベントでの明示 purge 付き）を許可**する。cache の TTL は claim の exp を超えないこと
- `hermes_profile_id = HMAC(profile_salt, principal_id)` — サーバ導出のみ。Hermes にもモデルにも生成・指定させない

## 9. Personal Hermes Profile

| 領域 | 内容 | scope |
|---|---|---|
| Personal Memory（v1） | 返事の長さ・言い回し・顧客名/商材名・資料の型・仕事の進め方 | 本人 + 管理者（管理者は表示前の監査 INSERT が成功した場合のみ。v1 は小俣さん 1 名） |
| Personal Sessions | **v1 では使わない**。FTS5 に会話本文を残さない | — |
| Company Shared | Skills・MCP tool 面・ポリシー・承認済み workflow | 全社（review 必須） |

本人の RLS 分離は維持する。**本人メモの表の RLS には既存定型の `OR app.user_role = 'admin'` を入れない**（既存定型は `infra/migrations/0025_digest_ack.sql:44-53`。admin を立てる経路が connect_web 以外にもあるため: `scripts/run_morning_digest_fargate.py:91`、`src/teamagent/ingest/repository.py:429,446`、`src/teamagent/connect_web/app.py:5094`、`scripts/backfill_entities.py:79`）。管理者閲覧は §10b.6 の DB 関数だけで行う。本人の「何を覚えてる？」には項目と管理者閲覧回数を返す。

## 10. Memory Governance

- **禁止**: Slack/Gmail/Salesforce/RAG/Drive 本文の Memory へのコピー（Source of Truth 側で都度検索）。Hermes には検索結果の永続化 API を渡さない＝構造で禁止
- Memory に書けるのは再利用可能な個人知識のみ（preferences / formatting / repeated corrections / workflow habits / preferred terminology / approved personal context）
- **DM 本人メモ v1** の書込は `propose → pending → approve` を採らず、`personal_memory.guard` の機械検査 → 自動反映 + 本文を持たない監査とする。Company Skill への昇格は引き続き Owner/Admin review 必須
- memory_read / memory_write は本文を含めず監査ログへ。管理者閲覧は表示より先に監査を確定する（§10b）

## 10b. DM 本人メモ v1（Hermes 学習係）

### 10b.1 目的と分離境界

Hermes の役割は**覚える係だけ**である。返事は現行 Aico（OpenClaw の Haiku）が本人メモを参考情報として作り、Hermes は返信生成、MCP tool 実行、Slack 操作、永続化を行わない。

| 境界 | v1 の決定 |
|---|---|
| Hermes が持つ secret | ingress bearer、TLS 鍵だけ |
| Hermes task role の Allow | jp. Haiku 推論プロファイルに限定した `bedrock:InvokeModel`、logs |
| 明示 Deny | secretsmanager、kms、rds、dynamodb、s3。RDS SG とインターネットへの経路も与えない |
| 到達できないもの | MCP、RDS、DynamoDB、Slack、既存 MCP bearer、既存 HMAC 鍵、OAuth token |
| 後続へ送るもの | callback、delegated claim、`HMAC_PURPOSE_HERMES_DELEGATION`、K_session、Hermes からの MCP 利用（v2 で再レビュー。v1 の M0〜M11 には含めない） |

同居案は採らない。現行 MCP task role は database URL と既存 bearer を読むため（`infra/terraform/fargate.tf:375-383`）、同じ task role を Hermes と共有するとこの境界を壊す。

### 10b.2 保存原則とデータ最小化

- 「Hermes に永続化 API を渡さない」原則を維持する。Hermes はジョブごとの使い捨て `HERMES_HOME` に本家 memory tool で書くだけで、`finally` で作業場を削除する
- MCP が Hermes の差分を受け取り、書込前に `personal_memory.guard` を通し、合格した項目だけを RDS へ保存する。読み出し時にも同じ検査をかけ直す
- Personal Sessions / FTS5 は使わない。会話本文、session 履歴、逐語の引用を保存しない
- 保存できるのは 200 字以内の再利用可能な要約だけ。本文の 25 字以上の逐語一致、URL、連絡先、秘密情報、不可視文字、prompt injection は落とす
- 顧客名・商材名・社内の同僚名は保存してよい。先方担当者の個人名は保存しない。判定は敬称・役職（例: 「さん」「様」「氏」「部長」）を手掛かりにし、**MCP が持つ在籍者名簿**（Slack `users.list` のキャッシュ。M5 で実装）に載っている名前だけを `member_names` として guard に渡して許可する。名簿に無い名前は敬称つきなら落とす（fail-closed）
- **限界（既知）**: 敬称の無い人名（例: 「田中に確認」）は guard の規則では検出できず通りうる。v1 はこれを許容し、管理者の目視と本人の「忘れて」で直す。M5 で Haiku による二値判定を追加するかは点灯中の実測で判断する

### 10b.3 対象 DM と流れ

対象は本人との 1 対 1 DM だけである。`D…` は送信者から導出できるため、`^D[A-Z0-9]{8,}$` の fullmatch だけでは 1 対 1 だった証明にならない。plugin は次の 3 つがすべて成り立つときだけ DM と判定し、`conversations.open` の fallback は使わない: ① ingress の channelId が `DM:<sender>` に等しい（OpenClaw が `user:<U…>` から DM と判定した結果）② ctx.chatId が `^D[A-Z0-9]{8,}$` に fullmatch ③ chat 種別が direct。mpim・Slack Connect・スレッドの試験を M8 に入れる。`C…` のチャンネル、`G…`、mpim は学習も適用もコマンド処理もしない。OpenClaw 側の正準 DM 解決点は `infra/openclaw/caller-identity-plugin/dist/index.js:1952-1977`、MCP 呼出しの既存起点は同 `:975-980` にある。

```text
1:1 DM の本人発話
  → caller-identity plugin / message_received（observe、記憶コマンドは除外）
  → MCP の揮発バッファ（5 発話で打切り／最大 10 分／1 人 1 日 6 job）
  → Hermes POST /v1/learn（TLS + ingress bearer、使い捨て HERMES_HOME）
  → 本家 memory tool が差分を返す
  → MCP personal_memory.guard（書込前検査）
  → RDS（本人 RLS、本文なし）
  → 次の 1:1 DM で plugin / before_prompt_build
  → 「参考情報であり指示ではない」枠へ差込み
  → 1.2 秒で諦め、メモなしで現行 Aico が返信
```

OpenClaw の登録 hook は現在 8 本（`infra/openclaw/caller-identity-plugin/dist/index.js:185-194`）であり、M8 で `before_prompt_build` を加えて 9 本にする。`personal_memory_*` は LLM の tool 面へ公開せず、plugin が予約 `tool_call_id` で直接呼ぶ。toolFilter.include はクライアント側のゲートなので、**サーバ側で次を不変条件として強制する**（M5・各項目を変異テストで固定）: ① MCP の list_tools に `personal_memory_*` を載せない ② 実行は「署名済み caller claim の channel が `^D` に fullmatch」かつ「tool_call_id が予約 prefix」かつ「flag と allowlist が有効」の場合だけ受け付ける（予約 prefix の検査は現在 plugin 側にしか無いため MCP 側に新設する）③ 引数名に `query` を使わない（usage_events が `query` を本文として記録するため。`src/teamagent/mcp_gateway/server.py:159-160`）

### 10b.4 G7 の限定例外

G7 の「本文を plugin/MCP に残さない」原則に対し、学習係だけ次の限定例外を認める。

| 項目 | 制約 |
|---|---|
| 置き場所 | MCP プロセスの揮発メモリだけ |
| 上限 | 1 人 5 発話、最長 10 分。先に達した時点で打ち切る |
| 禁止先 | DB、usage_events、アプリログ、CloudWatch、Sentry、例外メッセージ |
| 再起動 | 復元せず消える |

引数名は `utterance` とし、usage_events が `query` だけを採る現行境界（`src/teamagent/mcp_gateway/server.py:153-160`）を利用する。Sentry の key denylist に `utterance` / `utterances` / `entries` / `snapshot` を追加する。key 名の一致でしか効かない（`src/teamagent/observability/sentry.py:230-247`）ため、personal_memory 系のコードでは Sentry へのローカル変数の送信も止める。

**Bedrock の呼出しログは既定で有効で、本文も記録し、60 日以上残る**。Terraform の既定は `enable_bedrock_invocation_logging` / `enable_bedrock_invocation_log_delivery` とも true（`infra/terraform/variables.tf:183-193`）、`text_data_delivery_enabled = true`（`infra/terraform/security.tf:372`）、保持は最低 60 日の固定契約（`infra/terraform/variables.tf:195-198`）。Hermes の学習呼出し（5 発話＋既存メモ）もここに残り、本人の削除要求では消せない。本番で実際に配送されているかは点灯前に読み取りで確認するが、告知には「最低 60 日残り、削除要求では消えない」と書く。

本人メモを返事へ差し込むのは system 側（appendSystemContext 相当）に限り、利用者発話側（prepend）には入れない。OpenClaw の会話記録（EFS）に本人メモが毎ターン書き込まれるのを避けるため。M8 で「EFS に目印が残らない」試験を行う。

### 10b.5 本人操作、凍結、削除、退職

| 操作・事象 | 定義 |
|---|---|
| 「何を覚えてる？」 | 本人メモの番号つき一覧と、管理者に閲覧された累計回数を返す |
| 「○番を忘れて」 | 指定項目を物理削除する |
| 「覚えるのを止めて」 | `state=frozen`。新規学習も prompt への適用も止めるが、内容は残す |
| 「記憶を再開して」 | 明示操作で `state=active` に戻す。自動再開しない。普段の依頼と衝突しないよう全文一致で受け付ける |
| 「覚えたことを全部消して」 | 10 分以内の確認を要求し、確認後に本人メモを物理削除して `frozen` にする。再開は本人の「記憶を再開して」だけ |
| Slack `users.info deleted=true` | 本人メモを自動で物理削除する。API 失敗を退職と見なさない。ゲスト化は凍結だけ |

profile のキーは `team_id:U…`（Slack user_id）とし、email は属性として持つ。退職の確認は保存した `U…` で `users.info` を直接引く（email からの逆引きは無効化されたユーザーで失敗しうるため使わない）。§8 の「email をメモのキーにしない」と整合する。

resolver が `None` を返す原因には API 失敗も含まれる（`src/teamagent/adapters/slack_client.py:392-408`）ため、退職削除は `deleted=true` を直接確認した場合に限る。

「全部消して」の直後にも、次は即時には消えない。

| 残存先 | 期間・扱い |
|---|---|
| RDS 自動バックアップ | 最長 7 日（`infra/terraform/rds.tf:115`）。手動スナップショットは期限なく残る |
| RDS の遅いクエリログ | `log_min_duration_statement=1000` で bind 値ごと記録されうる（`infra/terraform/rds.tf:34-37`）。CloudWatch の保持期間に従う |
| Bedrock の呼出しログ | 既定で有効・本文を含む・最低 60 日（§10b.4）。削除要求では消えない |
| OpenClaw の会話記録（EFS） | 本人メモとは別の既存記録。Terraform に保持期限の設定が無い（`infra/terraform/openclaw_state.tf:9-18`）。本人メモを system 側に差し込むことで、メモ自体はここに書かない |
| Slack の DM 履歴 | 「何を覚えてる？」への返答（一覧）が Slack と会話記録に残る |
| Hermes の一時 HOME | ジョブ中だけ本文が存在する（本家の session DB を含む）。ジョブ終了時に削除。HOME の外（/tmp・キャッシュ）への書き込みも M2 の試験で目印検索する |
| plugin のキャッシュ | 最大 60 秒。凍結・削除のコマンドを受けたら即時に捨てる |

### 10b.6 管理者閲覧と監査

管理者閲覧口は connect_web の `/admin` 型画面だけとする。表示トランザクションでは、本文を持たない `personal_memory_audit` への INSERT を**表示前**に確定する。INSERT が失敗した場合は 503 を返し、項目、件数、断片を一切表示しない。閲覧は「監査 INSERT → 行を返す」を 1 本にした DB 関数（SECURITY DEFINER）だけで行い、EXECUTE は専用ロールにだけ与える。退職削除も DELETE 専用の DB 関数にする。M5/M6 の出口条件に「`app.user_role='admin'` を立てた接続でも本人メモの表は 0 行」の試験を入れる。v1 の管理者は小俣さん 1 名で、**本人メモ専用の allowlist（`PERSONAL_MEMORY_ADMIN_EMAILS`）**で管理する。利用状況画面の `CONNECT_ADMIN_EMAILS`（`src/teamagent/connect_web/app.py:660-664`）とは共用しない（後で利用状況を見せる人を足したときに本人メモまで見えるのを防ぐ）。「ちょうど 1 名」を契約テストで固定し、拡大は決裁を条件とする。監査には管理者 email、対象 profile の SHA-256 先頭 16 hex、件数、理由コードを記録し、メモ本文は記録しない。

## 11. Specialist Hermes

1 体の巨大 agent にしない。各 Specialist = profile + skills + **claim の allowed_tools subset（構造で制限・プロンプトではない）** + separate memory + separate observability。最初の 1 体は **Proposal Hermes**（高価値・失敗影響が限定的・proposal_builder の非同期 job 基盤が既にある・出力を既存フローと比較可能）。

## 12. AI General / Router

「何でも自分でやる agent」ではなく分類器。判定は OpenClaw の外側ループ（Haiku）に SOUL 指示 + tool description で行わせる（現行の tool 選択と同じ機構・新規コンポーネント不要）:

```
依頼 → Simple?（検索/数値/単純tool）→ YES → 既存 L1 Skill（fast path・現行のまま）
        └ NO → 経験/Planning/複数tool協調が必要? → YES → Hermes Specialist
                                                 └ NO → 既存フロー
```

## 13. Tool / MCP Architecture

- Hermes から見える tool 面は §7.5 の intersection で決まる**専用 callback 面**。既存 Skill の再実装は**ゼロ**（ToolSpec がそのまま tool 定義になる）
- `effective-tool-scope.json` に hermes 面の宣言を追加し、契約テスト（`test_openclaw_runtime_contract.py` 同型）で「宣言なき露出」を CI で封じる。**`run_hermes_agent` が OpenClaw の include に無いことも契約テストで断言**（dark 宣言）

## 14. Slack — retrieval source

Hermes Memory へ Slack を保存しない。ingest 済み検索（search）を MCP 経由で都度呼ぶ。`slack_summary`（本人 xoxp 限定）は per-user OAuth 系のため **v1 deny**（§7.5）。解禁は policy version と G1 強化後。

## 15. Google Workspace — 再実装しない

adapter 7 種 + per-user OAuth TokenStore（KMS+RLS+consent 照合）は実装・本番稼働済み。Hermes には Google credential を渡さず、将来（policy version 更新後）も既存 MCP tool を呼ばせるだけ。

## 16. Salesforce（将来）

同型: Salesforce Adapter + Skill を TeamAgent 側に新設 → MCP tool として公開 → Hermes は tool を呼ぶだけ。credential は Secrets Manager → MCP task のみ。user/company permission は server-side 評価。

## 17. Connect RAG

Connect RAG（connect.newstv.co.jp/app）は同一 repo の connect_web サービス + 同一 pgvector。Hermes からは既存 `search` / `clientkarte` 経由で到達済み扱い＝追加実装ほぼ不要。

## 18. Observability

既存の計器（structlog + usage_events + CloudWatch metric filter）に列を足す:

- 既存: request_id / skill / user_email / user_id / cost / latency / via
- 追加: `via="hermes"` / hermes_profile_id / session_id / specialist / delegated 検証結果 / calls_used/max / memory_read / memory_write / skill_proposal / fallback
- 監査ログ `hermes_callback_authorized`（tool / session_id / calls / **email は domain のみ**＝既存 `_domain_of` 流儀）
- Distributed trace: `parent_request_id` を OpenClaw → Gateway → Hermes → Callback Boundary まで貫通（claim に埋め込み済み）

## 19. Cost / Model / Budget

- Routing=Haiku（OpenClaw 外側・変更なし）/ Hermes planning=Sonnet（重い時のみ）/ Tool 実行=deterministic Python / Embedding=既存 LocalE5
- per-session 予算: `max_calls`（既定 8）+ `cost_cap_usd`（既定 0.5・既存 run_agent と同水準）+ wall-clock（同期 session ≤300s・`absolute_deadline` は v1 では exp と同値）+ per-tool timeout。予算は §7.4 の台帳（TransactWriteItems）で線形化
- profile 単位の日次上限は PR5 以降（既存 cost_guard / quota_store のパターンを流用）

## 20. Failure / Rollback / PR2 系列（A0 / A1 / B）の分割と dark 形態

- 各 Phase は env flag 1 個で完全 rollback: `USE_HERMES_ORCHESTRATOR=0` → list_tools から消滅（run_agent と同機構）
- Hermes down → run_hermes_agent は構造化エラー（既存 `_err` 契約）→ OpenClaw は既存 L1 で応答継続（SOUL の「境界が拒否したら素直に伝える」規範に接続）
- DM 本人メモ v1 は `USE_PERSONAL_MEMORY=0` と `PERSONAL_MEMORY_ENABLED=0` で学習・適用・コマンドを閉じる。context 取得失敗または 1.2 秒超過時はメモなしで返信を続け、Hermes 学習失敗時は既存メモを変更しない
- 凍結・全削除・退職を学習ジョブより優先する。ジョブ開始後でも profile version が変わった場合は Hermes の結果を破棄し、古い結果を復活させない

### 20.1 PR2 を 3 本へ分割した理由（監査で確定）

当初 PR2 は「Hermes dark runtime」1 本の想定だったが、その後の監査で**先に解かないと着手できない供給網側の問題**が 2 段見つかった。よって PR2 を **PR2-A0 / PR2-A1 / PR2-B** の 3 本へ分割する。

| 発見 | 実測内容 | 帰結 |
|---|---|---|
| ① Hermes image を署名リリース鎖へ追加する作業は、それ自体が独立した Supply-Chain Security 変更 | ECR 3 本（quarantine / verified-candidates / release）・promoter の 3 層 allowlist（pipeline / receipt subject name / receipt repository mapping）・receipt subject・contract・テストなど、**既存改修だけで最低 14 ファイル**に及ぶ | container 供給網作業を ECS 展開と同一 PR に混ぜない＝**PR2-A1** として切り出す |
| ② content-addressed buildspec の Terraform state が実態から取り残されている | buildspec の S3 key が body の sha256 由来のため、内容が変わると key が変わり replacement 判定になるが、**`aws_s3_object` 4 本の `prevent_destroy` が plan を停止させる**。つまり**現在の dev HEAD 自体が apply 不能**で、新しい S3 object を作れる承認済み apply 経路も存在しない | ① より前に供給網の土台を直す必要がある＝**PR2-A0** |

### 20.2 PR2-A0: Supply-Chain Adopt（Hermes は一切登場しない）

content-addressed buildspec を **hash-keyed append-only generation model** へ移行し、実態から取り残された Terraform state を安全に adopt できる仕組みを作る。世代（generation）を body の sha256 ごとのエントリとして**追記するだけ**の台帳にし、既存世代は destroy しない。

- **Hermes は一切登場しない**。この PR 単体でも、既存リリース鎖の apply 可能性を回復させる価値がある
- 既存の **`prevent_destroy` / S3 Object Lock（GOVERNANCE）/ bucket policy の Delete Deny は一切弱めない**。「消せるようにする」のではなく「消さずに済む形にする」
- state への取込みは create ではなく **adopt（import）**。旧アドレスは `removed` ブロックの `destroy = false` で state から外すだけとし、S3 実体には触れない
- 出口条件: dev HEAD で `terraform plan` が prevent_destroy 停止なしに通ること、かつ新しい buildspec 世代を出せる承認済み apply 経路が実在すること

### 20.3 PR2-A1: Hermes Supply-Chain Onboarding（ECS は作らない）

Hermes upstream image（Docker Hub `nousresearch/hermes-agent` の release tag を **digest 固定**）から薄い derived image を作り、TeamAgent の署名リリース鎖―― **quarantine → SBOM/Trivy/attestation → verified-candidates → promoter → release ECR** ――を通せる状態にする。

- **ECS は作らない**（service / task definition は PR2-B）。この PR の成果物は「検証済み release digest」1 つ
- 出口条件: release ECR に Hermes の digest が 1 本入り、receipt / attestation が既存 OpenClaw・MCP と同基準（Trivy C0/H0）で揃うこと

### 20.4 PR2-B: Hermes Dark Runtime（旧 PR2）

PR2-A1 が生成した **release digest** を使って ECS/Fargate に載せる。

- **dark runtime 形態（裁定済み）**: 常駐タスク 0（Terraform で desired_count を 0 と宣言・手動の ECS 直接操作ではない）を採る。受け入れ試験は **ECS RunTask で 1 タスクだけ起動し、startup → /healthz → Bedrock client 初期化 → CloudWatch logs を確認して終了**する形（本 repo の「run-task 検証」標準と同型）。常駐ゼロなので idle コストゼロ・外部 routing ゼロ・MCP exposure ゼロが自明に成立する。PR3 で接続する際に Terraform 変更として desired_count を 1 へ上げる（それでも flag OFF なら tool 面に出ない）。**v1 では M9（点灯）で tfvars の変更と `-var-file` 付き apply（Gate ②）により 0→1 にする**。ECS を手で直接操作しない

## 21. Migration Phases

DM 本人メモ v1 の決定済み順序は `M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8 → M9 → M10 → M11`。旧 `PR1 / PR2-A0 / PR2-A1 / PR2-B / PR3…` 表は汎用 Hermes 構想の履歴であり、v1 の実施順には使わない。M3 の Hermes 便に限り、ACTIVATION の adopt+Pin 完了後に A1 とする順序および「A1 の generation publisher より前に正名化」の禁止事項を外す（2026-09-25 決裁者承認）。M3 は generation 18 入力を更新して publish するため、「正名化をしないまま次の generation release へ進まない」という禁止事項にも当たる。**この禁止事項も Hermes 便に限り適用しない**（同じ裁定の範囲）。禁止事項の本文は維持し、ACTIVATION_STATE.md の「禁止事項」節に例外を注記する。

| Phase / PR | 内容 | flag | 出口条件 |
|---|---|---|---|
| M0 | docs: ADR・計画・ACTIVATION・利用者向け 1 枚 | — | 決裁内容、境界、M0〜M11、告知文が docs に一致し、code diff 0 |
| M1 | `personal_memory.guard` 純粋関数（未配線） | — | 境界/NFKC/純粋性/ablation/変異テストが緑 |
| M2 | Python 3.13 Hermes 学習ランナー + image（dark） | — | job 間 HOME 非共有・crash 時削除・config fail-closed・Trivy C0/H0 |
| M3 | 独立 Hermes pipeline と初回署名 release | — | release ECR digest、receipt/attestation、承認必須、未登録値 FATAL |
| M4 | Hermes ECS dark runtime（desired_count=0） | — | healthz→Bedrock→合成 1 job→本文非記録→終了、IAM/SG 契約緑 |
| M5 | MCP 保存・揮発 buffer・learner client・本人コマンド | `USE_PERSONAL_MEMORY=0` | RLS 分離、書込/読出し guard、DM gate、version race、本文非記録が緑 |
| M6 | connect_web 管理者閲覧・退職時削除 | — | 非管理者 403、監査失敗 503/非表示、deleted/API 失敗/guest の分岐が緑 |
| M7 | PR-R 最小版（実流量前の必須 Gate） | — | admission control、構造化 overload、in-flight、1.2 秒 fallback が緑 |
| M8 | OpenClaw plugin の observe/context/commands | `PERSONAL_MEMORY_ENABLED=0` | 1:1 DM 限定、LLM 経路拒否、timeout fallback、include 不掲載が緑 |
| M9 | 小俣さん 1 名へ点灯 | allowlist 1 名 | 告知、5 発話学習、次 turn 適用、5 コマンド、監査、凍結/削除、ログ非残存を実機確認 |
| M10 | 2〜3 名、5 営業日観察後に 16 名へ展開 | allowlist 拡大 | 法務/総務確認、決裁者 GO、件数/理由/p95/費用が許容範囲 |
| M11 | 便δ後の terraform import と guard 整合 | — | import 後 plan 差分 0、runtime guard 契約緑、台帳 close |

**M7 は M9（production user traffic 開始）前の必須 Gate**。M9 の点灯は「production user traffic 0」の原則に対する学習係だけの例外で、小俣さん 1 名から始める。詳細なファイル、試験、人の関門は [hermes_implementation_plan.md](hermes_implementation_plan.md) を正とする。

## 22. Capacity Control（検証で確定した現状と PR-R）

再検証（2026-08-18）で確定した事実: **RequestGate（同時 ≤4 の総量規制）は現行本番経路（OpenClaw→MCP）に適用されていない**。`mcp_gateway/` に参照 0 件・起動鎖の全段にゲート無し・`REQUEST_GATE_*` env は Terraform に 0 件。実効している制御は 物理 1 タスク / default ThreadPool（実効 6〜32・待ち行列無制限）/ pg_pool max_size=8（10s timeout）/ Bedrock リトライ のみで、admission control も「混雑中」の明示応答も無い。gate/pool の観測（MetricsSnapshotter）も旧 slack_bot 専用配線のため**本番の同時実行数は観測できていない**。

PR-R の必要条件（PR4 前の必須 Gate）:

1. **MCP admission control** — `dispatch_tool` 直前に既存 `RequestGate` を配線し、`REQUEST_GATE_*` env を mcp taskdef へ。`QueueFullError`/`GateTimeoutError` は構造化エラーで返し OpenClaw が「混雑しています」と伝える（明示 overload 応答）
2. **in-flight metrics** — MetricsSnapshotter を MCP プロセスへ配線（gate in_flight/peak_waiting/rejected + PoolStats）
3. **heavy-tool semaphore** — video_algorithm / proposal_builder 等の別枠制御（connect_web の SEARCH_CONCURRENCY セマフォと同パターン）
4. 100 人展開に向けた追加候補（別途）: executor 明示化 + OMP_NUM_THREADS=1 / 重ツール完全非同期化 / スケール（ALB 化 + gate 割り算）/ RDS 格上げ / per-user 流量制限 / 本番同型負荷試験

## 23. Risks

| リスク | 深刻度 | 緩和 |
|---|---|---|
| delegated claim の設計穴 | HIGH | §7 の敵対審査反映設計 + §25 マージブロッカーテスト。**PR2-B 完了後・PR3 着手前に delegated claim 周りの再レビューを実施**（裁定済み） |
| toolFilter がクライアント側ゲートであることの誤解 | HIGH | §6 の禁止形を明文化・callback は別 route + server-side policy |
| 会社共有モードで per-user OAuth 面が開く | HIGH | §7.5 v1 hard deny + 将来は policy version + G1 強化 |
| Memory への会社データ混入 | HIGH | 永続化 API 非公開 + 監査 job |
| DM 本文が Sentry/CloudWatch/Bedrock/OpenClaw に残る | HIGH | MCP 揮発バッファだけを例外化し key scrub、合成目印で確認。Bedrock/OpenClaw の保持は点灯前確認と告知 |
| 先方担当者名・貼付文面・prompt injection が記憶を汚染 | HIGH | 敬称ベースで迷ったら落とす、長文/引用/転送/URL/25 字逐語を guard、読出し時も再検査 |
| 管理者が監査前にメモを見る | HIGH | connect_web だけに限定し、監査 INSERT を表示前に確定。失敗時 503 で無表示 |
| 全削除後に遅延 job が項目を復活させる | HIGH | profile version 楽観 lock、凍結/削除後の結果を破棄、削除後は frozen |
| Hermes runtime の CVE/供給網 | MED | digest 固定・SBOM・署名リリース鎖に載せる（OpenClaw と同水準） |
| Python 3.13 image が Trivy C0/H0 を通らない | HIGH | M2 初日に候補 2 基盤を実測。依存を手動列挙しても不可なら方針を再裁定（現時点は未実測） |
| `before_prompt_build` が本番で発火しない | HIGH | M8 便で banner と初回発火を確認し、失敗時は M9 を止める |
| vpce SG への Hermes SG 追加漏れ | HIGH | `infra/terraform/vpc_endpoints.tf:21` の警告どおり 443 が落ちるため M4 の契約テストで必須化 |
| 本人が監視と受け取る | MED | 学習前の 1 回告知、用途/管理者/閲覧記録/評価非利用/削除後残存を明示し、本人に閲覧回数を返す |
| DM 返信が遅くなる | MED | context は 60 秒 cache（凍結・削除のコマンドで即時に破棄）、1.2 秒で諦めてメモなしで返信 |
| email 変更で RLS 行が見えなくなる | MED | v1 は email 判定。管理者修正の運用を用意し、将来 stable principal へ移行 |
| 二重オーケストレーション暴走 | MED | meta-tool 恒久 deny・max_calls/cost cap/absolute_deadline |
| 容量（実流量開始後） | MED | PR-R を必須 Gate 化（§22） |
| コスト超過 | MED | per-session cap + 既存コストアラーム |

## 24. Open Questions

1. Hermes の Bedrock 認可を Haiku/Sonnet の inference profile に限定する IAM 記述の粒度
2. renew 導入時の必須条件の再検証（v1 は非実装。導入するなら: Gateway mint・resolver 再実行 + is_member 再確認・absolute_deadline 不可越・renew_count cap・max_calls 非リセット）
3. mail_*/calendar_* の G1 強化（`identity_verified` 必須化）の実装時期 — policy version 2 の前提条件
4. Memory の保存先（RDS vs DynamoDB）と at-rest 暗号化の粒度
5. `identity.py:100-105`（company_member_metadata docstring）の stale 記述の修正 — .py 変更のため PR1 対象外・PR3 で併修
6. Salesforce 導入時期

## 25. Implementation Backlog / マージブロッカーテスト

詳細は [hermes_implementation_plan.md](hermes_implementation_plan.md) の「付録: v2（汎用 Hermes）の旧計画」。以下は v2 の旧 PR3 のマージブロッカー（v1 の M0〜M11 には適用しない）（これが緑でなければマージ不可）:

1. **cross-user session race**: 同一 Hermes プロセスに A/B の session が並存しても claim/K_session が混線しない（既存 `test_same_session_cross_user_race…` の同型）
2. **予算ストア障害時に skill が実行されない**（fail-closed・resolver より前）
3. **max_calls 並行 race**: 16 本同時 callback で成功がちょうど 8 本（nonce 消費 + call count increment が単一 `TransactWriteItems` で原子的であることの実証・replay で budget だけ削れないこと）
4. **鍵の双方向偽造不可**: caller 鍵で hermes claim を作れない / 逆も / MCP bearer ではどちらも不可
5. **denylist 優先**: allowed_tools に run_hermes_agent / mail_draft を入れても server-side denylist が勝つ
6. **resolver 再実行**: callback 時点でゲスト降格・退職・stranger 化したユーザーは fail-closed
7. **route×token クロス不達**: 既存 MCP bearer で callback route に入れない / callback bearer で /mcp に入れない
8. **起動時契約**: hermes 鍵未設定・caller 鍵/bearer と同値・台帳テーブル未設定で起動拒否

いずれも**変異テスト**（ガードを意図的に壊して赤くなるか）で実質性を証明する。
