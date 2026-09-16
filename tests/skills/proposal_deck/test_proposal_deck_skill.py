"""ProposalDeckSkill の単体テスト（bedrock を MagicMock、dummy template で pptx 生成）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_deck.contract import LENGTH_RULES, VALID_IDS, ComposerOutput
from teamagent.skills.proposal_deck.schema import ProposalDeckInput
from teamagent.skills.proposal_deck.skill import ProposalDeckSkill


@pytest.fixture(autouse=True)
def _explicit_local_media_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """These renderer integration tests intentionally exercise local media deps."""

    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")


def _full_composer_json() -> str:
    """全 95 placeholder を文字数規則どおり埋めた ComposerOutput の JSON。"""
    placeholders: dict[int, str] = {}
    for pid in sorted(VALID_IDS):
        if pid in LENGTH_RULES:
            lo, hi = LENGTH_RULES[pid]
            placeholders[pid] = "サ" * ((lo + hi) // 2)
        else:
            placeholders[pid] = f"値-{pid}"
    return ComposerOutput(placeholders=placeholders).model_dump_json()


def _dummy_template(path: Path) -> Path:
    """VALID_IDS から ｛N：ラベルN｝ を敷き詰めたダミーテンプレ pptx。"""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    blank = prs.slide_layouts[6]
    ids = sorted(VALID_IDS)
    for i in range(0, len(ids), 30):
        slide = prs.slides.add_slide(blank)
        tf = slide.shapes.add_textbox(Inches(0.5), Inches(0.5), Inches(9), Inches(6.5)).text_frame
        tf.word_wrap = True
        for pid in ids[i : i + 30]:
            tf.add_paragraph().text = f"｛{pid}：ラベル{pid}｝"
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    return path


def _resp(text: str) -> ConverseResponse:
    return ConverseResponse(
        text=text,
        usage=TokenUsage(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.01,
        ),
        model_id="jp.anthropic.claude-sonnet-4-6",
        latency_ms=100,
        stop_reason="end_turn",
    )


def _input(template: Path, out: Path, **kw: object) -> ProposalDeckInput:
    return ProposalDeckInput(
        product_name="ACME 青汁",
        goal="認知獲得",
        target_persona="20代女性",
        template_path=str(template),
        out_dir=str(out),
        **kw,  # type: ignore[arg-type]
    )


def test_generates_pptx_full_coverage(tmp_path: Path) -> None:
    template = _dummy_template(tmp_path / "t.pptx")
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp(_full_composer_json())

    skill = ProposalDeckSkill(bedrock=bedrock)
    out = skill.run(_input(template, tmp_path / "out"), ctx=SkillContext())

    assert out.coverage_ratio == 1.0
    assert out.filled_count == 95
    assert out.skipped_count == 0
    assert Path(out.pptx_path).exists()
    assert out.total_cost_usd == pytest.approx(0.01)
    assert bedrock.converse.call_count == 1


def test_self_repair_invalid_then_valid(tmp_path: Path) -> None:
    template = _dummy_template(tmp_path / "t.pptx")
    bedrock = MagicMock()
    bedrock.converse.side_effect = [
        _resp('{"placeholders": {"1": "x"}}'),  # 網羅不足
        _resp(_full_composer_json()),
    ]
    skill = ProposalDeckSkill(bedrock=bedrock)
    out = skill.run(_input(template, tmp_path / "out", max_repair=1), ctx=SkillContext())

    assert out.coverage_ratio == 1.0
    assert bedrock.converse.call_count == 2
    # 累計コスト（2 回分）
    assert out.total_cost_usd == pytest.approx(0.02)


def test_json_code_fence_is_extracted(tmp_path: Path) -> None:
    template = _dummy_template(tmp_path / "t.pptx")
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp("```json\n" + _full_composer_json() + "\n```")
    skill = ProposalDeckSkill(bedrock=bedrock)
    out = skill.run(_input(template, tmp_path / "out"), ctx=SkillContext())
    assert out.coverage_ratio == 1.0


def test_exhausted_repair_raises(tmp_path: Path) -> None:
    template = _dummy_template(tmp_path / "t.pptx")
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp('{"placeholders": {"1": "x"}}')
    skill = ProposalDeckSkill(bedrock=bedrock)
    with pytest.raises(ValueError):
        skill.run(_input(template, tmp_path / "out", max_repair=1), ctx=SkillContext())
    assert bedrock.converse.call_count == 2


def test_default_request_directory_is_removed_by_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = _dummy_template(tmp_path / "t.pptx")
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp(_full_composer_json())
    skill = ProposalDeckSkill(bedrock=bedrock)

    def fake_render(
        _composer: ComposerOutput,
        _template: Path,
        out_path: Path,
        *,
        request_id: str,
    ) -> Path:
        assert request_id == "cleanup-request"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"pptx")
        return out_path

    monkeypatch.setattr(skill, "_render_pptx", fake_render)
    out = skill.run(
        ProposalDeckInput(
            product_name="ACME",
            goal="認知",
            target_persona="20代",
            template_path=str(template),
        ),
        ctx=SkillContext(request_id="cleanup-request"),
    )
    request_dir = Path(out.pptx_path).parent
    assert request_dir.name.startswith("teamagent-deck-cleanup-request-")
    assert Path(out.pptx_path).is_file()

    skill.cleanup_output(out)

    assert not request_dir.exists()
    assert skill._temporary_output_dirs == set()


def test_default_request_directory_is_removed_when_render_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = _dummy_template(tmp_path / "t.pptx")
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp(_full_composer_json())
    skill = ProposalDeckSkill(bedrock=bedrock)
    request_dir: Path | None = None

    def fail_render(
        _composer: ComposerOutput,
        _template: Path,
        out_path: Path,
        *,
        request_id: str,
    ) -> Path:
        nonlocal request_dir
        del request_id
        request_dir = out_path.parent
        raise RuntimeError("render failed")

    monkeypatch.setattr(skill, "_render_pptx", fail_render)
    with pytest.raises(RuntimeError, match="render failed"):
        skill.run(
            ProposalDeckInput(
                product_name="ACME",
                goal="認知",
                target_persona="20代",
                template_path=str(template),
            ),
            ctx=SkillContext(request_id="failed-request"),
        )

    assert request_dir is not None
    assert not request_dir.exists()
    assert skill._temporary_output_dirs == set()


def test_publish_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wave3-⑨: USE_PROPOSAL_DECK_PUBLISH 未設定なら pptx_url=None・publish_pptx_file は呼ばれない."""
    from unittest.mock import patch

    monkeypatch.delenv("USE_PROPOSAL_DECK_PUBLISH", raising=False)
    template = _dummy_template(tmp_path / "t.pptx")
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp(_full_composer_json())

    skill = ProposalDeckSkill(bedrock=bedrock)
    with patch("teamagent.adapters.report_publish.publish_pptx_file") as mock_pub:
        out = skill.run(_input(template, tmp_path / "out"), ctx=SkillContext())
    assert out.pptx_url is None
    mock_pub.assert_not_called()


