"""施策実績のヒットに同じ施策の Drive 資料と広告主の業種を添えるテスト（campaign_files）。

再現する本番の実例（10-06 14:12・小俣さんの DM。社名・施策名は架空に置き換え）:
  「飲料メーカーの施策事例」→ Aico は施策実績（ショート動画DBのシート行）だけを返し、
  「資料は？」に「実ファイルが Drive に紐づいていない」と答えた。実際には同じ施策の
  Drive レポートが金庫にあった。また飲料ではない広告主の施策が混ざった。

フェイクの金庫は本番の失敗モードを再現する:
  - 施策実績は campaign_aggregate='true'・advertiser・campaign・題名「施策実績 <広告主> <施策名>」
  - Drive の題名は広告主名に「株式会社」「様」が付き、施策名も表記が揺れる
    （「アサイースムージー」↔「アサイーバナナスムージー」）
  - RLS: 本人に見えない Drive 資料は adapter が返さない（接続の user_email で絞る）
実 DB 0・実 Bedrock 0。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import (
    ConverseResponse,
    RerankResponse,
    RerankResult,
    TokenUsage,
)
from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search.campaign_files import (
    SHEET_ONLY_NOTE,
    advertiser_pattern,
    campaign_key,
    campaign_similarity,
    file_kind,
    pick_related_files,
)
from teamagent.skills.search.not_found import NOT_FOUND_HEAD
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill

ME = "me@vectorinc.co.jp"
OTHER = "other@vectorinc.co.jp"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SEARCH_CAMPAIGN_FILES",
        "SEARCH_NOT_FOUND_ANSWER",
        "USE_COMPOSITE_SEARCH",
        "SEARCH_ANSWER_SOURCE_LINKS",
        "USE_KNOWLEDGE_DELIVER",
    ):
        monkeypatch.delenv(name, raising=False)


# ── フェイク（施策実績 4 件・Drive 資料・RLS）───────────────────────────────


def _campaign(chunk_id: int, advertiser: str, campaign: str) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        content=f"施策実績: {advertiser} / {campaign}\n投稿本数: 12 本。総再生数 340,000。",
        score=0.9,
        metadata={
            "source_type": "gsheets",
            "title": f"施策実績 {advertiser} {campaign}",
            "campaign_aggregate": "true",
            "advertiser": advertiser,
            "campaign": campaign,
            "cls_project": advertiser,
            "cls_doc_type": "施策実績",
        },
    )


CAMPAIGNS = [
    # 飲料・Drive レポートあり（題名に「株式会社」「御中」）
    _campaign(1, "株式会社アオバ製薬", "うるおいシリカ水"),
    # 飲料・Drive レポートあり（施策名の表記ゆれ・「様」付き）
    _campaign(2, "株式会社ミドリマート", "アサイースムージー"),
    # 業種違い（日用品）・同じ施策の資料は無い
    _campaign(3, "ヒノデ化学株式会社", "あまみシロップ"),
    # 資料ゼロ（業種不明・数字はシートのみ）
    _campaign(4, "ハルカ茶園", "むぎ茶ボトル"),
]

# Drive 文書（title / source_uri / 業種 / 資料種別 / 見える人）。
DRIVE_DOCS: list[dict[str, Any]] = [
    {
        "title": "レポート_アオバ製薬_うるおいシリカ水_【アオバ製薬御中】うるおいシリカ水_拡散施策レポート.pptx",
        "source_uri": "gdrive://AOBA_REPORT",
        "cls_industry": "飲料",
        "cls_project": "アオバ製薬",
        "cls_doc_type": "報告書",
        "updated_at": "2026-08-01",
        "acl": {ME},
    },
    {
        "title": "アオバ製薬_虫よけスプレー_会社紹介.pdf",
        "source_uri": "gdrive://AOBA_OTHER",
        "cls_industry": "日用品",
        "cls_project": "アオバ製薬",
        "cls_doc_type": "提案書",
        "updated_at": "2026-09-01",
        "acl": {ME},
    },
    {
        "title": "レポート_株式会社ミドリマート様_アサイーバナナスムージーのレポート_202509.pptx",
        "source_uri": "gdrive://MIDORI_REPORT",
        "cls_industry": "飲料",
        "cls_project": "ミドリマート",
        "cls_doc_type": "報告書",
        "updated_at": "2026-09-20",
        "acl": {ME},
    },
    {
        "title": "ミドリマート様_アサイーバナナスムージー_提案書.pdf",
        "source_uri": "gdrive://MIDORI_PROPOSAL",
        "cls_industry": "飲料",
        "cls_project": "ミドリマート",
        "cls_doc_type": "提案書",
        "updated_at": "2026-07-01",
        "acl": {ME},
    },
    {
        "title": "ミドリマート様_アサイーバナナスムージー_素材一式.zip",
        "source_uri": "gdrive://MIDORI_ASSETS",
        "cls_industry": "飲料",
        "cls_project": "ミドリマート",
        "cls_doc_type": None,
        "updated_at": "2026-09-25",
        "acl": {ME},
    },
    {
        "title": "ヒノデ化学_手指消毒ジェル_提案書.pptx",
        "source_uri": "gdrive://HINODE_PROPOSAL",
        "cls_industry": "日用品",
        "cls_project": "ヒノデ化学",
        "cls_doc_type": "提案書",
        "updated_at": "2026-06-01",
        "acl": {ME},
    },
]


class _Pg:
    """search_similar_new_schema は施策実績を返し、Drive 候補は RLS（接続の email）で絞る。"""

    def __init__(self, hits: list[SearchHit], docs: list[dict[str, Any]]) -> None:
        self._hits = hits
        self._docs = docs
        self.drive_calls: list[list[str]] = []
        self.connections: list[dict[str, Any]] = []
        self._current_email: str | None = None

    def connection(self, **kw: Any) -> Any:
        self.connections.append(kw)
        self._current_email = kw.get("user_email")
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock())
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    def search_similar_new_schema(self, **_: Any) -> list[SearchHit]:
        return [SearchHit(h.chunk_id, h.content, h.score, dict(h.metadata)) for h in self._hits]

    def search_drive_by_client_names(self, **_: Any) -> list[SearchHit]:
        return []

    def list_client_names(self, **_: Any) -> list[str]:
        return []

    def resolve_file_urls_by_titles(self, *_: Any, **__: Any) -> dict[str, str]:
        return {}

    def find_drive_files_for_advertisers(
        self, conn: Any, advertisers: list[str], *, per_advertiser: int = 30, request_id: Any = None
    ) -> list[dict[str, Any]]:
        self.drive_calls.append(list(advertisers))
        rows: list[dict[str, Any]] = []
        for adv in advertisers:
            for doc in self._docs:
                if self._current_email not in doc["acl"]:
                    continue  # RLS: 見えない資料は返らない
                if adv in doc["title"] or adv in str(doc.get("cls_project") or ""):
                    rows.append({**{k: v for k, v in doc.items() if k != "acl"}, "advertiser": adv})
        return rows


def _converse(text: str) -> ConverseResponse:
    return ConverseResponse(
        text=text,
        usage=TokenUsage(
            input_tokens=1,
            output_tokens=1,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.001,
        ),
        model_id="m",
        latency_ms=1,
        stop_reason="end_turn",
    )


class _Embedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 8


def _skill(
    pg: _Pg, answer: str = "飲料の施策はアオバ製薬のうるおいシリカ水が伸びた。"
) -> SearchSkill:
    bedrock = MagicMock()
    bedrock.converse.return_value = _converse(answer)
    bedrock.rerank.side_effect = lambda **kw: RerankResponse(
        results=[RerankResult(index=i, relevance_score=0.8) for i in range(len(kw["documents"]))],
        model_arn="arn",
        latency_ms=1,
        query_count=1,
    )
    return SearchSkill(
        bedrock=bedrock,
        pgvector=pg,  # type: ignore[arg-type]
        embedder=_Embedder(),
        use_new_schema=True,
        use_cohere_rerank=True,
        drive_pool_floor=0,
        campaign_pool_floor=0,
        deal_pool_floor=0,
        min_relevance=0.4,
        min_relevance_fallback=0.05,
    )


def _message(skill: SearchSkill) -> str:
    bedrock: Any = skill._bedrock
    return str(bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"])


def _run(skill: SearchSkill, query: str = "飲料メーカーの施策事例", email: str = ME) -> Any:
    return skill.run(
        SearchInput(query=query),
        SkillContext(metadata={"user_email": email, "user_role": "member"}),
    )


def _hit(out: Any, chunk_id: int) -> Any:
    return next(h for h in out.hits if h.chunk_id == chunk_id)


# ── 結合（関連ファイル・業種・RLS・SQL 1 回）──────────────────────────────


def test_related_files_and_industry_are_attached_with_one_query() -> None:
    pg = _Pg(CAMPAIGNS, DRIVE_DOCS)
    out = _run(_skill(pg))

    assert len(pg.drive_calls) == 1  # ヒットごとに N 回投げない
    assert pg.drive_calls[0] == ["アオバ製薬", "ミドリマート", "ヒノデ化学", "ハルカ茶園"]

    aoba = _hit(out, 1)
    assert [f.title for f in aoba.related_files] == [DRIVE_DOCS[0]["title"]]
    assert aoba.related_files[0].kind == "レポート"
    assert aoba.related_files[0].url == "https://drive.google.com/file/d/AOBA_REPORT/view"
    # 業種は同じ施策の資料の最頻値（広告主の別資料＝日用品より優先）。
    assert aoba.industry == "飲料"

    midori = _hit(out, 2)
    # 表記ゆれ（アサイー「バナナ」スムージー）でも同じ施策。レポート・提案書を優先し 2 件まで。
    assert [f.kind for f in midori.related_files] == ["レポート", "提案書"]
    assert "素材一式" not in " ".join(f.title for f in midori.related_files)
    assert midori.industry == "飲料"

    hinode = _hit(out, 3)
    assert hinode.related_files == []  # 同じ施策の資料は無い（別商材の提案書は添えない）
    assert hinode.industry == "日用品"  # 広告主の資料全体の最頻値

    haruka = _hit(out, 4)
    assert haruka.related_files == [] and haruka.industry is None

    dumped = out.model_dump()
    assert "source_uri" not in dumped["hits"][0]["related_files"][0]  # 内部識別子は出さない


def test_answer_is_scoped_to_the_asked_industry() -> None:
    pg = _Pg(CAMPAIGNS, DRIVE_DOCS)
    skill = _skill(pg)
    _run(skill)
    msg = _message(skill)
    assert "# 問いの業種\n飲料" in msg
    assert "うるおいシリカ水" in msg and "アサイースムージー" in msg
    assert "あまみシロップ" not in msg  # 業種違い（日用品）は要約に渡さない
    # 業種不明は「業種不明」と明示して最後に回す。
    assert msg.index("むぎ茶ボトル") > msg.index("アサイースムージー")
    assert (
        "業種: 業種不明, 関連ファイル: なし（施策の数字はシート（ショート動画データベース）のみ）"
        in msg
    )
    assert "業種: 飲料, 関連ファイル: 『レポート_アオバ製薬" in msg
    assert "https://drive.google.com/file/d/AOBA_REPORT/view（レポート）" in msg
    assert "施策実績の書き方（厳守）" in msg


def test_answer_gets_links_and_sheet_only_when_summary_drops_them() -> None:
    pg = _Pg(CAMPAIGNS, DRIVE_DOCS)
    out = _run(_skill(pg, answer="飲料の施策は 2 件。再生数はアオバ製薬が 34 万回。"))
    assert "📎 施策のレポート・提案書" in out.answer
    assert "https://drive.google.com/file/d/AOBA_REPORT/view" in out.answer
    assert "https://drive.google.com/file/d/MIDORI_REPORT/view" in out.answer
    assert f"{SHEET_ONLY_NOTE}: ハルカ茶園 むぎ茶ボトル" in out.answer
    assert "ヒノデ化学" not in out.answer  # 業種違いはリンクも足さない


def test_no_footer_when_summary_already_cites_links() -> None:
    text = (
        "アオバ製薬 34 万回（『拡散施策レポート』 https://drive.google.com/file/d/AOBA_REPORT/view）。"
        "ミドリマート（https://drive.google.com/file/d/MIDORI_REPORT/view）。"
        f"ハルカ茶園は{SHEET_ONLY_NOTE}。"
    )
    out = _run(_skill(_Pg(CAMPAIGNS, DRIVE_DOCS), answer=text))
    assert out.answer == text


def test_files_invisible_under_rls_are_not_attached() -> None:
    docs = [dict(d) for d in DRIVE_DOCS]
    for d in docs:
        if d["source_uri"].startswith("gdrive://MIDORI"):
            d["acl"] = {OTHER}  # 本人には見えない
    pg = _Pg(CAMPAIGNS, docs)
    out = _run(_skill(pg))
    assert (
        pg.connections[-1]["user_email"] == ME and pg.connections[-1]["app_role"] == "teamagent_app"
    )
    midori = _hit(out, 2)
    assert midori.related_files == []
    assert "MIDORI" not in str(out.model_dump())
    assert f"{SHEET_ONLY_NOTE}: 株式会社ミドリマート アサイースムージー" in out.answer


def test_only_mismatched_industry_is_not_found() -> None:
    pg = _Pg([CAMPAIGNS[2]], DRIVE_DOCS)
    skill = _skill(pg)
    out = _run(skill)
    assert out.found is False
    assert out.answer.startswith(NOT_FOUND_HEAD)
    skill._bedrock.converse.assert_not_called()  # type: ignore[attr-defined]


def test_without_industry_word_nothing_is_dropped() -> None:
    pg = _Pg(CAMPAIGNS, DRIVE_DOCS)
    skill = _skill(pg)
    _run(skill, query="ショート動画の施策事例")
    msg = _message(skill)
    assert "あまみシロップ" in msg and "# 問いの業種\n" not in msg


def test_kill_switch_restores_previous_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_CAMPAIGN_FILES", "false")
    pg = _Pg(CAMPAIGNS, DRIVE_DOCS)
    skill = _skill(pg)
    out = _run(skill)
    assert pg.drive_calls == []
    assert all("related_files" not in h for h in out.model_dump()["hits"])
    msg = _message(skill)
    assert "あまみシロップ" in msg and "関連ファイル" not in msg
    assert "📎 施策" not in out.answer


def test_non_campaign_search_adds_no_query() -> None:
    hit = SearchHit(1, "提案書の本文", 0.9, {"source_type": "gdrive", "title": "提案書.pptx"})
    pg = _Pg([hit], DRIVE_DOCS)
    out = _run(_skill(pg))
    assert pg.drive_calls == []
    assert "related_files" not in out.model_dump()["hits"][0]


def test_lookup_failure_is_fail_open_and_claims_nothing() -> None:
    pg = _Pg(CAMPAIGNS, DRIVE_DOCS)

    def _boom(*_: Any, **__: Any) -> list[dict[str, Any]]:
        raise RuntimeError("db down")

    pg.find_drive_files_for_advertisers = _boom  # type: ignore[method-assign]
    skill = _skill(pg)
    out = _run(skill)
    assert out.found is True
    assert all("related_files" not in h for h in out.model_dump()["hits"])
    assert SHEET_ONLY_NOTE not in out.answer  # 照会できていないのに「無い」とは言わない


# ── 純関数 ────────────────────────────────────────────────────────────────


def test_campaign_similarity() -> None:
    assert campaign_similarity("うるおいシリカ水", DRIVE_DOCS[0]["title"]) == 1.0
    assert campaign_similarity("アサイースムージー", DRIVE_DOCS[2]["title"]) >= 0.85
    assert campaign_similarity("あまみシロップ", "ヒノデ化学_手指消毒ジェル_提案書.pptx") < 0.3
    assert campaign_similarity("", "x") == 0.0


def test_file_kind_and_priority() -> None:
    assert file_kind("〜_拡散施策レポート.pptx", None) == "レポート"
    assert file_kind("〜実施報告.pdf", None) == "レポート"
    assert file_kind("〜_提案書.pdf", None) == "提案書"
    assert file_kind("〜.pdf", "報告書") == "レポート"
    assert file_kind("素材.zip", None) == "資料"
    rows = [
        {"title": "むぎ茶ボトル_素材.zip", "source_uri": "gdrive://A", "updated_at": "2026-09-30"},
        {
            "title": "むぎ茶ボトル_提案書.pdf",
            "source_uri": "gdrive://B",
            "updated_at": "2026-01-01",
        },
        {
            "title": "むぎ茶ボトル_レポート.pdf",
            "source_uri": "gdrive://C",
            "updated_at": "2026-02-01",
        },
    ]
    picked = pick_related_files("むぎ茶ボトル", rows)
    assert [p["source_uri"] for p in picked] == ["gdrive://C", "gdrive://B"]


def test_long_titles_are_capped_for_the_payload_budget() -> None:
    rows = [{"title": "むぎ茶ボトル_レポート_" + "長" * 100, "source_uri": "gdrive://Z"}]
    picked = pick_related_files("むぎ茶ボトル", rows)
    assert len(picked[0]["title"]) == 61 and picked[0]["title"].endswith("…")


def test_campaign_key_falls_back_to_title() -> None:
    hit = SearchHit(
        1,
        "",
        0.9,
        {
            "campaign_aggregate": "true",
            "cls_project": "ハルカ茶園",
            "title": "施策実績 ハルカ茶園 むぎ茶ボトル",
        },
    )
    assert campaign_key(hit) == ("ハルカ茶園", "むぎ茶ボトル")


def test_advertiser_pattern_uses_client_normalization() -> None:
    assert advertiser_pattern("株式会社アオバ製薬") == "アオバ製薬"
    assert advertiser_pattern("ミドリマート様") == "ミドリマート"


# ── adapter（SQL は 1 回・LIKE のメタ文字は潰す）──────────────────────────────


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, Any]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _Conn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.cur = _Cursor(rows)

    def cursor(self) -> _Cursor:
        return self.cur


def test_adapter_runs_one_parameterized_query() -> None:
    conn = _Conn(
        [
            {"advertiser_idx": 2, "title": "t", "source_uri": "gdrive://X", "cls_industry": "飲料"},
            {"advertiser_idx": 9, "title": "bad", "source_uri": "gdrive://Y"},
        ]
    )
    client = PgVectorClient.__new__(PgVectorClient)
    rows = client.find_drive_files_for_advertisers(
        conn,  # type: ignore[arg-type]
        ["アオバ製薬", "100%_果汁", "アオバ製薬", "x"],
    )
    assert len(conn.cur.calls) == 1
    sql, params = conn.cur.calls[0]
    assert "source_type = 'gdrive'" in sql and "LATERAL" in sql
    assert params[0] == ["%アオバ製薬%", "%100\\%\\_果汁%"]
    assert rows == [
        {
            "advertiser": "100%_果汁",
            "title": "t",
            "source_uri": "gdrive://X",
            "cls_industry": "飲料",
            "case_industry": None,
            "cls_doc_type": None,
            "updated_at": None,
        }
    ]


def test_adapter_skips_query_without_advertisers() -> None:
    conn = _Conn([])
    client = PgVectorClient.__new__(PgVectorClient)
    assert client.find_drive_files_for_advertisers(conn, ["", " a "]) == []  # type: ignore[arg-type]
    assert conn.cur.calls == []
