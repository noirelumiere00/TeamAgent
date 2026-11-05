あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
背景: 社内 Slack AI の全 DM 判定で「連携・引数・連携リンク」の不満が 48 件（最多級）。社員の Google（Drive/Gmail/Calendar）連携が未連携・失効のとき、ツールが分かりにくいエラーを返したり、利用者に手作業を求めたりする。方針は「利用者に手作業を強いない・次の一手は 1 個」。
やること:
1. 連携が要るツール（mail_*・calendar_*・drive 系・oauth_connect など。src/teamagent/skills と adapters/google_oauth*）で、未連携・トークン失効・権限不足（scope 不足）を検知する箇所をすべて洗い出す。
2. どの経路でも同じ形で返す共通の返し方を 1 つ作る: 「◯◯の連携が切れています。こちらから 1 クリックで再連携できます: <再連携リンク>」の 1 行＋元の依頼は再連携後に自動で続けられることを示す（既存の oauth start link の仕組み・USE_OAUTH_START_LINKS を使う。新しい仕組みは作らない）。内部語（トークン・scope・401 など）を利用者に見せない。
3. 失効を検知したら、同じ会話で何度も同じ案内を出さない（1 会話 1 回）。
4. テスト: 未連携・失効・scope 不足のフェイク（本番の失敗の仕方＝google の invalid_grant・401・insufficientPermissions を再現）で、全経路が同じ 1 行を返すこと。`uv run --extra dev --extra mcp pytest -q -n auto tests/skills tests/adapters tests/mcp_gateway` 相当と ruff・mypy（変更ファイル）。
禁止: git commit・push、AWS・Google の実呼び出し、.env・secrets の読み取り。差分は小さく。最後に変更点（ファイル:行）・テスト結果を短く。
