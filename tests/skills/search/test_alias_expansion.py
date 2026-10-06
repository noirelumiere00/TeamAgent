"""P2の利用者発話・失敗モード。実DB/Slack/Bedrockへ接続しない。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from teamagent.skills.base import SkillContext
from teamagent.skills.search.alias_expansion import retry_inputs, scope_footer
from teamagent.skills.search.client_match import aliases, normalize_client, normalize_filter_client
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_composite_search import _bedrock, _pg, _skill
from tests.skills.search.test_not_found import _hit, _rerank


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    for flag in (
        "SEARCH_ALIAS_EXPANSION",
        "SEARCH_SCOPE_FOOTER",
        "SEARCH_ALIAS_TIMEOUT_S",
        "USE_COMPOSITE_SEARCH",
    ):
        monkeypatch.delenv(flag, raising=False)


class _Queries:
    """ベクトルと検索語を結び、実retrievalのフィルタ・並行実行を検査するフェイク。"""

    def __init__(self) -> None:
        self.values: dict[int, str] = {}
        self.lock = threading.Lock()

    def embed(self, text: str) -> list[float]:
        with self.lock:
            index = len(self.values)
            self.values[index] = text
        return [float(index)]


def _search(on_search: Any) -> tuple[Any, MagicMock, MagicMock]:
    b, pg = _bedrock(scores=[0.42] * 50), _pg([])
    skill, _ = _skill(b, pg)
    embedder = _Queries()
    skill._embedder = embedder
    pg.search_similar_new_schema.side_effect = lambda **kw: on_search(
        embedder.values[int(kw["embedding"][0])], kw
    )
    return skill, b, pg


@pytest.mark.parametrize(
    ("query", "alias", "product"),
    [
        ("サンギの提案資料を探して", "SANGI", "提案"),
        ("インペックスの資料を探して", "INPEX", "資料"),
        ("エムキュアの資料を探して", "ネイチャーラボ", "資料"),
        ("エリクシールの施策実績を探して", "資生堂", "施策実績"),
        ("めかぶとトップバリュの資料を探して", "イオントップバリュ", "めかぶ"),
        ("ファミマのおにぎりの資料を探して", "ファミリーマート", "おにぎり"),
        ("ホンダの提案資料を探して", "本田技研工業", "提案"),
    ],
)
def test_user_requests_rescued_by_alias(query: str, alias: str, product: str) -> None:
    seen: list[str] = []

    def search(q: str, _: Any) -> list[Any]:
        seen.append(q)
        return (
            [_hit(0.8, f"{alias} {product}の内容", title=f"{alias}資料", source_type="gdrive")]
            if alias in q
            else []
        )

    skill, b, pg = _search(search)
    out = skill.run(
        SearchInput(query=query), SkillContext(metadata={"user_email": "me@example.test"})
    )
    assert out.found is True and len(out.hits) == 1
    b.converse.assert_called_once()
    assert alias in out.answer.splitlines()[-1] and query in seen
    assert len(set(seen)) <= 3
    assert all(product in q for q in seen)
    assert all(
        call.kwargs["user_email"] == "me@example.test" for call in pg.connection.call_args_list
    )
    assert "Aicoができる作業を利用者へ戻す依頼は禁止" in b.converse.call_args.kwargs["system"]


@pytest.mark.parametrize(
    "query", ["西友フーズの麻辣湯の資料を探して", "SPEの資料を探して", "VVS/TTOの意味を教えて"]
)
def test_unconfirmed_terms_are_not_guessed(query: str) -> None:
    skill, b, _ = _search(lambda *_: [])
    out = skill.run(SearchInput(query=query), SkillContext(metadata={}))
    assert not out.found
    assert out.answer.endswith(f"探した範囲: 金庫を『{query}』で検索")
    assert len(skill._embedder.values) == 1
    b.converse.assert_not_called()


@pytest.mark.parametrize(
    "initial",
    [[], [_hit(0.1, title="他社資料")], [_hit(0.42, title="花王資料", client_name="花王")]],
)
def test_zero_low_score_and_mismatch_retry(initial: list[Any]) -> None:
    skill, b, _ = _search(
        lambda q, _: initial if "インペックス" in q else [_hit(0.8, "INPEX", title="INPEX資料")]
    )
    if initial and initial[0].score < 0.16:
        b.rerank.side_effect = lambda **kw: _rerank(
            [0.1 if "INPEX" not in kw["documents"][0] else 0.42]
        )
    out = skill.run(SearchInput(query="インペックスの資料"), SkillContext(metadata={}))
    assert out.found and out.hits[0].title == "INPEX資料"


def test_aliases_run_concurrently_and_preserve_all_sticky_filters() -> None:
    barrier = threading.Barrier(2)
    seen: list[dict[str, Any]] = []

    def search(q: str, kw: dict[str, Any]) -> list[Any]:
        if "トップバリュ" == (kw.get("metadata_contains") or {}).get("__client__"):
            return []
        barrier.wait(timeout=2)
        seen.append(kw)
        return [_hit(0.8, q, title="資料", chunk_id=1)]

    skill, _, _ = _search(search)
    input = SearchInput(
        query="トップバリュ めかぶ",
        filter_client="トップバリュ",
        filter_budget="〜100万",
        filter_doc_type="提案書",
        filter_solution="SNS運用",
        filter_industry="食品・飲料",
        strict_industry=True,
    )
    out = skill.run(
        input,
        SkillContext(
            metadata={"user_email": "me@example.test", "user_groups": ["g"], "user_role": "member"}
        ),
    )
    assert out.found and len(out.hits) == 1  # 同じchunkは二重に返さない
    assert len(seen) == 2
    for kw in seen:
        assert kw["sticky_filters"] == {
            "cls_budget": "〜100万",
            "cls_doc_type": "提案書",
            "cls_solution": "SNS運用",
        }
        assert kw["filter_industry"] == "食品・飲料" and kw["strict_industry"]
    assert {kw["metadata_contains"]["__client__"] for kw in seen} == {
        "イオン",
        "イオントップバリュ",
    }


def test_common_deadline_keeps_completed_alias_and_does_not_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEARCH_ALIAS_TIMEOUT_S", "0.1")
    release = threading.Event()

    def search(q: str, _: Any) -> list[Any]:
        if "イオントップバリュ" in q:
            release.wait(2)
            return []
        return [_hit(0.8, "イオン めかぶ", title="イオン資料")] if "イオン" in q else []

    skill, _, _ = _search(search)
    start = time.perf_counter()
    try:
        out = skill.run(SearchInput(query="トップバリュ めかぶ"), SkillContext(metadata={}))
    finally:
        release.set()
    assert time.perf_counter() - start < 1
    assert out.found and "再検索の一部は時間切れ・失敗" in out.answer


@pytest.mark.parametrize("failure", ["ratelimited", "missing_scope", "not_in_channel", "timeout"])
def test_retry_exceptions_leave_original_result(failure: str) -> None:
    def search(q: str, _: Any) -> list[Any]:
        if "INPEX" in q:
            raise RuntimeError(failure + " secret person/body")
        return []

    skill, b, _ = _search(search)
    out = skill.run(SearchInput(query="インペックスの資料"), SkillContext(metadata={}))
    assert not out.found and "時間切れ・失敗" in out.answer
    assert "secret" not in out.answer
    b.converse.assert_not_called()


def test_success_does_not_retry_and_fast_path_has_empty_answer() -> None:
    skill, b, _ = _search(lambda *_: [_hit(0.8, "INPEX", title="INPEX資料")])
    out = skill.run(SearchInput(query="INPEX", include_answer=False), SkillContext(metadata={}))
    assert out.found and out.answer == "" and len(skill._embedder.values) == 1
    b.converse.assert_not_called()


def test_kill_switches_restore_single_search_and_no_footer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_ALIAS_EXPANSION", "false")
    monkeypatch.setenv("SEARCH_SCOPE_FOOTER", "false")
    skill, b, _ = _search(lambda *_: [])
    out = skill.run(SearchInput(query="インペックスの資料"), SkillContext(metadata={}))
    assert not out.found and len(skill._embedder.values) == 1
    assert "探した範囲" not in out.answer
    b.converse.assert_not_called()


def test_legal_width_normalization_and_bounded_variants() -> None:
    assert normalize_client("㈱ＩＮＰＥＸ") == normalize_client("株式会社INPEX")
    assert normalize_filter_client("(株)ｲﾝﾍﾟｯｸｽ") == "インペックス"
    candidates = retry_inputs(SearchInput(query="（株）ｲﾝﾍﾟｯｸｽの資料"))
    assert [candidate.query for candidate in candidates] == ["インペックスの資料", "INPEXの資料"]
    assert not retry_inputs(SearchInput(query="XSPEZ ファミマート"))
    assert "INPEX" in aliases("インペックス")
    assert "SOMARCA" in aliases("ホーユー株式会社")


def test_scope_is_one_bounded_neutralized_line() -> None:
    footer = scope_footer(["<!channel>\n" + "あ" * 1000] * 3, slack=None)
    assert "\n" not in footer and "<!channel>" not in footer and len(footer) < 150


def test_found_on_first_search_has_no_scope_footer() -> None:
    """1 回目で見つかった答えには「探した範囲」を足さない（見つからなかったときだけ）。"""
    skill, b, _ = _search(lambda *_: [_hit(0.8, "INPEX", title="INPEX資料")])
    out = skill.run(SearchInput(query="INPEXの資料"), SkillContext(metadata={}))
    assert out.found and len(skill._embedder.values) == 1
    assert "探した範囲" not in out.answer
    b.converse.assert_called_once()


def test_aliases_are_static_explicit_pairs() -> None:
    """別名は明示の対だけ（推移展開しない・未確認の略称は足さない）。"""
    assert aliases("トップバリュ") == {"イオントップバリュ", "イオン"}
    assert "イオントップバリュ" not in aliases("イオン")  # イオン→トップバリュ→… と連鎖しない
    assert aliases("SPE") == set()
    assert aliases("西友フーズ") == set()


def test_gold_positive_drafts_have_no_invented_keywords() -> None:
    path = Path(__file__).resolve().parents[3] / "data/eval/sales_gold_set.yaml"
    drafts = [case for case in yaml.safe_load(path.read_text())["cases"] if case["id"] >= 51]
    assert len(drafts) == 9
    assert all(
        case["validation_status"] == "pending_db_confirmation"
        and case["expect_zero_hits"] is False
        and "本番DBで確認が要る" in case["notes"]
        and "expect_keywords" not in case
        for case in drafts
    )


@pytest.mark.parametrize("score", [0.1, 0.42])
def test_weak_or_unrelated_retry_never_becomes_evidence(score: float) -> None:
    skill, b, _ = _search(
        lambda q, _: [] if "インペックス" in q else [_hit(score, "花王", title="花王資料")]
    )
    b.rerank.side_effect = lambda **_: _rerank([score])
    out = skill.run(SearchInput(query="インペックスの資料"), SkillContext(metadata={}))
    assert not out.found and out.hits == []
    b.converse.assert_not_called()


def test_filter_only_aliases_keep_other_filters() -> None:
    candidates = retry_inputs(
        SearchInput(query="おにぎりの資料", filter_client="ファミマ", filter_doc_type="提案書")
    )
    assert len(candidates) == 1
    assert candidates[0].query == "おにぎりの資料"
    assert candidates[0].filter_client == "ファミリーマート"
    assert candidates[0].filter_doc_type == "提案書"


def test_large_query_never_exceeds_schema_limit() -> None:
    input = SearchInput(query="ファミマ " + "あ" * 995)
    assert len(input.query) == 1000
    assert retry_inputs(input) == []  # 別名が長くなっても入力上限を破らない


@pytest.mark.parametrize(
    "raw,expected", [("nan", 8.0), ("inf", 8.0), ("-1", 8.0), ("bad", 8.0), ("30", 20.0)]
)
def test_timeout_configuration_has_finite_upper_bound(
    raw: str, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEARCH_ALIAS_TIMEOUT_S", raw)
    skill, _, _ = _search(lambda *_: [])
    assert skill._alias_timeout_s == expected


def test_filter_only_search_scope_names_actual_alias_filter() -> None:
    candidates = retry_inputs(SearchInput(query="おにぎりの資料", filter_client="ファミマ"))
    skill, _, _ = _search(lambda *_: [])
    _, labels, _ = skill._retry_aliases(
        SearchInput(query="おにぎりの資料", filter_client="ファミマ"),
        SkillContext(metadata={}),
        [],
        None,
    )
    assert candidates[0].filter_client == "ファミリーマート"
    assert "ファミリーマート" in scope_footer(labels, slack=None)


def test_retry_logging_does_not_include_query_body_or_error_message() -> None:
    from structlog.testing import capture_logs

    def fail(q: str, _: Any) -> list[Any]:
        if "INPEX" in q:
            raise RuntimeError("PRIVATE_BODY_PERSON")
        return []

    skill, _, _ = _search(fail)
    with capture_logs() as logs:
        skill.run(SearchInput(query="インペックスの資料"), SkillContext(metadata={}))
    encoded = json.dumps(logs, ensure_ascii=False)
    assert "PRIVATE_BODY_PERSON" not in encoded and "インペックス" not in encoded
    events = [entry for entry in logs if entry["event"] == "search_alias_retry"]
    assert len(events) == 1
    assert set(events[0]) == {
        "event",
        "log_level",
        "request_id",
        "attempts",
        "incomplete",
        "rescued",
    }


def test_full_width_hit_matches_explicit_client_filter() -> None:
    skill, b, _ = _search(
        lambda *_: [_hit(0.8, "資料本文", client_name="株式会社ＩＮＰＥＸ", title="提案書")]
    )
    out = skill.run(
        SearchInput(query="INPEXの資料", filter_client="INPEX"), SkillContext(metadata={})
    )
    assert out.found
    b.converse.assert_called_once()


def test_merge_keeps_main_hits_budget_order_and_related_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEARCH_BUDGET_SORT", "true")
    skill, _, _ = _search(lambda *_: [])
    hits = [
        _hit(0.4, "イオン", chunk_id=1, cls_budget="500万〜"),
        _hit(0.3, "イオン", chunk_id=2, cls_budget="〜100万"),
        _hit(1.0, "イオン", chunk_id=3, is_related_drive=True),
    ]
    monkeypatch.setattr(skill, "_retrieve", lambda *_a, **_kw: hits)
    rescued, _, incomplete = skill._retry_aliases(
        SearchInput(query="トップバリュ", top_k=2, sort_budget_near="〜100万"),
        SkillContext(metadata={}),
        [],
        None,
    )
    assert [hit.chunk_id for hit in rescued] == [2, 1, 3]
    assert not incomplete


@pytest.mark.parametrize(
    "query,alias", [("INPEXの資料", "インペックス"), ("HONDAの資料", "ホンダ")]
)
def test_ascii_alias_also_expands_back_to_kana(query: str, alias: str) -> None:
    skill, b, _ = _search(lambda q, _: [_hit(0.8, alias, title=alias)] if alias in q else [])
    out = skill.run(SearchInput(query=query), SkillContext(metadata={}))
    assert out.found
    b.converse.assert_called_once()
