"""knowledge_search_url Skill 本体。

ユーザーが「検索ページ教えて」「ブラウザで資料探したい」「検索 UI の URL」等と言ったとき、
社内資料検索 Web UI（connect-web）の URL を返す。OAuth 連携リンク（oauth_connect）と同じく
URL を生成して案内文と一緒に返すだけで、データ I/O は一切しない。

URL は ``CONNECT_BASE_URL`` から組み立てる:
  - 検索ページ:   ``{base}/search``
  - グラフ閲覧:   ``{base}/search/graph``
  - Aico Vault:   ``{base}/app``（＋ ``#client:<名前>`` ディープリンク）
``CONNECT_BASE_URL`` 未設定（Web UI 未デプロイ）のときは、壊れた相対リンクを返さず
「まだ公開されていない」旨を返す（fail-safe・後方互換）。

URL 組み立てロジック（``build_search_web_links`` / ``build_app_client_link``）は
MCP 境界（mcp_gateway）からも再利用し、search ツールの応答に web_url/graph_url/app_url を
載せるときと同一の真実源にする。

ディープリンクのフラグメント規約（app.html 側 JS と対）:
  - ``#client:<percent-encoded クライアント名>`` … 該当クライアントのノートを開く。
    名前解決は app.html 側の ``navTarget()``（stem 直一致 → ``nrm()`` 正規化一致の二段）が担う
    ため、ここでは **生のクライアント名を percent-encode するだけ**でよい（stem 化しない）。
  - フラグメントはサーバに送信されず、ログイン境界（form POST）では消えるため、
    login ページが sessionStorage に退避し app.html が復元する（connect_web/app.py 参照）。
"""

from __future__ import annotations

import os
from typing import ClassVar
from urllib.parse import quote

import structlog
from pydantic import BaseModel

from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.knowledge_search_url.schema import (
    KnowledgeSearchUrlInput,
    KnowledgeSearchUrlOutput,
)

logger = structlog.get_logger(__name__)

# 案内文（Web UI が使えるとき）。本人の Google ログインが要る点を必ず添える。
_INSTRUCTION = "社内ナレッジをブラウザで検索・グラフ閲覧できます。Googleログインが必要です。"
# 案内文（Web UI 未デプロイ）。壊れたリンクは出さず、未公開である旨を明示する。
_NOT_DEPLOYED = (
    "社内ナレッジ検索の Web UI はまだ公開されていません（管理者へ: CONNECT_BASE_URL 未設定）。"
    "Slack で「○○の資料」と話しかければ、その場で検索してお答えします。"
)


def connect_base_url() -> str:
    """``CONNECT_BASE_URL`` を前後空白・末尾スラッシュ無しで返す（未設定/空白のみは ""）。"""
    return os.environ.get("CONNECT_BASE_URL", "").strip().rstrip("/")


def build_search_web_links(base: str | None = None) -> dict[str, str]:
    """検索 UI / グラフ UI の URL 辞書を返す。

    base 未指定なら env から解決する。``CONNECT_BASE_URL`` が未設定/空白のみなら **空 dict**
    を返す（壊れた相対リンク ``/search`` を絶対に出さない＝呼び出し側はキー有無で分岐できる）。
    """
    resolved = connect_base_url() if base is None else base.strip().rstrip("/")
    if not resolved:
        return {}
    return {"web_url": f"{resolved}/search", "graph_url": f"{resolved}/search/graph"}


def build_app_url(base: str | None = None) -> str:
    """Aico Vault（Obsidian 風ナレッジ UI ``/app``）の URL。base 未解決なら ""。"""
    resolved = connect_base_url() if base is None else base.strip().rstrip("/")
    if not resolved:
        return ""
    return f"{resolved}/app"


def build_app_client_link(client_name: str, base: str | None = None) -> str:
    """クライアントノートへのディープリンク ``{base}/app#client:<name>`` を返す。

    - クライアント名は**生の名前のまま** percent-encode する（``#`` フラグメント内で
      Slack の自動リンク化が日本語で切れないように quote(safe="")）。stem 化・正規化は
      しない＝解決は app.html 側 navTarget()（stem 直一致→nrm() 正規化一致）に委譲する。
    - base 未解決 or 名前が空白のみなら ""（壊れたリンクを出さない・呼び出し側は真偽で分岐）。
    """
    app = build_app_url(base)
    name = (client_name or "").strip()
    if not app or not name:
        return ""
    return f"{app}#client:{quote(name, safe='')}"


@register
class KnowledgeSearchUrlSkill(BaseSkill[KnowledgeSearchUrlInput, KnowledgeSearchUrlOutput]):
    """社内ナレッジ検索 Web UI の URL を返す Skill（URL のみ・データ I/O なし）。"""

    name: ClassVar[str] = "knowledge_search_url"
    description: ClassVar[str] = (
        "社内ナレッジをブラウザで検索・グラフ閲覧できる Web UI の URL を返す。"
        "『検索ページ教えて』『ブラウザでナレッジ探したい』『検索画面のURL』"
        "『社内ナレッジ検索のリンク』『資料検索のページ』等を言われたら呼ぶ。"
        "引数は不要で、対象は常に話しかけている本人。"
        "返した URL を本人に提示すること（Google ログインが必要）。"
    )
    input_schema: ClassVar[type[BaseModel]] = KnowledgeSearchUrlInput
    output_schema: ClassVar[type[BaseModel]] = KnowledgeSearchUrlOutput

    def run(self, _input: KnowledgeSearchUrlInput, ctx: SkillContext) -> KnowledgeSearchUrlOutput:
        log = ctx.bind_logger(self.name)
        links = build_search_web_links()

        if not links:
            # Web UI 未デプロイ。壊れたリンクは出さず、未公開である旨を返す。
            log.info("knowledge_search_url_unavailable", reason="no_connect_base_url")
            return KnowledgeSearchUrlOutput(
                available=False, web_url=None, graph_url=None, message=_NOT_DEPLOYED
            )

        web_url = links["web_url"]
        graph_url = links["graph_url"]
        message = (
            "🔎 *社内ナレッジの検索ページ* です（Google ログインが必要）。\n"
            f"・ブラウザで検索: {web_url}\n"
            f"・グラフで閲覧: {graph_url}\n"
            f"{_INSTRUCTION}"
        )
        log.info("knowledge_search_url_issued")
        return KnowledgeSearchUrlOutput(
            available=True, web_url=web_url, graph_url=graph_url, message=message
        )
