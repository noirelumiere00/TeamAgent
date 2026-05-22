# TeamAgent v3.1 → v3.2 移行ランブック（ドラフト）

> **目的**: 現在動作している v3.1 構成（`boto3 + slack-bolt + 自前 Skill Registry`）を、Sprint 2 末ゲート①で **B 案採用**が確定した場合に v3.2 構成（**OpenClaw Gateway + Amazon Bedrock + FastAPI 橋渡し**）へ段階的に移行するための運用手順書。
>
> **適用範囲**: Sprint 2 W1 PoC（2026-05-30）から Sprint 4 末（2026-07-10）まで。
>
> **前提となる訂正文書**: [`docs/v3.1/teamagent_design_corrections_2026-05-22.md`](../v3.1/teamagent_design_corrections_2026-05-22.md)（特に v0.3 セクション 5・6・7）。
>
> **ステータス**: ドラフト v0.1（2026-05-22）。Sprint 2 W2 ゲート①の結論を踏まえて v1.0 に昇格する。

---

## 0. アーキテクチャ目標（v3.1 → v3.2）

```
[v3.1 — 現在]
Slack ──(socket-mode, Python slack-bolt)── runtime/slack_bot.py ── SkillDispatcher ── SearchSkill
                                                                                          ├── BedrockClient (boto3)
                                                                                          ├── PgVectorClient (psycopg)
                                                                                          └── LocalE5Embedder

[v3.2 — B 案採用後]
Slack ──(socket-mode, OpenClaw Node.js)── EC2: openclaw-gateway:18789
                                                  │
                                                  ├── Bedrock provider (TS SDK, IAM Role 経由)
                                                  └── HTTP ──▶ teamagent-skills FastAPI (Python)
                                                                  ├── BedrockClient (boto3)
                                                                  ├── PgVectorClient (psycopg → RDS pgvector 東京)
                                                                  └── LocalE5Embedder
```

要点：

- Slack / モデル呼び出しの **入口**は OpenClaw に寄せる（23 チャネル拡張に備える）。
- 既存 Python 実装（`/Users/s-komata/Documents/TeamAgent/src/teamagent/skills/`）は **HTTP で再公開**して 85% 流用する。
- 認証は **API キーを使わず EC2 IAM Role + IMDSv2**。

---

## 1. 前提条件チェックリスト（移行開始前）

| # | 項目 | 確認方法 | 期待結果 |
|---|---|---|---|
| 1 | AWS アカウント | `aws sts get-caller-identity --profile default` | `Account` と `Arn` が返る |
| 2 | リージョン | `aws configure get region` | `ap-northeast-1` |
| 3 | Bedrock モデル有効化 | `aws bedrock list-foundation-models --region ap-northeast-1 --query 'modelSummaries[?contains(modelId,\`claude-sonnet-4-6\`)]'` | `jp.anthropic.claude-sonnet-4-6-v1:0` が返る |
| 4 | Slack App | Slack `api.slack.com/apps` で `TeamAgent_Dev_Ver.2` が存在 | `xoxb-...` と `xapp-...` トークンを `.env.example` 形式で控えている |
| 5 | pgvector RDS | `psql "$PG_DSN" -c 'SELECT count(*) FROM proposals_chunks;'` | 行数が 1 以上（Sprint 1 投入済み） |
| 6 | GitHub 権限 | `gh auth status` | `noirelumiere00` で push / PR 可 |
| 7 | gh CLI | `gh --version` | 2.x 以降 |
| 8 | AWS CLI | `aws --version` | 2.13 以降 |
| 9 | SSM プラグイン | `session-manager-plugin --version` | 1.2 以降。未インストールなら DEPLOYMENT.md セクション 2 を参照 |
| 10 | Docker | `docker --version && docker compose version` | Compose v2 |
| 11 | 既存 v3.1 テスト | `cd /Users/s-komata/Documents/TeamAgent && uv run pytest -q` | 24 件 PASS（移行前のベースライン） |
| 12 | Slack スコープ | App OAuth & Permissions 画面 | `app_mentions:read`, `chat:write`, `im:history`, `im:write` ほか 17 個 |

**成功確認方法**: 上記 12 項目すべてがチェック完了。失敗があれば該当行をスキップせず Sprint 2 W1 開始前に解消する。`uv run pytest -q` が落ちる場合は移行作業に入らず、v3.1 系の修正を優先する。

---

## 2. Sprint 2 W1（2026-05-30 〜 2026-06-05）— PoC

