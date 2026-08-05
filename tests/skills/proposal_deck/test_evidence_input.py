"""ProposalDeckInput から local/worker renderer までの証拠画像配線。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_deck.contract import (
    LENGTH_RULES,
    VALID_IDS,
    ComposerOutput,
    EvidenceImage,
)
from teamagent.skills.proposal_deck.schema import ProposalDeckInput
from teamagent.skills.proposal_deck.skill import ProposalDeckSkill


def _full_composer_json(*, evidence_images: dict[int, list[EvidenceImage]] | None = None) -> str:
    placeholders = {
        placeholder_id: (
            "サ" * (sum(LENGTH_RULES[placeholder_id]) // 2)
            if placeholder_id in LENGTH_RULES
            else f"値-{placeholder_id}"
        )
        for placeholder_id in sorted(VALID_IDS)
    }
    return ComposerOutput(
        placeholders=placeholders,
        evidence_images=evidence_images or {},
    ).model_dump_json()


def _image(placeholder_id: int, image_path: str) -> EvidenceImage:
    return EvidenceImage(
        placeholder_id=placeholder_id,
        rank=1,
        keyword="集中",
        image_path=image_path,
        video_url="https://www.tiktok.com/@creator/video/evidence",
    )


def test_compose_deterministically_overrides_model_evidence(tmp_path: Path) -> None:
    input_image = _image(58, str(tmp_path / "input.jpg"))
    model_payload = json.loads(_full_composer_json())
    model_payload["evidence_images"] = {
        "48": [
            {
                "placeholder_id": 48,
                "rank": 1,
                "keyword": "モデル捏造",
                "image_path": str(tmp_path / "model.jpg"),
            }
        ]
    }
    bedrock = MagicMock()
    bedrock.converse.return_value = SimpleNamespace(
        text=json.dumps(model_payload, ensure_ascii=False),
        usage=SimpleNamespace(cost_usd=0.0),
    )
    skill = ProposalDeckSkill(bedrock=bedrock)

    composer, _cost = skill._compose(
        ProposalDeckInput(
            product_name="商品",
            goal="認知",
            target_persona="生活者",
            evidence_images={58: [input_image]},
            max_repair=0,
        ),
        SkillContext(request_id="compose-evidence"),
    )

    assert composer.evidence_images == {58: [input_image]}


def test_input_rejects_mismatched_evidence_key(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=r"does not match image\.placeholder_id"):
        ProposalDeckInput(
            product_name="商品",
            goal="認知",
            target_persona="生活者",
            evidence_images={60: [_image(58, str(tmp_path / "image.jpg"))]},
        )


def test_render_filter_rejects_non_campaign_local_path(tmp_path: Path) -> None:
    image_path = tmp_path / "unowned.jpg"
    image_path.write_bytes(b"\xff\xd8\xffimage")
    image = _image(58, str(image_path))
    composer = ComposerOutput.model_construct(placeholders={}, evidence_images={58: [image]})

    filtered = ProposalDeckSkill._filter_render_evidence(composer, "request")

    assert filtered.evidence_images == {}


def test_local_render_enables_existing_image_injector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LocalMediaClient:
        @classmethod
        def is_configured(cls) -> bool:
            return False

        @classmethod
        def local_runtime_enabled(cls) -> bool:
            return True

        @classmethod
        def require_configured(cls) -> None:
            raise AssertionError("configured media path must not be used")

    calls: dict[str, object] = {}

    def fake_render(
        composer: ComposerOutput,
        template: Path,
        output: Path,
        *,
        enable_images: bool,
    ) -> Path:
        calls.update(
            composer=composer,
            template=template,
            output=output,
            enable_images=enable_images,
        )
        return output

    monkeypatch.setattr("teamagent.adapters.media_job.MediaJobClient", LocalMediaClient)
    monkeypatch.setattr("teamagent.skills.proposal_deck.renderer.render_deck", fake_render)
    campaign_dir = tmp_path / "teamagent-campaign-local-evidence-owned"
    campaign_dir.mkdir()
    image_path = campaign_dir / "image.jpg"
    image_path.write_bytes(b"\xff\xd8\xffimage")
    image = _image(58, str(image_path))
    composer = ComposerOutput.model_construct(placeholders={}, evidence_images={58: [image]})

    output = ProposalDeckSkill._render_pptx(
        composer,
        tmp_path / "template.pptx",
        tmp_path / "output.pptx",
        request_id="local-evidence",
    )

    assert output == tmp_path / "output.pptx"
    assert calls["enable_images"] is True


def test_worker_render_stages_evidence_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class WorkerMediaClient:
        @classmethod
        def is_configured(cls) -> bool:
            return True

        @classmethod
        def local_runtime_enabled(cls) -> bool:
            return False

        @classmethod
        def require_configured(cls) -> None:
            return None

        def render_proposal_pptx(
            self,
            template: bytes,
            composer_json: bytes,
            *,
            request_fingerprint: str,
            evidence_images: list[tuple[int, int, bytes, str]],
        ) -> bytes:
            captured.update(
                template=template,
                composer_json=composer_json,
                request_fingerprint=request_fingerprint,
                evidence_images=evidence_images,
            )
            return b"rendered-pptx"

    monkeypatch.setattr("teamagent.adapters.media_job.MediaJobClient", WorkerMediaClient)
    template = tmp_path / "template.pptx"
    template.write_bytes(b"template")
    campaign_dir = tmp_path / "teamagent-campaign-worker-evidence-owned"
    campaign_dir.mkdir()
    image_path = campaign_dir / "image.png"
    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    image_path.write_bytes(image_bytes)
    image = _image(58, str(image_path))
    composer = ComposerOutput.model_construct(placeholders={}, evidence_images={58: [image]})

    output = ProposalDeckSkill._render_pptx(
        composer,
        template,
        tmp_path / "output.pptx",
        request_id="worker-evidence",
    )

    assert output.read_bytes() == b"rendered-pptx"
    assert captured["request_fingerprint"] == "worker-evidence:proposal-pptx"
    assert captured["evidence_images"] == [(58, 1, image_bytes, "image/png")]
