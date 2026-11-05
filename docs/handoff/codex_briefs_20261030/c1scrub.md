あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
背景: 社内 Slack AI の全 DM 判定（10/30・607 件）で最多の不満は「内部の語・英語のシステム文・一時リンク・長く読みにくい」128 件。例の型: ツールのエラー名や英語の例外文がそのまま出る、署名付きの一時 URL（X-Amz-… や長い query）・途中で「…」に切れたリンク、内部の仕組みの名前（ツール名・fanout・vault・MCP など）、見出しと箇条が長く続く。
仕組み: 最終の返事は OpenClaw の caller-identity plugin（infra/openclaw/caller-identity-plugin/dist/index.js の scrubOutboundReplyText など）と MCP 側の直接配信（src/teamagent/mcp_gateway/direct_*）を通る。
やること:
1. 最終の返事を出す全経路で、次を機械で直す共通の整形を 1 か所に寄せる（既存の scrub を拡張・新しい層は作らない）: (a) 英語の例外文・エラー名・スタックの断片を日本語の 1 文（「処理の途中で止まりました。もう一度お試しください」等）に置換 (b) 署名付きの一時 URL を除去し、同じ資料の恒久リンク（Drive の原本リンク等）が手元にあればそれに置換、無ければ「資料を見る」リンクを出さない (c) 「…」で途中が切れた URL を出さない (d) 内部の仕組みの名前（ツール名の英字・内部用語の一覧を定数で）を除去・言い換え。
2. 長さ: 既存の「答えの短縮」の上限があればそれを使い、無ければ本文が長すぎる場合は結論＋根拠 3 件までに切る既存の仕組みに乗せる（新設しない）。
3. テスト: 上の型ごとに 1 件以上の入力→出力の固定テスト（plugin の node テストと Python 側）。変異テストで外すと赤になること。`node --test`（plugin の test）と `uv run --extra dev --extra mcp pytest -q -n auto tests/mcp_gateway` と ruff。
禁止: git commit・push、AWS・Slack の実呼び出し、.env・secrets の読み取り。差分は小さく。最後に変更点・テスト結果を短く。
