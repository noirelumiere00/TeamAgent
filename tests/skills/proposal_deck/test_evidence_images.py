"""ComposerOutput.evidence_images（Phase2 証拠画像メタ）の単体テスト。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from teamagent.skills.proposal_deck.contract import (
    LENGTH_RULES,
    VALID_IDS,
    ComposerOutput,
    EvidenceImage,
)


def _full_placeholders() -> dict[int, str]:
    """全 95 placeholder を文字数規則どおり埋めた dict。"""
    placeholders: dict[int, str] = {}
    for pid in sorted(VALID_IDS):
        if pid in LENGTH_RULES:
            lo, hi = LENGTH_RULES[pid]
            placeholders[pid] = "サ" * ((lo + hi) // 2)
        else:
            placeholders[pid] = f"値-{pid}"
    return placeholders


def _img(pid: int = 92, rank: int = 1) -> EvidenceImage:
    return EvidenceImage(
        placeholder_id=pid,
        rank=rank,
        keyword="ラーメン",
        source_url="https://cdn.example/x.jpg",
        image_path="/tmp/x.jpg",
        video_url="https://www.tiktok.com/@x/video/1",
    )


def test_evidence_images_defaults_empty() -> None:
    """後方互換: evidence_images 未指定で空 dict（既存 Bedrock 出力が壊れない）。"""
    out = ComposerOutput(placeholders=_full_placeholders())
    assert out.evidence_images == {}


def test_full_coverage_still_valid_with_evidence_images() -> None:
    """evidence_images があっても 95 枠被覆 validator は通る（直交性）。"""
    out = ComposerOutput(
        placeholders=_full_placeholders(),
        evidence_images={92: [_img(92, 1), _img(92, 2)]},
    )
    assert out.coverage_ratio == 1.0
    assert len(out.evidence_images[92]) == 2


def test_evidence_images_do_not_affect_coverage() -> None:
    """画像だけ載せても被覆は埋まらない（uncovered で fail する）ことを確認。"""
    with pytest.raises(ValidationError, match="uncovered placeholders"):
        ComposerOutput(
            placeholders={1: "x"},
            evidence_images={92: [_img(92, 1)]},
        )


def test_evidence_images_not_counted_in_length_rules() -> None:
    """LENGTH_RULES は placeholders text のみ検証。evidence_images は長さ検証されない。"""
    placeholders = _full_placeholders()
    # 56 は (300,700)。短い keyword の画像があっても 56 の length には無影響。
    out = ComposerOutput(
        placeholders=placeholders,
        evidence_images={56: [_img(56, 1)]},
    )
    assert 56 in out.placeholders


def test_evidence_images_invalid_key_rejected() -> None:
    """key が VALID_IDS 外（48 は欠番）なら fail。"""
    with pytest.raises(ValidationError, match="evidence_images reference invalid ids"):
        ComposerOutput(
            placeholders=_full_placeholders(),
            evidence_images={
                48: [EvidenceImage(placeholder_id=92, rank=1, keyword="k", image_path="/p")]
            },
        )


def test_evidence_images_key_must_match_image_pid() -> None:
    """dict key と image.placeholder_id の不一致は fail。"""
    with pytest.raises(ValidationError, match="does not match"):
        ComposerOutput(
            placeholders=_full_placeholders(),
            evidence_images={
                92: [EvidenceImage(placeholder_id=91, rank=1, keyword="k", image_path="/p")]
            },
        )


def test_evidence_image_invalid_placeholder_id() -> None:
    with pytest.raises(ValidationError, match="invalid placeholder id"):
        EvidenceImage(placeholder_id=48, rank=1, keyword="k", image_path="/p")


def test_evidence_image_requires_a_source() -> None:
    """source_url も image_path も無いと fail。"""
    with pytest.raises(ValidationError, match="requires source_url or image_path"):
        EvidenceImage(placeholder_id=92, rank=1, keyword="k")


def test_evidence_image_rank_ge_1() -> None:
    with pytest.raises(ValidationError):
        EvidenceImage(placeholder_id=92, rank=0, keyword="k", image_path="/p")


def test_evidence_image_keyword_non_empty() -> None:
    with pytest.raises(ValidationError):
        EvidenceImage(placeholder_id=92, rank=1, keyword="", image_path="/p")


def test_evidence_images_json_round_trip() -> None:
    """参照型のみ → model_dump_json / model_validate_json で完全往復（bytes 不使用の核心）。"""
    out = ComposerOutput(
        placeholders=_full_placeholders(),
        evidence_images={92: [_img(92, 1)]},
    )
    js = out.model_dump_json()
    back = ComposerOutput.model_validate_json(js)
    assert back.evidence_images[92][0].keyword == "ラーメン"
    assert back.evidence_images[92][0].source_url == "https://cdn.example/x.jpg"


def test_missing_evidence_images_field_uses_default() -> None:
    """JSON に evidence_images key が無い（既存 Bedrock 応答）→ default_factory で空 dict。"""
    out = ComposerOutput(placeholders=_full_placeholders())
    js_no_field = out.model_dump_json(exclude={"evidence_images"})
    back = ComposerOutput.model_validate_json(js_no_field)
    assert back.evidence_images == {}
