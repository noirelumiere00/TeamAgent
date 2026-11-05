# 依頼（やり直し）: 似た提案書を「直近 2・特に似ている 2・自社 1」の 5 件で出す

作業場所: ~/dev/worktrees/teamagent-recnew（branch fix/recommend-prefer-recent-20261102・PR #795）
対象: src/teamagent/skills/recommend/skill.py の `RecommendSkill.run`（並べ方と answer の組み立て）と tests/skills/recommend/test_recommend_skill.py
前の版（同点 0.05 以内だけ日付順）は利用者の要望と違うので置き換える。`_date` の共通化は残してよい。

## 利用者の要望（そのまま）
事例は日時・スピード感が大事。直近のもの → その下に関連度の高いもの → 最後に自社の過去提案。資料が複数あるなら 5 個くらいで、2・2・1 のようにバランスよく。

## 仕様
1. 他社の候補（リンクあり優先・1 社 1 件の既存ルールは維持）から:
   - 「直近の似た提案」最大 2 件: 日付が今日から 6 か月以内の候補を日付の新しい順。
   - 「特に似ている提案」最大 2 件: 残りの候補を関連度（hit.score）順。直近で使った候補・会社は除く。
   - 直近が 2 件に満たなければ、空いた分は「特に似ている提案」で埋める（合計の他社は最大 4 件）。
2. 「自社の過去提案」最大 1 件: 同じ会社の候補のうち日付が一番新しいもの（今の same_client と同じ扱い）。
3. answer は 3 つの小見出しで分ける（空の節は出さない）。各カードの形（*案件名*（年月）・似ている点・資料の案内）は今のまま。
   - 「直近の似た提案」「特に似ている提案」「自社の過去提案」
4. 「今日」は既存の JST の現在時刻ヘルパー（リポジトリ内の now_jst 等）を使い、テストでは固定する。新しく作らない。
5. RecommendOutput の similar_proposals は表示順（直近→似ている）、same_client_materials は自社 1 件。total_count は他社の件数。

## やらないこと
- 検索・取込・新しい依存・LLM 呼び出しの変更はしない。

## 合格条件
- テスト: (a) 6 か月以内 3 件・古い高関連 3 件 → 直近 2（新しい順）＋似ている 2（関連度順）＋自社 1 (b) 直近 0 件 → 似ている 4 件 (c) 直近の会社と同じ会社は「似ている」に重ねない (d) 自社は最新 1 件 (e) 空の節の見出しを出さない。
- 次が全部緑:
  uv run --extra dev --extra mcp pytest tests/skills/recommend -q
  uv run --extra dev --extra mcp ruff check src/teamagent/skills/recommend tests/skills/recommend
  uv run --extra dev --extra mcp mypy src/teamagent/skills/recommend
- コミットはしない（Claude がレビューしてからコミットする）。
