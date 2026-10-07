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


def test_publish_text_result_goes_through_put_and_presign() -> None:
    """publish_text(_result) は独自 put+presign を持たず _put_and_presign に寄せる（P8①）。

    bucket/key/region が返らないと /r 短縮リンクにできず、presigned を渡すしかなくなる。
    """
    obj = report_publish.PublishedObject(
        url="https://b.s3.amazonaws.com/payload-offload/x.json?X-Amz-Signature=s",
        bucket="b",
        key="payload-offload/x.json",
        region="ap-northeast-1",
    )
    with patch.object(report_publish, "_put_and_presign", return_value=obj) as mock:
        got = report_publish.publish_text_result(
            '{"a": 1}', prefix="payload-offload/", bucket="b", request_id="rid"
        )
    assert got is obj
    kwargs = mock.call_args.kwargs
    assert mock.call_args.args[0] == b'{"a": 1}'
    assert kwargs["content_type"].startswith("application/json")
    assert kwargs["ext"] == ".json"
    assert kwargs["prefix"] == "payload-offload/"
    assert kwargs["bucket"] == "b"
    assert kwargs["request_id"] == "rid"
    assert kwargs["query"] == ""  # 本文は CloudWatch に残さない


def test_publish_text_wrapper_returns_url_only() -> None:
    obj = report_publish.PublishedObject(url="https://s3/x?sig", bucket="b", key="k")
    with patch.object(report_publish, "_put_and_presign", return_value=obj):
        assert report_publish.publish_text("body") == "https://s3/x?sig"
    with patch.object(report_publish, "_put_and_presign", return_value=None):
        assert report_publish.publish_text("body") is None
    assert report_publish.publish_text_result("") is None  # 空文字は put しない
