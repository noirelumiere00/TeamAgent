# infra/openclaw — OpenClaw 外殻の隔離デプロイ雛形（WS-B）

TeamAgent を OpenClaw＋Claude(Bedrock) の自律型エージェントへ移行するための、
**自律外殻(OpenClaw)を安全に隔離して動かす**ためのテンプレ一式。すべて雛形であり、
実デプロイ（EC2 配置・IAM 適用・トンネル）はゲート①（Node 本番持込）承認＋本人操作で別途行う。
`terraform apply` は使わず targeted/手動。

> 関連プラン: `~/.claude/plans/mossy-snacking-locket.md`（§A 一次ソース確認結果・§C 本雛形）。

## 0. 不変条件（破ってはいけない）

1. **営業データ非接触**：`openclaw-gateway` は RDS/Secrets/KMS への IAM 権限もネットワーク到達も持たない。
   creds を持つのは `teamagent-mcp` バックエンドだけ。
2. **per-user 認可は MCP 境界内**：OpenClaw は単一信頼オペレータモデル（per-user 認可をしない／
   `sessionKey` は認可でない／共有 secret は full operator scope）。RLS・本人 OAuth は Python 側で 100% 死守。
3. **MCP は streamable-http**（私設網 `mcpnet`・bearer・内部のみ）。stdio は使わない
   （親=OpenClaw が子として MCP を同居起動し creds/network 隔離を壊すため）。
4. **秘密値はコミットしない**：設定にも `.env` にも“値”を書かず、Secrets Manager から注入する。

## 1. WS-B-B1 一次ソース確認サマリ（2026-06-09・`gh`/公式GitHub/docs 直確認）

| 項目 | 結論 |
|---|---|
| 実体/ライセンス | `openclaw/openclaw`（377k★）・**MIT**（OpenClaw Foundation）。旧名 Moltbot/Clawdbot |
| ランタイム | **TypeScript/Node**（Node 22.19+ 必須・24 推奨）＝ゲート①不可避 |
| 版pin | 最新 stable **2026.6.1**。min-safe **≥2026.5.26**（2026-05-28 の GHSA 30+件: Critical2/High多数を充足） |
| 信頼モデル | **単一信頼オペレータ**（敵対的マルチテナント境界ではない）→ 認可は MCP 内側 |
| 既定の危うさ | host exec 既定 **YOLO**(`security=full`/`ask=off`) → 本テンプレで `tools.exec.mode:"deny"` に封じる |
| Bedrock | `amazon-bedrock`/`bedrock-converse-stream`/**`auth:aws-sdk`（APIキー不要）**。region 未指定で us-east-1 に落ちる → 東京を明示 |
| MCP | `mcp.servers.<名>`、`streamable-http` 対応。`toolFilter`＋`tools.profile`＋sandbox `alsoAllow` の三層で露出制御 |

## 2. ファイル一覧

| ファイル | 役割 |
|---|---|
| `docker-compose.yml` | 隔離 gateway（digest pin・read_only・cap_drop ALL・no-new-privileges・loopback・資源上限）＋ creds 保持の `teamagent-mcp`。私設網 `mcpnet`(internal) で接続 |
| `openclaw.config.json5` | tool 最小化（exec deny / fs workspaceOnly / browser 既定厳格）・MCP(http,bearer,読取系のみ)・Bedrock(aws-sdk,東京,discovery 無効) |
| `iam/openclaw-role.policy.json` | 外殻ロール: `bedrock:InvokeModel(+Stream)` のみ Allow / Secrets・KMS・RDS は明示 Deny |
| `iam/mcp-backend-role.policy.json` | バックエンドロール: 対象 Secret・KMS decrypt・RDS connect・L2 Bedrock |
| `SOUL.md` / `HEARTBEAT.md` | ペルソナ / （P2 用）プロアクティブ・チェックリスト（P1 は heartbeat 無効） |
| `firewall/DOCKER-USER.after.rules` | 公開ポートの ingress 許可（内部網のみ）＋ egress は SG/プロキシで別途 |
| `.env.example` | 注入する**変数名のみ**（値は持たない） |

## 3. デプロイ手順（ゲート①承認後・本人が実行）

```sh
# (a) 版pin の digest を再確認・更新（タグ再ポイントに惑わされないため必ず digest で固定）
TOK=$(curl -s "https://ghcr.io/token?scope=repository:openclaw/openclaw:pull&service=ghcr.io" \
  | sed -E 's/.*"token":"([^"]+)".*/\1/')
curl -s -o /dev/null -D - -H "Authorization: Bearer $TOK" \
  -H "Accept: application/vnd.oci.image.index.v1+json" \
  https://ghcr.io/v2/openclaw/openclaw/manifests/2026.6.1 | grep -i docker-content-digest
# → docker-compose.yml の image: の @sha256:... を一致させる
# 2026-06-09 時点: sha256:b12f76a7947e4cdd328bf3ea1045d41a5494b33852c911e9bc4fdd03dde469d5

# (b) Bedrock の推論プロファイル ID を確認（モデル ID を openclaw.config.json5 に合わせる）
aws bedrock list-inference-profiles --region ap-northeast-1

# (c) シークレットを“環境へ注入”（値はコミットしない）。EC2 はインスタンスロール、ローカル検証は --env-file 等。
#     openclaw-role / mcp-backend-role の最小 IAM を各サービスへ付与。

# (d) 起動
docker compose -f infra/openclaw/docker-compose.yml up -d
```

## 4. 受け入れ確認（疎通・越権ゼロ）

- `teamagent-mcp`: `GET /healthz` が 200。`/mcp` は **bearer 無し→401**（fail-closed）。
- OpenClaw → MCP: 読取系 tool だけが見える（`toolFilter`）。write/draft/proposal_deck は不可視。
- **RLS 越権ゼロ**: 2 ユーザーで相互の専有データを要求 → 他人の行は 0。`_user_context.user_email` 無しは MCP 側で fail-closed。
- 外殻が RDS/Secrets に到達不可（疎通テストで証明）。exec/shell tool は呼べない（`mode:"deny"`）。

## 5. ロールバック

`USE_OPENCLAW_FRONTEND` フラグ OFF →（OpenClaw が Slack ingress を持つ間は現行 Bot 停止のため）
現行 Socket Mode Bot を起動で 1 分復帰。MCP バックエンドは捨てない（現行 Bot からも将来叩ける）。

## 6. 次にやること（P0→P1）

- `scripts/run_mcp_http_server.py`（本リポジトリ）= この compose の `teamagent-mcp` が起動する HTTP MCP。
- WS-C: `user_email` を OpenClaw でなく **Slack token から MCP 側で解決**（なりすまし防止）。
- P0 隔離 PoC: 営業データ非接触の環境で「RLS-through-MCP 漏洩ゼロ／インジェクション越権ゼロ／Bedrock(IAM)疎通」を実証。
