あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
背景: 全 DM 判定（10/30）で「不要な聞き返し」76 件・「会話の流れが崩れる（直前に話した会社を忘れる・1 通ずれる・混線）」59 件。型: 直前の会話で話した企業・案件・チャンネルについて「どの企業ですか」と聞き返す、Aico 自身が提案した次の一手に「お願い」と答えたのに「何をお手伝いしましょう」と流れが切れる、添付のシートのタブ名など推せることを何項目も確かめてから着手する。
仕組み: OpenClaw の外側ループ（infra/openclaw/SOUL.md の聞き返し・会話の規則、caller-identity plugin の会話管理）と、MCP の共通入力ルール（USE_INPUT_POLICY_V1・src/teamagent/skills/_shared の input policy）。会話の区切り（UTC13 時・idleMinutes）と「新しい会話の 1 通目」の扱い（#539・#546）も関係する。
やること:
1. 聞き返しが起きる経路（SOUL の規則・input policy の needs_input・ツールの必須引数）を列挙し、直前の会話（同じ DM スレッド・直近 N 往復）から推せる値で埋められるのに聞き返している箇所を特定する。
2. 直前の会話の対象（企業名・案件・チャンネル・Aico が提案した次の一手）を引き継いで、聞き返さずに着手する仕組みを、既存の部品で入れる（例: 直前の Aico の提案に「お願い」「それで」と返されたら、その提案の 1 番目を実行する。直前に特定した企業を省略された主語に使う）。推せないときだけ 1 問で聞く。
3. SOUL を変える場合は字数上限の契約テストを守る。plugin を変える場合は node テストを足す。テストは先に失敗するものから。`uv run --extra dev --extra mcp pytest -q -n auto tests/skills tests/mcp_gateway tests/infra` 相当・node テスト・ruff。
禁止: git commit・push、AWS・Slack の実呼び出し、.env・secrets の読み取り。差分は小さく。最後に変更点・テスト結果・挙動の変化を短く。