ゴール：**EC2 上の OpenClaw Gateway が Slack mention に応答して Bedrock Claude のテキストを返す**ところまで。既存 Python Skill は呼ばない。

### 2.1 リポジトリの fork

```bash
# 作業ディレクトリ
cd /Users/s-komata/Documents/TeamAgent/

# aws-samples を fork（gh で owner 配下に fork → ローカル clone）
gh repo fork aws-samples/sample-OpenClaw-on-AWS-with-Bedrock \
  --clone --remote --org=""

cd sample-OpenClaw-on-AWS-with-Bedrock

# 念のため upstream を固定
git remote add upstream https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock.git
git fetch upstream main
```

**成功確認**: `git remote -v` で `origin`（自分）と `upstream`（aws-samples）の両方が見える。

### 2.2 EC2 キーペアの作成

```bash
aws ec2 create-key-pair \
  --key-name teamagent-openclaw-poc \
  --region ap-northeast-1 \
  --query 'KeyMaterial' \
  --output text > ~/.ssh/teamagent-openclaw-poc.pem

chmod 400 ~/.ssh/teamagent-openclaw-poc.pem
```

**成功確認**: `aws ec2 describe-key-pairs --key-names teamagent-openclaw-poc --region ap-northeast-1` が `KeyPairId` を返す。

### 2.3 CloudFormation を ap-northeast-1 で apply

aws-samples の README はデフォルト `us-west-2` だが、TeamAgent は東京リージョン縛りなので `--region ap-northeast-1` を明示する。

```bash
# テンプレートのファイル名は openclaw-bedrock.yaml（旧称 clawdbot 由来）
aws cloudformation create-stack \
  --stack-name teamagent-openclaw-poc \
  --template-body file://openclaw-bedrock.yaml \
  --parameters \
    ParameterKey=KeyPairName,ParameterValue=teamagent-openclaw-poc \
    ParameterKey=openclawModel,ParameterValue=jp.anthropic.claude-sonnet-4-6-v1:0 \
    ParameterKey=InstanceType,ParameterValue=t3.medium \
    ParameterKey=CreateVPCEndpoints,ParameterValue=true \
  --capabilities CAPABILITY_IAM \
  --region ap-northeast-1

# 完了待ち（約 8 〜 12 分）
aws cloudformation wait stack-create-complete \
  --stack-name teamagent-openclaw-poc \
  --region ap-northeast-1
```

ポイント：

- `openclawModel` は東京リージョンで使える推論プロファイル `jp.anthropic.claude-sonnet-4-6-v1:0` を指定する（参考: 訂正ノート v0.3 セクション 5.1）。
- `t3.medium` は ARM 縛りを外して x86 にしておく（PoC で SSM がはまった場合の差分原因を減らす）。Sprint 3 以降に `t4g.medium` へ切り替え可能。
- `CreateVPCEndpoints=true` で Bedrock / SSM / S3 を VPC 内通信に閉じる。

**成功確認**: `aws cloudformation describe-stacks --stack-name teamagent-openclaw-poc --region ap-northeast-1 --query 'Stacks[0].StackStatus'` が `CREATE_COMPLETE`。

### 2.4 罠：`AWS_PROFILE=default` を `~/.openclaw/.env` に追記

これは Sprint 2 W1 で**ほぼ確実に踏む**罠なので、PoC 起動と同日に対処する。

**症状**: Slack から mention して `⚠ Agent failed before reply: No API key found for amazon-bedrock.` が返る。

**原因**: OpenClaw 2026.4.5+ の `pi-coding-agent` エンジンは `openclaw.json` の `"auth": "aws-sdk"` を読まず、`AWS_PROFILE` 環境変数を必要とする。systemd user service は shell env を継承しない。

**対処**:

```bash
INSTANCE_ID=$(aws cloudformation describe-stacks \
  --stack-name teamagent-openclaw-poc \
  --region ap-northeast-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`InstanceId`].OutputValue' \
  --output text)

aws ssm start-session --target "$INSTANCE_ID" --region ap-northeast-1
# ↓ ここから EC2 上
sudo -u ubuntu bash
echo "AWS_PROFILE=default" >> ~/.openclaw/.env
systemctl --user restart openclaw-gateway.service
systemctl --user status openclaw-gateway.service | head -20
exit
exit
```

**成功確認**: `~/.openclaw/.env` を `cat` して `AWS_PROFILE=default` の行があり、`systemctl --user status openclaw-gateway.service` が `active (running)`。

