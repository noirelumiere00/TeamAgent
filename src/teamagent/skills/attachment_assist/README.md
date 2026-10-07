# attachment_assist

Slack の会話に添付されたファイルを読み取り、要約・翻訳などをテキストで返します。

- `ATTACHMENT_PERMALINK_ALLOWED_EMAILS`: 投稿リンク経路は `ATTACHMENT_PERMALINK_ENABLED` が真、かつ本人確認済みの呼出者がこの許可リストに一致する場合だけ有効（既定の空値は全員拒否、`*` は本人確認済み全員、カンマ区切りで個人を指定）。
