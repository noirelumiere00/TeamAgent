"""core の n_per_kw 上限（TIKTOK_N_PER_KW_MAX）と dispatcher Lambda の受理上限の一致契約。

2026-09-02 本番: お土産資料が n_per_kw=120 を送り、dispatcher Lambda
（infra/terraform/lambda/tiktok_dispatch/handler.py）の ``maximum=30`` で全軸
TIKTOK_MEDIA_JOB_FAILED（TikTok n_per_kw is invalid）。core 側の単一情報源と Lambda の
定数がずれた瞬間に赤くなるよう、Lambda コードは import せずテキストとして読んで照合する
（Lambda は boto3 client 生成など import 時副作用を持つため）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.media_job import MediaJobClient
from teamagent.media.contracts import TIKTOK_N_PER_KW_MAX, TikTokAcquireOperation

_DISPATCHER = (
    Path(__file__).parents[2] / "infra" / "terraform" / "lambda" / "tiktok_dispatch" / "handler.py"
)
# _bounded_int(operation["n_per_kw"], minimum=1, maximum=30, name="TikTok n_per_kw")
_N_PER_KW_BOUND = re.compile(
    r'_bounded_int\(\s*operation\["n_per_kw"\]\s*,\s*minimum=(?P<minimum>\d+)\s*,'
    r'\s*maximum=(?P<maximum>\d+)\s*,\s*name="TikTok n_per_kw"\s*,?\s*\)'
)


def _dispatcher_n_per_kw_bounds() -> tuple[int, int]:
    source = _DISPATCHER.read_text(encoding="utf-8")
    matches = _N_PER_KW_BOUND.findall(source)
    assert len(matches) == 1, (
        'dispatcher の n_per_kw 検証（_bounded_int(operation["n_per_kw"], ...)）が'
        f" 1 箇所でない: {len(matches)} 箇所"
    )
    minimum, maximum = matches[0]
    return int(minimum), int(maximum)


def test_dispatcher_n_per_kw_maximum_matches_core_contract() -> None:
    minimum, maximum = _dispatcher_n_per_kw_bounds()
    assert minimum == 1
    assert maximum == TIKTOK_N_PER_KW_MAX, (
        f"dispatcher handler.py maximum={maximum} と core の TIKTOK_N_PER_KW_MAX="
        f"{TIKTOK_N_PER_KW_MAX} が食い違う。上げ下げは同じ PR で両方を動かすこと"
    )


def test_core_limit_is_within_contract_model_range() -> None:
    # 契約モデル（le=120・深掘り実装の余地）より dispatcher 上限は狭い側にある。
    operation = TikTokAcquireOperation(
        kind="tiktok_acquire",
        keywords=("シャンプー",),
        n_per_kw=TIKTOK_N_PER_KW_MAX,
        videos_per_kw=0,
        artifact_mode="metadata_only",
    )
    assert operation.n_per_kw == TIKTOK_N_PER_KW_MAX


def _client_that_must_not_send(monkeypatch: pytest.MonkeyPatch) -> MediaJobClient:
    monkeypatch.setattr(MediaJobClient, "__init__", lambda self: None)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("n_per_kw guard must reject before building/sending a job")

    monkeypatch.setattr(MediaJobClient, "_absolute_deadline", _boom)
    monkeypatch.setattr(MediaJobClient, "_request", _boom)
    monkeypatch.setattr(MediaJobClient, "run_sync", _boom)
    return MediaJobClient()


@pytest.mark.parametrize("max_videos", [TIKTOK_N_PER_KW_MAX + 1, 120, 0, -1])
def test_search_tiktok_rejects_out_of_range_n_per_kw_before_sending(
    monkeypatch: pytest.MonkeyPatch, max_videos: int
) -> None:
    client = _client_that_must_not_send(monkeypatch)
    with pytest.raises(ValueError, match=rf"n_per_kw={max_videos} .*1\.\.{TIKTOK_N_PER_KW_MAX}"):
        client.search_tiktok(
            "シャンプー",
            request_fingerprint="tiktok-search:test",
            max_videos=max_videos,
        )


def test_search_tiktok_passes_limit_value_through(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[TikTokAcquireOperation] = []
    monkeypatch.setattr(MediaJobClient, "__init__", lambda self: None)
    monkeypatch.setattr(MediaJobClient, "_absolute_deadline", lambda self, timeout_s: 0)

    def _capture(self: MediaJobClient, operation: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        assert isinstance(operation, TikTokAcquireOperation)
        sent.append(operation)
        return {"operation": operation}

    def _run_sync(self: MediaJobClient, request: Any, *, timeout_s: int) -> tuple[Any, Any]:
        return {"posts.json": b'{"posts": [{"id": "1"}]}'}, {}

    monkeypatch.setattr(MediaJobClient, "_request", _capture)
    monkeypatch.setattr(MediaJobClient, "run_sync", _run_sync)

    posts = MediaJobClient().search_tiktok(
        "シャンプー",
        request_fingerprint="tiktok-search:test",
        max_videos=TIKTOK_N_PER_KW_MAX,
    )
    assert posts == [{"id": "1"}]
    assert [operation.n_per_kw for operation in sent] == [TIKTOK_N_PER_KW_MAX]
