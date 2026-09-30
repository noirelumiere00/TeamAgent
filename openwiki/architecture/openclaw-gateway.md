---
type: architecture
title: OpenClaw ゲートウェイ（Slack 受け口）
description: Aico の Slack 受け口である OpenClaw の設定（Socket Mode・dmPolicy/allowFrom・Haiku モデル・native ツールの封鎖・MCP 接続と toolFilter）、起動時の entrypoint 検査、SOUL/IDENTITY の seed、CI の不変条件チェックと effective-tool-scope。
tags: [openclaw, slack, config, security, soul, toolfilter]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T07:51:05.076Z
sources:
  - id: openwiki-source-95e7edbccd5ba6a6207fc3bc
    resource: repo://infra/docker/openclaw-entrypoint.mjs
  - id: openwiki-source-58cab42b7659adb32893d79a
    resource: repo://infra/openclaw/effective-tool-scope.json
  - id: openwiki-source-11b6c3c162105aea7c4bba90
    resource: repo://infra/openclaw/openclaw.config.json5
  - id: openwiki-source-7ba7bf2481758d43c5b078b1
    resource: repo://scripts/check_openclaw_config.py
generated: { by: "claude-code", at: "2026-09-29T07:51:05.076Z" }
---

# OpenClaw ゲートウェイ（Slack 受け口）

## 役割

<!-- openwiki: broken internal link [/openwiki/architecture/mcp-gateway.md] link "/openwiki/architecture/mcp-gateway.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
OpenClaw（TypeScript/Node 製のエージェント実行基盤、本番は 2026.7.1）は Slack からの DM・メンションを受け、Bedrock の Claude Haiku 4.5 で「どのツールを呼ぶか」と「最終的な返事の文面」を決める外殻。**営業データには直接触れない**: 能力は「Slack 受信」「Bedrock 推論」「レビュー済み TeamAgent MCP ツール」の 3 つだけで、本人ごとの認可は MCP gateway 側で行う（[MCP gateway](/openwiki/architecture/mcp-gateway.md)）。

たとえるなら、OpenClaw は受付係。用件を聞いて適切な窓口（MCP ツール）に回し、返事を整えて返すが、金庫（DB・メール・Drive）の鍵は持っていない。

設定の正本は `infra/openclaw/openclaw.config.json5`（JSON5・秘密値は書かず `${VAR}` で env を参照）。

## 主な設定

| 区分 | 設定 | 意味 |
|---|---|---|
| エージェント | `agents.list[0].model` | Bedrock の東京推論プロファイル（`amazon-bedrock/jp.anthropic.claude-haiku-4-5-…`） |
| | `identity.name` | `Aico` |
<!-- openwiki: broken internal link [/openwiki/architecture/caller-identity-and-button-bindings.md] link "/openwiki/architecture/caller-identity-and-button-bindings.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| | `heartbeat.every: "0m"` | プロアクティブ実行なし（ボタン押下後に AI の run が起きない理由。[ボタン束縛](/openwiki/architecture/caller-identity-and-button-bindings.md)） |
| | `bootstrapMaxChars: 32000` | SOUL.md などの 1 ファイル上限。既定 20,000（UTF-16 単位）で SOUL.md が切れて全ツールが止まった事故の対策 |
| プラグイン | `plugins.allow` | `slack`・`amazon-bedrock`・`teamagent-caller-identity` の 3 つだけ |
| Slack | `mode: "socket"` | Socket Mode（公開エンドポイント不要） |
| | `groupPolicy: "open"` | bot が招待されたチャンネルでだけ反応 |
| | `dmPolicy` / `allowFrom` | 焼き込み値は `"open"` + `["*"]`（下記） |
| | `replyToModeByChatType` | チャンネル・グループはスレッド返信、DM は平打ち |
| | `statusReactions` | 👀→🧠→🛠→✅/❌ の進捗リアクション |
| セッション | `session.dmScope: "per-channel-peer"` | 利用者ごとに会話を分離（既定だと人をまたいで混ざる） |
| 履歴 | `messages.groupChat.historyLimit: 20` | 長いスレッドでのトークン増を止める |
| ツール | `tools.profile: "minimal"` + `alsoAllow: ["bundle-mcp"]` | MCP ツールだけを見せる |
| | `tools.deny` | message・read・write・edit・send・delete・upload・sessions_* などの native ツールを全部拒否 |
| | `exec.mode: "deny"`、`fs.workspaceOnly` | ホストコマンド実行禁止 |
| ゲートウェイ | `bind: "loopback"`、`port: 18789`、`terminal.enabled: false` | 管理面は外に出さない |
| MCP | `mcp.servers.teamagent.url` | Cloud Map 内部名の `:8787/mcp`、`streamable-http`、bearer ヘッダ |
| | `timeout: 600` | 全ツール共通の上限（秒） |
| | `toolFilter.include` / `exclude` | Aico から見えるツールの明示許可リスト |

