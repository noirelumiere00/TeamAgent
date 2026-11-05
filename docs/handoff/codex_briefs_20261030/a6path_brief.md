# 依頼: knowledge_deliver で「該当: N ページ」が出ない経路を特定して直す（A6 の 3 回目）

作業場所: ~/dev/worktrees/teamagent-a6path（branch fix/deliver-page-path-20261105・origin/dev 起点。#821・#823 は入っている）

## 本番の事実（11-05）
- 13:07 に r100（#823 入り）が本番に入った。
- 13:09、小俣さんの DM で「花王のmeltの提案書で、施策の全体構成が書いてあるところをファイルで送って」と頼んだ。返事は 1 通で、「該当する提案書です。 / <ファイル名>.pdf / 原本: <Drive リンク>」。**「該当: N ページ」は出なかった**。
- 本番 DB（読み取りのみ）で、その資料（gdrive・タイトル「提案_株式会社花王_meltプロモーション施策_…」・Drive ID 1sgF6klCvfR5aTn3l70GJ-1oWu8UMM5dN）の chunks を数えた:
  - 29 件。`page_num` 列は 0 件、`metadata->>'page_num'` は 29 件すべてにあった。
- つまり番号は DB にある。それでも出ないので、knowledge_deliver が使う検索の経路で、hit の page_num が埋まっていないはず。

## 調べること
- knowledge_deliver（src/teamagent/skills/knowledge_deliver/skill.py）→ SearchSkill.run → どの経路（fanout・drive_first・直接回答・統合 DB 検索・タイトル一致の Drive 解決など）で hit が作られるかを追う。本番の env（SEARCH_DRIVE_FIRST=*・SEARCH_FANOUT_ALLOWED_EMAILS=*・SEARCH_DIRECT_ANSWER=1 で not_found,list_fast,comparison）を前提にする。
- #823 で直したのは src/teamagent/adapters/pgvector_client.py の 2 か所の SQL（`c.metadata->>'page_num' AS metadata_page_num` と `_chunk_page_num`）。それ以外に chunk から hit を作る SQL や経路（統合ビュー・RPC・documents だけの hit など）が無いか、grep で全部洗う。
- knowledge_deliver 側の条件（`about_client`・`h.source_type in (None, "gdrive")`・`h.page_num`・file_id の一致）で落ちていないかも確かめる。

## 直すこと
- 番号を落としている経路で、#823 と同じ規則（列が優先・NULL なら metadata の 1 以上の整数）で page_num を埋める。共通の関数（_chunk_page_num）を使い、同じ規則を 2 か所に書かない。
- テストを足す。本番の env を前提に、knowledge_deliver が実際に通る経路で、metadata にだけ page_num がある資料のとき「該当: N ページ」が出ることを確かめる。

## 合格条件（全部緑）
uv run --extra dev --extra mcp --extra media pytest -q -n 4 tests/skills/knowledge_deliver tests/skills/search tests/adapters
uv run --extra dev --extra mcp ruff check src tests
uv run --extra dev --extra mcp ruff format --check src tests
uv run --extra dev --extra mcp mypy src/teamagent/skills/knowledge_deliver src/teamagent/skills/search src/teamagent/adapters
pytest の事前確認（ps）は Claude が済ませた。手元で走る pytest はこの 1 本だけなので、実行してよい。

## やらないこと
- マイグレーション・本番 DB の書き込み・ツールの description の変更。
- git add・commit・push、AWS、.env・secrets の読み取り。

最後に: 番号を落としていた経路（ファイル:行）・直し方・テスト結果を短く。
