# 依頼: カルテ（clientkarte）の答えを、コードからそのまま届ける（A3 の 2 回目）

作業場所: ~/dev/worktrees/teamagent-a3direct（branch feat/karte-direct-post-20261105・origin/dev 起点）

## 本番で起きたこと（11-05 13:24・#822 が本番に入った後・小俣さんの DM）
「JTBの案件って今どうなってる？」に、clientkarte が呼ばれた。#822 で足した「8月26日に…予定していた（結果は未確認）」の書き方は残ったが、次の 2 つは返事から消えていた。
- 「（M/D 時点）」
- case_state の「状態: …（根拠日）」の行（出力されたかどうかも、ログからは分からない）

会話のモデル（OpenClaw）がツールの結果を自分の書式に書き直すとき、日付や状態の行を落としていると見ている。

## 既にある仕組み（これに揃える）
- src/teamagent/mcp_gateway/direct_summary.py（USE_DIRECT_SUMMARY_POST・DIRECT_SUMMARY_POST_ALLOWED_EMAILS。本番は全員 ON）
- src/teamagent/mcp_gateway/direct_search_answer.py（SEARCH_DIRECT_ANSWER）
- src/teamagent/mcp_gateway/direct_video_metrics.py
いずれも「コードが作った答えを、そのまま利用者の DM に投稿し、モデルには『投稿済み・繰り返さない』とだけ返す」形。server.py 1713 行付近で呼ばれている。

## 直すこと
1. clientkarte も同じ形で、答えをそのまま DM に投稿できるようにする。新しいフラグを 2 つ作る:
   - USE_DIRECT_KARTE_POST: 既定 OFF
   - DIRECT_KARTE_POST_ALLOWED_EMAILS: 空なら誰にも適用しない
   既存の direct_* と同じ作り（DM 限定・失敗したら従来どおりモデルに返す fail-open・二重投稿しない）にする。
2. 投稿する本文は、clientkarte の answer（#822 の「時点」「予定だった」「状態」行を含む）。長さの上限や Slack の書式は、direct_summary と同じ扱い。
3. clientkarte の終わりのログ（clientkarte_done）に、次の項目を足す（本文は書かない）:
   - has_request_text（bool）
   - asks_status（bool）
   - case_state_lines（状態の行の件数）
   - asof_lines（「時点」を付けた件数）
4. env を足したら、tfvars・terraform の変数も既存の direct_* と同じ形で足す（既定 OFF・空）。契約テスト（tests/infra の env の一覧など）も合わせる。

## 合格条件
- テストを足す:
  - (a) フラグ ON・許可されたメールの DM では、そのまま投稿され、モデルには「投稿済み」が返る。
  - (b) フラグ OFF・許可されていない人・DM でないときは、従来どおり。
  - (c) 投稿に失敗したら、従来どおりモデルに返す。
  - (d) clientkarte_done のログに 4 つの項目が出る。
- 次が全部緑:
  uv run --extra dev --extra mcp pytest -q -n 4 tests/mcp_gateway tests/skills/clientkarte tests/infra
  uv run --extra dev --extra mcp ruff check src tests
  uv run --extra dev --extra mcp ruff format --check src tests
  uv run --extra dev --extra mcp mypy src/teamagent/mcp_gateway src/teamagent/skills/clientkarte
- pytest の事前確認（ps）は Claude が済ませた。手元で走る pytest はこの 1 本だけなので、実行してよい。

## やらないこと
- ツールの description の変更（#820 の字数上限・入力の形の契約がある）。
- git add・commit・push、AWS、.env・secrets の読み取り。
