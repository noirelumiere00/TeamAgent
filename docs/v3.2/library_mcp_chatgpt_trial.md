# ChatGPT向け動画ライブラリMCPの接続手順

既存リンクとの互換のためファイル名に `trial` を残す。現在の実装はPostgreSQLによる永続OAuthとrefresh tokenに対応し、旧来の「単一プロセス内・10分で再接続が必要」という試作制約を置き換える。全体の運用・Claudeの接続・退職時の失効は [接続と運用](library_mcp_clients.md) を参照する。

**本番migration・アプリ反映と、実Google認証を含むChatGPT検索E2Eは未確認。** ローカルのSDK／ASGI／DBテストを本番疎通の証明にはしない。

## 機能と境界

- `ping`: 伏せたメールと固定の接続確認文。
- `search_videos`: キーワード、PR、種別、業種、目的、広告主、施策で検索。最大20件／回、100ページ。strictな入力モデルで追加キーを拒否する。
- `get_video`: 検索結果のプラットフォームと動画IDから1件取得。

全ツールは読み取り専用。画面と同じ本人コンテキスト、現在の管理者設定、非公開除外条件を共用する。固定の出力モデルを使い、広告費、広告指標、S3キー、署名URL、動画本体、自由形式のDB行を返さない。投稿文はタイトル相当の先頭行200字まで。DBエラーの詳細をクライアントへ返さない。

## 認証と接続管理

scopeは `library:read` だけ、OAuth public client／DCRを使う。利用者用APIキーやOAuth client secretの作成・配布は不要。

access tokenは10分、refreshは毎回rotationする。code交換／refresh成功時にgrantとrefreshの期限を**その時点から90日後**へ更新する。90日は未使用期限であり、初回同意からの絶対期限ではない。通常はクライアントが自動更新し、解除・長期未使用・refresh再利用検知などで失効した場合にGoogle認証をやり直す。

OAuth状態はDBに保存され、アプリ再起動後も引き継ぐ。code／access／refresh本体は保存しない。新規grantには既存Cookieに依存しないfresh Google本人確認と明示同意が必要。[接続の管理](https://connect.newstv.co.jp/mcp-auth/connections)で本人の接続を解除できる。これらの画面・動作は新アプリ反映後に利用可能になる。

## 登録と反映

1. アプリ反映前に [`0044_library_mcp_oauth.sql`](../../infra/migrations/0044_library_mcp_oauth.sql) を正規のmigration runnerで適用し、role／ACLを確認する。詳細は [運用手順](library_mcp_clients.md#反映とロールバック)。
2. [ChatGPT Plugins](https://chatgpt.com/plugins) → **＋ → Add custom MCP server**。名前「動画ライブラリ」、Server URL `https://connect.newstv.co.jp/mcp`、OAuth、自動登録（DCR）、scope欄があれば `library:read` を設定する。
3. 実クライアントが要求する**正確なcallback**を管理画面または安全な `mcp_spike_redirect_observed` イベントのhost／pathから確認する。ワイルドカード・類似ホスト・任意URLを許可しない。フォームを作り直すとcallback IDが変わる場合がある。
4. 稼働中の設定を確認し、既存Claude／Claude Codeの承認済みcallbackをすべて保持して対象ChatGPTのcallbackを追加する。Terraformの `enable_connect_library_mcp=true`、`connect_mcp_redirect_uris`、現行の `connect_base_url` を使う。
5. 正本の署名付きimage公開とTerraform runtime guardの完全saved planで、レビュー済みアプリ・callback設定を反映する。直接task definition登録、直接service更新、部分plan、ingest停止、guardのfingerprint検査を外す操作はしない。guardが通常taskの交代を検知した場合も失敗を無視しない。
6. 同じChatGPTフォームで作成を再開し、会社Googleアカウント・戻り先・本人の動画閲覧権限を確認して同意する。作成したプラグインをインストールし、会話で **@動画ライブラリ** を選ぶ。
7. `ping` → 上限1件の検索 → その動画の詳細を確認する。access期限後のrefresh、アプリ再起動後の継続、本人の解除後の拒否も確認する。

リリース後は [HTML接続ガイド](https://connect.newstv.co.jp/mcp/setup) から設定画面を開ける。配布用の `newstv-mcp-setup.html` は別のプレビュー成果物であり、開くだけでは登録や接続許可を実行しない。

## 検証と運用上の注意

テストでは実SDKのOAuth handlers／ASGI transportとfake store／専用DBを使い、登録・fresh Google nonce・同意・code・token・refresh・initialize・tools/list・検索・詳細・失効を検査する。本番データをテストへ持ち込まない。禁止callback、Origin、PKCE、期限切れ、並行消費、refresh replay、管理者解除、非公開／不存在、ページ上限、機密出力の除外、DB障害時の秘匿も対象とする。

Googleのログアウト・アカウント無効化だけでは既存MCP grantは失効しない。退職等では [運用担当によるgrant失効](library_mcp_clients.md#退職アカウント停止時の失効) を別途実施する。アプリのロールバック時も0044のテーブルや接続状態を削除しない。

旧案との違いは [設計書冒頭の実装優先事項](library_mcp_design.md) を参照。本番の登録・Google認証・実検索が成功するまでは、利用可能とは報告しない。
