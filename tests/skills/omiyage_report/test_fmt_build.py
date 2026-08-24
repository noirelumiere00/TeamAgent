"""render_fmt_deck / build_image_pptx / 配信文の検証。"""

from __future__ import annotations

from typing import Any

import pytest

import teamagent.skills.omiyage_report.fmt.build as build_module
from teamagent.adapters.media_job import MediaJobClient
from teamagent.skills.omiyage_report.fmt.build import (
    REVISION_NOTE,
    FmtRenderError,
    build_delivery_comment,
    build_image_pptx,
    render_fmt_deck,
)
from teamagent.skills.omiyage_report.fmt.editable import EDIT_MARKER

from .fmt_fixtures import make_deck_content


def test_artifacts_have_paired_filenames_and_labels() -> None:
    artifacts = render_fmt_deck(make_deck_content(), generated_on="2026-08-24")
    assert artifacts.image_filename == "omiyage_fmt_2026-08-24.pptx"
    assert artifacts.editable_filename == f"omiyage_fmt_2026-08-24_{EDIT_MARKER}.pptx"
    assert len(artifacts.slide_labels) == 9
    assert artifacts.slide_labels[0] == "01 A"
    assert artifacts.slide_labels[-1].startswith("09 H 総括")
    assert artifacts.editable_pptx[:2] == b"PK"


def test_html_size_gate_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_module, "_MAX_HTML_BYTES", 1024)
    with pytest.raises(FmtRenderError, match="exceeds media worker limit"):
        render_fmt_deck(make_deck_content(), generated_on="2026-08-24")


def test_build_image_pptx_requests_1920x1080_scale1(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_slides_to_pptx(self: MediaJobClient, html: str, **kwargs: Any) -> bytes:
        captured.update(kwargs, html=html)
        return b"PPTX"

    monkeypatch.setattr(MediaJobClient, "is_configured", staticmethod(lambda: True))
    monkeypatch.setattr(MediaJobClient, "__init__", lambda self: None)
    monkeypatch.setattr(MediaJobClient, "slides_to_pptx", fake_slides_to_pptx)

    body = build_image_pptx("<html></html>", request_fingerprint="req:omiyage-fmt")
    assert body == b"PPTX"
    assert captured["width"] == 1920
    assert captured["height"] == 1080
    assert captured["device_scale_factor"] == 1
    assert captured["request_fingerprint"] == "req:omiyage-fmt"


def test_delivery_comment_order_and_fixed_note() -> None:
    comment = build_delivery_comment(
        ["要点1", "要点2", "要点3"],
        "次の一手: 詳細解剖版もこのスレッドで依頼できます。",
    )
    lines = comment.splitlines()
    assert lines[:3] == ["要点1", "要点2", "要点3"]
    assert lines[3] == REVISION_NOTE
    assert "修正はこのスレッドで再依頼" in REVISION_NOTE
    assert lines[4].startswith("次の一手")
