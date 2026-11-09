# 動画ライブラリMCP：ChatGPT・Claudeの接続と運用

接続名は **動画ライブラリ**、URLは `https://connect.newstv.co.jp/mcp`。OAuth public client／DCR、scopeは **`library:read` だけ**。利用者用APIキー・client secretは作成・配布しない。

**本番migration・アプリ反映と、実Google認証を含む本番E2Eは未確認。** コードテスト成功を本番接続済みと扱わない。[ChatGPTの詳細手順](library_mcp_chatgpt_trial.md)、[旧設計と実装の差分](library_mcp_design.md)も参照。

## 利用者の入口

リリース後は **[HTML接続ガイド](https://connect.newstv.co.jp/mcp/setup)** でURLのコピーと設定画面への移動ができる。配布用 `newstv-mcp-setup.html` は別のプレビュー成果物。どちらも開くだけでは登録・Google認証・接続許可を実行しない。

- **ChatGPT:** [Plugins](https://chatgpt.com/plugins) → ＋ → Add custom MCP server。名前・URL・OAuth／DCRを設定し、Google認証・同意後にプラグインをインストール。会話で **@動画ライブラリ** を選ぶ。組織で追加が制限されていれば管理者が登録する。
- **Claude:** Team／EnterpriseはOwner等が[組織Connectors](https://claude.ai/admin-settings/connectors)で一度登録する。Add → Custom → Web、OAuth clientは **Register automatically**。各利用者は[Connectors](https://claude.ai/customize/connectors)のConnectから自分の会社Googleで認証・同意し、会話の＋ → Connectorsで有効にする。個人登録も同じ設定。published identity／任意client ID URLの取得には対応しない。
- callbackは実要求の**完全一致**。既存のClaude ai／comと承認済みの正確なlocalhost callbackをすべて保持する。ChatGPTのcallbackは管理画面または安全な `mcp_spike_redirect_observed` のhost／pathから確認し、正規の設定リリースで追加する。フォーム作り直しでIDが変われば再確認。ワイルドカードや新規ポートの推測追加はしない。

公式手順: [ChatGPT](https://developers.openai.com/api/docs/guides/custom-mcp-server)、[Claude](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)。

## 継続接続と解除

- accessは10分。クライアントがrefreshで自動更新し、通常は10分ごとのGoogleログインは不要。
- grant／refreshは**最後のcode交換・refresh成功から90日間の未使用期限**。refresh成功ごとに延長し、初回からの絶対90日／旧案の絶対7日ではない。動画検索だけでは延長しない。
- OAuth状態はPostgreSQLに永続化し、再起動後も継続。code／access／refresh本体は保存せずハッシュだけを保存する。refreshは毎回rotationし、消費済みrefreshの再利用でgrant全体を失効。並行更新・応答喪失後の再送でも再接続が必要になり得る。
- 新規grantごとにtransaction／ブラウザnonceに結び付けたfresh Google確認と明示同意が必要。既存Connect Cookieだけでは認可しない。本人の動画閲覧権限と現在の管理者／非公開条件を使う読み取り3ツール（ping・検索・詳細）のみ。
- リリース後の[接続の管理](https://connect.newstv.co.jp/mcp-auth/connections)で本人の接続を解除する。grant全体が失効し、access／refreshは次の検証から拒否。再接続はクライアント側からGoogle認証をやり直す。token文字列は表示しない。

## 反映とロールバック

1. 保護された正規DB運用環境で接続先・適用予定を確認する。既存の `DATABASE_URL` を使い、DSN／認証値を引数・履歴・ログへ出さない。この作業のために `GetSecretValue` を追加実行しない。
2. **アプリ反映前に** [`0044_library_mcp_oauth.sql`](../../infra/migrations/0044_library_mcp_oauth.sql) を既存forward-only runnerで適用する。先に `python scripts/migrate.py --dry-run`、予定外の未適用migrationがなければ `python scripts/migrate.py`。runnerがtransaction／checksumを管理する。本番で `--rerun` は使わない。
3. `schema_migrations` の0044とACLを確認。所有者は **library_mcp_definer**（NOLOGIN／NOBYPASSRLS／NOINHERIT）。5テーブルはRLS ENABLE／FORCE＋所有者policy。`library_mcp_owner`はpolicy名で、role名ではない。
4. `teamagent_app`は固定SECURITY DEFINER関数の必要なEXECUTEだけを持ち、直接SELECT／DML・ownerへのSET ROLE・非公開helper実行は不可。`teamagent_dashboard`にも直接権限を与えない。migration時の一時owner membershipは終了時にrevokeされる。
5. 署名付きimageの公開までは現行releaseスクリプトを使える。**アプリ反映の実行経路は別途レビューが必要**。最新の[運用記録](../handoff/aws_resources_and_ops_20261105.md)はTerraform apply禁止・直接AWS操作を記録する一方、既存README／CLAUDE.mdにはguard必須が残る。どちらかを推測で選んで実行しない。既存の全体更新helperは他サービスも更新するため、connect-web限定の代わりに使わない。署名証跡、実稼働bindingからの完全差分、排他制御、対象限定を満たすレビュー済み経路でアプリと正確なcallbackだけを反映する。Terraform経路を再整備して使う場合は完全saved planを維持する。既存env名 `CONNECT_MCP_SPIKE_REDIRECT_ALLOW` は互換のため残る。
6. ingestの通常task交代でもlive fingerprintは変わる。receipt／planが失効したら停止する。ingest停止、schedule変更、fingerprint検査除外で回避しない。安定した通常運用枠か、正規の反映経路が整うまで保留する。
7. 本番でmetadata→登録→実Google→tools/list→上限1件検索／詳細→access期限後refresh→再起動継続→本人解除後拒否を確認。コード／専用DBテストは実Google・クラウド接続の代替ではない。code／Bearer／Cookie／token hash／DB行を証跡へ保存しない。

ロールバックは正規手順でレビュー済み旧アプリへ戻す。**0044・grant／credential行を削除・再適用しない。** 旧アプリが永続認証非対応なら接続継続は保証しない。失効は明示revokeで行う。

## 退職・アカウント停止時の失効

**Googleログアウト／アカウント無効化だけでは、検証済みHDを持つ既存grantは失効しない。** Directoryへの都度照会はない。社内アカウントの利用停止とMCP grant失効を両方実施する。

承認されたtrusted DB運用担当が対象者を確認する。通常の接続は `PgMCPStore.list_grants(email)` の各IDへ `revoke_grant(grant_id, email)`。resourceは正規 `/mcp` に固定し、任意emailを操作する公開HTTPは作らない。listは有効refresh付き接続を最大100件返すので、必要なら解除後に再取得する。

code発行直後なども含む**全grant**の一括失効は、既にownerへSETできる正規DB管理セッションで下記を使う。実roleは `library_mcp_definer`、本人列は `subject`。SET ROLE拒否時は権限を追加・迂回せず既存DB管理担当へ戻す。新管理UIやruntimeの権限追加は不要。

この例は保護された既存 `DATABASE_URL` を使い、対象メールを非表示で入力する。引数・履歴にPIIや資格情報を載せず、出力は件数だけ。新tokenは発行しない。

```python
import os
from getpass import getpass

import psycopg

subject = getpass("失効する本人のメール（非表示）: ").strip().lower()
if not subject or input("対象者を確認済みなら REVOKE と入力: ") != "REVOKE":
    raise SystemExit("中止")
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE library_mcp_definer")
        cur.execute(
            "UPDATE public.library_mcp_grants SET revoked_at=clock_timestamp() "
            "WHERE subject=%s AND resource=%s AND revoked_at IS NULL",
            (subject, "https://connect.newstv.co.jp/mcp"),
        )
        count = cur.rowcount
print(f"失効した接続: {count} 件")
```

commit後にtrusted storeで有効な接続の残件を確認し、再認可を防ぐ社内アカウント運用も完了させる。再接続が必要な正当な利用者はfresh Google確認からやり直す。
