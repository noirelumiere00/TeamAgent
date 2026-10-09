"""検査失敗でも生成済み PPTX を配信へ渡せる。"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from teamagent.ingest import office_extract
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_deck.schema import ProposalDeckOutput
from teamagent.skills.proposal_deck.skill import ProposalDeckSkill
from tests.skills._shared.test_deck_review import _deck
from tests.skills.proposal_deck.test_proposal_deck_skill import _full_composer_json, _input, _resp


@pytest.mark.parametrize("failure", [False, True])
def test_scan_failure_does_not_stop_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: bool
) -> None:
    template = tmp_path / "template.pptx"
    template_texts = [[] for _ in range(83)]
    template_texts[2] = ["要確認（テンプレの説明）"]
    template.write_bytes(_deck(template_texts))
    texts = [[] for _ in range(83)]
    texts[2] = ["要確認（テンプレの説明）"]
    texts[6] = ["要確認"]
    texts[62] = ["要確認"]
    body = _deck(texts)

    def render(_composer, _template, out_path, *, request_id):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(body)
        return out_path

    monkeypatch.setattr(ProposalDeckSkill, "_render_pptx", staticmethod(render))
    monkeypatch.setattr(
        ProposalDeckSkill, "_publish_if_enabled", staticmethod(lambda *_a, **_kw: None)
    )
    monkeypatch.setattr(ProposalDeckSkill, "_emit_pdf_if_enabled", lambda *_a, **_kw: (None, None))
    if failure:
        from teamagent.skills._shared import deck_review

        def fail(_data):
            raise office_extract.OfficePayloadError(
                "unsafe_content_volume", mime_type=office_extract.PPTX_MIME, actual_bytes=123
            )

        monkeypatch.setattr(deck_review, "extract_pptx_slide_shapes", fail)
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp(_full_composer_json())
    with capture_logs() as logs:
        out = ProposalDeckSkill(bedrock=bedrock).run(
            _input(template, tmp_path / "out"), SkillContext()
        )
    assert out.review_slides == (None if failure else [7, 63])
    assert Path(out.pptx_path).read_bytes() == body
    from teamagent.skills.proposal_builder.skill import ProposalBuilderSkill
    from tests.skills.proposal_builder.test_tiktok_enrichment import (
        _configure_builder_test,
        _research,
    )
    from tests.skills.proposal_builder.test_tiktok_enrichment import (
        _input as builder_input,
    )

    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "0")
    _configure_builder_test(monkeypatch, media_configured=False)
    monkeypatch.setenv("PROPOSAL_BUILDER_DELIVER_INTERNAL_DRAFTS", "1")
    deck = MagicMock()
    deck.run.return_value = out.model_copy(update={"skipped_ids": [41, 42], "skipped_count": 2})
    slack = AsyncMock()
    slack.upload_file.return_value = True
    delivered = ProposalBuilderSkill(
        search=object(), deck=deck, slack=slack, account_db_path="unused.xlsx"
    ).run(
        builder_input(_research()),
        SkillContext(metadata={"channel_id": "C123", "thread_ts": "1.2"}),
    )
    assert delivered.slack_delivered
    assert delivered.status == "draft"
    assert slack.upload_file.await_args.kwargs["title"].startswith("DRAFT_裏取り前_")
    assert Path(slack.upload_file.await_args.args[1]).read_bytes() == body
    if failure:
        assert (
            "要確認の位置は自動で数えられませんでした"
            in slack.upload_file.await_args.kwargs["initial_comment"]
        )
    else:
        assert "7・63枚目" in slack.upload_file.await_args.kwargs["initial_comment"]
    if failure:
        event = next(item for item in logs if item["event"] == "proposal_deck_review_scan_failed")
        assert {
            key: value
            for key, value in event.items()
            if key not in {"request_id", "skill", "user_id"}
        } == {
            "event": "proposal_deck_review_scan_failed",
            "log_level": "warning",
            "error_type": "OfficePayloadError",
        }


def test_deck_output_defaults_to_uninspected() -> None:
    out = ProposalDeckOutput(
        pptx_path="fake.pptx",
        filled_count=95,
        skipped_count=0,
        coverage_ratio=1.0,
        total_cost_usd=0.0,
    )
    assert out.review_slides is None
