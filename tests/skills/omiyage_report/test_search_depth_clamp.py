"""お土産資料の検索深度は dispatcher の n_per_kw 上限を絶対に超えない。

2026-09-02 本番: 既定 120 をそのまま n_per_kw に載せ、dispatcher Lambda の
``maximum=30`` で全軸 TIKTOK_MEDIA_JOB_FAILED（TikTok n_per_kw is invalid）。
既定・env・明示指定のどの経路でも TIKTOK_N_PER_KW_MAX で clamp されることを固定する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.adapters.tiktok_scraper import TikTokSearchResult
from teamagent.media.contracts import TIKTOK_N_PER_KW_MAX
from teamagent.skills.base import SkillContext
from teamagent.skills.omiyage_report import skill as skill_module
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput
from teamagent.skills.omiyage_report.skill import (
    OmiyageReportSubmitSkill,
    clamp_search_depth,
    configured_search_depth,
)


@dataclass
class _RecordingSearcher:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, query: str, **kwargs: Any) -> TikTokSearchResult:
        self.calls.append({"query": query, **kwargs})
        return TikTokSearchResult(query=query, search_type="keyword", videos=())


class _InlineLauncher:
    """背景スレッドを起動せず、その場で同期実行する（検索呼び出しを即観測するため）。"""

    def __call__(self, target: Any, name: str) -> None:
        target()


def _skill(searcher: _RecordingSearcher, **kwargs: Any) -> OmiyageReportSubmitSkill:
    return OmiyageReportSubmitSkill(
        store=ProposalJobStore(table_name="", memory={}),
        searcher=searcher,
        deck_builder=lambda plan, out_dir, request_id: ("", ""),
        thread_launcher=_InlineLauncher(),
        analyzer_factory=lambda request_id: None,
        plan_uploader=lambda key, body, content_type: "",
        heartbeat_seconds=0,
        **kwargs,
    )


def _input() -> OmiyageReportSubmitInput:
    return OmiyageReportSubmitInput(
        brand="エムキュア",
        competitors=["ラサーナ"],
        keywords=["ヘアケア"],
    )


def test_default_search_depth_equals_dispatcher_limit() -> None:
    assert skill_module._SEARCH_DEPTH_DEFAULT == TIKTOK_N_PER_KW_MAX
    assert TIKTOK_N_PER_KW_MAX == 30


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, TIKTOK_N_PER_KW_MAX),  # 未設定 → 既定＝上限
        ("120", TIKTOK_N_PER_KW_MAX),  # 事故前の既定値を env で指定しても上限へ
        ("31", TIKTOK_N_PER_KW_MAX),
        ("30", 30),
        ("20", 20),  # 下げる方向は通る
        ("5", 10),  # 下限 10
        ("abc", TIKTOK_N_PER_KW_MAX),  # 不正値は既定
    ],
)
def test_configured_search_depth_never_exceeds_dispatcher_limit(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int
) -> None:
    if raw is None:
        monkeypatch.delenv("OMIYAGE_SEARCH_DEPTH", raising=False)
    else:
        monkeypatch.setenv("OMIYAGE_SEARCH_DEPTH", raw)
    depth = configured_search_depth()
    assert depth == expected
    assert 1 <= depth <= TIKTOK_N_PER_KW_MAX


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(120, 30), (31, 30), (30, 30), (7, 7), (0, 1), (-5, 1)],
)
def test_clamp_search_depth(requested: int, expected: int) -> None:
    assert clamp_search_depth(requested) == expected


def test_explicit_search_depth_is_clamped_in_constructor() -> None:
    searcher = _RecordingSearcher()
    assert _skill(searcher, search_depth=120)._search_depth == TIKTOK_N_PER_KW_MAX
    assert _skill(searcher, search_depth=TIKTOK_N_PER_KW_MAX + 1)._search_depth == (
        TIKTOK_N_PER_KW_MAX
    )
    assert _skill(searcher, search_depth=12)._search_depth == 12


def test_submitted_n_per_kw_never_exceeds_dispatcher_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env で 120 を要求しても、検索器へ渡る max_videos（＝n_per_kw）は上限以下。"""

    monkeypatch.setenv("OMIYAGE_SEARCH_DEPTH", "120")
    searcher = _RecordingSearcher()
    skill = _skill(searcher)  # search_depth 未指定 → env 経路
    accepted = skill.run(_input(), SkillContext(request_id="req-clamp", user_id="U1"))

    assert accepted.status == "queued"
    assert [call["query"] for call in searcher.calls] == ["ヘアケア", "エムキュア", "ラサーナ"]
    assert all(call["max_videos"] == TIKTOK_N_PER_KW_MAX for call in searcher.calls)
    assert all(1 <= call["max_videos"] <= TIKTOK_N_PER_KW_MAX for call in searcher.calls)
    # 台帳に残す request_summary も同じ値（status 応答・監査で 120 と嘘をつかない）
    job = skill._store.get_job(accepted.job_id)
    assert job is not None
    assert json.loads(str(job["request_summary"]))["search_depth"] == TIKTOK_N_PER_KW_MAX


def test_accepted_message_states_realistic_duration() -> None:
    searcher = _RecordingSearcher()
    accepted = _skill(searcher).run(_input(), SkillContext(request_id="req-eta", user_id="U1"))
    assert accepted.status == "queued"
    assert "目安 10〜30 分" in accepted.message
    assert "TikTok 取得と動画分析に時間がかかります" in accepted.message
    assert "『まだ？』で確認できます" in accepted.message
    assert "秒後" not in accepted.message
