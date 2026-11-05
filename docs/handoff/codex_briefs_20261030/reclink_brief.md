# 依頼: 似た提案書で「リンクのある提案書」を全体で先に選び、同じ「似ている点」の繰り返しを消す（recommend）

作業場所: ~/dev/worktrees/teamagent-reclink（branch fix/recommend-linked-first-20261104・dev 起点）
対象: src/teamagent/skills/recommend/skill.py の `RecommendSkill.run` と `_similarity` 周り、tests/skills/recommend/test_recommend_skill.py

## 本番で起きたこと（11-04 11:25・r94/r95 後）
「伊藤ハムの提案書に似た過去の提案書を探して」で他社 4 件＋自社 1 件が出たが、
- 5 件とも Drive リンクの無い候補（ログ recommend_material_unregistered が 5 件）。11-02 の版では同じ質問でリンク付きの提案書（サントリー 1001・エスビー食品 0911 など）が出ていた。
- 他社 4 件の「似ている点」がすべて同じ文「ショート動画提案（UGCや切り抜き、メディア）」。
疑い: 「直近の似た提案」枠が日付の新しいリンク無しの記録（表の行など）を先に取り、その会社が「1 社 1 件」で埋まってリンク付きの提案書が押し出された。

## 直すこと（小さく）
1. 他社の選び方（直近 2・特に似ている 2）を、まずリンク付き（`_material_url` が取れる）候補だけで行う。他社が 4 件に満たない時だけ、残りをリンク無しの候補で同じ規則（直近→関連度）で埋める。1 社 1 件の重複除外はリンク付きを先に通すこと（リンク無しの行が会社枠を先取りしない）。
2. 自社の枠（1 件）も同様にリンク付きの最新を優先し、無ければリンク無しの最新。
3. 同じ回答の中で「似ている点」の文が他の候補と完全に同じになったものは、その文を使わず `_similarity` の次の手（値の列挙／業種だけなら「中身の共通点は資料で確認してください」）に落とす。最初の 1 件も含めて重複した文は全部落とす（どれも決まり文句なので）。
4. 見出し・カードの形・2/2/1 の件数・6 か月の直近は今のまま。

## やらないこと
- 検索・取込・新しい依存・LLM 呼び出しの変更はしない。

## 合格条件
- テスト追加: (a) 直近のリンク無し行と同じ会社のリンク付き提案書がある時、リンク付きが選ばれる (b) リンク付きが 2 件しか無ければ残り 2 件はリンク無しで埋まる (c) 3 件の detail が同じ文なら 3 件ともその文を使わない (d) 自社枠はリンク付き最新優先。
- 次が全部緑:
  uv run --extra dev --extra mcp pytest tests/skills/recommend tests/skills/test_routing_descriptions_catalog.py -q
  uv run --extra dev --extra mcp ruff check src/teamagent/skills/recommend tests/skills/recommend
  uv run --extra dev --extra mcp ruff format --check src/teamagent/skills/recommend tests/skills/recommend
  uv run --extra dev --extra mcp mypy src/teamagent/skills/recommend
- コミットはしない（Claude がレビューしてからコミットする）。
