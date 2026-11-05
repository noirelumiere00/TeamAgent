あなたは teamagent リポジトリの開発者。作業場所はこの worktree（branch fix/media-case-fragment-20261030、origin/dev 起点）。日本語で報告する。

対象: src/teamagent/skills/media_case_search/campaigns.py の aggregate_cases（自社メディアの TikTok 投稿を「キャンペーン単位」にまとめて Slack で答える機能）。テストは tests/skills/media_case_search/。

本番で見つかった問題: 「国内旅行 CP の事例」と聞くと、同じ施策の投稿が 2 つのまとまりに割れる。
- まとまり1: 「TikTok GO の宿泊紹介（2025年10月〜2026年6月）」56 本
- まとまり2: 「TikTok GO ホテル（2026年3月）」1 本
2 は 1 と同じブランド・同じ施策（タグ #tiktokgo を含む宿泊紹介）なのに、別の識別子が付いて独立している。

直したいこと: 同じブランドで、識別子が少数（例えば 2 本以下）のまとまりは、同じブランドの施策タグを共有する最大のまとまりへ吸収する。ただし別ブランド（例: 楽天トラベルと TikTok GO）は絶対に混ぜない（以前 union-find で連鎖して混ざった事故がある。1 投稿 1 識別子の現在の方針は保つ）。
- 吸収先が見つからない少数のまとまりは今のまま残す。
- 本数・再生合計・期間・いちばん伸びた投稿は吸収後の値で出す。

手順: まず失敗するテスト（上の 56＋1 の再現。投稿データは最小の架空値でよい）を書き、直し、既存テストも含めて `uv run --extra dev --extra mcp pytest -q tests/skills/media_case_search` と ruff・mypy（変更ファイル）を通す。差分は小さく保つ。
禁止: git commit・push、AWS・外部 API の呼び出し、.env・secrets の読み取り。
最後に、変更点・テスト結果・判断に迷った点を短くまとめる。
