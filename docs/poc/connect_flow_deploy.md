# 営業の自己サービス Google 連携フロー — デプロイ & 1人テスト手順

営業が **Slack で `/teamagent connect` → リンクを開いて許可 → 連携完了**（ターミナル不要）を
実現する。実装: `src/teamagent/connect_web/`（コールバック）＋ `slack_bot.py` の `/teamagent_connect`。

## 仕組み（おさらい）
1. 営業が Slack で `/teamagent connect` → Bot が**本人専用の同意リンク**を ephemeral で返す
2. リンクを開く → Google 同意（7サービス readonly）→ 「許可」
3. Google が **connect_web の `/oauth2/callback`** にリダイレクト
4. callback が state 検証(CSRF) → code 交換 → **KMS暗号化して RDS に保存** → 「✅連携完了」表示

## 必要な env（Bot と connect_web で共有）
| env | 用途 | Bot | connect_web |
|-----|------|-----|-------------|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth クライアント（**Web型**） | ✓ | ✓ |
| `OAUTH_STATE_SECRET` | CSRF state の HMAC 鍵（**両者で同一値**） | ✓ | ✓ |
| `OAUTH_REDIRECT_URI` | connect_web の公開 callback URL | ✓ | ✓ |
| `OAUTH_KMS_KEY_ID` | トークン暗号化（`alias/teamagent-oauth-tokens`） | – | ✓ |
| `DATABASE_URL` | RDS（SSMトンネル or 同一VPC） | – | ✓ |

> `OAUTH_STATE_SECRET` は任意の長いランダム文字列を**Botとconnect_webで同じ値**に。
> 例: `export OAUTH_STATE_SECRET="$(openssl rand -hex 32)"`（両プロセスに同じ値を渡す）

## Google Cloud Console（あなたの作業）
- **Web アプリケーション型 OAuth クライアント**を使う（Desktop型はloopback専用）。
  - **承認済みリダイレクト URI** に `OAUTH_REDIRECT_URI` の値（例 `http://localhost:8788/oauth2/callback`、
    本番は `https://<連携サーバ>/oauth2/callback`）を登録。
- 同意画面は Internal（社内）。スコープは 7サービス readonly（コード側で固定）。
- ※ 既存の Desktop 型クライアント(`teamagent/dev/google_oauth`)は loopback 専用なので、
  **localhost ポートを redirect に登録できる Web 型**を別途用意するのが確実。

---

## ① 1人でローカルテスト（最短・同一Mac）
テスト相手が**この Mac を使える**前提（s-komata 自身など）。中央デプロイ不要。

```bash
cd ~/Documents/teamagent-orchestrator-poc

# Web型クライアントの値 + 共有 state secret + callback URL
export GOOGLE_CLIENT_ID='<Web型クライアントID>.apps.googleusercontent.com'
read -s GOOGLE_CLIENT_SECRET; export GOOGLE_CLIENT_SECRET
export OAUTH_STATE_SECRET="$(openssl rand -hex 32)"          # 控えておく（Botと同値に）
export OAUTH_REDIRECT_URI='http://localhost:8788/oauth2/callback'   # Console に登録した値
export OAUTH_KMS_KEY_ID='alias/teamagent-oauth-tokens'
export OAUTH_KMS_REGION='ap-northeast-1'
export DATABASE_URL="postgresql://teamagent:$(aws secretsmanager get-secret-value --secret-id teamagent/dev/db_password --region ap-northeast-1 --query SecretString --output text)@localhost:15433/teamagent"

# connect_web（callback）を起動（SSMトンネル 15433 が必要）
PYTHONPATH=src .venv/bin/python -m teamagent.connect_web   # → http://localhost:8788
```

別ターミナルで **同意リンクを生成**（Bot を起動せずに動作確認する最短路）:
```bash
cd ~/Documents/teamagent-orchestrator-poc
# 上と同じ GOOGLE_CLIENT_ID/SECRET / OAUTH_STATE_SECRET / OAUTH_REDIRECT_URI を export 済みの前提
PYTHONPATH=src .venv/bin/python - <<'PY'
from teamagent.adapters.google_oauth_flow import OAuthConsentFlow
import os
url, _ = OAuthConsentFlow(redirect_uri=os.environ["OAUTH_REDIRECT_URI"]).authorization_url("tester@vectorinc.co.jp")
print(url)
PY
```
→ 出た URL をブラウザで開く → 同意 → `http://localhost:8788/oauth2/callback` に戻り「✅連携完了」。
→ ダッシュボード（localhost:8787）の「Workspace連携状況」が **+1名** になれば成功。

## ② Slack から本番運用（営業に配る）
1. connect_web を**中央の到達可能 URL（HTTPS）にデプロイ**（EC2/ECS等）。`OAUTH_REDIRECT_URI` を
   その公開 URL（`https://.../oauth2/callback`）にし、Console のリダイレクト URI にも登録。
2. **Bot 側に env を設定**（`GOOGLE_CLIENT_ID/SECRET`・`OAUTH_STATE_SECRET`(connect_webと同値)・
   `OAUTH_REDIRECT_URI`）して Bot を再起動。Slack アプリに**スラッシュコマンド `/teamagent_connect`** を登録。
3. 営業は Slack で **`/teamagent connect`** → 返ってきたリンクを開いて「許可」するだけ。

---

## セキュリティ要点
- connect_web は**トークン書き込み（KMS暗号化 + RDS）= teamagent_app 級**。read-only 管理ダッシュボードとは
  **別アプリ・別権限**に分離してある（画面側にトークン書込権限を持たせない）。
- state は HMAC 署名（`OAUTH_STATE_SECRET`）で本人性・改竄を検証（CSRF対策）。
- 同意ページ/ログに refresh token は出さない（G8）。保存は KMS 暗号化＋本人行 RLS。

_作成: 2026-06-04 / PoC branch poc/multiskill-orchestrator。_
