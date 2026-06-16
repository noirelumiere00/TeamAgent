"""HTML-first 統合: report_publish.publish_artifact と _html.theme の単体検証。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from teamagent.adapters import report_publish
from teamagent.skills._html import theme


def test_artifact_kinds_mapping() -> None:
    kinds = report_publish.ARTIFACT_KINDS
    assert set(kinds) == {"report_html", "slides_html", "proposal_html", "pptx", "pdf"}
    assert kinds["slides_html"].ext == ".html"
    assert kinds["slides_html"].content_type.startswith("text/html")
    assert kinds["pptx"].ext == ".pptx"
    assert kinds["pdf"].content_type == "application/pdf"


def test_publish_artifact_delegates_with_kind_spec() -> None:
    with patch.object(report_publish, "publish_file", return_value="https://s3/x") as mock:
        url = report_publish.publish_artifact("/tmp/x.html", "slides_html", query="集中")
    assert url == "https://s3/x"
    kwargs = mock.call_args.kwargs
    assert kwargs["content_type"].startswith("text/html")
    assert kwargs["ext"] == ".html"
    assert kwargs["prefix"] == report_publish.ARTIFACT_KINDS["slides_html"].prefix
    assert kwargs["query"] == "集中"


def test_publish_artifact_unknown_kind_raises() -> None:
    with pytest.raises(ValueError):
        report_publish.publish_artifact("/tmp/x", "bogus")


def test_theme_constants() -> None:
    assert "sans-serif" in theme.FONT_STACK_JP
    assert "Hiragino" in theme.FONT_STACK_JP
    assert "contenteditable" in theme.CONTENTEDITABLE_CSS
    assert ":hover" in theme.CONTENTEDITABLE_CSS and ":focus" in theme.CONTENTEDITABLE_CSS
    assert "data-noexport" in theme.EDIT_TIP_HTML
