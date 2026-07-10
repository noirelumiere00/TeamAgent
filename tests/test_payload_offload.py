"""payload_offload（v0.3 Task 8）のテスト（外部I/O無し・S3はフェイク）。

検証主眼: 既定OFF・allowlist（PII系tool非対象）・閾値未満は素通し・
構造保持の切り詰め（URL/要約キーの扱い）・S3失敗時のfail-open（原文のまま）・
dispatch_tool 経由の順序（offload→リンク注入）。
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from teamagent.mcp_gateway import payload_offload as po
from teamagent.mcp_gateway.server import SEARCH_TOOL_NAME, USER_CONTEXT_KEY, dispatch_tool
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext


@pytest.fixture(autouse=True)
def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_PAYLOAD_OFFLOAD", "1")
    monkeypatch.setenv("PAYLOAD_OFFLOAD_MAX_CHARS", "1000")
    monkeypatch.setenv("PAYLOAD_OFFLOAD_FIELD_CHARS", "100")


def _fake_publish(monkeypatch: pytest.MonkeyPatch, url: str | None) -> list[str]:
    uploaded: list[str] = []

    def _pub(text: str, **kw: Any) -> str | None:
        uploaded.append(text)
        return url

    import teamagent.adapters.report_publish as rp

    monkeypatch.setattr(rp, "publish_text", _pub)
    return uploaded


def test_flag_off_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_PAYLOAD_OFFLOAD", raising=False)
    big = {"answer": "x" * 5000}
    assert po.maybe_offload("search", big, request_id="r") is big


def test_non_allowlisted_tool_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    # per-user PII 系（mail_summary 等）は巨大でも絶対に退避しない（署名URLはRLSバイパス）。
    _fake_publish(monkeypatch, "https://s3/x")
    big = {"answer": "x" * 5000}
    assert po.maybe_offload("mail_summary", big, request_id="r") is big
    assert "mail_summary" not in po.OFFLOAD_TOOLS and "morning_digest" not in po.OFFLOAD_TOOLS


def test_under_threshold_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_publish(monkeypatch, "https://s3/x")
    small = {"answer": "short"}
    assert po.maybe_offload("search", small, request_id="r") is small


def test_offload_trims_structure_preserving(monkeypatch: pytest.MonkeyPatch) -> None:
    uploaded = _fake_publish(monkeypatch, "https://s3/full")
    data = {
        "answer": "要約" * 300,  # summary キー: 5倍上限（500字）まで
        "hits": [
            {
                "content": "本文" * 300,
                "source_uri": "gdrive://" + "f" * 300,  # URL キーは切らない
                "score": 0.9,
            }
        ],
    }
    out = po.maybe_offload("search", data, request_id="r")
    assert out["offloaded"] is True and out["full_url"] == "https://s3/full"
    assert len(out["hits"]) == 1 and out["hits"][0]["score"] == 0.9  # 構造は保持
    assert out["hits"][0]["content"].endswith("〔省略・全文は offload URL へ〕")
    assert len(out["hits"][0]["content"]) <= 100 + 30
    assert out["hits"][0]["source_uri"] == data["hits"][0]["source_uri"]  # リンク温存
    assert len(out["answer"]) <= 500 + 30  # 要約キーは5倍許容
    # S3 に上がったのは切り詰め前の全文。
    assert json.loads(uploaded[0])["hits"][0]["content"] == "本文" * 300


def test_s3_failure_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_publish(monkeypatch, None)  # 退避失敗
    big = {"answer": "x" * 5000}
    out = po.maybe_offload("search", big, request_id="r")
    assert out is big and "offloaded" not in out  # 切り詰めもしない（引用全損を避ける）


# ── dispatch_tool 経由（ミドルウェア順: offload → リンク注入） ────────────────


class _QIn(BaseModel):
    q: str


class _BigSearchOut(BaseModel):
    answer: str
    hits: list[dict[str, Any]] = []


class _BigSearchSkill(BaseSkill[_QIn, _BigSearchOut]):
    name: ClassVar[str] = SEARCH_TOOL_NAME
    description: ClassVar[str] = "テスト用: 巨大応答を返す search。"
    input_schema: ClassVar[type[BaseModel]] = _QIn
    output_schema: ClassVar[type[BaseModel]] = _BigSearchOut

    def run(self, input: _QIn, ctx: SkillContext) -> _BigSearchOut:
        return _BigSearchOut(answer="A" * 3000, hits=[{"content": "B" * 3000}])


async def test_dispatch_offloads_then_injects_links(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_publish(monkeypatch, "https://s3/full")
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example.co.jp")
    by_name = {SEARCH_TOOL_NAME: ToolSpec(SEARCH_TOOL_NAME, "t", _BigSearchSkill)}
    resp = await dispatch_tool(
        by_name,
        SEARCH_TOOL_NAME,
        {"q": "x", USER_CONTEXT_KEY: {"user_email": "a@b.co"}},
        require_rls=True,
    )
    out = json.loads(resp[0].text)
    assert out["offloaded"] is True and out["full_url"] == "https://s3/full"
    # リンク注入は offload の後＝注入キーは切り詰められず完全な URL のまま（順序契約）。
    assert out["web_url"] == "https://connect.example.co.jp/search"
    assert len(out["web_url"]) < 100  # 切り詰めマークが付いていない
