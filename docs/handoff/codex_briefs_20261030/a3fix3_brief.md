# 依頼: カルテをそのまま届けたときの見え方を直す（A3 の 3 回目）

作業場所: ~/dev/worktrees/teamagent-a3fix3（branch fix/karte-direct-polish-20261105・origin/dev 起点。#822・#826 は入っている）

## 本番で起きたこと（11-05 14:53・USE_DIRECT_KARTE_POST は小俣さんだけ ON）
「新しい会話」の後に「JTBの案件って今どうなってる？」を送ると、clientkarte の答えがそのまま DM に投稿された。ログ clientkarte_done は has_request_text=True・asks_status=True・case_state_lines=0・asof_lines=3。投稿の中身に次の 4 つの問題があった。

1. **内部の値が出ている**: 本文に「[chunk_id: 388837349]」「[chunk_id: 186333800]」がそのまま出た。モデルの書き直しが無くなったので、内部の参照記号が利用者に見える。
2. **「時点」がばらばら**: 案件ごとの見出しに付かず、本文の最後に「UGC（TTO，切り抜きなど）（6/30 時点）」「UGC（TTO、切り抜きなど）, その他（8/7 時点）」「その他（8/26 時点）」という行が並んだ。カルテの本文（LLM が書く）は「*直販・企業広報案件（…）*」のような太字の見出しで、#822 が想定した「### 案件」形式ではない。
3. **状態の行が出ない**: case_state_lines=0。同じ JTB について、search 経由の case_state では「JTB 企業広報の提案 2026-09-11 → 見送り」が出ていた（11-04 の本番）。clientkarte 側の状態の引き方（search_drive_by_client_names など）が、search 側と違う経路・名前照合で、その記録に当たっていない。
4. 1 行目の Markdown 見出し「# JTB クライアント・カルテ」が、Slack ではそのまま「#」で出る。

## 直すこと
1. 直接投稿の本文から、`[chunk_id: N]` などの内部の参照記号を取り除く。既存の「送る直前の洗浄」（#566・C1 #766 の最終整形。grep で探す）があればそれを使い、無ければ direct_karte に足す。モデル経由の通常の返しの挙動は変えない。
2. 「時点」は案件ごとに 1 回、その案件の見出し行の末尾に付ける。見出しの形は、カルテの本文が実際に出す形（太字の行 `*…*` / `**…**` / `### …`）に合わせる。見出しが見つからない案件は、本文の末尾に「（M/D 時点の記録）」を 1 行だけ足す。ばらばらの行は出さない。
3. clientkarte の状態の引き方を、search 側で case_state を出している経路（src/teamagent/skills/search/timeline_status.py・fanout.py / skill.py の case_state の呼び出し）と同じ関数・同じ取引先名の照合（normalize_client・names_overlap・resolve_topic_client）に揃える。同じ取引先名なら、search と同じ状態の記録に当たるようにする。
4. 直接投稿の本文の Markdown 見出し（`#` `##` `###`）は、Slack の太字（`*…*`）に変える。

## 合格条件
- テストを足す。now_jst は固定する。
  - (a) `[chunk_id: N]` が投稿に出ない。
  - (b) 太字の見出しを持つ本文で、「（M/D 時点）」が各見出しの末尾に 1 回だけ付き、末尾のばらばらの行が無い。
  - (c) search の case_state で状態が出る取引先・記録のとき、clientkarte でも同じ状態の行が出る。両者で同じフィクスチャを使う。
  - (d) `# 見出し` が `*見出し*` になる。
- 次が全部緑:
  uv run --extra dev --extra mcp pytest -q -n 4 tests/mcp_gateway tests/skills/clientkarte tests/skills/search
  uv run --extra dev --extra mcp ruff check src tests
  uv run --extra dev --extra mcp ruff format --check src tests
  uv run --extra dev --extra mcp mypy src/teamagent/mcp_gateway src/teamagent/skills/clientkarte src/teamagent/skills/search
- pytest の事前確認（ps）は Claude が済ませた。手元で走る pytest はこの 1 本だけなので、実行してよい。

## やらないこと
- ツールの description の変更、DB・取込・マイグレーションの変更。
- git add・commit・push、AWS、.env・secrets の読み取り。

最後に: 状態の行が出なかった原因（ファイル:行）・変更点・テスト結果を短く。