### DM の開放範囲（dmPolicy / allowFrom）

OpenClaw の Slack plugin は `dmPolicy: "open"` でも `allowFrom` に `"*"` が無いと列挙者だけを通す allowlist として動き、他の DM を**無音で捨てる**（過去の本番事故）。そのため 2 段で守っている。

1. **CI**: `scripts/check_openclaw_config.py` が `dmPolicy`・`groupPolicy`・`allowFrom` を抽出し、「`open` なのに `"*"` が無い」「`allowFrom` が空配列」「不正な値」を検出して exit 1（CI の「OpenClaw config invariants」ステップ）。
2. **起動時**: `infra/docker/openclaw-entrypoint.mjs` が env `SLACK_DM_ALLOWLIST` を必須にする。`"*"` なら焼き込みの `open + ["*"]` を維持し、`U…` の ID 列（1〜100 件・重複なし・空白なし）なら `dmPolicy: "allowlist"` と exact ID の `allowFrom` に同時に置き換える。未設定・空・混在は起動拒否（exit 78）。

## entrypoint の起動検査

`openclaw-entrypoint.mjs` はシェルの無いイメージで Node から直接起動され、次を行う。

- 必須秘密（`SLACK_BOT_TOKEN`・`SLACK_APP_TOKEN`・`OPENCLAW_GATEWAY_TOKEN`・`TEAMAGENT_MCP_BEARER`・`TEAMAGENT_CALLER_CLAIM_SECRET`）の存在を確認。caller claim の秘密が MCP bearer と同じなら拒否。`SLACK_TEAM_ID` の形も検査。
- 必須プラグイン 3 つの配置先・パッケージ名・版（OpenClaw 本体と同じ 2026.7.1、caller-identity は 1.0.0）を固定。
- 子プロセスに渡す env を許可リスト（ECS の認証情報・CA・プロキシ設定など）に限定。`NODE_OPTIONS` は受け付けない。
- 実行ディレクトリを `/tmp/teamagent-openclaw` に固定し、symlink を拒否して 0700 で作る。
- レビュー済みの `SOUL.md`・`HEARTBEAT.md`・`IDENTITY.md` を**毎起動上書き**で workspace に seed する。workspace は EFS 上に永続するため、初回だけ seed する方式では改名後も旧人格が残り続けた。
- 起動完了時に `openclaw_runtime_ready`（commit・branch・dmPolicy・allowFrom 件数・uid）を JSON で stderr に出す。

## SOUL.md（ペルソナと振る舞いの規則）

`infra/openclaw/SOUL.md` は毎リクエストの system prompt に載る指示書で、次のような節を持つ: 役割、社内用語辞書、セキュリティ境界、MCP ツール呼び出しの不変条件、「できません」と言う前にツール一覧を当たる、連携（`oauth_connect`）は一語でも呼び URL を自作しない、朝ダイジェストの各ボタンへの対応、メール・カレンダー・Slack 要約・添付・動画・リサーチ系ツールの使い分け、検索結果の忠実性、Slack の書き方、長時間処理への答え方、トーンと文体。

