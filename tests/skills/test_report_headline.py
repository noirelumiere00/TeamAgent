"""レポート見出し生成（Bedrock・上限つき）のテスト。

見出しは装飾なので、**採用しない判断が安全側**であることを固定する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from teamagent.skills._html import headline as hl
from teamagent.skills._html.report import Report, render_report
from teamagent.skills._shared import report_html


@dataclass
class _Resp:
    text: str


class _FakeBedrock:
    """converse の最小スタブ。渡された引数を検証できるよう記録する。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.kwargs: dict[str, Any] = {}
        self.messages: list[dict[str, Any]] = []

    def converse(self, messages: list[dict[str, Any]], request_id: str, **kw: Any) -> _Resp:
        self.messages = messages
        self.kwargs = kw
        return _Resp(self.text)


@pytest.fixture(autouse=True)
def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_HTML_REPORT_HEADLINE", "1")


def _make(text: str, body: str = "### サマリ\n包まない系が伸びている") -> str | None:
    return hl.make_headline(body, bedrock=_FakeBedrock(text), request_id="r")


class TestAccept:
    def test_plain_line_is_accepted(self) -> None:
        assert _make("包まないほど伸びている") == "包まないほど伸びている"

    def test_only_first_line_is_used(self) -> None:
        assert _make("包まないほど伸びている\n（他の案）別案") == "包まないほど伸びている"


class TestReject:
    def test_too_long_is_rejected(self) -> None:
        assert _make("あ" * 41) is None

    def test_url_is_rejected(self) -> None:
        assert _make("詳細は https://example.com を参照") is None

    def test_html_is_rejected(self) -> None:
        assert _make("<b>包まない</b>") is None

    def test_preamble_is_rejected(self) -> None:
        assert _make("見出し: 包まないほど伸びている") is None

    def test_empty_is_rejected(self) -> None:
        assert _make("   ") is None

    def test_empty_body_skips_the_call(self) -> None:
        assert hl.make_headline("", bedrock=_FakeBedrock("x"), request_id="r") is None


class TestLimits:
    def test_flag_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("USE_HTML_REPORT_HEADLINE", raising=False)
        assert _make("包まないほど伸びている") is None

    def test_input_is_truncated(self) -> None:
        fake = _FakeBedrock("見出しの案")
        hl.make_headline("あ" * 5000, bedrock=fake, request_id="r")
        sent = fake.messages[0]["content"][0]["text"]
        assert len(sent) == hl._INPUT_MAX

    def test_generation_params_are_bounded(self) -> None:
        fake = _FakeBedrock("見出しの案")
        hl.make_headline("本文", bedrock=fake, request_id="r")
        assert fake.kwargs["max_tokens"] == 80
        assert fake.kwargs["temperature"] == 0.0

    def test_exception_is_swallowed(self) -> None:
        class _Boom:
            def converse(self, *a: Any, **k: Any) -> _Resp:
                raise RuntimeError("bedrock down")

        assert hl.make_headline("本文", bedrock=_Boom(), request_id="r") is None

    def test_missing_client_returns_none(self) -> None:
        assert hl.make_headline("本文", bedrock=None, request_id="r") is None


class TestRendering:
    def test_headline_is_rendered_above_subtitle(self) -> None:
        html = render_report(
            Report(title="T", headline="包まないほど伸びている", subtitle="補足の一行")
        )
        assert "class='lead'" in html
        assert html.index("包まないほど伸びている") < html.index("補足の一行")

    def test_no_headline_renders_no_lead(self) -> None:
        assert "class='lead'" not in render_report(Report(title="T"))

    def test_headline_is_escaped(self) -> None:
        html = render_report(Report(title="T", headline="<script>x</script>"))
        assert "<script>" not in html


class TestWiring:
    def test_publish_attaches_headline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeBedrock("包まないほど伸びている")
        monkeypatch.setattr(
            "teamagent.adapters.bedrock_client.BedrockClient.from_env",
            classmethod(lambda cls, **kw: fake),
        )
        out = report_html._with_headline(
            Report(title="T", body_md="本文"), request_id="r", tool="tiktok_search"
        )
        assert out.headline == "包まないほど伸びている"

    def test_client_failure_keeps_report(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(cls: Any, **kw: Any) -> Any:
            raise RuntimeError("no creds")

        monkeypatch.setattr(
            "teamagent.adapters.bedrock_client.BedrockClient.from_env", classmethod(boom)
        )
        report = Report(title="T", body_md="本文")
        assert report_html._with_headline(report, request_id="r", tool="t") is report
