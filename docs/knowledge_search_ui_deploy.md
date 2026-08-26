# 社内資料 検索 Web UI を Aico から配布する — デプロイ手順（人間ゲート）

Aico（Slack）が、OAuth「連携して」リンクと同じ要領で **社内資料検索 Web UI の URL** を返す。

- Aico が検索すると、応答に `web_url`（`{CONNECT_BASE_URL}/search`）と `graph_url`
  （`{CONNECT_BASE_URL}/search/graph`）が載り、「ブラウザ/グラフで開く」を案内できる。
- 「検索ページ教えて」系には専用ツール `knowledge_search_url` が URL ＋一言案内を返す。
- **`CONNECT_BASE_URL` 未設定なら URL は一切出ない**（壊れた相対リンクを出さない・後方互換）。
  `knowledge_search_url` はその場合「まだ公開されていません」と返す。

Python 側（本リポジトリ）の変更はコード済み。**実際にリンクが機能するには以下の人間ゲートが必要**。

---

## 人間ゲートの手順

### (a) mcp イメージを再ビルド
本変更（gateway 注入＋`knowledge_search_url` skill＋factory 登録）を含む mcp イメージを焼く。
ビルド元 commit を `--build-arg GIT_COMMIT=$(git rev-parse HEAD)` で刻み、`infra/deploy_log.md` に追記。

### (b) マイグレーション 0015 を適用
`infra/migrations/0015_search_feedback.sql` を RDS に適用（👍/👎 フィードバック用テーブル。
`IF NOT EXISTS` で冪等・追加のみ）。Web UI の `/api/v1/feedback` が依存する。

### (c) terraform apply（connect_web）
`infra/terraform/connect_web.tf` の connect-web タスクを apply。
検索 UI が新スキーマ＋再ランクで動くために `USE_NEW_SCHEMA=true` / `USE_COHERE_RERANK=true` と
Bedrock（rerank）の IAM が要る（既に tf に配線済み）。
- ⚠️ 過去の地雷: 他フラグ（`enable_connect_web` 等）を tfvars に明記しないと plan が
  destroy しかける。apply 前に plan 差分を必ず確認。

### (d) env / secret を設定
connect-web に以下が要る（(c) の tf で配線済み・値の供給だけ確認）:
- `CONNECT_BASE_URL` … Web UI の公開 base（例 `https://connect.newstv.co.jp`）。
  **mcp/openclaw 側にも同じ値を渡す**（Aico が `web_url`/`graph_url` を組み立てる真実源）。
- `CONNECT_GOOGLE_CLIENT_ID` … 検索 UI の Google ログイン（Web 型クライアント）。
- `OAUTH_STATE_SECRET`（session/CSRF 署名鍵・secret）/ `CONNECT_GOOGLE_CLIENT_SECRET`（secret）。
- `CONNECT_SEARCH_SESSION_SECRET` … 検索 cookie の署名鍵（未設定だと再起動毎に全員ログアウト）。
- `CONNECT_SEARCH_COOKIE_SECURE=1` … **HTTPS 公開時は必須**。未設定だと検索 cookie が
  Secure フラグ無しで発行される（HTTPS 配下のセッション cookie は Secure を付けること）。
- `CONNECT_SEARCH_ALLOWED_EMAILS` … 許可メール（既定 s-komata のみ＝PoC は本人だけ）。

### (e) API Gateway にパスを追加（情シス）
公開エンドポイント（API GW → VPC Link → 内部 ALB → connect-web）に検索 UI のパスを通す:
- `/search`（検索ページ）
- `/search/graph`（グラフ閲覧）
- `/api/v1/*`（検索 API ＋ `/api/v1/feedback` の 👍/👎）

### (f) OpenClaw toolFilter に新ツールを含める
`infra/openclaw/openclaw.config.json5` の `toolFilter.include` に `knowledge_search_url` を追加済み。
MCP 側で **`USE_KNOWLEDGE_SEARCH_URL_TOOL=1`** を ON にしてから openclaw を再デプロイする
（factory が当該 env OFF だとツールが MCP に出ない）。
`web_url`/`graph_url` の search 応答注入は env フラグ不要（`CONNECT_BASE_URL` の有無だけで自動）。

---

## 動作確認
1. mcp に `CONNECT_BASE_URL` を渡した状態で Aico に資料検索を依頼 → 応答末尾に
   ブラウザ/グラフのリンクが出る。
2. Aico に「検索ページ教えて」 → `knowledge_search_url` が `/search`・`/search/graph` を返す。
3. リンクを開く → Google ログイン → 検索 UI が表示される。

> `CONNECT_BASE_URL` 未設定で本番に出しても **壊れたリンクは一切出ない**（リンク無しで動作継続）。

_作成: 2026-06-23 / branch feat/v3.1-monorepo。Python 側コード済み・上記 (a)-(f) は人間ゲート。_
