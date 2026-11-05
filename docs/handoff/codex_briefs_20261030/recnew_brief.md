# 依頼: 似た提案書で新しい資料を先に出す（recommend）

作業場所: ~/dev/worktrees/teamagent-recnew（branch fix/recommend-prefer-recent-20261102・#794 のブランチ起点）
対象: src/teamagent/skills/recommend/skill.py の `RecommendSkill.run` の候補の並べ方（`for hit in sorted(groups[0], ...)` の所）と tests/skills/recommend/test_recommend_skill.py

## いまの問題
「伊藤ハムの提案書に似た過去の提案書を探して」で、同じ会社の枠に 2025年11月の提案書が出た。ナレッジには 2026年5月の伊藤ハム資料もある。
利用者から「資料が気持ち古い」と言われた。

## 直すこと（小さく）
1. 同じ会社の枠（1 件）は、同じ会社の候補のうち日付が一番新しいものを出す（資料リンクがあるものを優先する既存の順は保つ）。
2. 他社の候補は関連度順を基本にし、関連度（hit.score）の差が 0.05 以内の候補どうしは日付が新しい方を先にする。日付が取れない候補は同点内で後ろ。
3. 日付は `_to_item` が使っているのと同じ取り方（entry.date / extract_title_date の既存経路）を再利用する。新しい日付解析を書かない。

## やらないこと
- 検索・取込・新しい依存・LLM 呼び出しの変更はしない。

## 合格条件
- テストを追加: (a) 同じ会社で 2025-11 と 2026-05 があれば 2026-05 が出る (b) 関連度がほぼ同じ他社 2 件は新しい方が先 (c) 関連度が大きく違えば古くても関連度が高い方が先。
- 次が全部緑:
  uv run --extra dev --extra mcp pytest tests/skills/recommend -q
  uv run --extra dev --extra mcp ruff check src/teamagent/skills/recommend tests/skills/recommend
  uv run --extra dev --extra mcp mypy src/teamagent/skills/recommend
- コミットはしない（Claude がレビューしてからコミットする）。
