# 依頼: 案件の状態の一覧（case_state）は「状態を聞かれた時だけ」・最大 3 件・内部の位置を出さない

作業場所: ~/dev/worktrees/teamagent-casestq（branch fix/case-state-only-status-questions-20261104・dev 起点）
対象:
- src/teamagent/skills/search/timeline_status.py（`render_case_states`・`status_answer` の問い判定）
- src/teamagent/skills/search/fanout.py（1157 行付近 `if case_state_enabled(...)`）
- src/teamagent/skills/search/skill.py（1369 行付近 `if case_state_enabled(...)`）
- 対応するテスト（tests/skills/search/ 配下の case_state / timeline_status のテストに合わせる）

## 本番で起きたこと（11-04 13:09・CASE_STATE_EVIDENCE_ALLOWED_EMAILS で本人だけ ON）
「JTB と HIS の過去の提案書や事例ってある？」（状態は聞いていない）に対して、検索の答えが
「対象 1（資料名: …）正式状態は不明（正本未確認）資料種別… 根拠日… 出典… / 位置: chunk:-1371208571」
が 10 件以上並ぶ一覧に丸ごと置き換わり、2 通に分かれて出た。

## 直すこと（小さく）
1. 状態の問いの判定を 1 か所にまとめる: `status_answer` 内の正規表現（今どう|どうなって|進捗|現況|状況|現状|今の状態|ステータス）を module 定数＋関数 `asks_status(query) -> bool`（1 行目だけを見る今の扱いを保つ）にし、`status_answer` もそれを使う。受注・失注・決まった・決定・提案中 などの状態語の問い（「〜は決まった？」「受注した？」）も含める。
2. fanout.py と skill.py の両方で、`case_state_enabled(...)` かつ `asks_status(input.query)` の時だけ case_answer を作る。状態を聞いていない問いは従来の検索の答えのまま。
3. `render_case_states` は対象を最大 3 件にする（4 件目以降は「ほか N 件」と 1 行）。
4. 「位置:」は利用者に意味の無い内部値（`chunk:` や `message:` で始まるもの）なら出さない。出典リンクは残す。

## やらないこと
- 状態の判定ロジック・取込・新しい依存・LLM 呼び出しの変更はしない。

## 合格条件
- テスト: (a) 状態を聞かない問いでは case_state で置き換わらない（fanout と skill の両経路） (b) 「◯◯の進捗は？」では従来どおり case_state (c) 「◯◯は受注した？」も case_state (d) 対象 5 件 → 3 件＋「ほか 2 件」 (e) `chunk:` の位置は出ない。
- 次が全部緑:
  uv run --extra dev --extra mcp pytest tests/skills/search -q
  uv run --extra dev --extra mcp ruff check src/teamagent/skills/search tests/skills/search
  uv run --extra dev --extra mcp ruff format --check src/teamagent/skills/search tests/skills/search
  uv run --extra dev --extra mcp mypy src/teamagent/skills/search
- コミットはしない（Claude がレビューしてからコミットする）。
