"""reminder_notify Lambda の ``kind=digest`` 分岐（新 Lambda を作らない設計）。

死守: 予約ペイロードには channel が入っていない。ここは ECS RunTask を起こすだけで、
宛先（本人 DM）は Fargate 側が user_ref → email → conversations.open で解決し直す。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

HANDLER_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "infra"
    / "terraform"
    / "lambda"
    / "reminder_notify"
    / "handler.py"
)


def _load() -> Any:
    name = "reminder_notify_digest_under_test"
    spec = importlib.util.spec_from_file_location(name, HANDLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()

REF = "a" * 32


def _event(body: dict[str, Any]) -> dict[str, Any]:
    return {"Records": [{"body": json.dumps(body)}]}


class _FakeEcs:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run_task(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        return {"tasks": [{}]}


@pytest.fixture
def ecs(monkeypatch: pytest.MonkeyPatch) -> _FakeEcs:
    fake = _FakeEcs()

    class _Boto:
        @staticmethod
        def client(name: str) -> Any:
            assert name == "ecs"
            return fake

    monkeypatch.setitem(sys.modules, "boto3", _Boto)
    monkeypatch.setenv("DIGEST_CLUSTER_ARN", "arn:aws:ecs:ap-northeast-1:1:cluster/c")
    monkeypatch.setenv(
        "DIGEST_TASK_DEFINITION_ARN", "arn:aws:ecs:ap-northeast-1:1:task-definition/t:7"
    )
    monkeypatch.setenv("DIGEST_SUBNET_IDS", "subnet-a,subnet-b")
    monkeypatch.setenv("DIGEST_SECURITY_GROUP_IDS", "sg-1")
    monkeypatch.setenv("DIGEST_CONTAINER_NAME", "morning-digest")
    return fake


def test_digest_payload_starts_one_user_task(ecs: _FakeEcs) -> None:
    mod.handler(_event({"v": 1, "kind": "digest", "user_ref": REF, "date": "2026-09-11"}), None)
    assert len(ecs.calls) == 1
    overrides = ecs.calls[0]["overrides"]["containerOverrides"][0]
    env = {e["name"]: e["value"] for e in overrides["environment"]}
    assert env == {
        "MORNING_DIGEST_MODE": "single",
        "MORNING_DIGEST_USER_REF": REF,
        "MORNING_DIGEST_DATE": "2026-09-11",
    }
    assert overrides["name"] == "morning-digest"


def test_digest_payload_carries_no_channel(ecs: _FakeEcs) -> None:
    """変異: payload の channel を宛先として使う実装に戻すと、ここで KeyError/赤。"""
    body = {"v": 1, "kind": "digest", "user_ref": REF, "date": "2026-09-11"}
    assert "channel" not in body
    mod.handler(_event(body), None)
    dumped = json.dumps(ecs.calls[0])
    assert "D0" not in dumped


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "digest", "user_ref": "short", "date": "2026-09-11"},
        {"kind": "digest", "user_ref": "Z" * 32, "date": "2026-09-11"},
        {"kind": "digest", "user_ref": REF, "date": "20260911"},
        {"kind": "digest", "user_ref": REF},
    ],
)
def test_malformed_digest_payload_starts_nothing(body: dict[str, Any], ecs: _FakeEcs) -> None:
    """形式不正は fail-closed（リトライしても直らないので DLQ も汚さない）。"""
    mod.handler(_event(body), None)
    assert ecs.calls == []


def test_digest_is_a_noop_when_env_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定 OFF: env が無ければ boto3 すら触らない。"""
    for key in (
        "DIGEST_CLUSTER_ARN",
        "DIGEST_TASK_DEFINITION_ARN",
        "DIGEST_SUBNET_IDS",
        "DIGEST_SECURITY_GROUP_IDS",
    ):
        monkeypatch.delenv(key, raising=False)
    called: list[str] = []

    class _Boto:
        @staticmethod
        def client(name: str) -> Any:  # pragma: no cover
            called.append(name)
            raise AssertionError("既定 OFF では ECS を触らない")

    monkeypatch.setitem(sys.modules, "boto3", _Boto)
    out = mod.handler(
        _event({"v": 1, "kind": "digest", "user_ref": REF, "date": "2026-09-11"}), None
    )
    assert out == {"ok": True, "count": 1}
    assert called == []


def test_reminder_payload_still_posts_to_slack(monkeypatch: pytest.MonkeyPatch) -> None:
    """既存のリマインド経路は 1 バイトも変わらない（回帰）。"""
    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "_post_message", lambda ch, text: posted.append((ch, text)))
    mod.handler(
        _event(
            {"v": 1, "channel": "D001", "start_hm": "14:00", "url": "https://x", "title": "定例"}
        ),
        None,
    )
    assert posted and posted[0][0] == "D001"
    assert "定例" in posted[0][1]


def test_non_dm_channel_reminder_is_still_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "_post_message", lambda ch, text: posted.append((ch, text)))
    mod.handler(_event({"v": 1, "channel": "C001", "start_hm": "14:00"}), None)
    assert posted == []
