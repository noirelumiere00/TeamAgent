# 依頼: 似た提案書の「似ている点」を中身から作る（recommend）

作業場所: ~/dev/worktrees/teamagent-recsim（branch fix/recommend-similar-content-20261102・dev 起点）
対象: src/teamagent/skills/recommend/skill.py の `RecommendSkill._similarity` とテスト tests/skills/recommend/test_recommend_skill.py

## いまの問題（本番 11-02 18:08 の実際の返事）
「伊藤ハムの提案書に似た過去の提案書を探して」に対して 3 件とも
- 業種「食品・飲料」が共通。ショート動画提案（UGCや切り抜き、メディア）
- 業種「食品・飲料」が共通。            ← 業種だけ
- 業種「食品・飲料」が共通。注力タイミングでの広告強化で購買への寄与。
業種は検索の前提なので全件同じになり、開く理由にならない。

## 直すこと（小さく）
1. 業種（cls_industry）だけが共通のときは、業種を先頭に置かない。候補の中身（施策 cls_solution / proposed_menu、対象層、商材、本文から取った detail）を主にする。
   例: 「似ている点：同じ食品・飲料で、ショート動画（UGC・切り抜き）の提案。」
2. 施策・対象層・商材のどれかが共通なら、それを先に書く（業種は省くか後ろ）。
3. 中身が何も取れず業種しかないときは、正直に「似ている点：同じ業種（食品・飲料）の提案。中身の共通点は資料で確認してください。」
4. 1 行は 90 字以内に切る（既存の _clean を使う）。
5. 会社名・住所を出さない既存の usable() の守りはそのまま。

## やらないこと
- 新しい依存・新しい LLM 呼び出し・検索の変更はしない。_similarity の中だけで済ませる。

## 合格条件
- 上の 1〜3 の各ケースのテストを test_recommend_skill.py に 1 本ずつ追加（既存の作り方に合わせる）。
- 次が全部緑:
  uv run --extra dev --extra mcp pytest tests/skills/recommend -q
  uv run --extra dev --extra mcp ruff check src/teamagent/skills/recommend tests/skills/recommend
  uv run --extra dev --extra mcp mypy src/teamagent/skills/recommend
- コミットはしない（Claude がレビューしてからコミットする）。
