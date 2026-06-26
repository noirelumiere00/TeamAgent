"""api_search の client/budget フィルタ受け取り・allowlist 拒否・budget 射影テスト。

設計 v2 §2.5/§4 の検証項目:
- filter_client は strip して SearchInput.filter_client に渡る（空文字は None）
- filter_budget / sort_budget_near は 3 バンド allowlist のみ通す（不正値は無視）
- include_unknown_budget が bool で渡る
- hits dict に budget が含まれる

既存 test_search_routes.py の fake skill / build ヘルパーを流用する（実 DB/Bedrock 0）。
"""

from __future__ import annotations

from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchHitOut, SearchInput, SearchOutput
from tests.connect_web.test_search_routes import (
    _auth_cookie,
    _build,
    _FakeSearchSkill,
)


def test_api_search_passes_filter_client() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_client": "日本ガイシ"},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_client == "日本ガイシ"


def test_api_search_blank_filter_client_is_none() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_client": "  "},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_client is None


def test_api_search_accepts_valid_budget_band() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_budget": "500万〜"},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_budget == "500万〜"


def test_api_search_rejects_invalid_budget_band() -> None:
    """allowlist 外（不正 budget 文字列・SQL injection 風含む）は無視され None になる。"""
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_budget": "1000万' OR '1'='1"},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_budget is None


def test_api_search_rejects_unknown_as_filter_budget() -> None:
    """'不明' は allowlist（3 バンド）に無いので filter_budget としては受けない。"""
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_budget": "不明"},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_budget is None


def test_api_search_accepts_valid_sort_budget_near() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "sort_budget_near": "100〜500万"},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].sort_budget_near == "100〜500万"


def test_api_search_rejects_invalid_sort_budget_near() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "sort_budget_near": "bogus"},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].sort_budget_near is None


def test_api_search_passes_include_unknown_budget() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_budget": "〜100万", "include_unknown_budget": True},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].include_unknown_budget is True


class _FakeSkillWithBudget(_FakeSearchSkill):
    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        self.calls.append((input, ctx))
        return SearchOutput(
            answer="要約",
            hits=[
                SearchHitOut(
                    chunk_id=9,
                    content="本文",
                    score=0.9,
                    source_uri="gdrive://9",
                    source_type="gdrive",
                    title="提案",
                    budget="500万〜",
                )
            ],
            total_cost_usd=0.01,
        )


def test_api_search_includes_budget_in_hits() -> None:
    client, _, _ = _build(skill=_FakeSkillWithBudget())
    r = client.post("/api/v1/search", json={"query": "提案"}, cookies=_auth_cookie())
    assert r.status_code == 200
    assert r.json()["hits"][0]["budget"] == "500万〜"
