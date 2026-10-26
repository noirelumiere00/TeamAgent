# Slack 本人連携の scope 設定

`user_oauth_scopes.merge.json` は既存アプリへの追加用断片です。アプリ全体の manifest ではなく、
JSON Merge Patch として自動適用するものでもありません。既存 manifest の
`oauth_config.scopes.user` に記載の scope を追加し、既存の bot scope、redirect URL、events
などを保持してください。Slack API の対象は Aico の本人連携で使う
`CONNECT_SLACK_CLIENT_ID` のアプリ（未設定時のみ `SLACK_CLIENT_ID`）です。

管理画面の **OAuth & Permissions → User Token Scopes → Add an OAuth Scope** でも
同じ設定ができます。今回必要な追加は `files:read` です。定義は
`src/teamagent/adapters/slack_oauth_flow.py` の `SLACK_USER_SCOPES` と照合しています。
scope の設定だけでは保存済み user token は更新されないため、小俣さん本人が Aico に
「連携」と送り、返ってきた「Slack を連携」から「許可」を押して再同意します。

リンクは既存の `oauth_connect` が検証済みの Slack user/workspace と本人メールから
HMAC 署名・nonce・30分 TTL 付きで生成します。直接の
`https://slack.com/oauth/v2/authorize?...&user_scope=...&state=...`、または
`USE_OAUTH_START_LINKS` 有効時の `{CONNECT_BASE_URL}/slack/oauth/start/{state}` を使います。
callback は `/slack/oauth/callback` です。固定の state や token をファイルに記録しません。

参照: [App manifest reference](https://docs.slack.dev/reference/app-manifest/)、
[OAuth 認可と scope 追加](https://docs.slack.dev/authentication/installing-with-oauth/)。
