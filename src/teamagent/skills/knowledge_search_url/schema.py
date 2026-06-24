"""knowledge_search_url Skill の I/O スキーマ（Pydantic v2）。

社内資料検索 Web UI（connect-web の ``/search`` ・ ``/search/graph``）の URL を返す。
データ I/O は無く、``CONNECT_BASE_URL`` から URL を組み立てて案内文と一緒に返すだけ。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class KnowledgeSearchUrlInput(BaseModel):
    """knowledge_search_url の入力。

    対象は常に「呼び出した本人」（配信は Slack 側が解決）で、検索語などの意味的引数は
    持たない。OpenClaw からは引数なしで呼べる（oauth_connect と同流儀）。
    """


class KnowledgeSearchUrlOutput(BaseModel):
    """knowledge_search_url の出力。検索 UI / グラフ UI の URL と案内文を返す。

    Web UI 未デプロイ（``CONNECT_BASE_URL`` 未設定）のときは ``available=False`` ＋
    URL 群は None ＝壊れた相対リンクは一切返さない（後方互換・fail-safe）。
    """

    available: bool = Field(description="Web UI が利用可能か（CONNECT_BASE_URL 設定済みなら True）")
    web_url: str | None = Field(
        default=None, description="社内資料をブラウザで検索するページの URL（未デプロイ時は None）"
    )
    graph_url: str | None = Field(
        default=None, description="検索結果のグラフ閲覧ページの URL（未デプロイ時は None）"
    )
    message: str = Field(description="Slack にそのまま出せる案内文（URL を含む）")
