# 依頼: 資料のページ番号が検索結果に届くようにする（A6 の続き）

作業場所: ~/dev/worktrees/teamagent-a6pagenum（branch fix/chunk-page-num-20261105・origin/dev 起点）

## 背景
#821 で、knowledge_deliver が検索ヒットの `page_num` から「該当: N ページ」を出すようになった。しかし多くの資料で番号が出ない。原因は次のとおり。
- 取込（src/teamagent/ingest/pipeline.py 1265〜1290 行付近）は、PDF のページ番号を chunk の `metadata={"page_num": ...}` にだけ入れている。`ChunkUpsert.page_num` 列（src/teamagent/ingest/repository.py 110 行・1825 行の INSERT）には入れていない。
- 統合検索は列の `page_num` を読むので、None になる。

## 直すこと（小さく）
1. 取込: PDF（と、同じ形でページ・スライド・タブ番号を metadata に入れている経路）で、`ChunkUpsert(page_num=...)` にも同じ値を入れる。metadata の値はそのまま残す。
2. 検索: chunk の page_num を読む SQL / 組み立てで、列が NULL なら `metadata->>'page_num'`（整数にできるときだけ）を使う。これで、取込済みの既存資料にも DB を書き換えずに効く。対象は search の SearchHit に page_num を詰める箇所すべて（grep で探す）。
3. 本番 DB の更新（backfill）・マイグレーションは**しない**。

## 合格条件
- テストを足す:
  - (a) 取込で、PDF の chunk に列の page_num が入る。
  - (b) 列が NULL で metadata に page_num があると、検索ヒットの page_num に入る。
  - (c) metadata の page_num が数字でなければ None。
  - (d) 列と metadata の両方があれば、列が優先。
  実 PostgreSQL のテストがある箇所はその形に合わせる。
- 次が全部緑:
  uv run --extra dev --extra mcp pytest -q tests/ingest tests/skills/search tests/skills/knowledge_deliver
  uv run --extra dev --extra mcp ruff check src/teamagent/ingest src/teamagent/skills/search tests/ingest tests/skills/search
  uv run --extra dev --extra mcp ruff format --check src/teamagent/ingest src/teamagent/skills/search tests/ingest tests/skills/search
  uv run --extra dev --extra mcp mypy src/teamagent/ingest src/teamagent/skills/search

## やらないこと
- マイグレーション・本番 DB の書き込み・新しい依存・LLM 呼び出しの変更。
- git add・commit・push、AWS、.env・secrets の読み取り。

最後に: 変更点・番号が出るようになる資料の範囲・テスト結果を短く。