def test_publish_enabled_returns_signed_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wave3-⑨: USE_PROPOSAL_DECK_PUBLISH=1 で publish_pptx_file を呼んで URL を載せる."""
    from unittest.mock import patch

    monkeypatch.setenv("USE_PROPOSAL_DECK_PUBLISH", "1")
    template = _dummy_template(tmp_path / "t.pptx")
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp(_full_composer_json())

    fake_url = "https://example.s3.ap-northeast-1.amazonaws.com/proposal_deck/x.pptx?sig=..."
    skill = ProposalDeckSkill(bedrock=bedrock)
    with patch(
        "teamagent.adapters.report_publish.publish_pptx_file", return_value=fake_url
    ) as mock_pub:
        out = skill.run(_input(template, tmp_path / "out"), ctx=SkillContext())
    assert out.pptx_url == fake_url
    mock_pub.assert_called_once()
    kwargs = mock_pub.call_args.kwargs
    assert kwargs.get("query") == "ACME 青汁"


def test_publish_failure_falls_back_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wave3-⑨: publish_pptx_file が例外を投げても skill は成功扱い (pptx_url=None)."""
    from unittest.mock import patch

    monkeypatch.setenv("USE_PROPOSAL_DECK_PUBLISH", "true")
    template = _dummy_template(tmp_path / "t.pptx")
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp(_full_composer_json())

    skill = ProposalDeckSkill(bedrock=bedrock)
    with patch(
        "teamagent.adapters.report_publish.publish_pptx_file",
        side_effect=RuntimeError("S3 down"),
    ):
        out = skill.run(_input(template, tmp_path / "out"), ctx=SkillContext())
    assert out.pptx_url is None
    assert out.coverage_ratio == 1.0  # skill 自体は成功


def test_prompt_v2_assigns_two_ids_per_short_video_plan() -> None:
    """2026-09-16 本番: モデルが案A〜Dを {96}〜{99} に 1 枠ずつ書き {100}〜{103} が未被覆で 5 回失敗。
    プロンプトが案ごとの 2 ID 割りを明示していることを固定する。"""
    from teamagent.prompts.loader import load_prompt

    system = load_prompt("proposal_deck", "v2", "system")
    for pair in (
        "`{96}`/`{97}`=案A",
        "`{98}`/`{99}`=案B",
        "`{100}`/`{101}`=案C",
        "`{102}`/`{103}`=案D",
    ):
        assert pair in system
    assert "8IDすべてを埋め" in system


def test_exhausted_repair_logs_error_summary_without_model_text(tmp_path: Path) -> None:
    """失敗理由の要約は warning ログに残り、pydantic の input_value（モデル出力本文）は含めない。"""
    template = _dummy_template(tmp_path / "t.pptx")
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp('{"placeholders": {"1": "SECRET-MODEL-TEXT"}}')
    skill = ProposalDeckSkill(bedrock=bedrock)
    ctx = SkillContext()
    logger = MagicMock()
    ctx.bind_logger = lambda _name: logger  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        skill.run(_input(template, tmp_path / "out", max_repair=0), ctx=ctx)
    calls = [
        c
        for c in logger.warning.call_args_list
        if c.args and c.args[0] == "proposal_deck_compose_failed"
    ]
    assert len(calls) == 1
    kwargs = calls[0].kwargs
    assert kwargs["attempts"] == 1
    assert "uncovered placeholders" in kwargs["error_summary"]
    assert "SECRET-MODEL-TEXT" not in kwargs["error_summary"]
    assert "[type=" not in kwargs["error_summary"]


def test_merged_ids_48_to_55_in_model_output_are_dropped_before_validation(tmp_path: Path) -> None:
    """モデルが {48}〜{55} を skipped（または placeholders）へ書いても repair を浪費せず通す（2026-09-16 実走）。"""
    import json

    template = _dummy_template(tmp_path / "t.pptx")
    payload = json.loads(_full_composer_json())
    payload.setdefault("skipped_placeholders", []).append(
        {"id": 50, "reason": "出力対象外（{47}に統合済み）"}
    )
    payload["placeholders"]["49"] = "統合済みのはずの本文"
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp(json.dumps(payload, ensure_ascii=False))
    skill = ProposalDeckSkill(bedrock=bedrock)
    out = skill.run(_input(template, tmp_path / "out", max_repair=0), ctx=SkillContext())
    assert bedrock.converse.call_count == 1
    assert out.coverage_ratio == 1.0
