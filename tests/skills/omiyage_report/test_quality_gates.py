"""品質の門（段 1・2026-09-25）: 9/17 の GABAN 版で起きたことを資料の前で止める。

- 入力: ブランド名入りの一般キーワード・ブランド自身の競合（決定論）、不適切な競合（LLM・fail-open）
- 検索結果: 商材と関係の無い動画を集計から外す（LLM・軸ごと・fail-open）
- 界隈の分類表を商材カテゴリで選ぶ
LLM は呼び出しの形（プロンプト→応答本文）だけを差し替え、応答の解釈は本物を通す。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.omiyage_report import input_check
from teamagent.skills.omiyage_report.input_check import (
    COMPETITOR_REASONS,
    KEYWORD_HAS_BRAND,
    CompetitorCheck,
    check_competitors,
    check_relevance,
    find_input_problems,
    llm_checks_enabled,
)
from teamagent.skills.omiyage_report.metrics import PostRecord
from teamagent.skills.omiyage_report.preflight import build_needs_input_message, run_preflight
from teamagent.skills.omiyage_report.schema import (
    OmiyageReportStatusInput,
    OmiyageReportSubmitInput,
)
from teamagent.skills.omiyage_report.skill import (
    OmiyageReportStatusSkill,
    OmiyageReportSubmitSkill,
)
from teamagent.skills.omiyage_report.video_analysis import (
    DEFAULT_CLUSTER_RULES,
    FOOD_CLUSTER_RULES,
    GENERAL_CLUSTER_RULES,
    cluster_rules_for,
)

from .test_integration import (
    _analyzer_factory,
    _ctx,
    _KaoLikeSearcher,
    _ReleasedLauncher,
    _Slack,
    _Uploader,
)

# --- 決定論の入力点検 ---------------------------------------------------------------------


def test_brand_in_general_keyword_is_a_problem() -> None:
    problems = find_input_problems("GABAN", ["エスビー食品"], ["GABAN レシピ", "スパイスカレー"])
    assert [(p.field, p.value) for p in problems] == [("keywords", "GABAN レシピ")]
    assert problems[0].reason == KEYWORD_HAS_BRAND


def test_competitor_name_in_keyword_and_brand_as_competitor_are_problems() -> None:
    problems = find_input_problems(
        "GABAN", ["GABAN スパイス", "エスビー食品"], ["エスビー食品 カレー粉"]
    )
    assert {(p.field, p.value) for p in problems} == {
        ("keywords", "エスビー食品 カレー粉"),
        ("competitors", "GABAN スパイス"),
    }


def test_clean_input_and_one_char_names_have_no_problem() -> None:
    assert find_input_problems("GABAN", ["エスビー食品"], ["スパイスカレー 作り方"]) == []
    # 1 文字の名前は部分一致を見ない（「B」は「競合B」の一部ではない）
    assert find_input_problems("B", ["競合B"], ["シャンプー"]) == []


def test_needs_input_message_lists_problems_and_reply_fields() -> None:
    request = OmiyageReportSubmitInput(
        brand="GABAN", competitors=["エスビー食品"], keywords=["GABAN レシピ"]
    )
    result = run_preflight(request)
    assert not result.ready
    assert result.missing == ()
    assert result.fields_to_fill == ("keywords",)
    message = build_needs_input_message(request, result)
    assert "見直してほしい入力があるため、まだ着手していません。" in message
    assert "一般検索キーワード「GABAN レシピ」" in message
    assert "一般検索キーワード：" in message
    assert "不足している必須情報" not in message


# --- 競合の妥当性（LLM） -------------------------------------------------------------------


def _reply(payload: object) -> Any:
    def caller(prompt: str, request_id: str) -> str:
        caller.prompts.append(prompt)  # type: ignore[attr-defined]
        return f"判定しました。\n```json\n{json.dumps(payload, ensure_ascii=False)}\n```"

    caller.prompts = []  # type: ignore[attr-defined]
    return caller


def test_competitor_check_keeps_only_known_names_and_fixed_reasons() -> None:
    caller = _reply(
        {
            "verdicts": [
                {"name": "House食品", "verdict": "parent_or_group"},
                {"name": "カレーハウス CoCo壱番屋", "verdict": "restaurant_or_retail"},
                {"name": "エスビー食品", "verdict": "ok"},
                {"name": "入力に無い会社", "verdict": "parent_or_group"},  # 捨てる
                {"name": "エスビー食品", "verdict": "かなり怪しい"},  # 語彙外は捨てる
            ]
        }
    )
    check = check_competitors(
        "GABAN",
        ["House食品", "カレーハウス CoCo壱番屋", "エスビー食品"],
        "スパイス・調味料",
        "req",
        caller=caller,
    )
    assert check.checked
    assert check.invalid == {
        "House食品": "parent_or_group",
        "カレーハウス CoCo壱番屋": "restaurant_or_retail",
    }
    assert "スパイス・調味料" in caller.prompts[0]


@pytest.mark.parametrize("reply", ["わかりません", '{"verdicts": "none"}', "not json {"])
def test_competitor_check_unparsable_is_fail_open(reply: str) -> None:
    check = check_competitors("GABAN", ["House食品"], "", "req", caller=lambda p, r: reply)
    assert check == CompetitorCheck(invalid={}, checked=False)


def test_competitor_check_exception_is_fail_open() -> None:
    def boom(prompt: str, request_id: str) -> str:
        raise TimeoutError("bedrock")

    assert check_competitors("GABAN", ["House食品"], "", "req", caller=boom).checked is False


# --- 関連性（LLM） -----------------------------------------------------------------------


def _post(video_id: str, caption: str, tags: tuple[str, ...] = ()) -> PostRecord:
    return PostRecord(
        video_id=video_id,
        url=f"https://www.tiktok.com/@x/video/{video_id}",
        author="x",
        caption=caption,
        hashtags=tags,
        rank=1,
        plays=100,
        likes=1,
        comments=0,
        shares=0,
        saves=0,
        followers=10,
        nickname="x",
        cover_url="",
        duration_sec=10,
    )


def test_relevance_check_returns_only_known_ids() -> None:
    posts = [
        _post("1", "ギャバン大将の名場面", ("onepiece", "gaban")),
        _post("2", "GABANのスパイスでキーマカレー", ("スパイスカレー",)),
    ]
    caller = _reply({"unrelated": ["1", "999"]})
    check = check_relevance(
        query="GABAN",
        brand="GABAN",
        category="スパイス・調味料",
        competitors=["エスビー食品"],
        keywords=["スパイスカレー"],
        posts=posts,
        request_id="req",
        caller=caller,
    )
    assert check.checked
    assert check.excluded_ids == frozenset({"1"})
    assert "ギャバン大将の名場面" in caller.prompts[0]
    assert "#onepiece" in caller.prompts[0]


def test_relevance_check_unparsable_is_fail_open() -> None:
    check = check_relevance(
        query="GABAN",
        brand="GABAN",
        category="",
        competitors=[],
        keywords=[],
        posts=[_post("1", "x")],
        request_id="req",
        caller=lambda p, r: "no json",
    )
    assert check.checked is False
    assert check.excluded_ids == frozenset()


@pytest.mark.parametrize(
    ("flag", "model", "expected"),
    [("", "", False), ("", "jp.anthropic.claude", True), ("0", "jp.x", False), ("1", "", True)],
)
def test_llm_checks_enabled(
    monkeypatch: pytest.MonkeyPatch, flag: str, model: str, expected: bool
) -> None:
    monkeypatch.setenv(input_check.LLM_CHECKS_ENV, flag)
    monkeypatch.setenv("BEDROCK_MODEL_ID", model)
    assert llm_checks_enabled() is expected


# --- 界隈の分類表 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "brand", "keywords", "rules"),
    [
        ("スパイス・調味料", "GABAN", ["スパイスカレー"], FOOD_CLUSTER_RULES),
        ("", "GABAN", ["スパイスカレー 作り方"], FOOD_CLUSTER_RULES),
        ("スキンケア", "エムキュア", ["ヘアケア"], DEFAULT_CLUSTER_RULES),
        ("", "エムキュア", ["ヘアケア"], DEFAULT_CLUSTER_RULES),
        ("格安SIM", "UQ mobile", ["スマホ 乗り換え"], GENERAL_CLUSTER_RULES),
        ("", "UQ mobile", ["MNP 手順"], GENERAL_CLUSTER_RULES),
    ],
)
def test_cluster_rules_follow_category(
    category: str, brand: str, keywords: list[str], rules: Any
) -> None:
    assert cluster_rules_for(category, brand, keywords) is rules


# --- 受付と資料作成の流れ -------------------------------------------------------------------


def _skill(**kwargs: Any) -> tuple[OmiyageReportSubmitSkill, ProposalJobStore, _ReleasedLauncher]:
    store = ProposalJobStore(table_name="", memory={})
    launcher = _ReleasedLauncher()
    events: list[str] = []
    skill = OmiyageReportSubmitSkill(
        store=store,
        searcher=_KaoLikeSearcher(events=events),
        deck_builder=_fake_builder,
        slack=_Slack(events=events),
        thread_launcher=launcher,
        analyzer_factory=_analyzer_factory(events),
        plan_uploader=kwargs.pop("uploader", _Uploader()),
        heartbeat_seconds=0,
        search_depth=30,
        analysis_per_axis=10,
        **kwargs,
    )
    return skill, store, launcher


def _fake_builder(deck_plan_json: str, out_dir: str, request_id: str) -> tuple[str, str]:
    from pathlib import Path

    (Path(out_dir) / "deck.json").write_text(deck_plan_json, encoding="utf-8")
    image = Path(out_dir) / "omiyage_fmt_x.pptx"
    image.write_bytes(b"PK-image")
    editable = Path(out_dir) / "omiyage_fmt_x_edit.pptx"
    editable.write_bytes(b"PK-editable")
    return str(image), str(editable)


def _input(**kwargs: Any) -> OmiyageReportSubmitInput:
    base = {"brand": "エムキュア", "competitors": ["ラサーナ"], "keywords": ["ヘアケア"]}
    base.update(kwargs)
    return OmiyageReportSubmitInput(**base)


def test_invalid_competitor_returns_needs_input_without_job() -> None:
    calls: list[tuple[str, tuple[str, ...], str]] = []

    def checker(brand: str, competitors: Any, category: str, request_id: str) -> CompetitorCheck:
        calls.append((brand, tuple(competitors), category))
        return CompetitorCheck(invalid={"ラサーナ": "parent_or_group"}, checked=True)

    skill, _store, _launcher = _skill(competitor_checker=checker)
    out = skill.run(_input(category="ヘアケア"), _ctx())
    assert out.status == "needs_input"
    assert out.missing == ["competitors"]
    assert COMPETITOR_REASONS["parent_or_group"] in out.message
    assert "競合ブランド「ラサーナ」" in out.message
    assert out.job_id in ("", None)
    assert calls == [("エムキュア", ("ラサーナ",), "ヘアケア")]


def test_competitor_checker_failure_does_not_block() -> None:
    def checker(*_args: Any) -> CompetitorCheck:
        raise RuntimeError("bedrock down")

    skill, _store, launcher = _skill(competitor_checker=checker)
    out = skill.run(_input(), _ctx())
    assert out.status == "queued"
    assert launcher.finished.wait(timeout=60)


def test_unrelated_videos_are_excluded_and_disclosed() -> None:
    uploader = _Uploader()
    seen: list[str] = []

    def relevance(**kwargs: Any) -> input_check.RelevanceCheck:
        seen.append(kwargs["query"])
        # ブランド軸の 4 本のうち 2 本を「関係の無い動画」と判定
        if kwargs["query"] == "エムキュア":
            return input_check.RelevanceCheck(excluded_ids=frozenset({"b3", "b4"}), checked=True)
        return input_check.RelevanceCheck(excluded_ids=frozenset(), checked=True)

    skill, store, launcher = _skill(relevance_checker=relevance, uploader=uploader)
    accepted = skill.run(_input(), _ctx())
    assert accepted.status == "queued"
    assert launcher.finished.wait(timeout=60)
    done = OmiyageReportStatusSkill(store=store).run(
        OmiyageReportStatusInput(job_id=accepted.job_id), _ctx()
    )
    assert done.status == "done"
    assert sorted(seen) == sorted(["ヘアケア", "エムキュア", "ラサーナ"])

    audit = next(json.loads(body) for key, body in uploader.objects.items() if "audit" in key)
    assert audit["relevance"]["excluded"] == 2
    assert audit["relevance"]["excluded_by_axis"] == {"ブランド名「エムキュア」検索": 2}
    brand_axis = next(a for a in audit["axes"] if a["role"] == "brand")
    assert brand_axis["fetched"] == 2  # 集計は残った 2 本だけ
    plan = next(json.loads(body) for key, body in uploader.objects.items() if "audit" not in key)
    target_row = plan["deck_meta"]["method_target_constraints"][1]
    assert "関係の無い動画（同名の別作品など）2本は集計から除外" in target_row


def test_relevance_unchecked_axis_keeps_all_and_says_so() -> None:
    uploader = _Uploader()

    def relevance(**_kwargs: Any) -> input_check.RelevanceCheck:
        return input_check.RelevanceCheck(excluded_ids=frozenset(), checked=False)

    skill, _store, launcher = _skill(relevance_checker=relevance, uploader=uploader)
    assert skill.run(_input(), _ctx()).status == "queued"
    assert launcher.finished.wait(timeout=60)
    plan = next(json.loads(body) for key, body in uploader.objects.items() if "audit" not in key)
    assert "関係の無い動画の除外は未実施" in plan["deck_meta"]["method_target_constraints"][1]
    audit = next(json.loads(body) for key, body in uploader.objects.items() if "audit" in key)
    assert len(audit["relevance"]["unchecked_axes"]) == 3
