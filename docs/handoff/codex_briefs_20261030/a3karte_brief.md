# 依頼: 「◯◯の案件どうなってる？」にカルテが古い予定を今の状態のように答える件（A3・clientkarte）

作業場所: ~/dev/worktrees/teamagent-a3karte（branch fix/karte-status-asof-20261105・origin/dev 起点）

## 本番で起きたこと（11-05 10:13・小俣さんの DM・CASE_STATE_EVIDENCE_ALLOWED_EMAILS は本人だけ ON）
「JTBの案件って今どうなってる？」→ モデルは clientkarte を選び、次のように答えた:
- 「宿題: *8/26 に自主提案を予定していた*（結果確認が必要）」「コンペ決定: 10月中旬〜下旬」「先方担当者が9月下旬退職予定」
- いつ時点の情報かが書かれていない。過ぎた予定（8/26・10月中旬）が、今の状態のように並んでいる。
- 前日の検索（search＋case_state）では「JTB 企業広報の提案 2026-09-11 → 見送り」と返っていて、内容が食い違う。

## 直すこと（小さく）
1. **基準日を出す**: clientkarte の答えで、各案件に、情報の元になった日付（カルテの最終更新日・最終接点）を「（◯/◯ 時点）」として 1 回付ける。
2. **過ぎた予定の書き方**: 予定・期日（提案予定日・決定時期など）が今日（now_jst）より前なら「◯/◯ 予定だった（結果は未確認）」と書く。今の予定のようには書かない。
3. **状態の正本と突き合わせる**: `case_state_enabled(email)` かつ `timeline_status.asks_status(query)` のとき、search 側と同じ case_state の判定（src/teamagent/skills/search/timeline_status.py の render_case_states など）をその取引先で引く。結果がある案件には、状態と根拠日を 1 行添える（例「状態: 見送り（9/11 の提案資料より）」）。無ければ何も足さない。
4. 1〜3 は、今の答えの形（見出し・箇条）を大きく変えない。ツールの description は変えない（#820 で短くしたため）。

## 合格条件
- テストを足す。日付は now_jst を固定する:
  - (a) 最終更新が 8 月のカルテに「（8/xx 時点）」が付く。
  - (b) 過去の予定日が「予定だった（結果は未確認）」になる。
  - (c) case_state が ON の本人で、状態語の質問なら状態行が付く。OFF の人や状態を聞いていない質問では付かない。
  - (d) case_state に該当が無ければ何も足さない。
- 次が全部緑:
  uv run --extra dev --extra mcp pytest -q tests/skills/clientkarte tests/skills/search/test_timeline_status*.py
  uv run --extra dev --extra mcp ruff check src/teamagent/skills/clientkarte tests/skills/clientkarte
  uv run --extra dev --extra mcp ruff format --check src/teamagent/skills/clientkarte tests/skills/clientkarte
  uv run --extra dev --extra mcp mypy src/teamagent/skills/clientkarte

## やらないこと
- 取込・DB・LLM 呼び出しの変更、新しい依存の追加、ツールの description の変更。
- git commit・push、AWS 呼び出し、.env・secrets の読み取り。

最後に: 変更点・テスト結果・未確認のことを短く。
