"""HTML レポートがある結果は .json 退避しない（二重保存と壊れたリンクの回避）。"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.mcp_gateway import payload_offload


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_PAYLOAD_OFFLOAD", "true")
    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", "example.co.jp")
    monkeypatch.setenv("PAYLOAD_OFFLOAD_MAX_CHARS", "200")


def _big(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"videos": [{"desc": "あ" * 200} for _ in range(5)]}
    data.update(extra or {})
    return data


class _PublishSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_: Any, **__: Any) -> str:
        self.calls += 1
        return "https://s3.example/offloaded.json"


def _install(monkeypatch: pytest.MonkeyPatch) -> _PublishSpy:
    spy = _PublishSpy()
    monkeypatch.setattr("teamagent.adapters.report_publish.publish_text", spy, raising=True)
    return spy


def test_report_url_still_keeps_a_lossless_json_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTML レポートがあっても生JSONの退避はやめない。

    レポートは人が読む用で、表に出ない実数値（いいね/コメント/シェア/タグ/説明全文）は
    落ちている。切り詰めで消えた値の復元先が無くなるため、全文の正本は JSON のまま残す。
    """
    spy = _install(monkeypatch)
    out = payload_offload.maybe_offload(
        "tiktok_search", _big({"report_url": "https://connect.example/r/abc"}), request_id="r"
    )
    assert spy.calls == 1
    assert out["full_url"] == "https://s3.example/offloaded.json"
    assert out["offloaded"] is True


def test_note_tells_which_link_to_show_a_human(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    out = payload_offload.maybe_offload(
        "tiktok_search", _big({"report_url": "https://connect.example/r/abc"}), request_id="r"
    )
    assert "report_url" in out["offload_note"]
    assert "人へ渡さない" in out["offload_note"]
    assert out["report_url"] == "https://connect.example/r/abc"


def test_without_report_url_falls_back_to_json_offload(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _install(monkeypatch)
    out = payload_offload.maybe_offload("tiktok_search", _big(), request_id="r")
    assert spy.calls == 1
    assert out["full_url"] == "https://s3.example/offloaded.json"


def test_without_report_url_the_note_is_the_plain_one(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _install(monkeypatch)
    out = payload_offload.maybe_offload("tiktok_search", _big({"report_url": ""}), request_id="r")
    assert spy.calls == 1
    assert "report_url" not in out["offload_note"]


def test_short_payload_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    data = {"report_url": "https://connect.example/r/abc", "videos": []}
    assert payload_offload.maybe_offload("tiktok_search", data, request_id="r") == data
