# Google 連携失敗の返却経路

`USE_OAUTH_START_LINKS=1` のとき、`skills/_shared/google_connection.py` のガードで
本人の Google 連携失敗を同じ一行の `message` にする。既存の出力型と成功時の処理は保つ。
Google の再連携中に Slack の別の連携リンクを重ねて要求しない。

| 検知・伝播する場所 | 未連携 | 失効・権限不足 |
| --- | --- | --- |
| `_shared/mail_connection.resolve_gmail_for_user` → `mail_summary` / `mail_followup` / `mail_draft` / `meeting_prep` | 本人の行なし | 空の refresh 情報。API 失敗は共通分類器 |
| `mail_summary._search` / メッセージ取得 / `_api_failure` | 上記 | API 例外 → 構造化 error |
| `mail_followup` の案件検索・受信箱トリアージ・各スレッド取得 | 上記 | API 例外 → 構造化 error または部分取得の失敗通知 |
| `mail_reply._resolve_gmail` / 検索・スレッド取得・下書き作成・削除 | 本人の行なし | 元例外・原因例外、部分取得の失敗通知 |
| `_shared.mail_history.fetch_counterpart_history` | 呼出元で検証 | 相手の過去メール検索・各メッセージ取得の失敗通知 |
| `mail_to_internal_context._resolve_gmail` / `_mail_signal` | 本人の行なし | 元例外・原因例外 |
| `mail_draft` の一覧選択 / 署名ボタン | 共通部品・`generate_draft_for_thread` | 内側 Skill とオンデマンド生成の失敗を外側へ伝播 |
| `morning_digest._resolve_token` / メール・予定取得 / オンデマンド下書き | 本人の行なし | fetch 状態・各 API 呼び出しの失敗通知 |
| `calendar_freebusy.run`（空き時間 / 予定一覧）| 本人の行なし | 構築・API の例外を共通分類器で判定 |
| `calendar_event.run`（自由文 / 署名ボタン）| 本人の行なし | 保存済み書込権限の不足・予定作成の API 失敗 |
| `schedule_propose.run` | 本人の行なし | 空き枠取得・下書き・仮予定登録・保存済み書込権限の不足 |
| `workspace_search.run`（Calendar / People）| 本人の行なし | 構築・API の元例外 |
| `meeting_prep.run` / メール材料取得 | 予定接続なし | 予定・メールの元例外 / 部分成功の失敗通知 |
| `search.saved_file_refs.resolve_saved_files` → `search.run` | 本人の行なし | Sheets 台帳読取・Drive メタデータ確認の失敗通知 |
| `knowledge_deliver.run` / MCP の資料フォローアップ復元 | 内側の検索・本人参照の再検証 | 入れ子の失敗を外側へ伝え、共通案内を返す |
| `oauth_connect._google_status` / `_google_liveness` | 保存行なし | 保存済み権限の不足・既存の refresh 生存確認（`OAUTH_CONNECT_LIVENESS_PROBE`） |
| `mail_constraints.run` | 本人オプトインの検証後、本人の行なし | 本人 OAuth / 注入 Gmail の API 認証失敗。フラグ OFF の旧 DWD 経路は維持 |

`adapters/google_oauth_flow.py` は既存の本人署名 state と同意 URL を生成し、code 交換を行う。
`google_oauth_env.py` は設定展開のみで、本人の接続状態は判定しない。
実際の資格情報の検証は `google_auth.build_user_credentials`、refresh の生存確認は
`google_liveness.probe` にある。start / callback の署名・TTL・一度限りの消費・本人照合は変更しない。
共有 OAuth / サービスアカウントで動く Drive 配送・取り込みは社員本人の再連携では直らないため、
本人向けガードを付けない。RLS で検索する取り込み済み Drive 文書も OAuth の事前検証は増やさない。

Google と同じ構造の `RefreshError(invalid_grant)`、HTTP 401、HTTP 403 の
`insufficientPermissions` / `ACCESS_TOKEN_SCOPE_INSUFFICIENT`、`invalid_scope` を分類する。
`invalid_client`、管理者ポリシー、単なるファイルアクセス禁止、レート制限、5xx を混同しない。
利用者への `message` にはこれらの識別子を入れず、MCP も案内だけを返す。

会話の鍵は本人・Slack team・channel・thread（DM 直下では署名済み session）で分ける。
同じ thread 内では session 更新によって案内を増やさない。
既存の OAuth state 用 DynamoDB 条件付き書込を `GOOGLE_RECONNECT#` prefix で再利用し、
複数プロセスの重複を防ぐ。保存するのはハッシュと時刻だけ。TTL は既存 OAuth state と同じ30分。
共有台帳が未設定 / 障害のときは既存の入力確認キャッシュと同様にプロセス内で抑止する。

## 自動再開の既存機構に関する制約

現在の `connect_web.app.oauth2_callback` は本人確認と接続情報の保存で終了する。
元のツール・引数・配送先の保存や、callback 後の実行予約は存在しない。
新しい再開機構を追加しない条件に従い、この変更は「再連携後に元の依頼を続けられる」と案内するが、
自動再実行を約束しない。下書き・予定が部分的に作成済みのときも再実行を追加しない。