> 出典: [aws-samples TROUBLESHOOTING.md セクション 1](https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock/blob/main/TROUBLESHOOTING.md)、OpenClaw Issue #32290。

### 2.5 Slack トークンの投入

PoC 期間中は既存の `TeamAgent_Dev_Ver.2` トークンを共用する（Sprint 4 で完全移管時に再発行）。

```bash
aws ssm start-session --target "$INSTANCE_ID" --region ap-northeast-1
sudo -u ubuntu bash
cd ~/.openclaw

# 既存 .env に追記（AWS_PROFILE=default の行は残す）
cat >> .env <<'EOF'
SLACK_BOT_TOKEN=xoxb-XXXXXXXX-XXXXXXXX-XXXXXXXXXXXXXXXXXXXXXXXX
SLACK_APP_TOKEN=xapp-1-XXXXXXXXXXX-XXXXXXXXXXX-XXXXXXXXXXXXXXXXXXXX
EOF
chmod 600 .env

# openclaw.json に Slack channel を有効化
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".openclaw" / "openclaw.json"
cfg = json.loads(p.read_text())
cfg.setdefault("channels", {})["slack"] = {
    "enabled": True,
    "mode": "socket",
    "appToken": {"source": "env", "id": "SLACK_APP_TOKEN"},
    "botToken": {"source": "env", "id": "SLACK_BOT_TOKEN"},
}
p.write_text(json.dumps(cfg, indent=2))
PY

systemctl --user restart openclaw-gateway.service
XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u openclaw-gateway.service -n 50 --no-pager
```

**成功確認**: ログに `slack.socket-mode.connected` か同等の `connected` メッセージ。エラーログ（`Missing scope` / `invalid_auth`）がない。

### 2.6 PoC: `@TeamAgent_Dev_Ver.2 hello`

Slack の任意のチャネル（推奨 `#teamagent-poc`）で Bot を invite してから：

```
@TeamAgent_Dev_Ver.2 hello
```

期待される動作：

1. OpenClaw Gateway が `app_mention` を受信。
2. Bedrock `jp.anthropic.claude-sonnet-4-6-v1:0` を呼ぶ。
3. Slack に同じスレッドへ応答が返る（数秒〜十数秒）。

**成功確認**:

