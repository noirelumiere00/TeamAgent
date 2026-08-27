"""MediaJobClient の boto3 Session/client プロセス内キャッシュ（C3）。

``MediaJobClient()`` は呼び出しのたびに新規生成されるため、キャッシュはモジュール
レベルでしか効かない。ここで固定するのは 4 つの不変量:
  1. Session はプロセスで 1 つ（インスタンスを跨いで共有）
  2. 同じ (service, region, deadline由来timeout) なら client も同一実体
  3. 残予算が違えば別 client（短い締切で長い timeout の client を掴まない）
  4. 注入 Session（テスト/呼び出し側の実体）は共有キャッシュへ一切混ざらない
  5. キーが増え続けても辞書は上限で止まる
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest

from teamagent.adapters import media_job
from teamagent.adapters.media_job import MediaJobClient

_BUCKET = "teamagent-media-test"


class _FakeClient:
    def __init__(self, service: str, region: str, config: Any) -> None:
        self.service = service
        self.region = region
        self.config = config


class _CountingSession:
    """boto3.session.Session の代役。生成回数と client 生成回数を数える。"""

    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1
        self.client_calls: list[tuple[str, str, float]] = []

    def client(self, service: str, *, region_name: str, config: Any) -> _FakeClient:
        self.client_calls.append((service, region_name, float(config.connect_timeout)))
        return _FakeClient(service, region_name, config)


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch: pytest.MonkeyPatch) -> Any:
    media_job.reset_boto_cache()
    _CountingSession.instances = 0
    monkeypatch.setattr(boto3.session, "Session", _CountingSession)
    yield
    media_job.reset_boto_cache()


def _client(clock_value: float = 100.0) -> MediaJobClient:
    return MediaJobClient(
        queue_url="queue",
        table="jobs",
        bucket=_BUCKET,
        clock=lambda: clock_value,
    )


def test_session_is_created_once_across_instances() -> None:
    """別インスタンスでも Session は 1 つだけ生成される（実測 1 生成 ≒ 40ms の削減元）。"""

    first = _client()._client("s3", 200.0)
    second = _client()._client("dynamodb", 200.0)

    assert _CountingSession.instances == 1
    assert isinstance(first, _FakeClient)
    assert isinstance(second, _FakeClient)


def test_same_service_and_deadline_reuses_one_client() -> None:
    """同一キーなら client 実体まで使い回す（Session 生成も client 生成も 1 回）。"""

    first = _client()._client("s3", 200.0)
    second = _client()._client("s3", 200.0)

    assert first is second
    session = media_job._shared_boto_session()
    assert session.client_calls == [("s3", "ap-northeast-1", 30.0)]


def test_different_remaining_budget_yields_a_different_client() -> None:
    """残予算が違えば timeout が違う＝別 client（取り違え防止）。"""

    long_budget = _client()._client("s3", 200.0)  # remaining 100s → phase 30.0
    short_budget = _client()._client("s3", 110.0)  # remaining 10s → phase 5.0

    assert long_budget is not short_budget
    assert long_budget.config.connect_timeout == 30.0
    assert short_budget.config.connect_timeout == 5.0


def test_injected_session_never_touches_the_shared_cache() -> None:
    """注入 Session は共有キャッシュを読みも書きもしない。"""

    injected = _CountingSession()
    _CountingSession.instances = 0
    client = MediaJobClient(
        session=injected,
        queue_url="queue",
        table="jobs",
        bucket=_BUCKET,
        clock=lambda: 100.0,
    )

    produced = client._client("s3", 200.0)

    assert isinstance(produced, _FakeClient)
    assert injected.client_calls == [("s3", "ap-northeast-1", 30.0)]
    # 共有 Session は一度も作られず、キャッシュも空のまま。
    assert _CountingSession.instances == 0
    assert media_job._BOTO_CLIENTS == {}


def test_client_cache_is_bounded() -> None:
    """締切間際の端数キーが積んでも辞書は上限で止まる（無制限増殖を作らない）。"""

    limit = media_job._BOTO_CLIENT_CACHE_MAX
    produced = [_client()._client("s3", 100.0 + 0.5 * (index + 1)) for index in range(limit + 5)]

    assert all(isinstance(item, _FakeClient) for item in produced)
    assert len(media_job._BOTO_CLIENTS) == limit


def test_tiktok_task_store_status_path_uses_the_shared_cache() -> None:
    """完了通知の見張りが叩く一番熱い経路がキャッシュを素通りしないこと。

    ``TikTokTaskStore._session()`` が Session を作って渡すと MediaJobClient から
    見て「注入 Session」になり、共有キャッシュを丸ごと迂回する。見張りは
    30 秒間隔・最大 60 分＝最大 120 回このパスを通るので、素通りは効く。
    """

    import time

    from teamagent.adapters.tiktok_task_store import TikTokTaskStore

    # store 側の MediaJobClient は実時計を使うので、締切も実時刻で置く。
    deadline = time.time() + 300.0
    store = TikTokTaskStore()

    first = store._client(store._session())._client("dynamodb", deadline)
    second = store._client(store._session())._client("dynamodb", deadline)

    assert first is second
    assert _CountingSession.instances == 1
