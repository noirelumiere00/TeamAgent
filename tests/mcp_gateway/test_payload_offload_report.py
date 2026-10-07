"""長文退避の返却物: full_url は /r 短縮リンクだけ・署名付き S3 URL はモデルへ渡さない（P8①）。

退避先の presigned URL は STS 一時認証で署名され 30 分前後で失効し、OpenClaw はクエリ付き
長 URL を壊す（%2B→空白）。漏洩リスクだけあって使い物にならないので、短縮リンクが作れない
ときは full_url を出さない（fail-closed）。HTML レポートがある結果でも .json 退避はやめない
（二重保存ではなく、表に出ない実数値の正本を残す）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from structlog.testing import capture_logs

from teamagent.adapters.report_publish import PublishedObject
from teamagent.mcp_gateway import payload_offload

_BUCKET = "teamagent-dev-raw-files"
_KEY = "payload-offload/0123456789abcdef0123456789abcdef.json"
# 本番と同じ形: STS 一時認証（X-Amz-Security-Token）で署名された SigV4 presigned。
_PRESIGNED = (
    f"https://{_BUCKET}.s3.ap-northeast-1.amazonaws.com/{_KEY}"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=ASIAEXAMPLE%2F20261007%2Fap-northeast-1"
    "%2Fs3%2Faws4_request&X-Amz-Date=20261007T000000Z&X-Amz-Expires=604800"
    "&X-Amz-SignedHeaders=host&X-Amz-Security-Token=IQoJb3JpZ2luX2VjEBw%2BTOKEN"
    "&X-Amz-Signature=deadbeef"
)
_REPORT_SECRET = "report-offload-secret-" + "r" * 32
_SHORTLINK_ENVS = (
    "USE_REPORT_SHORTURL",
    "CONNECT_BASE_URL",
    "REPORT_LINK_HMAC_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY",
    "REPORT_LINK_TTL_S",
    "MAIL_ACTION_HMAC_SECRET",
    "VSEO_REPORT_BUCKET",
    "PAYLOAD_OFFLOAD_BUCKET",
    "PAYLOAD_OFFLOAD_PREFIX",
)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_PAYLOAD_OFFLOAD", "true")
    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", "example.co.jp")
    monkeypatch.setenv("PAYLOAD_OFFLOAD_MAX_CHARS", "200")
    for name in _SHORTLINK_ENVS:
        monkeypatch.delenv(name, raising=False)


def _shortlink_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """本番 mcp と同じ前提（USE_REPORT_SHORTURL・CONNECT_BASE_URL・専用 HMAC 鍵）を揃える。"""
    monkeypatch.setenv("USE_REPORT_SHORTURL", "1")
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example")
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _REPORT_SECRET)


def _big(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"videos": [{"desc": "あ" * 200} for _ in range(5)]}
    data.update(extra or {})
    return data


class _PublishSpy:
    """publish_text_result のフェイク。本番どおり presigned 入りの PublishedObject を返す。"""

    def __init__(self, obj: PublishedObject | None) -> None:
        self.calls = 0
        self.kwargs: list[dict[str, Any]] = []
        self.obj = obj

    def __call__(self, *_: Any, **kw: Any) -> PublishedObject | None:
        self.calls += 1
        self.kwargs.append(kw)
        return self.obj


def _install(
    monkeypatch: pytest.MonkeyPatch, *, bucket: str = _BUCKET, key: str = _KEY
) -> _PublishSpy:
    spy = _PublishSpy(
        PublishedObject(url=_PRESIGNED, bucket=bucket, key=key, region="ap-northeast-1")
    )
    monkeypatch.setattr("teamagent.adapters.report_publish.publish_text_result", spy, raising=True)
    return spy


def _assert_no_presigned(out: dict[str, Any]) -> None:
    rendered = json.dumps(out, ensure_ascii=False, default=str)
    assert "X-Amz-" not in rendered
    assert "amazonaws.com" not in rendered.lower()
    assert "X-Amz-Security-Token" not in rendered


# ── 本命: full_url は短縮リンクだけ ──────────────────────────────────────────


def test_full_url_is_short_link_without_any_presigned_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前提充足: full_url は /r/<token>（クエリ無し）。出力のどこにも署名付き URL が無い。"""
    _shortlink_ready(monkeypatch)
    spy = _install(monkeypatch)
    out = payload_offload.maybe_offload("tiktok_search", _big(), request_id="r")
    assert spy.calls == 1
    assert out["offloaded"] is True
    assert out["full_url"].startswith("https://connect.example/r/")
    assert "?" not in out["full_url"]
    _assert_no_presigned(out)


def test_short_link_token_resolves_to_the_offloaded_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """発行した token は connect-web の /r と同じ decode で bucket/key に戻る（404 にならない）。"""
    from teamagent.adapters.report_link_token import decode_report_token

    _shortlink_ready(monkeypatch)
    _install(monkeypatch)
    out = payload_offload.maybe_offload("tiktok_search", _big(), request_id="r")
    token = out["full_url"].rsplit("/r/", 1)[1]
    assert decode_report_token(token) == (_BUCKET, _KEY, "ap-northeast-1")


