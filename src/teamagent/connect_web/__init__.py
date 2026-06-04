"""per-user OAuth 連携の Web コールバック（営業の自己サービス連携の受け口）。

営業が Slack `/teamagent connect` で得た同意URLを開き Google で許可すると、Google が
この Web アプリの ``/oauth2/callback`` にリダイレクトする。ここで:
  state 検証(CSRF/本人性) → code を refresh token に交換 → **KMS暗号化して RDS に保存**。

セキュリティ境界: これは**トークン書き込み（KMS encrypt + oauth_tokens INSERT = teamagent_app 級）**
を行うため、read-only 管理ダッシュボード（teamagent_dashboard）とは**別アプリ・別権限**に分離。
中央の到達可能URL(HTTPS)にデプロイし、その URL を Web型クライアントの redirect_uri に登録する。
"""
