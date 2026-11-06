# ChatGPT向け動画ライブラリMCPの限定試用

既存の試作はClaude向けの認証とpingのみだったため、ChatGPTから登録するとコールバックの完全一致検証で拒否されていた。ChatGPTのOriginを許可し、登録済みの戻り先ホストに応じた同意画面と、既存ライブラリを読む2ツールを追加する。

## 機能と境界

- `ping`: 伏せたメールと固定の接続確認文。
- `search_videos`: キーワード、PR、種別、業種、目的、広告主、施策で検索。既定は総再生数順。最大20件/回、100ページ。保存順は保存率。入力はstrictなPydanticモデルで追加キーを拒否。
- `get_video`: 検索結果のプラットフォームと動画IDから1件取得。

全ツールはreadOnlyHint=true。画面と同じ`teamagent_app`の本人コンテキスト、現在の管理者設定、`NOT v.is_private`を共用する。出力は固定モデルへの明示代入とし、広告費、広告指標、S3キー、署名URL、動画本体、自由形式のDB行を返さない。投稿文はタイトル相当の先頭行200字まで。投稿URLは既存検証器を通す。DBエラーの詳細をクライアントへ反射しない。

## 認証の制約

これは**限定試用版**であり、OAuthクライアント・コード・トークンは引き続き単一プロセス内に置く。access tokenは10分、refresh tokenは未実装。再起動・期限切れ後は再接続が必要。多重レプリカや常用の無人接続は対象外。既存の永続OAuth設計は`library_mcp_design.md`のPR1を参照。この制約は同意画面にも表示する。

## 登録と反映

1. ChatGPT Webのプラグイン→追加→カスタムMCP。名前「動画ライブラリ」、サーバーURL `https://connect.newstv.co.jp/mcp`、OAuth、DCR、scope `library:read`。
2. 実クライアントが要求した**正確な**コールバックを安全な`mcp_spike_redirect_observed`イベントのhost/pathから確認する。任意のURL、ワイルドカード、類似ホストは許可しない。ChatGPTのフォームを開き直すとcallback IDが変わる場合がある。
3. 稼働中のMCP設定を取得し、既存Claude/Claude Codeの正確な戻り先を保持したうえで、対象ChatGPTの戻り先を追加する。Terraformの`enable_connect_library_mcp=true`と`connect_mcp_redirect_uris`に設定し、`connect_base_url`は現行originを維持する。
4. 既定OFFを維持したコードをレビューし、正本`infra/deploy/`の署名付きimage公開とTerraform runtime guardの完全saved planで反映する。task definitionの直接登録、update-serviceの直打ち、部分planは使わない。新しいimageはconnect-webだけに反映し、無関係なruntime・設定変更を混ぜない。
5. 同じChatGPTフォームから作成し、会社Googleアカウント、戻り先、`library:read`と10分の制約を確認して許可。クライアントsecretやGoogle/DB credentialの手入力は不要。
6. ChatGPTのツールカタログで3ツールを確認し、`ping`→上限1件の検索→その動画詳細を試す。書き込みツールがないことを確認。10分後の再認証と、将来の常用化は別途確認する。

## 検証

実SDKのOAuth handlers/ASGI transportによる登録→同意→code→token→initialize→tools/list→検索→詳細をローカルで確認する。fake DBを使い、本番データはテストへ持ち込まない。禁止callback、Origin、PKCE、期限切れ、管理者解除、非公開/不存在、ページ上限、広告費・保存先情報の除外、DBエラーの秘匿、既存画面の回帰を含む。

本番の登録・Google認証・ChatGPTでの実検索が成功するまでは、利用可能とは報告しない。
