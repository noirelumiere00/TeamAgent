# 依頼: 資料を届けるとき「該当ページ」と「原本リンク」を 1 通で付ける（A6・knowledge_deliver）

作業場所: ~/dev/worktrees/teamagent-a6pages（branch feat/deliver-pages-20261105・origin/dev 起点）

## 背景
全 DM の判定で「欲しい資料・原本・形式が出ない」が上位（10/6 に 37 件・10/30 にも 40 件）。利用者は「◯◯の提案書出して」で PDF・PPTX をもらっても、**何ページ目に該当箇所があるか**が分からず、全部めくって探している。原本（Drive）のリンクが添付と別の通になったり、無かったりもする。

## まず調べること（コードを読むだけ）
- src/teamagent/skills/knowledge_deliver/skill.py と schema.py、src/teamagent/skills/_shared/drive_slack_delivery.py（添付と DM の部品）。
- 検索結果（search の hit / chunk）に、PDF のページ番号や PPTX のスライド番号が入っているか。取込（src/teamagent/ingest/ の pdf・pptx の抽出）で chunk の metadata に page/slide を残しているか。
  - 残っているなら、それを使う。
  - 残っていないなら、**取込の変更はせず**、今回はページを出さない（その事実と、取込で残すための変更案を報告だけする）。

## 直すこと（小さく）
1. 届けるファイルごとに、該当箇所のページ・スライド番号を最大 3 つ付ける（例「該当: 12・13 ページ」）。番号の出所は検索でヒットした chunk だけ。推測しない。
2. 届けるファイルごとに、Drive の原本リンクを同じ通に必ず付ける。添付が失敗した時も、原本リンクと該当ページは返す（fail-open を保つ）。
3. 1 回の依頼で届けるのは最大 3 ファイル（今の上限があればそれに従う）。1 通にまとめる。
4. 出力の KnowledgeRef に `pages: list[int]`（空可）を足す。ツールの説明（description）は増やさない。#820 でツール定義を短くしているので、説明に文を足さないこと。

## 合格条件
- テストを足す:
  - (a) chunk に page がある PDF では「該当: N ページ」が出る。
  - (b) 番号が無い資料では、ページの行を出さない。
  - (c) 添付に失敗しても、原本リンクとページは返る。
  - (d) 4 件ヒットしても 3 件まで。
- 次が全部緑:
  uv run --extra dev --extra mcp pytest -q tests/skills/knowledge_deliver tests/skills/_shared
  uv run --extra dev --extra mcp ruff check src/teamagent/skills/knowledge_deliver src/teamagent/skills/_shared tests/skills/knowledge_deliver
  uv run --extra dev --extra mcp ruff format --check src/teamagent/skills/knowledge_deliver src/teamagent/skills/_shared tests/skills/knowledge_deliver
  uv run --extra dev --extra mcp mypy src/teamagent/skills/knowledge_deliver src/teamagent/skills/_shared

## やらないこと
- 取込・DB・LLM 呼び出しの変更、新しい依存の追加。
- git commit・push、AWS 呼び出し、.env・secrets の読み取り。

最後に: ページ番号の出所（あった／無かった）・変更点・テスト結果を短く。
