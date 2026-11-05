あなたは teamagent リポジトリの開発者。作業場所はこの worktree（branch chore/case-state-plan-20261030）。日本語で報告する。
docs/case_state_plan_20261030.md（あなたが前回書いた計画）の推奨「案 A」の最初の PR（PR2: 数字と同じ根拠束に状態・日付・出典を定義する）を実装する。
- 計画どおり、既存の timeline_status.py を拡張・再利用し、新しい状態判定を二重に作らない。
- 「提案資料」「営業報告」「投稿の存在」「正式な受注・完了」を別の事実として持ち、正本が無いものは「不明」と明示する（推測で受注・実績にしない）。
- 既定 OFF のフラグ（email_allowed 方式・空＝誰も・*＝全員）で囲み、既存の挙動は変えない。
- テストは先に失敗するもの（提案しただけの案件が実績・受注として答えられる再現）を書く。`uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/skills/search tests/ingest`（関係するテスト）と ruff・mypy（変更ファイル）を通す。
- DB migration が必要になる設計なら、実装せず報告で止める（計画では不要の想定）。
禁止: git commit・push、AWS・DB の実接続、.env・secrets の読み取り。顧客名を書かない。差分は小さく。最後に変更点（ファイル:行）・テスト結果・PR3 への引き継ぎを短くまとめる。
