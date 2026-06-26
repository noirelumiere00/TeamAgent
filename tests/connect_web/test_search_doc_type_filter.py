"""api_search の filter_doc_type / filter_solution 転送・allowlist 拒否・gdrive URL 整形。

仕様 §A/§C の検証項目（実 DB/Bedrock 0・既存 fake skill / build ヘルパー流用）:
- filter_doc_type は _DOC_TYPES allowlist のみ SearchInput に渡る（不正値は None）
- filter_solution は strip-or-None で渡る（長すぎは 50 文字に切り詰め）
- 未指定なら従来どおり None（後方互換）
- source_type=='gdrive' の hit は https://drive.google.com/file/d/<id>/view へ整形して返す
- 非 gdrive / file_id 抽出不能なら従来 source_uri に fail-open
- connect_web._DOC_TYPES は ingest.classify._DOC_TYPES と同値（drift 防止）
"""

from __future__ import annotations

from teamagent.connect_web.app import _DOC_TYPES as _WEB_DOC_TYPES
from teamagent.ingest.classify import _DOC_TYPES as _CLASSIFY_DOC_TYPES
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchHitOut, SearchInput, SearchOutput
from tests.connect_web.test_search_routes import (
    _auth_cookie,
    _build,
    _FakeSearchSkill,
)


def test_doc_types_allowlist_matches_classify() -> None:
    """UI option / allowlist の drift 防止: connect_web の literal は classify と同値。"""
    assert _WEB_DOC_TYPES == _CLASSIFY_DOC_TYPES


# --- A: filter_doc_type / filter_solution 転送 ----------------------------------------


def test_api_search_passes_valid_doc_type() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_doc_type": "提案書"},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_doc_type == "提案書"


def test_api_search_rejects_invalid_doc_type() -> None:
    """allowlist 外（自由文字列・injection 風含む）は無視され None になる。"""
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_doc_type": "資料' OR '1'='1"},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_doc_type is None


def test_api_search_blank_doc_type_is_none() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_doc_type": "  "},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_doc_type is None


def test_api_search_passes_filter_solution() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_solution": "動画広告"},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_solution == "動画広告"


def test_api_search_blank_solution_is_none() -> None:
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_solution": "   "},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_solution is None


def test_api_search_truncates_long_solution() -> None:
    """SearchInput.filter_solution は max_length=50。境界手前で切り詰めて 422 にしない。"""
    client, sk, _ = _build()
    r = client.post(
        "/api/v1/search",
        json={"query": "提案", "filter_solution": "あ" * 80},
        cookies=_auth_cookie(),
    )
    assert r.status_code == 200
    assert sk.calls[0][0].filter_solution == "あ" * 50


def test_api_search_no_doc_type_solution_defaults_none() -> None:
    """未指定なら従来どおり None（後方互換）。"""
    client, sk, _ = _build()
    r = client.post("/api/v1/search", json={"query": "提案"}, cookies=_auth_cookie())
    assert r.status_code == 200
    assert sk.calls[0][0].filter_doc_type is None
    assert sk.calls[0][0].filter_solution is None


# --- C: gdrive:// → 実ブラウザで開けるリンク整形 --------------------------------------


class _FakeSkillGdrive(_FakeSearchSkill):
    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        self.calls.append((input, ctx))
        return SearchOutput(
            answer="要約",
            hits=[
                SearchHitOut(
                    chunk_id=9,
                    content="本文",
                    score=0.9,
                    source_uri="gdrive://FILE_9",
                    source_type="gdrive",
                    title="提案",
                )
            ],
            total_cost_usd=0.01,
        )


def test_api_search_rewrites_gdrive_uri_to_view_link() -> None:
    client, _, _ = _build(skill=_FakeSkillGdrive())
    r = client.post("/api/v1/search", json={"query": "提案"}, cookies=_auth_cookie())
    assert r.status_code == 200
    hit = r.json()["hits"][0]
    assert hit["source_uri"] == "https://drive.google.com/file/d/FILE_9/view"
    # doc_id（FB 識別子）は元の gdrive:// のまま保つ。
    assert hit["doc_id"] == "gdrive://FILE_9"


class _FakeSkillHttpsSource(_FakeSearchSkill):
    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        self.calls.append((input, ctx))
        return SearchOutput(
            answer="要約",
            hits=[
                SearchHitOut(
                    chunk_id=1,
                    content="本文",
                    score=0.9,
                    source_uri="https://example.com/doc",
                    source_type="web",
                    title="記事",
                )
            ],
            total_cost_usd=0.01,
        )


def test_api_search_non_gdrive_uri_unchanged() -> None:
    """非 gdrive は従来どおり source_uri をそのまま返す（後方互換）。"""
    client, _, _ = _build(skill=_FakeSkillHttpsSource())
    r = client.post("/api/v1/search", json={"query": "提案"}, cookies=_auth_cookie())
    assert r.status_code == 200
    assert r.json()["hits"][0]["source_uri"] == "https://example.com/doc"


class _FakeSkillGdriveNoId(_FakeSearchSkill):
    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        self.calls.append((input, ctx))
        return SearchOutput(
            answer="要約",
            hits=[
                SearchHitOut(
                    chunk_id=2,
                    content="本文",
                    score=0.9,
                    source_uri="gdrive://",  # file_id 抽出不能
                    source_type="gdrive",
                    title="壊れた",
                )
            ],
            total_cost_usd=0.01,
        )


def test_api_search_gdrive_without_id_fail_open() -> None:
    """file_id を取れない gdrive:// は従来 source_uri に fail-open（例外を出さない）。"""
    client, _, _ = _build(skill=_FakeSkillGdriveNoId())
    r = client.post("/api/v1/search", json={"query": "提案"}, cookies=_auth_cookie())
    assert r.status_code == 200
    assert r.json()["hits"][0]["source_uri"] == "gdrive://"
