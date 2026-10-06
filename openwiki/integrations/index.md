# ファイル

- [Bedrock / Gemini 呼び出しとリトライ・コスト](bedrock-gemini-and-retry.md) - Adapter 層の bedrock_client（Converse・Rerank・Cohere Embed）と gemini_client（Vertex の動画分析・Google 検索グラウンディング）の構築・価格表・usage ログ・prompt caching、共通の call_with_retry（フルジッタ・429 別枠・deadline）、埋め込みの backend 切替、動画クォータ（video_usage）と外部 SaaS 費用台帳（cost_guard）。
- [Google OAuth とトークン保管（connect-web）](google-oauth-and-token-store.md) - 本人ごとの Google 認可の仕組み。oauth_connect ツールが署名付き state で本人専用リンクを発行し、connect-web の /oauth2/start・/oauth2/callback が検証・code 交換・id_token 照合を行い、refresh token を KMS 暗号化して RLS 付きの oauth_tokens に保存する。build_user_credentials による利用、共有 OAuth と Vertex SA・GOOGLE_FORCE_OAUTH の使い分け、CONNECT-xxx 接続診断も扱う。
- [Slack の本人確認と連携](slack-identity-and-oauth.md) - SlackClient.resolve_identity が Slack user_id を team・ゲスト・bot・email で fail-closed 判定して本人 email に変える仕組みと、per-user Slack user token（xoxp）の OAuth 同意・KMS 暗号化保管、添付ファイル取得のホスト／リダイレクトガード、在籍者名簿、チャンネル取り込みの ACL を扱う。