def test_offload_uses_payload_offload_prefix_and_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """退避先は payload-offload/（allowlist 済み prefix）と PAYLOAD_OFFLOAD_BUCKET。"""
    _shortlink_ready(monkeypatch)
    monkeypatch.setenv("PAYLOAD_OFFLOAD_BUCKET", _BUCKET)
    spy = _install(monkeypatch)
    payload_offload.maybe_offload("tiktok_search", _big(), request_id="r")
    assert spy.kwargs[0]["prefix"] == "payload-offload/"
    assert spy.kwargs[0]["bucket"] == _BUCKET


# ── fail-closed: 短縮リンクが作れないなら full_url を出さない ─────────────────


def test_no_short_link_prereqs_means_no_full_url_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """USE_REPORT_SHORTURL 無効: 退避と切り詰めは行うが full_url キー自体を出さない。"""
    spy = _install(monkeypatch)
    with capture_logs() as logs:
        out = payload_offload.maybe_offload("tiktok_search", _big(), request_id="r")
    assert spy.calls == 1
    assert out["offloaded"] is True
    assert "full_url" not in out
    _assert_no_presigned(out)
    assert any(e["event"] == "payload_offload_no_short_url" for e in logs)
    assert "リンクは発行できなかった" in out["offload_note"]


@pytest.mark.parametrize("missing", ["CONNECT_BASE_URL", "REPORT_LINK_HMAC_SECRET"])
def test_flag_on_but_prereq_missing_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """フラグ ON でも鍵/BASE_URL が欠けたら presigned へ落とさず full_url 無し。"""
    _shortlink_ready(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    _install(monkeypatch)
    out = payload_offload.maybe_offload("tiktok_search", _big(), request_id="r")
    assert "full_url" not in out
    _assert_no_presigned(out)


def test_custom_prefix_outside_allowlist_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAYLOAD_OFFLOAD_PREFIX を allowlist 外に変えると token が decode できない＝出さない。"""
    _shortlink_ready(monkeypatch)
    monkeypatch.setenv("PAYLOAD_OFFLOAD_PREFIX", "custom-offload/")
    _install(monkeypatch, key="custom-offload/abc.json")
    out = payload_offload.maybe_offload("tiktok_search", _big(), request_id="r")
    assert "full_url" not in out
    _assert_no_presigned(out)


def test_bucket_outside_allowlist_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAYLOAD_OFFLOAD_BUCKET が VSEO_REPORT_BUCKET と違うと /r が 404 になるので出さない。"""
    _shortlink_ready(monkeypatch)
    _install(monkeypatch, bucket="other-bucket")
    out = payload_offload.maybe_offload("tiktok_search", _big(), request_id="r")
    assert "full_url" not in out
    _assert_no_presigned(out)


def test_encode_returning_none_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _shortlink_ready(monkeypatch)
    _install(monkeypatch)
    monkeypatch.setattr(
        "teamagent.adapters.report_link_token.encode_report_token", lambda *a, **k: None
    )
    out = payload_offload.maybe_offload("tiktok_search", _big(), request_id="r")
    assert "full_url" not in out
    _assert_no_presigned(out)


def test_presigned_slipping_through_short_url_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """多層防御: 短縮 URL 化が将来 presigned を返す改変を受けても最後の門で止める。"""
    _shortlink_ready(monkeypatch)
    _install(monkeypatch)
    monkeypatch.setattr(
        "teamagent.skills._shared.report_delivery.short_url_or_none",
        lambda result, *, request_id: result.url,
    )
    out = payload_offload.maybe_offload("tiktok_search", _big(), request_id="r")
    assert "full_url" not in out
    _assert_no_presigned(out)


# ── HTML レポート併存時の挙動（従来どおり）────────────────────────────────────


def test_report_url_still_keeps_a_lossless_json_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTML レポートがあっても生JSONの退避はやめない。

    レポートは人が読む用で、表に出ない実数値（いいね/コメント/シェア/タグ/説明全文）は
    落ちている。切り詰めで消えた値の復元先が無くなるため、全文の正本は JSON のまま残す。
    """
    _shortlink_ready(monkeypatch)
    spy = _install(monkeypatch)
    out = payload_offload.maybe_offload(
        "tiktok_search", _big({"report_url": "https://connect.example/r/abc"}), request_id="r"
    )
    assert spy.calls == 1
    assert out["full_url"].startswith("https://connect.example/r/")
    assert out["offloaded"] is True
    _assert_no_presigned(out)


def test_note_tells_which_link_to_show_a_human(monkeypatch: pytest.MonkeyPatch) -> None:
    _shortlink_ready(monkeypatch)
    _install(monkeypatch)
    out = payload_offload.maybe_offload(
        "tiktok_search", _big({"report_url": "https://connect.example/r/abc"}), request_id="r"
    )
    assert "report_url" in out["offload_note"]
    assert "人へ渡さない" in out["offload_note"]
    assert "社外共有不可" in out["offload_note"]
    assert out["report_url"] == "https://connect.example/r/abc"


def test_without_report_url_the_note_is_the_plain_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _shortlink_ready(monkeypatch)
    spy = _install(monkeypatch)
    out = payload_offload.maybe_offload("tiktok_search", _big({"report_url": ""}), request_id="r")
    assert spy.calls == 1
    assert "report_url" not in out["offload_note"]
    assert "短縮リンク" in out["offload_note"]


def test_short_payload_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    data = {"report_url": "https://connect.example/r/abc", "videos": []}
    assert payload_offload.maybe_offload("tiktok_search", data, request_id="r") == data