- Slack に返信が来る（内容は問わない）。
- EC2 で `journalctl --user -u openclaw-gateway.service -n 100` に `bedrock.invoke ok` 系のログ。
- CloudWatch Logs / Bedrock メトリクスで `InvokeModel` が 1 件以上カウントされる：

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Bedrock \
  --metric-name Invocations \
  --start-time $(date -u -v-15M +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 60 --statistics Sum \
  --region ap-northeast-1
```

ここまでで Sprint 2 W1 完了。失敗した場合は次節 2.7 のロールバック手順で stack を一旦消す。

### 2.7 W1 で詰まったときの即時ロールバック

```bash
aws cloudformation delete-stack \
  --stack-name teamagent-openclaw-poc \
  --region ap-northeast-1
aws cloudformation wait stack-delete-complete \
  --stack-name teamagent-openclaw-poc \
  --region ap-northeast-1
```

v3.1 の Slack Bot（`runtime/slack_bot.py`）は何もしていなければ稼働継続中。

---

## 3. Sprint 2 W2（2026-06-06 〜 2026-06-12）— ゲート①

ゴール：**B 案採用 / D 案不採用の最終判断**を確定し、Sprint 3 以降の作業範囲を凍結する。

### 3.1 動作確認チェックリスト

| # | 確認項目 | コマンド / 操作 | 期待結果 |
|---|---|---|---|
| 1 | Bedrock 直接呼び出し（EC2 内） | `aws bedrock-runtime invoke-model --model-id jp.anthropic.claude-sonnet-4-6-v1:0 --body '{"messages":[{"role":"user","content":[{"text":"ping"}]}],"inferenceConfig":{"maxTokens":50}}' --region ap-northeast-1 /tmp/o.json && cat /tmp/o.json` | `output.message.content[0].text` が返る |
| 2 | Slack mention 単発応答 | `@TeamAgent_Dev_Ver.2 1+1は？` | 数値 2 を含む応答 |
| 3 | Slack DM 応答 | DM で `hello` 送信 | 応答が返る |
| 4 | スレッド継続 | 上記応答にぶら下げて `日本語で要約して` | 同スレッドへ返信 |
| 5 | 同時接続 | 2 ユーザーが同時 mention | 双方に応答（順序問わず）|
| 6 | 落ちないか | 1 時間放置後に再度 mention | 応答が変わらず返る |
| 7 | ログ取得 | CloudWatch Logs `/openclaw/gateway/teamagent-openclaw-poc` | エラー率 < 5% |
| 8 | コスト | Cost Explorer Bedrock | 1 日 $1 未満 |

### 3.2 子会社運用ヒアリングとの突合

参照: `docs/v3.1/teamagent_subsidiary_questions_v2.md` の回答結果と上記 8 項目を一覧表で並べ、以下の 3 観点で OK/NG を付ける。

- **運用継続性**：子会社が現在使っている AI ボットの停止時間 / 復旧手順と、OpenClaw の運用負荷が許容範囲か。
- **権限分離**：子会社ユーザー 120 名に対する Skill 実行権限と、TeamAgent の営業 20 名権限が衝突しないか。
- **インシデント対応**：ClawHavoc 級事例が発生した場合に、`clawhub.disabled: true` 運用が子会社の期待値と矛盾しないか。

### 3.3 セキュリティ部レビュー（必須項目）

セキュリティ部に渡す資料は以下を最低限揃える：

1. CloudFormation テンプレート（fork 後の差分込み）
2. IAM ロールの `Action` 一覧（`bedrock:InvokeModel` / `bedrock:InvokeModelWithResponseStream` / `ssm:*` / `logs:*`）
3. EC2 セキュリティグループ（インバウンドは SSM のみ、18789 はローカルポートフォワード）
4. `~/.openclaw/.env` の取り扱い（`chmod 600`、ローテーション手順）
5. ClawHub 無効化の確認: `openclaw.json` で `"clawhub": {"disabled": true}` を Sprint 3 入り口で必ず投入する旨を明記

**成功確認**: セキュリティ部から書面（メール / Slack DM）で「Sprint 3 進行可」と承認。

### 3.4 B 案採用 / D 案不採用 の最終判断材料

ゲート①で「B 案採用」と決められる条件（全て満たすこと）：

- 3.1 の 8 項目すべて OK。
- 3.2 の子会社ヒアリングで「運用停止リスクあり」と評価された項目が 0。
- 3.3 のセキュリティ部承認が出ている。
- Bedrock 月次見込みコストが `EC2 t3.medium($30) + Bedrock($50想定) + 通信 < $100/月` に収まる。

満たさない項目が 1 つでもあれば **D 案（不採用）** に倒す。倒した場合は `sample-OpenClaw-on-AWS-with-Bedrock` の stack を `delete-stack` し、v3.1 構成のまま Sprint 3 に進む（Skill 追加・観測強化に集中する）。

---

## 4. Sprint 3（2026-06-13 〜 2026-06-26）— FastAPI ラップ実装

ゴール：既存 `src/teamagent/skills/` を **HTTP API として再公開**し、OpenClaw 側 SKILL から呼べる状態にする。Slack 入口はまだ v3.1（`runtime/slack_bot.py`）に残す。

### 4.1 `services/teamagent_skills_api/` を新設

ディレクトリ構成：

```
/Users/s-komata/Documents/TeamAgent/
├── services/
│   └── teamagent_skills_api/
│       ├── __init__.py
│       ├── main.py              # FastAPI エントリ
│       ├── routes/
│       │   ├── __init__.py
│       │   └── search.py        # POST /skills/search/invoke
│       ├── schema.py            # 共通の Envelope / ErrorResponse
│       ├── Dockerfile
│       └── pyproject.toml       # uv 管理、既存 src/teamagent をローカル依存
└── src/teamagent/               # 既存実装（無改修）
```

作成コマンド：

```bash
cd /Users/s-komata/Documents/TeamAgent/
mkdir -p services/teamagent_skills_api/routes
touch services/teamagent_skills_api/__init__.py
touch services/teamagent_skills_api/routes/__init__.py
```

### 4.2 FastAPI 最小実装サンプル

`services/teamagent_skills_api/schema.py`:

```python
"""HTTP 境界用の共通スキーマ。Skill の Pydantic は src/teamagent からそのまま使う。"""
from __future__ import annotations
from pydantic import BaseModel, Field

class InvokeMeta(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    user_id: str | None = None

class ErrorResponse(BaseModel):
    request_id: str
    error_code: str
    message: str
```

`services/teamagent_skills_api/routes/search.py`:

```python
"""POST /skills/search/invoke — 既存 SearchSkill を HTTP に上げる薄ラッパ。"""
from __future__ import annotations
import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from teamagent.adapters.embeddings_client import LocalE5Embedder
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchInput, SearchOutput
from teamagent.skills.search.skill import SearchSkill

router = APIRouter(prefix="/skills/search", tags=["search"])

# embedder のロードは重いのでモジュール起動時に 1 回だけ
_skill = SearchSkill(embedder=LocalE5Embedder())

class InvokeBody(BaseModel):
    input: SearchInput
    request_id: str | None = None
    user_id: str | None = None

@router.post("/invoke", response_model=SearchOutput)
def invoke(body: InvokeBody) -> SearchOutput:
    req_id = body.request_id or f"req-{uuid.uuid4().hex[:12]}"
    ctx = SkillContext(request_id=req_id, user_id=body.user_id)
    try:
        return _skill.run(body.input, ctx)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"skill_failed req={req_id}: {type(e).__name__}")
```

`services/teamagent_skills_api/main.py`:

```python
"""FastAPI エントリ。`uvicorn services.teamagent_skills_api.main:app` で起動。"""
from __future__ import annotations
from fastapi import FastAPI
from services.teamagent_skills_api.routes import search as search_routes

app = FastAPI(title="teamagent-skills", version="0.1.0")
app.include_router(search_routes.router)

@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
```

ローカル起動確認：

```bash
cd /Users/s-komata/Documents/TeamAgent
uv run uvicorn services.teamagent_skills_api.main:app --host 0.0.0.0 --port 8001
```

**成功確認**:

```bash
curl -s http://localhost:8001/healthz
# → {"status":"ok"}

curl -s -X POST http://localhost:8001/skills/search/invoke \
  -H 'Content-Type: application/json' \
  -d '{"input":{"query":"A社の前回提案は？","top_k":3}}' | jq .answer
# → Claude による日本語要約が返る
```

`runtime/slack_bot.py:71-99` の `SkillDispatcher.get_search_skill / run_search` と同一の Skill 実行経路を共有していることを確認する（依存先は `src/teamagent/skills/search/skill.py:64-103`）。

### 4.3 OpenClaw `SKILL.md` の書き方（HTTP 呼び出しテンプレート）

OpenClaw 上の Skill は YAML frontmatter + 自然言語本文だが、HTTP 呼び出しは本文の `curl` 指示で表現できる（訂正ノート v0.3 セクション 2.5）。

EC2 上で：

```bash
mkdir -p ~/.openclaw/skills/teamagent-search
cat > ~/.openclaw/skills/teamagent-search/SKILL.md <<'EOF'
---
name: teamagent-search
description: 営業 20 名向けの社内ナレッジ検索。過去提案書・議事録・メールを自然文クエリで検索し、引用付きで要約する。
allowed_tools: [bash]
---

# 役割

ユーザーから自然文クエリを受け取ったら、TeamAgent skills API に HTTP リクエストして検索結果を返す。

# 手順

1. ユーザーの問い合わせ文字列を `$QUERY` として取り出す。
2. 以下のコマンドで `teamagent-skills` API に問い合わせる：

```bash
curl -sS -X POST http://teamagent-skills.internal:8001/skills/search/invoke \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg q "$QUERY" '{input:{query:$q, top_k:5}}')"
```

3. レスポンス JSON の `answer` をユーザーに返す。`hits[].source` があれば「参考資料」として併記する。
4. 失敗時（HTTP != 200）はそのまま謝罪メッセージを返し、`request_id` を含めるよう促す。
EOF
```

**成功確認**: OpenClaw Web UI（`http://localhost:18789/?token=...`）で `teamagent-search` Skill が一覧に出現し、テスト実行で 200 が返る。

### 4.4 Docker Compose の更新

`infra/docker/docker-compose.yml`（既存 60 行、Sprint 1 で `postgres / adminer / minio` 構成）に `teamagent-skills` を追加する。

差分（既存 `volumes:` ブロックの直前に追記）：

```yaml
  teamagent-skills:
    build:
      context: ../../
      dockerfile: services/teamagent_skills_api/Dockerfile
    container_name: teamagent-skills
    restart: unless-stopped
    ports:
      - "8001:8001"
    environment:
      AWS_REGION: ap-northeast-1
      AWS_PROFILE: default
      PG_DSN: postgresql://teamagent:teamagent@postgres:5432/teamagent
      BEDROCK_MODEL_ID: jp.anthropic.claude-sonnet-4-6-v1:0
    volumes:
      - ~/.aws:/root/.aws:ro
    depends_on:
      postgres:
        condition: service_healthy
```

`services/teamagent_skills_api/Dockerfile`（新規）：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY services ./services
RUN uv sync --frozen
EXPOSE 8001
CMD ["uv", "run", "uvicorn", "services.teamagent_skills_api.main:app", "--host", "0.0.0.0", "--port", "8001"]
```

**成功確認**:

```bash
cd /Users/s-komata/Documents/TeamAgent/infra/docker
docker compose up -d teamagent-skills
curl -s http://localhost:8001/healthz | jq .
docker compose logs teamagent-skills | tail -20
```

### 4.5 pytest 24 件 + Pact 契約テスト

既存テストの実行：

```bash
cd /Users/s-komata/Documents/TeamAgent
uv run pytest -q          # 既存 24 件
uv run mypy --strict src/teamagent
```

Pact 契約テスト（新規）の置き場：`tests/contracts/test_search_invoke_pact.py`。最小例：

```python
"""OpenClaw 側が curl で投げる JSON が FastAPI 側の SearchInput と整合することを確認。"""
from fastapi.testclient import TestClient
from services.teamagent_skills_api.main import app

client = TestClient(app)

def test_search_invoke_contract():
    # OpenClaw SKILL.md 側で生成する JSON 形を固定
    payload = {"input": {"query": "A社の前回提案", "top_k": 5}}
    resp = client.post("/skills/search/invoke", json=payload)
    assert resp.status_code in (200, 500)  # 500 は実 DB 無しのとき
    body = resp.json()
    if resp.status_code == 200:
        # SearchOutput のキーがすべて存在することを契約として固定
        assert {"answer", "hits", "total_cost_usd"} <= set(body.keys())
```

実行：

```bash
uv run pytest tests/contracts -q
```

**成功確認**: 既存 24 件 + Pact 1 件 = 25 件以上 PASS。mypy strict は 0 エラー。

---

## 5. Sprint 4（2026-06-27 〜 2026-07-10）— Slack 移管

ゴール：Slack の入口を v3.1 の `runtime/slack_bot.py` から OpenClaw に**完全に切り替える**。

### 5.1 既存 `runtime/slack_bot.py` の引退手順

1. **凍結フラグ追加**: `runtime/slack_bot.py:208-218` の `_run()` 冒頭に環境変数チェックを足す。

   ```python
   if os.environ.get("TEAMAGENT_LEGACY_SLACK") != "1":
       raise SystemExit("v3.2 移行済み。OpenClaw を使ってください。")
   ```

2. **デフォルト停止**: ローカル / EC2 のどこでも `python -m teamagent.runtime.slack_bot` が起動しないようにする。
3. **ドキュメント差し替え**: `README.md` / `README.v2.md` のローカル起動セクションを「Slack は OpenClaw に移管済み」へ更新。

**成功確認**: `python -m teamagent.runtime.slack_bot` が SystemExit を返す。

### 5.2 OpenClaw 側に Slack を移管

すでに Sprint 2 W1 で OpenClaw 側に Slack 設定済み（`~/.openclaw/.env` + `openclaw.json`）。Sprint 4 では：

- 同じ Slack App `TeamAgent_Dev_Ver.2` を使い続ける（トークン再発行なし）。
- v3.1 の Bolt 接続を切るタイミング = OpenClaw 側を本番チャネル `#teamagent` に invite するタイミング。

### 5.3 Event Subscriptions の URL 切り替え

Socket Mode を使っているので Event Subscriptions の **Request URL は不要**（Slack 側からは Socket Mode 1 つしか張れないため、Bolt 側を止めれば自動的に OpenClaw に集約される）。Webhook モードへの切替が必要になった場合のみ：

1. Slack App → Event Subscriptions → Enable Events
2. `Request URL` に `https://<openclaw-public-domain>/slack/events` を入力
3. Slack が `url_verification` チャレンジを投げる → OpenClaw が `challenge` をそのまま返す

### 5.4 リバースプロキシ / TLS 設定（Webhook モード採用時のみ）

Socket Mode のままなら不要。Webhook に倒す場合：

- EC2 前段に Application Load Balancer（ALB）を立てる。
- ACM で `*.teamagent.example.com` の証明書を取得（東京リージョン）。
- ALB Listener: 443 → EC2 18789（HTTP）。
- Security Group: ALB → EC2 18789 のみ許可。
- OpenClaw Gateway 側に `OPENCLAW_PUBLIC_BASE_URL=https://teamagent.example.com` を設定。

**成功確認**: `curl -s https://teamagent.example.com/healthz` が 200 を返し、Slack Event Subscriptions の Verified が緑になる。

---

## 6. ロールバック手順

v3.2 で深刻な障害が出た場合、**v3.1 構成に巻き戻す**手順。

### 6.1 即時切り戻し（〜 30 分）

1. OpenClaw Gateway を停止：

   ```bash
   aws ssm start-session --target "$INSTANCE_ID" --region ap-northeast-1
   sudo -u ubuntu bash -c 'systemctl --user stop openclaw-gateway.service'
   ```

2. v3.1 Slack Bot を再起動（ローカル Mac か別 EC2 で）：

   ```bash
   cd /Users/s-komata/Documents/TeamAgent
   TEAMAGENT_LEGACY_SLACK=1 \
   SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... \
   uv run python -m teamagent.runtime.slack_bot
   ```

3. Slack で `@TeamAgent_Dev_Ver.2 ping` を打って Bolt 経由で応答が返ることを確認。

### 6.2 Terraform state の扱い

- `infra/terraform/` は **RDS pgvector のみ** を管理しており、OpenClaw 用 EC2 は CloudFormation 側（aws-samples fork）。
- ロールバックでは Terraform state には触らない（pgvector は v3.1 / v3.2 両方で共有）。
- CloudFormation 側だけ：

   ```bash
   aws cloudformation delete-stack \
     --stack-name teamagent-openclaw-poc \
     --region ap-northeast-1
   ```

### 6.3 Slack トークン再取得の要否

- **Sprint 4 までは不要**: v3.1 と OpenClaw が同じ `TeamAgent_Dev_Ver.2` の Bot / App トークンを共用しているため、片方を止めるだけで切り替わる（Socket Mode は同時 1 接続のみ）。
- **Sprint 4 で本番チャネル移管後にロールバック**する場合に限り、Slack App 側の管理者で `xoxb-` / `xapp-` を一度 revoke → 再発行し、`runtime/slack_bot.py` 側の `.env` に投入し直す。理由：直前まで OpenClaw が掴んでいた Socket Mode が残留する可能性があるため。

### 6.4 ロールバック判断の境界

| 障害 | 一次対応 | ロールバック |
|---|---|---|
| Slack 応答が 5 分連続無応答 | 5.1 セクション 1 で gateway を再起動 | 30 分以上続けば実施 |
| Bedrock 4xx スパイク | モデル ID と IAM を確認 | コスト異常を伴えば実施 |
| FastAPI 5xx 多発 | docker compose logs / pytest 再実行 | 復旧見込み 1 時間超で実施 |
| EC2 突然落ち | CloudFormation で自動回復 | 自動回復 2 回連続失敗で実施 |

---

## 7. 観測・運用

### 7.1 CloudWatch Logs 統合

- OpenClaw Gateway: CloudFormation 既定で `/openclaw/gateway/<stack-name>` ロググループに出力。保持期間は CloudFormation 側で 30 日に設定 → Sprint 3 で 90 日へ延長予定（コンプライアンス要件次第）。
- FastAPI: コンテナ標準出力 → CloudWatch Logs Agent → `/teamagent/skills/<env>`。`structlog` の JSON ログをそのまま流す。
- Slack 側応答時間: OpenClaw が `bedrock.invoke.duration_ms` をログに含むので Logs Insights で集計可能。

**Logs Insights クエリ例**（直近 1 時間のエラー率）：

```
fields @timestamp, request_id, error_code
| filter @logStream like /teamagent-skills/
| stats count() as total, sum(strcontains(@message, '"level":"error"')) as errs by bin(5m)
| display bin, errs/total as error_rate
```

### 7.2 メトリクス（usage / cost / latency）

| メトリクス | 取得元 | 取得方法 | 目標 SLO |
|---|---|---|---|
| 1 日あたりの mention 数 | OpenClaw ログ | Logs Insights `stats count(*) by bin(1d)` | — |
| Bedrock InvokeModel 数 | AWS/Bedrock | CloudWatch Metric | — |
| 1 クエリあたりコスト | FastAPI `total_cost_usd` | CloudWatch Custom Metric（put_metric_data） | < $0.05 |
| 応答 latency p95 | FastAPI ログ | Logs Insights | < 15 秒 |
| FastAPI 5xx 率 | ALB（採用時）or アプリログ | CloudWatch | < 1% |

`SearchOutput.total_cost_usd` は既存 `src/teamagent/skills/search/skill.py:163-164` で計算済みなので、`/skills/search/invoke` レスポンスから CloudWatch Custom Metric に流すだけでよい。

### 7.3 アラート設定

CloudWatch Alarms（最低限）：

```bash
# Bedrock スロットリング
aws cloudwatch put-metric-alarm \
  --alarm-name teamagent-bedrock-throttle \
  --metric-name ClientErrors \
  --namespace AWS/Bedrock \
  --statistic Sum --period 60 --threshold 5 \
  --comparison-operator GreaterThanThreshold --evaluation-periods 2 \
  --alarm-actions arn:aws:sns:ap-northeast-1:<acct>:teamagent-oncall \
  --region ap-northeast-1

# FastAPI 5xx
aws cloudwatch put-metric-alarm \
  --alarm-name teamagent-skills-5xx \
  --metric-name HTTPCode_Target_5XX_Count \
  --namespace AWS/ApplicationELB \
  --statistic Sum --period 60 --threshold 10 \
  --comparison-operator GreaterThanThreshold --evaluation-periods 3 \
  --alarm-actions arn:aws:sns:ap-northeast-1:<acct>:teamagent-oncall \
  --region ap-northeast-1

# 1 日あたりコスト
aws cloudwatch put-metric-alarm \
  --alarm-name teamagent-daily-cost \
  --metric-name EstimatedCharges \
  --namespace AWS/Billing \
  --statistic Maximum --period 21600 --threshold 5 \
  --comparison-operator GreaterThanThreshold --evaluation-periods 1 \
  --alarm-actions arn:aws:sns:ap-northeast-1:<acct>:teamagent-oncall \
  --region us-east-1
```

SNS トピック `teamagent-oncall` には営業 20 名のうち担当 2 名 + 情シス 1 名のメールアドレスを登録する。

### 7.4 インシデント対応プロセス

1. **検知**: CloudWatch Alarm → SNS → メール / Slack `#teamagent-alerts`。
2. **トリアージ**（5 分以内）:
   - 影響範囲（Slack 応答全停止 / 一部 Skill のみ / 高コスト）を判定。
   - 直近 30 分の Logs Insights を見て原因仮説を立てる。
3. **緩和**:
   - Slack 応答停止 → 6.1 の即時切り戻し。
   - 高コスト → OpenClaw `openclaw.json` で `maxTokens` を一時的に半分にして restart。
   - Skill 単体障害 → FastAPI のみ再起動（`docker compose restart teamagent-skills`）。
4. **記録**: `docs/v3.2/incidents/<YYYY-MM-DD>-<short>.md` を作成。最低限：時系列 / 影響 / 原因 / 再発防止策。
5. **再発防止**: アラート閾値 / Skill タイムアウト / IAM 最小権限を 1 週間以内にレビュー。

---

## 8. 完了基準（Sprint 4 末）

- Slack 入口が OpenClaw に完全に移っている。
- `services/teamagent_skills_api/` が 24h 連続稼働で 5xx < 1%。
- `runtime/slack_bot.py` は `TEAMAGENT_LEGACY_SLACK=1` を立てない限り起動しない。
- CloudWatch Alarm 3 本（throttle / 5xx / cost）がすべて Green。
- v3.2 ドキュメント本体（`docs/v3.2/teamagent_overview_v3.2.md`）が本ランブックを差分なしに参照できる。

---

## 9. 参考文献

- `docs/v3.1/teamagent_design_corrections_2026-05-22.md`（特に v0.3 セクション 5・6・7）
- aws-samples [README.md](https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock/blob/main/README.md) / [DEPLOYMENT.md](https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock/blob/main/DEPLOYMENT.md) / [TROUBLESHOOTING.md](https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock/blob/main/TROUBLESHOOTING.md)
- OpenClaw 公式 Bedrock provider: <https://raw.githubusercontent.com/openclaw/openclaw/main/docs/providers/bedrock.md>
- OpenClaw 公式 Slack channel: <https://raw.githubusercontent.com/openclaw/openclaw/main/docs/channels/slack.md>
- 既存実装の主要参照点:
  - `src/teamagent/skills/base.py:64-126`（BaseSkill + Registry）
  - `src/teamagent/skills/search/skill.py:64-103`（SearchSkill.run）
  - `src/teamagent/runtime/slack_bot.py:61-99`（SkillDispatcher）
  - `infra/docker/docker-compose.yml`（v3.2 で `teamagent-skills` を追加）

---

## 更新履歴

| 日付 | バージョン | 更新内容 |
|---|---|---|
| 2026-05-22 | v0.1 | 初版ドラフト（Sprint 2 W1 〜 Sprint 4 まで通し、ロールバック / 観測を含む） |
