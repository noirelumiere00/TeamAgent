# TeamAgent 管理画面 — セットアップ & 起動 runbook（MVP・ローカル）

オーナーが自分の Google でログインして、利用状況・コスト・レイテンシ・エラー・Workspace 連携状況・
同時実行を見るための内部ダッシュボード。MVP は**あなたの Mac でローカル起動**し、既存の SSM
ポートフォワード経由で東京 RDS を読む（ブラウザは localhost＝公開面ゼロ）。

実装: `src/teamagent/dashboard/`（FastAPI・依存追加なし）。データ源 = Bot が書く
`usage_events`/`runtime_metrics` と既存 `oauth_tokens`（read-only ロール `teamagent_dashboard`）。

---

## 0. 全体像（3ステップ）

1. **DB を準備**: migration 0007/0008 を RDS に適用（usage_events / runtime_metrics / read-only ロール）。
2. **Bot を起動**: `DATABASE_URL` があれば Bot が自動で利用イベント記録＋15秒メトリクス snapshot を始める（追加設定不要）。
3. **画面を起動**: `python -m teamagent.dashboard` をローカルで実行 → ブラウザで `http://127.0.0.1:8787`。
   - 最初は **dev-bypass** で即閲覧可。Google ログインは Web OAuth クライアント作成後に有効化。

---

## 1. DB マイグレーション適用（0007 / 0008）

SSM トンネルを上げて（踏み台経由・ローカル 15433 等）、`DATABASE_URL` を通して migrate を流す。

```bash
# 1) トンネル（別ターミナルで維持）
aws ssm start-session --target i-04fd1f367b454f641 --region ap-northeast-1 \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com"],"portNumber":["5432"],"localPortNumber":["15433"]}'

# 2) DB パスワード
PGPW=$(aws secretsmanager get-secret-value --secret-id teamagent/dev/db_password \
  --region ap-northeast-1 --query SecretString --output text)

# 3) forward-only ランナーで未適用ぶんだけ適用（0007/0008 が走る）
cd ~/Documents/teamagent-orchestrator-poc
DATABASE_URL="postgresql://teamagent:${PGPW}@localhost:15433/teamagent" \
  PYTHONPATH=src .venv/bin/python scripts/migrate.py
```

> `scripts/migrate.py` は適用済みを `schema_migrations` で管理し、未適用の 0007/0008 だけを流す。
> 0007 は read-only ロール `teamagent_dashboard` を作り、`teamagent` に SET ROLE 権限を付与する。

---

## 2. Bot 側テレメトリ（自動・追加実装不要）

Bot（`slack_bot.py`）は起動時に `DATABASE_URL` があれば自動で:
- 各リクエスト出口で `usage_events` に1行記録（best-effort・本文/PII/トークンは保存しない）。
- 15秒ごとに RequestGate/接続プールの値を `runtime_metrics` に snapshot。

調整 env（任意）: `RUNTIME_METRICS_INTERVAL_S`(=15) / `REQUEST_GATE_CONCURRENCY`(=4) /
`REQUEST_GATE_QUEUE_MAX`(=64)。`DATABASE_URL` 未設定なら記録は自動 off（Bot は通常起動）。

---

## 3. 管理画面の起動

### 3-a. まず動かす（dev-bypass・ローカル即閲覧）

Google OAuth クライアント作成前でも、ローカルでデータを確認できる:

```bash
cd ~/Documents/teamagent-orchestrator-poc
DATABASE_URL="postgresql://teamagent:${PGPW}@localhost:15433/teamagent" \
  DASHBOARD_DEV_BYPASS=1 \
  PYTHONPATH=src .venv/bin/python -m teamagent.dashboard
# → ブラウザで http://127.0.0.1:8787
```

> dev-bypass は認証をスキップする**ローカル開発専用**。`127.0.0.1` のみ listen なので外部からは
> 見えないが、本番運用では必ず off にして Google ログインを使うこと。

### 3-b. Google ログインを有効化（本番運用・推奨）

