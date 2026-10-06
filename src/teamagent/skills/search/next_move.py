"""search の結果に添える「次の一手」（``suggested_next``・1 個だけ・決定論）。

背景（2026-10-06・小俣さんの指摘）: Aico は 1 回の依頼で 1 種類のツールしか使わないことが
多い。ツールの説明（description＝毎リクエストの固定トークン）は増やさずに、結果の側に
「次に使える既存ツールを 1 つ」書いておき、モデルが 2 本目を選べるようにする。

方針（_shared/next_step.py と同じ）:
  - 提案は **実在し今 ON のツール**に限る（受け皿の env gate と段階公開の allowlist を見る）。
  - **1 個だけ**（UX 方針: 次の一手は 1 個）。本文に既に提案（📎 実ファイル送付）が付いて
    いるときは、それと同じ一手にそろえる（2 つ目を足さない）。
  - 提案するだけで実行はしない（実行は利用者の了承後）。``SUGGEST_NEXT_STEP`` で全停止できる。
  - 文面はモデル向け（ツール名を含む）。利用者向けの文にはツール名を出さない旨を添える。
"""

from __future__ import annotations

import re

from teamagent.skills._shared.next_step import suggestions_enabled, tool_enabled
from teamagent.skills._shared.rollout import rollout_allowed

# 文末に添える共通の注意（モデル向け）。
_TAIL = "（実行は利用者の了承後・道具名は利用者に出さない）"

DELIVER_NEXT = "knowledge_deliver: 利用者が「送って」と答えたら、この資料の実ファイルを送る" + _TAIL
SLACK_NEXT = "slack_search: 同じ語で Slack の投稿も探すか、利用者に 1 回だけ聞く" + _TAIL
WEB_NEXT = "web_research: 業界・市場の公開情報を調べるか、利用者に 1 回だけ聞く" + _TAIL
KARTE_NEXT = "clientkarte: {client}の取引の全体像（カルテ）を出すか、利用者に 1 回だけ聞く" + _TAIL
DRAFT_NEXT = (
    "proposal_draft: 見つかった事例を使って提案のたたき台を作るか、利用者に 1 回だけ聞く" + _TAIL
)

# 事例・実績を探す問い（見つかった事例を次の提案へ使う一手が自然な問い）。
_CASE_QUERY_RE = re.compile(r"(事例|実績|成功例|成功した|ケース|施策例|うまくいった)")
_CLIENT_LABEL_CHARS = 30


def suggest_next_move(
    *,
    query: str,
    found: bool,
    slack_status: str | None,
    slack_shown: int,
    delivery_offered: bool,
    query_client: str | None,
    requester: str | None,
) -> str | None:
    """結果に添える次の一手（1 個）。何も勧めないなら None。

    Args:
        found: 金庫に該当があったか（not_found.judge_found）。
        slack_status: 複合検索の Slack の状態（None＝複合検索をしていない）。
        slack_shown: 複合検索で表示した Slack の投稿数。
        delivery_offered: 本文に「📎 実ファイルをお送りしますか？」が付いているか。
        query_client: 利用者が名指しした既知の取引先（自社名は除外済み）。
        requester: 依頼者の email（段階公開の allowlist 判定）。
    """
    if not suggestions_enabled():
        return None
    if delivery_offered:
        # 本文の提案と同じ一手にそろえる（次の一手は 1 個）。
        return DELIVER_NEXT
    if not found:
        if slack_shown:
            return None  # 金庫に無く Slack にある＝答えは Slack から出ている
        if slack_status in (None, "error") and tool_enabled("USE_SLACK_SEARCH_TOOL"):
            return SLACK_NEXT  # Slack をまだ探せていない
        if tool_enabled("USE_WEB_RESEARCH_TOOL") and rollout_allowed(
            "WEB_RESEARCH_ALLOWED_EMAILS", requester
        ):
            return WEB_NEXT  # 社内に無い＝公開情報へ
        return None
    if query_client:
        client = " ".join(query_client.split())[:_CLIENT_LABEL_CHARS]
        return KARTE_NEXT.format(client=client)
    if _CASE_QUERY_RE.search(query or ""):
        return DRAFT_NEXT
    return None


__all__ = [
    "DELIVER_NEXT",
    "DRAFT_NEXT",
    "KARTE_NEXT",
    "SLACK_NEXT",
    "WEB_NEXT",
    "suggest_next_move",
]
