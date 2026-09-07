"""preflight（不足検出・補完・決定論文言）と submit の受付境界。"""

from __future__ import annotations

from typing import Any

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.base import SkillContext
from teamagent.skills.omiyage_report.preflight import (
    OmiyageSuggestions,
    build_accepted_message,
    build_needs_input_message,
    estimate_duration,
    run_preflight,
)
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput
from teamagent.skills.omiyage_report.skill import OmiyageReportSubmitSkill


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="omiyage-preflight-test",
        user_id="U123",
        metadata={"channel_id": "C123", "thread_ts": "123.456"},
    )


def test_preflight_detects_missing_fields_exactly() -> None:
    result = run_preflight(OmiyageReportSubmitInput(brand="エムキュア"))
    assert result.missing == ("competitors", "keywords")
    assert result.ready is False

    result = run_preflight(
        OmiyageReportSubmitInput(competitors=["ラサーナ"], keywords=["ヘアケア"])
    )
    assert result.missing == ("brand",)

    result = run_preflight(
        OmiyageReportSubmitInput(
            brand="エムキュア",
            competitors=["ラサーナ", "THE ANSWER"],
            keywords=["ヘアケア"],
        )
    )
    assert result.ready is True


def test_preflight_uses_completion_source_only_for_missing_fields() -> None:
    def source(brand: str) -> OmiyageSuggestions:
        assert brand == "エムキュア"
        return OmiyageSuggestions(
            competitors=("ラサーナ", "THE ANSWER"),
            keywords=("ヘアケア", "シャンプー"),
            source="clientkarte",
        )

    result = run_preflight(
        OmiyageReportSubmitInput(brand="エムキュア", keywords=["ヘアケア"]),
        source,
    )
    assert result.missing == ("competitors",)
    assert [s.field for s in result.suggestions] == ["competitors"]
    assert result.suggestions[0].candidates == ["ラサーナ", "THE ANSWER"]
    assert result.suggestions[0].source == "clientkarte"


def test_preflight_completion_source_failure_is_not_fatal() -> None:
    def broken(brand: str) -> OmiyageSuggestions:
        raise RuntimeError("karte unavailable")

    result = run_preflight(OmiyageReportSubmitInput(brand="エムキュア"), broken)
    assert result.missing == ("competitors", "keywords")
    assert result.suggestions == ()


def test_needs_input_message_structure() -> None:
    input = OmiyageReportSubmitInput(brand="エムキュア")
    result = run_preflight(
        input,
        lambda _brand: OmiyageSuggestions(competitors=("ラサーナ",), source="vault"),
    )
    message = build_needs_input_message(input, result)
    # 受領済みは営業が書いた名前を原文表示
    assert "受領済み：" in message
    assert "- 対象ブランド：エムキュア" in message
    # 不足の必須情報だけを列挙
    assert "不足している必須情報：" in message
    assert "- 競合ブランド（1社以上）" in message
    assert "- 一般検索キーワード（1つ以上）" in message
    assert "対象ブランド（" not in message.split("不足している必須情報：")[1].split("補完候補")[0]
    # 補完候補と回答欄 + 作成指示文
    assert "補完候補（カルテ・金庫から）：" in message
    assert "- 競合ブランド候補：ラサーナ" in message
    assert "以下をコピーしてご返信ください。" in message
    assert "競合ブランド：" in message
    assert "一般検索キーワード：" in message
    # 回答欄の末尾は「指示」行で、その後ろに骨子へ切り替える逃げ道 1 行だけが続く
    lines = message.rstrip().split("\n")
    assert lines[-3] == "指示：この内容で資料を作成してください"
    assert lines[-2] == ""
    assert "『骨子で』とだけ返信してください" in lines[-1]


def test_needs_input_message_without_suggestions_has_no_suggestion_block() -> None:
    input = OmiyageReportSubmitInput(brand="エムキュア")
    result = run_preflight(input)
    message = build_needs_input_message(input, result)
    assert "補完候補" not in message


def _must_not_be_called(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("must not be called before inputs are complete")


def test_submit_with_missing_inputs_returns_needs_input_and_creates_no_job() -> None:
    memory: dict[str, dict[str, Any]] = {}
    store = ProposalJobStore(table_name="", memory=memory)
    skill = OmiyageReportSubmitSkill(
        store=store,
        searcher=_must_not_be_called,
        deck_builder=_must_not_be_called,
        thread_launcher=_must_not_be_called,
        heartbeat_seconds=0,
    )
    out = skill.run(OmiyageReportSubmitInput(brand="エムキュア"), _ctx())
    assert out.status == "needs_input"
    assert out.job_id == ""
    assert out.missing == ["competitors", "keywords"]
    assert "まだ着手していません" in out.message
    assert memory == {}  # ジョブ行を作らない


def test_accepted_message_states_computed_duration_not_seconds() -> None:
    """受付文は依頼内容から算出した目安（約 M 分）を言い切り、秒見込みや固定の「10〜30 分」を含まない。"""

    input = OmiyageReportSubmitInput(
        brand="エムキュア", competitors=["ラサーナ"], keywords=["ヘアケア"]
    )
    estimate = estimate_duration(axes=3, analysis_videos=25, analysis_concurrency=2)
    message = build_accepted_message(input, estimate)
    assert message.startswith("お土産資料（対象: エムキュア / 競合: ラサーナ / 一般KW: ヘアケア）")
    assert "の作成を受け付けました。" in message
    assert "目安 約 40 分（TikTok 取得 3 軸＋動画分析 最大 25 本）。" in message
    assert "途中経過は『まだ？』で確認できます。" in message
    assert "完成したPPTXは依頼元のスレッド（DM ならこの DM）へ添付します。" in message
    assert "10〜30" not in message
    assert "秒後" not in message
    assert "完成予定" not in message
    assert "omiyage_" not in message
