あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
対象は統合提案書の自動調査 src/teamagent/skills/proposal_research/skill.py（mcp イメージ側）。

## 起きていること（10-30 本番）
自動調査の段B（注釈付きメモ → 中間JSON）で、2 回とも「不足した区分」が出て ResearchError になり、提案書ジョブ全体が失敗した:
- 1 本目: H_event（季節とイベント）が空
- 2 本目: G_insight（不満と欲求）と H_event が空
同じ依頼が別の時刻には通る（10-29〜10-30 の 6 本は成功）。デプロイ差分・環境変数の差は無く、モデルの揺れと段Bの判定の弱さが原因と判断した。

段Bの弱いところ（コードで確認済み）:
- `_materialize`: G_insight は 4 文すべてに、G_insight_H_event メモの使える番号（`sources.allowed(memo_refs[...])`）だけが付いていないと区分ごと {} になる。H_event は 1 件だけで、番号が区分外か、値が 1 つでも空なら {}。
- 段Bのプロンプトの usable_sources は全区分の一覧で、区分ごとにどの番号が使えるかを渡していない。
- 再試行の指示は「不足した区分: G_insight, H_event」だけで、何が悪いか（区分外の番号・空の値・件数不足）を伝えていない。
- `_missing_sections` で 2 回目に raise するので、段Cの `_finalize` にある区分ごとの修復（その区分のメモ＋その区分で使える番号だけを渡す repair_section）に届かない。
- 失敗時は区分ごとの捨てた理由がログに出ないので、次に起きても原因を特定できない。

## 直すこと
1. 段B: 中間JSONは読めたが区分が足りないまま再試行を使い切ったら、raise せず段Cへ進み、`_finalize` の区分修復に任せる。先に確かめること: `parse_gemini_research` が空の G_insight / H_event を拒む（でないと required に入らない）・`_FINAL_RETRY_TOTAL_USD` で 2 区分を直せる・`attempted` で各区分 1 回・H_event の単一オブジェクトが `_materialize(replacement, …)` を通る。修復でも直らなければ従来どおり ResearchError（利用者向け文言は今のまま）。
2. 段Bのプロンプトと再試行の指示に、区分ごとの使える番号（allowed の集合）を渡す。再試行では項目ごとの失敗理由をコードで伝える（ref_outside_section / inline_ref_outside_section / empty_field / insight_count_short など。本文は入れない）。プロンプトの .md にも「各区分は、その区分のメモの番号だけを使う」と明記する。
3. ログ: 段Bの不足時と最終失敗時に、区分ごとの memo_refs 数・allowed 数・理由別の捨てた件数を出す（本文・URL は出さない）。`prepare` の URL 確認結果も reason / bot_blocked の件数だけ出す。
4. 失敗通知の文面: 提案書ジョブの失敗を DM に知らせる箇所で、一般的な理由ではなく error_summary（例「調査結果を提案書の形にまとめられませんでした（足りない区分: 季節とイベント）」）と「同じ依頼をもう一度送ると通ることがあります」を出す。どこで文面を作っているか探して最小の変更で。
5. テスト（先に失敗するもの）: フェイク Gemini で、段Bが G_insight に区分外の番号・H_event に空の値を返す → 以前は ResearchError、修正後は区分修復で直って完了。修復の予算切れ・修復でも直らない場合は従来どおり ResearchError。再試行の指示に区分ごとの番号と理由コードが入る。失敗通知に error_summary が入る。

## 共通
- `uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/skills/proposal_research tests/skills/proposal_builder tests/mcp_gateway`（無ければ近いディレクトリ）と ruff・mypy（変更ファイル）。
- 禁止: git commit・push、AWS・Gemini・Bedrock・Slack の実呼び出し、.env・secrets の読み取り。顧客名を報告に書かない。差分は小さく。
- 最後に変更点（ファイル:行）・テスト結果・残る懸念を短く。
