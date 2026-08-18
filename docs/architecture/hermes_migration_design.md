# ADR: Hermes Agent 段階導入設計（hermes_migration_design）

- Status: **Accepted for docs（PR1）— 実装は PR ごとに個別承認**
- 作成日: 2026-08-18（Session 1 監査 + Session 2 再検証・敵対審査を反映した補正版）
- 基準: dev @ 95d45a8。本文中の file:line は同 commit 時点
- 関連: [hermes_implementation_plan.md](hermes_implementation_plan.md)（PR 分割・テスト戦略）

---

## 1. Executive Summary

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
| Hermes | Personal Agent / Memory / Experience | 本人スコープ memory・短命 session claim + K_session | RDS/pgvector・OAuth token・Secrets・Slack token・既存 MCP bearer |
| Bedrock Claude | Intelligence | — | — |

## 7. Security Boundary — Delegated Session Claim 設計（敵対審査反映版）

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
| Personal Memory | 好み・定型フォーマット・作業パターン・過去の修正・優先順位 | 本人のみ（profile_id 単位・RLS） |
| Personal Sessions | Hermes セッション履歴（FTS5 検索） | 本人のみ |
| Company Shared | Skills・MCP tool 面・ポリシー・承認済み workflow | 全社（review 必須） |

## 10. Memory Governance

- **禁止**: Slack/Gmail/Salesforce/RAG/Drive 本文の Memory へのコピー（Source of Truth 側で都度検索）。Hermes には検索結果の永続化 API を渡さない＝構造で禁止
- Memory に書けるのは再利用可能な個人知識のみ（preferences / formatting / repeated corrections / workflow habits / preferred terminology / approved personal context）
- 書込は propose → pending → approve。Company Skill への昇格は Owner/Admin review 必須
- memory_read / memory_write は監査ログへ（§18）

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

## 20. Failure / Rollback / PR2 の dark 形態

- 各 Phase は env flag 1 個で完全 rollback: `USE_HERMES_ORCHESTRATOR=0` → list_tools から消滅（run_agent と同機構）
- Hermes down → run_hermes_agent は構造化エラー（既存 `_err` 契約）→ OpenClaw は既存 L1 で応答継続（SOUL の「境界が拒否したら素直に伝える」規範に接続）
- **PR2 の dark runtime 形態（裁定済み）**: 常駐タスク 0（Terraform で desired_count を 0 と宣言・手動の ECS 直接操作ではない）を採る。受け入れ試験は **ECS RunTask で 1 タスクだけ起動し、startup → /healthz → Bedrock client 初期化 → CloudWatch logs を確認して終了**する形（本 repo の「run-task 検証」標準と同型）。常駐ゼロなので idle コストゼロ・外部 routing ゼロ・MCP exposure ゼロが自明に成立する。PR3 で接続する際に Terraform 変更として desired_count を 1 へ上げる（それでも flag OFF なら tool 面に出ない）

## 21. Migration Phases

| Phase / PR | 内容 | flag | 出口条件 |
|---|---|---|---|
| PR1 | docs only（本 ADR + README 全面更新） | — | CI 緑・code diff 0 |
| PR2 | Hermes dark runtime（ECS 常駐タスク 0・IAM 最小・healthz） | — | RunTask 受け入れ試験（§20） |
| PR3 | run_hermes_agent + delegated claim + callback boundary + Security Tests | USE_HERMES_ORCHESTRATOR=0 のまま | マージブロッカーテスト 8 本（§25）全緑・OC include 非掲載の契約テスト |
| **PR-R** | **容量制御（必須 Gate・§22）** | — | admission control + in-flight metrics + heavy-tool semaphore + 明示 overload 応答 |
| PR4 | Proposal Specialist を限定ユーザーへ | HERMES_ALLOWED_EMAILS | 既存フローとの A/B（quality/latency/cost/citation） |
| PR5 | Personal Profile（profile store） | USE_HERMES_PROFILES | profile 分離テスト緑 |
| PR6 | Personal Memory（approval 付き） | USE_HERMES_MEMORY | memory 隔離テスト緑 |
| PR7 | Multi source（GWS/Slack/RAG/Salesforce・全て MCP 経由・policy version 更新を含む） | tool 別 | 各 tool の監査ログ確認 |
| PR8 | AI General / Router | SOUL+config | fast path の latency 劣化なし |
| Phase 7 | OpenClaw role review: **KEEP / THIN / REPLACE** をここで初めて判断 | — | Hermes 成熟度評価 |

**PR-R は PR4（実ユーザー routing 開始）前の必須 Gate**。PR1〜PR3 は dark のためブロッカーではない。

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
| delegated claim の設計穴 | HIGH | §7 の敵対審査反映設計 + §25 マージブロッカーテスト。**PR2 完了後・PR3 着手前に delegated claim 周りの再レビューを実施**（裁定済み） |
| toolFilter がクライアント側ゲートであることの誤解 | HIGH | §6 の禁止形を明文化・callback は別 route + server-side policy |
| 会社共有モードで per-user OAuth 面が開く | HIGH | §7.5 v1 hard deny + 将来は policy version + G1 強化 |
| Memory への会社データ混入 | HIGH | 永続化 API 非公開 + 監査 job |
| Hermes runtime の CVE/供給網 | MED | digest 固定・SBOM・署名リリース鎖に載せる（OpenClaw と同水準） |
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

詳細は [hermes_implementation_plan.md](hermes_implementation_plan.md)。PR3 のマージブロッカー（これが緑でなければマージ不可）:

1. **cross-user session race**: 同一 Hermes プロセスに A/B の session が並存しても claim/K_session が混線しない（既存 `test_same_session_cross_user_race…` の同型）
2. **予算ストア障害時に skill が実行されない**（fail-closed・resolver より前）
3. **max_calls 並行 race**: 16 本同時 callback で成功がちょうど 8 本（nonce 消費 + call count increment が単一 `TransactWriteItems` で原子的であることの実証・replay で budget だけ削れないこと）
4. **鍵の双方向偽造不可**: caller 鍵で hermes claim を作れない / 逆も / MCP bearer ではどちらも不可
5. **denylist 優先**: allowed_tools に run_hermes_agent / mail_draft を入れても server-side denylist が勝つ
6. **resolver 再実行**: callback 時点でゲスト降格・退職・stranger 化したユーザーは fail-closed
7. **route×token クロス不達**: 既存 MCP bearer で callback route に入れない / callback bearer で /mcp に入れない
8. **起動時契約**: hermes 鍵未設定・caller 鍵/bearer と同値・台帳テーブル未設定で起動拒否

いずれも**変異テスト**（ガードを意図的に壊して赤くなるか）で実質性を証明する。
