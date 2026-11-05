あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
背景: 全 DM 判定（10/30）で 2 番目に多い不満は「一度探しただけで『無い』と答え、探す作業を利用者に戻す」113 件（「見つからないので制作チームに聞いて」「自分でチャンネルを見て」等）。直近 1 週は事例の良い率が 42%→14%、資料探しが 45%→21% に下がった。
仕組み: 検索は src/teamagent/skills/search（skill.py・fanout.py・composite.py・drive_first.py・direct_search_answer）。本番 env: SEARCH_DIRECT_ANSWER=1（KINDS=not_found,list_fast,comparison）・SEARCH_DRIVE_FIRST=1（全員）・SEARCH_FANOUT=1・SEARCH_MIN_RELEVANCE=0.4（fallback 0.05）・USE_COHERE_RERANK。
やること:
1. 「無い（not_found）」を返すまでに何をしているかを経路ごとに整理し、1 回の検索・1 つの手段だけで not_found を確定している箇所をファイル:行で列挙。特に、10/19 以降に入った変更（git log --since=2026-10-19 -- src/teamagent/skills/search src/teamagent/mcp_gateway）で、not_found が増えうるもの（直接回答の not_found・Drive 優先で他の源を見ない・関連度の閾値など）を特定する。
2. not_found を返す前に必ず探し方を広げる共通の段を入れる（既存の部品で）: 表記ゆれ・別名（既存の別名展開があれば使う）、Drive 以外の源（Slack・カルテ・事例・金庫）への fanout、閾値の段階的な緩和。それでも無いときだけ「無い」と言い、そのときも「どこを・何の語で探したか」を 1 行で示し、利用者に作業を戻さない（「◯◯に聞いて」「自分で見て」は禁止）。
3. テスト: 1 回目で 0 件・広げると見つかるフェイクで、not_found にならないこと。広げても無いときの文面。既存テストを壊さない。`uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/skills/search tests/mcp_gateway` と ruff・mypy。
禁止: git commit・push、AWS・DB・Slack の実接続、.env・secrets の読み取り。差分は小さく。最後に、増えた原因の見立て（変更 commit）・変更点・テスト結果を短く。
