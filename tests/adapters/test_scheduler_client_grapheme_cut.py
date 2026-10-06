"""予定リマインドの件名・場所を、絵文字（書記素クラスタ）の途中で切らないことのテスト。

予定名・場所は本人 DM（🔔 まもなく: …）にそのまま出る。60 字の上限が絵文字の途中に
来ると、片割れ（孤立した地域指示子・宙ぶらりんの ZWJ・肌色の抜けた 👍 など）が残る。

本番と同じ経路を通す: ``SchedulerClient.schedule_reminder`` が作る payload（SQS の本文）
→ 通知 Lambda（``reminder_notify/handler.py``）が組み立てる投稿本文。Lambda 側にも
60 字切りがあるが、adapter が収めて送れば効かない（Lambda は別デプロイ物なので触らない）。

⚠️ 不可視文字（ZWJ・VS16・結合文字）はソースに直接書かず ``\\u`` エスケープで書く。
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.scheduler_client import SchedulerClient

_JST = _dt.timezone(_dt.timedelta(hours=9))
_LIMIT = 60
_HANDLER_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "infra"
    / "terraform"
    / "lambda"
    / "reminder_notify"
    / "handler.py"
)

_CLUSTERS = {
    "family": "\U0001f468‍\U0001f469‍\U0001f467",  # 👨 ZWJ 👩 ZWJ 👧
    "flag": "\U0001f1ef\U0001f1f5",  # 地域指示子 2 個（🇯🇵）
    "skin_tone": "\U0001f44d\U0001f3fd",  # 👍 肌色
    "keycap": "1️⃣",  # 1 VS16 囲み
    "tag_flag": "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
    "ivs_kanji": "葛\U000e0100",  # 異体字セレクタ付きの漢字
    "decomposed_kana": "が",  # か＋結合濁点
    "cjk": "定",
}


class _FakeBoto:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_schedule(self, **kw: Any) -> None:
        self.calls.append(kw)


def _load_handler() -> Any:
    mod_name = "reminder_notify_grapheme_cut_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, _HANDLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _posted_text(title: str, location: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """adapter → SQS 本文 → Lambda の投稿本文。"""
    fake = _FakeBoto()
    SchedulerClient(
        group_name="teamagent-dev-reminders",
        queue_arn="arn:aws:sqs:ap-northeast-1:1:teamagent-dev-reminders.fifo",
        role_arn="arn:aws:iam::1:role/rem-scheduler",
        client=fake,
    ).schedule_reminder(
        channel="D1",
        start_iso="2026-07-15T14:00:00+09:00",
        end_iso="2026-07-15T15:00:00+09:00",
        fire_at=_dt.datetime(2026, 7, 15, 13, 55, tzinfo=_JST),
        url="https://meet.example/x",
        request_id="r",
        title=title,
        location=location,
    )
    handler = _load_handler()
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")  # secretsmanager を迂回（テスト用経路）
    posted: list[str] = []
    monkeypatch.setattr(handler, "_post_message", lambda channel, text: posted.append(text))
    body = fake.calls[0]["Target"]["Input"]
    assert handler.handler({"Records": [{"body": body}]}, None)["ok"]
    assert len(posted) == 1
    return posted[0]


def _want(title: str, location: str) -> str:
    return f"🔔 まもなく: *{title}* （14:00〜15:00・{location}）\n<https://meet.example/x|開く>"


@pytest.mark.parametrize("name", sorted(_CLUSTERS))
def test_reminder_title_and_location_do_not_split_a_cluster(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = _CLUSTERS[name]
    for inside in range(1, len(cluster)):
        title = "定" * (_LIMIT - inside)
        location = "室" * (_LIMIT - inside)
        got = _posted_text(title + cluster + "例", location + cluster + "例", monkeypatch)
        assert got == _want(title, location)
    # 上限ちょうどでクラスタが終わるなら残す（落としすぎない）。
    title = "例" * (_LIMIT - len(cluster)) + cluster
    location = "室" * (_LIMIT - len(cluster)) + cluster
    got = _posted_text(title + "例", location + "例", monkeypatch)
    assert got == _want(title, location)
