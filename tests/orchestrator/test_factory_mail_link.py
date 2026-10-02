"""factory 経由（MCP 本番経路）の mail_to_internal_context に社内検索が繋がっていることの回帰テスト.

2026-09-30 の不具合: factory が ``MailToInternalContextSkill(token_store=...)`` だけで組み立て、
``search_skill`` を渡していなかった。skill は search が None だと社内側を ``[]`` で返すため、
Slack（Aico）→ MCP の本番経路では「社内の関連資料」欄が常に空だった。skill 単体テストは
fake search を直接渡すので緑のまま見逃されていた。

ここでは skill を手で組み立てず、``build_production_tools()`` が作る本物の ToolSpec を使う。
差し替えるのは外部 I/O（埋め込み/pgvector を持つ SearchSkill・本人トークン・Gmail API）だけ。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

import teamagent.orchestrator.factory as factory
from teamagent.adapters.gmail_client import GmailClient
from teamagent.adapters.oauth_token_store import InMemoryTokenStore, OAuthToken
from teamagent.mcp_gateway.server import SEARCH_TOOL_NAME, USER_CONTEXT_KEY, dispatch_tool
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import SkillContext
from teamagent.skills.mail_to_internal_context.skill import MailToInternalContextSkill

OWNER = "s-komata@vectorinc.co.jp"
GROUPS = ["vectorinc.co.jp", "sales"]
# メール側にだけ置く文字列。社内検索クエリへ漏れたら G6 違反。
MAIL_ONLY_TEXT = "件名だけに書いた極秘キャンペーン名"


@dataclass
class _Hit:
    content: str
    score: float
    metadata: dict[str, Any]


class _SearchOut(BaseModel):
    answer: str


@dataclass
class _RecordingSearch:
    """本物の SearchSkill の代わり（retrieve_hits / run の引数と ctx を記録する）。"""

    hits: list[_Hit]
    queries: list[str] = field(default_factory=list)
    retrieve_ctxs: list[SkillContext] = field(default_factory=list)
    run_ctxs: list[SkillContext] = field(default_factory=list)

    def retrieve_hits(
        self, query: str, ctx: SkillContext, *, top_k: int = 5, **kw: Any
    ) -> list[_Hit]:
        self.queries.append(query)
        self.retrieve_ctxs.append(ctx)
        return self.hits[:top_k]

    def run(self, input: Any, ctx: SkillContext) -> _SearchOut:
        self.run_ctxs.append(ctx)
        return _SearchOut(answer="ok")

    def cleanup_output(self, output: Any) -> None:
        return None


@dataclass
class _Ref:
    id: str
    thread_id: str = "t"


@dataclass
class _Msg:
    headers: dict[str, str]
    internal_date_ms: int | None = 1_700_000_000_000
    id: str = "m"
    thread_id: str = "t"


class _FakeGmail:
    def __init__(self) -> None:
        self.msgs = [
            _Msg(headers={"From": "担当 <tantou@mori.co.jp>", "Subject": MAIL_ONLY_TEXT}),
        ]

    def list_messages(
        self, query: str | None, request_id: str, *, max_results: int = 50, **kw: Any
    ) -> tuple[list[_Ref], None]:
        return ([_Ref(id=f"m{i}") for i in range(len(self.msgs))], None)

    def get_message(
        self, msg_id: str, request_id: str, *, format: str = "full", user_id: str = "me"
    ) -> _Msg:
        assert format == "metadata"  # G3/G6: 本文 payload は取らない
        return self.msgs[int(msg_id[1:])]


def _hits() -> list[_Hit]:
    return [
        _Hit(
            content="森ビルの件、与件は来週まとめる",
            score=0.81,
            metadata={
                "source_type": "slack",
                "source_uri": "slack://C091/1748244936.050099",
                "channel_name": "#案件_森ビル",
            },
        ),
        _Hit(
            content="森ビル向け提案 v2",
            score=0.74,
            metadata={
                "source_type": "drive",
                "drive_url": "https://drive.google.com/file/d/abc",
                "file_name": "森ビル提案v2.pptx",
            },
        ),
    ]


@pytest.fixture
def search(monkeypatch: pytest.MonkeyPatch) -> _RecordingSearch:
    """重い SearchSkill を記録用の偽物に差し替え、USE_MAIL_LINK_TOOL=1 で factory を組める状態にする。"""
    fake = _RecordingSearch(hits=_hits())
    monkeypatch.setattr(factory, "_build_search_skill", lambda: fake)
    monkeypatch.setenv("USE_MAIL_LINK_TOOL", "1")
    monkeypatch.delenv("OAUTH_KMS_KEY_ID", raising=False)
    return fake


def _specs_by_name() -> dict[str, ToolSpec]:
    return {s.name: s for s in factory.build_production_tools()}


def test_factory_mail_link_tool_receives_shared_search(search: _RecordingSearch) -> None:
    by_name = _specs_by_name()
    skill = by_name[MailToInternalContextSkill.name].instantiate()
    assert isinstance(skill, MailToInternalContextSkill)
    # search tool と同じ 1 インスタンス（embedder の二重ロードをしない・同じ検索設定）。
    assert skill._search_skill is search
    assert by_name[SEARCH_TOOL_NAME].instantiate() is search


def test_mcp_dispatch_returns_internal_refs_with_same_rls_context(
    search: _RecordingSearch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLACK_WORKSPACE_DOMAIN", raising=False)
    monkeypatch.setenv("SLACK_WORKSPACE", "vectorinc")  # 本番 mcp タスクと同じく設定済み
    store = InMemoryTokenStore({OWNER: OAuthToken(refresh_token="rt-test")})
    monkeypatch.setattr(factory, "_build_token_store", lambda: store)
    gmail = _FakeGmail()
    readonly_flags: list[bool] = []

    def _from_user_token(cls: type, token: OAuthToken, *, readonly: bool = True) -> Any:
        assert token is store.get(OWNER)  # G1: 本人のトークンだけを使う
        readonly_flags.append(readonly)
        return gmail

    monkeypatch.setattr(GmailClient, "from_user_token", classmethod(_from_user_token))

    by_name = _specs_by_name()
    user_context = {"user_email": OWNER, "user_groups": GROUPS}

    out = json.loads(
        asyncio.run(
            dispatch_tool(
                by_name,
                MailToInternalContextSkill.name,
                {
                    "client_name": "森ビル",
                    "topic_hint": "与件",
                    USER_CONTEXT_KEY: user_context,
                },
                require_rls=True,
            )
        )[0].text
    )

    assert "error" not in out, out
    # 修正前はここが [] だった（search 未注入）。
    titles = [r["title"] for r in out["internal_refs"]]
    assert titles == ["#案件_森ビル", "森ビル提案v2.pptx"]
    # Aico に渡るのは開けるリンク（slack:// の内部識別子を URL として使わせない）。
    assert [r["url"] for r in out["internal_refs"]] == [
        "https://vectorinc.slack.com/archives/C091/p1748244936050099",
        "https://drive.google.com/file/d/abc",
    ]
    assert out["mail_signal"]["recent_count"] == 1
    assert readonly_flags == [True]

    # G6: 社内検索へ渡るのは client_name + topic_hint だけ（メールの件名・本文は渡らない）。
    assert search.queries == ["森ビル 与件"]
    assert all(MAIL_ONLY_TEXT not in q for q in search.queries)

    # search tool を同じ _user_context で呼んだときと同じ RLS 文脈で社内検索している。
    asyncio.run(
        dispatch_tool(
            by_name,
            SEARCH_TOOL_NAME,
            {"query": "森ビル 与件", USER_CONTEXT_KEY: user_context},
            require_rls=True,
        )
    )
    (mail_ctx,) = search.retrieve_ctxs
    (search_ctx,) = search.run_ctxs
    for key in ("user_email", "user_groups", "user_role"):
        assert mail_ctx.metadata.get(key) == search_ctx.metadata.get(key), key
    assert mail_ctx.metadata["user_email"] == OWNER
    assert set(mail_ctx.metadata["user_groups"]) >= set(GROUPS)