モデルへの指示だけでは止まらない事故（URL の自作・ツールを呼ばない断り・文面の組み直し）があったため、重要な部分は plugin（層1〜3）や MCP 側の直接投稿でコードとしても強制している。SOUL.md の長さは `tests/infra/test_soul_contract.py` の上限で縛られている（bootstrap は毎リクエストのトークンを食う）。

## どのツールが Aico から呼べるか

`toolFilter.include` に載っていても、MCP 側でそのツールが登録されていなければ呼べない。逆も同じ。`infra/openclaw/effective-tool-scope.json` がこの関係の正本で、`effectiveRule` に「OpenClaw の include に載り、かつ MCP backend がデプロイ済みタスクで登録したツールだけが呼べる」と書かれている。各ツールの副作用分類（`effect`）、terraform 側のゲート、`enabledBy`（`always` / env / `never` など）もここで管理される。

<!-- openwiki: broken internal link [/openwiki/architecture/tool-registry-and-feature-flags.md] link "/openwiki/architecture/tool-registry-and-feature-flags.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
意図的に include しないもの: `run_agent`（L2 オーケストレーター。OpenClaw 自身が外殻なので二重オーケストレーションを避ける）、`chitchat`・`recommend`・`proposal_campaign`・`mail_constraints`・`workspace_search`・`proposal_deck`・同期版 `proposal_builder`、`*confirm*`。`video_approval`・`operation_log`・`knowledge_search_url` は include にあるが scope 上 `enabledBy=never` で、解禁には「tf の task env・scope の enabledBy・contract テスト・OpenClaw イメージ再ビルド」の 4 点を同じ変更で揃える。詳細は [ツール登録と機能フラグ](/openwiki/architecture/tool-registry-and-feature-flags.md)。

## イメージと剪定

<!-- openwiki: broken internal link [/openwiki/operations/container-images-and-build.md] link "/openwiki/operations/container-images-and-build.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`infra/openclaw/README.md` によれば、本番イメージは OpenClaw 2026.7.1・`linux/arm64`・digest 固定の Chainguard（Wolfi）Node ベースで UID/GID 65532 で動く。最終イメージにシェル・パッケージマネージャ・ブラウザ・Playwright・コンパイラ・テスト資材は入らない。`prune-runtime.mjs` がモジュール閉包を計算してから不要パッケージを消し、Slack と Bedrock の動作に必要な閉包が残ることを `--network none` で確かめる。ビルドと公開のゲートは [コンテナイメージとビルド](/openwiki/operations/container-images-and-build.md)。README は production bundle contract が `release.ready=false` のままであり、ローカルの PASS は本番の承認ではないと明記している。

## 注意・食い違い

- config の MCP `timeout` は 600 秒だが、`src/teamagent/mcp_gateway/detached_jobs.py` の説明では OpenClaw が約 6 分（360 秒）で実行を打ち切るとされている。ツール呼び出しの上限と、エージェント run 全体の上限は別物と読めるが、どの設定が 360 秒を決めているかは本 config からは確認できない。
- config のコメントには過去の版（2026.6.x）の記述が残っているが、本番は 2026.7.1（Dockerfile・entrypoint・plugins-lock が一致）とコメント自身が訂正している。
- `deploy_to_ec2.sh` 系の古い手順は Slack secret の命名を旧形式に戻しうる（CLAUDE.md §4 B5）。OpenClaw 用の Slack token は ECS secrets から `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` として注入される。

## テスト

- `scripts/check_openclaw_config.py`（CI）と、その単体テスト（`tests/scripts/`）。
- `tests/infra/test_soul_contract.py`（SOUL の長さと必須節）。
- `tests/test_openclaw_action_bindings.py`・`tests/test_openclaw_button_direct.py`（plugin の実物を使う E2E）。
