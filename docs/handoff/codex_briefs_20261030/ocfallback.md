あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
本番の事故（10/30 10:23〜10:29 JST）: 社内 Slack AI（OpenClaw 外側ループ・モデル amazon-bedrock/jp.anthropic.claude-haiku-4-5-20251001-v1:0）が Bedrock の "Service unavailable"（503）で 6 分間、全員の DM に「Something went wrong」を返した。ログ: `model fallback decision: decision=candidate_failed ... reason=timeout next=none`。同時刻、同じモデルへの手元からの直接呼び出しは成功しており、一時的な容量不足と見られる。
やること: infra/openclaw/openclaw.config.json5 の外側ループのモデル設定に、OpenClaw の仕組みとして用意されているフォールバック（primary が timeout/503/throttle のとき次の候補へ）を足す。候補は同じ東京の jp.anthropic.claude-sonnet-4-6（同じアカウント・同じ地域。global/us の推論プロファイルは使わない＝データの所在を変えない）。
- OpenClaw v2026.7.1 の設定スキーマでフォールバックをどう書くかを、リポジトリ内の根拠（plugins-lock・dist の写し・既存のテスト・README・npm キャッシュ ~/.npm 内の openclaw パッケージがあればその dist）から確かめる。根拠が見つからなければ推測で書かず、要確認として報告する。
- 既存の設定の検査（tests/ 配下の openclaw 設定の契約テスト、run-live-rollout-gates.mjs 等）が通るようにし、フォールバック設定を固定するテストを足す。
- 費用: Sonnet はフォールバック時だけ。通常時の挙動・費用は変えない。
- `uv run --extra dev --extra mcp pytest -q -n auto tests/infra tests/openclaw`（無ければ関連テストを探す）と node の関連テストを通す。
禁止: git commit・push、AWS・Bedrock の実呼び出し、.env・secrets の読み取り。差分は小さく。最後に変更点（ファイル:行）・根拠・テスト結果・要確認を短くまとめる。