#### あなたが Google Cloud Console でやる作業（1回）
既存 Workspace 連携と同じ GCP プロジェクト（ntv-ai / vectorinc 組織）で:
1. **OAuth 同意画面**: User Type = **Internal**（社内のみ・審査不要）。既に Internal ならそのまま。
2. **認証情報 → OAuth クライアント ID を作成 → 種類「ウェブ アプリケーション」**（既存 Desktop 型とは別物）。
   - **承認済み JavaScript 生成元**: `http://127.0.0.1:8787`（と `http://localhost:8787`）。
   - リダイレクト URI は id_token 方式では不要（生成元のみでよい）。
3. 発行された **クライアント ID** を控える（Secret は不要）。
4. スコープは `openid email profile` のみ（Workspace データ系は管理画面に不要）。

#### 画面サーバ側 env
```bash
DATABASE_URL="postgresql://teamagent:${PGPW}@localhost:15433/teamagent" \
DASHBOARD_GOOGLE_CLIENT_ID="xxxxx.apps.googleusercontent.com" \
DASHBOARD_ALLOWED_EMAILS="you@vectorinc.co.jp" \
DASHBOARD_ALLOWED_HD="vectorinc.co.jp" \
DASHBOARD_SESSION_SECRET="$(openssl rand -hex 32)" \
  PYTHONPATH=src .venv/bin/python -m teamagent.dashboard
```
- `DASHBOARD_ALLOWED_EMAILS`: 閲覧を許す本人（＋少数管理者）をカンマ区切り。
- `DASHBOARD_ALLOWED_HD`: 会社ドメイン（個人 Gmail/他組織を弾く二重防御）。
- `DASHBOARD_SESSION_SECRET`: 未設定だと再起動で要再ログイン。固定したいので env で渡す。
- 将来 EC2 等で HTTPS 公開する場合のみ `DASHBOARD_COOKIE_SECURE=1`。

---

## 4. 画面の内容（MVP）

- **KPI 帯**: 今日の件数 / アクティブ利用者 / 当月コスト(推算) / 24h エラー率
- **折れ線**: 日次リクエスト数・日次コスト（30日）
- **混雑パネル**: 現在/ピークの並列・キュー・拒否数、DB 接続プール（使用/アイドル/timeout）
- **Skill 別**（直近7日）: 件数・コスト・p50/p95
- **ユーザ別**（直近30日・コスト順）
- **Workspace 連携状況**: 認可済み一覧・scope 充足（**トークンは復号・取得しない**）
- **/errors**: 直近のエラー/拒否一覧（request_id 付き・本文なし → Sentry へ request_id で照合）

---

## 5. セキュリティ要点（設計どおり）

- 画面は **完全 read-only**。専用ロール `teamagent_dashboard` は **SELECT のみ**、`oauth_tokens` の
  暗号化列 `refresh_token_enc` は **列単位 GRANT 対象外**（読めない）。KMS 復号権限も与えない。
- RLS は `app.user_role='admin'` GUC を立てた接続だけに usage/metrics/oauth の SELECT を許す。
  この GUC を立てるのは管理画面接続のみ（Bot 経路は admin を立てない）。
- 認証は `id_token 検証 + email_verified + 会社ドメイン(hd) + allowlist` の三段。セッションは
  HMAC 署名 Cookie（HttpOnly/SameSite=Lax・短寿命）。creds はサーバのみ、ブラウザには集計値だけ。
- 本文・回答・クエリ文字列は保存しない（usage_events は文字数 `query_chars` のみ）。

---

## 6. 既知の制約 / 今後

- **コストは推算**（料金表ベース）。確定値は AWS Cost Explorer / Google 請求と突合。
- **未連携の営業（W3）** は「営業16名の名簿」が必要（現状ソース無し）。名簿テーブルを別 migration で
  足せば「16名中 何名 未連携」を出せる。
- video_analysis / tiktok_search（Gemini）のコストは MVP では一部 0 計上のことがある（Bedrock 系は捕捉済み）。
- 単一プロセス前提。Bot をマルチプロセス化したら `runtime_metrics.instance_id` で集約する。
- 将来 EC2/ECS 公開時は HTTPS/WAF・IAM Role・`DASHBOARD_COOKIE_SECURE=1`・redirect URI 追加。

_作成: 2026-06-04 / PoC branch poc/multiskill-orchestrator。_
