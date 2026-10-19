"""ネットワークも課金も発生しない差し込み実装。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scripts.user_eval.core import Message


@dataclass
class FakeClock:
    seconds: float = 0

    def now(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


@dataclass
class FakeTransport:
    # 送信ごとの、read 呼び出し順の差分バッチ。
    replies: list[list[list[Message]]]
    sent: list[tuple[str, str]] = field(default_factory=list)
    reads: int = 0

    def send(self, dm_id: str, text: str, *, timeout_s: float) -> str:
        self.sent.append((dm_id, text))
        self.reads = 0
        return str(len(self.sent) - 1)

    def read(self, dm_id: str, token: str, *, timeout_s: float) -> list[Message]:
        batches = self.replies[int(token)]
        batch = batches[self.reads] if self.reads < len(batches) else []
        self.reads += 1
        return batch


@dataclass
class FakeJudge:
    verdict: str = "good"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def judge(self, prompt: str, conversation: dict[str, Any], *, timeout_s: float) -> dict[str, str]:
        self.calls.append({"prompt": prompt, "conversation": conversation})
        return {"verdict": self.verdict, "reason": "偽物による配線確認（実機評価ではない）"}


@dataclass
class FakeLogReader:
    calls: list[str] = field(default_factory=list)

    def read(self, request_id: str, events: tuple[str, ...], *, timeout_s: float) -> list[dict[str, Any]]:
        self.calls.append(request_id)
        return [{"event": event, "fake": True} for event in events]
