"""TeamAgent 管理画面（admin dashboard）。

オーナーが自分の Google でログインして、利用状況・コスト・レイテンシ・エラー・
Workspace 連携状況・同時実行を閲覧する内部ダッシュボード（read-only）。

構成（設計: 管理画面 Agent 協議）:
- FastAPI（既存venvに導入済）。テンプレートは依存を増やさず Python 生成 HTML、
  セッションは stdlib HMAC 署名 Cookie（jinja2/itsdangerous を要求しない）。
- 認証: Google id_token 検証 + email allowlist + 会社ドメイン(hd) 検証。dev-bypass あり。
- データ: Bot が RDS に書いた usage_events / runtime_metrics / oauth_tokens を、専用の
  read-only ロール teamagent_dashboard（admin GUC）で SELECT する。**復号・本文は扱わない**。

MVP はオーナーの Mac でローカル起動（SSM トンネル経由で RDS）。docs の runbook 参照。
"""
