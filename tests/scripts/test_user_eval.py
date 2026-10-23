"""利用者の目で見える契約を、ネットワークを使わず固定する。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.user_eval.core import (
    JUDGE_PROMPT,
    Attachment,
    Citation,
    Message,
    Runner,
    check,
    load_scenarios,
    markdown,
    run_suite,
)
from scripts.user_eval.fakes import FakeClock, FakeJudge, FakeLogReader, FakeTransport


def msg(text: str = "回答", **kwargs: Any) -> Message:
    return Message("a", text, 1, **kwargs)


def scenario(utterances: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": "example",
        "utterances": utterances or ["依頼"],
        "checks": [{"type": "must_not_ask_back"}, {"type": "llm_judge"}],
    }


CASES: list[tuple[dict[str, Any], list[Message], list[Message]]] = [
    (
        {"type": "must_contain_link", "kind": "drive"},
        [msg("<https://docs.google.com/document/d/x|資料>")],
        [msg("https://drive.google.com.evil.invalid/x")],
    ),
    (
        {"type": "must_contain_link", "kind": "slack"},
        [msg("https://example.slack.com/archives/C1/p1")],
        [msg("https://example.com")],
    ),
    (
        {"type": "must_contain_link", "kind": "tiktok"},
        [msg("https://www.tiktok.com/@demo/video/1")],
        [msg("リンクなし")],
    ),
    (
        {"type": "must_attach_file", "extensions": ["pdf", "pptx"]},
        [msg(attachments=[Attachment("REPORT.PDF")])],
        [msg("report.pdf を作ります")],
    ),
    (
        {"type": "must_not_contain", "patterns": ["現在は", "進行.?"]},
        [msg("実施済み")],
        [msg("進行中")],
    ),
    (
        {"type": "must_not_repeat_numbers_after_metrics_post"},
        [msg("📊 動画A 再生12,000 保存56"), Message("b", "冒頭の演出が効いた", 2)],
        [msg("📊 動画A 再生12,000 保存56"), Message("b", "動画A は1.2万回再生、保存56", 2)],
    ),
    (
        {"type": "must_not_ask_back"},
        [msg("資料を添付しました。")],
        [msg("ブランドを教えてください。")],
    ),
    (
        {"type": "must_not_cite_own_answers"},
        [
            msg(
                "https://demo.slack.com/archives/C1/p1",
                citations=[Citation("https://demo.slack.com/archives/C1/p1", "U1", False)],
            )
        ],
        [
            msg(
                "資料はこちら",
                citations=[Citation("https://demo.slack.com/archives/D1/p1", "AICO", True)],
            )
        ],
    ),
    (
        {"type": "status_freshness"},
        [msg("2026-10-15 時点では進行中。実施済みの施策もある。")],
        [msg("進行中です。別の施策は2026-10-15 時点では完了。")],
    ),
    ({"type": "max_latency_s", "seconds": 120}, [msg()], [msg()]),
]


@pytest.mark.parametrize(
    "spec,good,bad", CASES, ids=[f"{i}-{c[0]['type']}" for i, c in enumerate(CASES)]
)
def test_check_positive_negative(
    spec: dict[str, Any], good: list[Message], bad: list[Message]
) -> None:
    assert check(spec, good, 120)["passed"] is True
    assert check(spec, bad, 121)["passed"] is False


@pytest.mark.parametrize(
    "text", ["どのブランドですか？", "候補を教えて下さい。", "いかがですか?", "対応しますか", ""]
)
def test_ask_back_endings(text: str) -> None:
    assert not check({"type": "must_not_ask_back"}, [msg(text)], 1)["passed"]


def test_ask_back_exception_is_explicit() -> None:
    assert check(
        {"type": "must_not_ask_back", "allow_patterns": ["送信してよいですか？"]},
        [msg("資料完成。送信してよいですか？")],
        1,
    )["passed"]


@pytest.mark.parametrize(
    "text", ["2026-99-99 時点では進行中", "2026-10-15 進行中", "実施済み、現在は進行中", "資料です"]
)
def test_freshness_requires_valid_date_for_each_status(text: str) -> None:
    assert not check({"type": "status_freshness"}, [msg(text)], 1)["passed"]


def test_missing_provenance_fails() -> None:
    assert not check(
        {"type": "must_not_cite_own_answers"}, [msg("https://demo.slack.com/archives/D1/p1")], 1
    )["passed"]


def test_metrics_same_post_and_missing_metrics() -> None:
    spec = {"type": "must_not_repeat_numbers_after_metrics_post"}
    assert not check(spec, [msg("📊 動画A 再生100 保存5\n\n説明: 再生100")], 1)["passed"]
    assert not check(spec, [msg("分析しました")], 1)["passed"]
    assert check(spec, [msg("📊 再生100 保存5"), Message("b", "2026年に2案を比較", 2)], 1)["passed"]


@pytest.mark.parametrize("dm_id", ["C123", "G123", "D1,C1", "D1 D2", "", "U123", "D1\n"])
def test_channel_rejected_before_send(dm_id: str) -> None:
    transport = FakeTransport([])
    with pytest.raises(ValueError):
        Runner(dm_id, transport, FakeJudge())
    assert transport.sent == []


def test_runner_buttons_edits_logs_and_judge_once(tmp_path: Path) -> None:
    clock, judge, logs = FakeClock(), FakeJudge(), FakeLogReader()
    transport = FakeTransport(
        [
            [
                [msg("検索中", kind="progress")],
                [msg("どのブランドですか？", kind="evaluation_buttons")],
                [Message("answer", "仮回答", 3, request_id="r1")],
                [
                    Message(
                        "answer",
                        "資料を添付しました",
                        3,
                        attachments=[Attachment("a.pdf")],
                        request_id="r1",
                    )
                ],
            ]
        ]
    )
    runner = Runner("D123", transport, judge, logs, clock, quiet_s=2, timeout_s=10)
    rows = run_suite(runner, [scenario()], tmp_path)
    row = rows[0]
    assert row["passed"]
    assert row["latencies_s"] == [2]
    assert [m["text"] for m in row["turns"][0]["messages"]] == ["検索中", "資料を添付しました"]
    assert logs.calls == ["r1"]
    assert len(row["logs"][0]["events"]) == 2
    assert len(judge.calls) == 1
    assert row["checks"][-1]["type"] == "llm_judge"
    assert json.loads((tmp_path / "results.jsonl").read_text())["id"] == "example"
    report = (tmp_path / "results.md").read_text()
    assert report.startswith(
        "| scenario | must_not_ask_back | response_complete | llm_judge |\n| --- |"
    )
    assert "○" in report


@pytest.mark.parametrize("kind", ["evaluation_buttons", "progress", "empty", "stream"])
def test_wait_is_bounded(kind: str) -> None:
    clock = FakeClock()
    batches = (
        []
        if kind == "empty"
        else [
            [Message(str(i), "継続中", i, kind="answer" if kind == "stream" else kind)]
            for i in range(20)
        ]
    )
    judge = FakeJudge()
    runner = Runner("D1", FakeTransport([batches]), judge, clock=clock, quiet_s=2, timeout_s=5)
    result = runner.run(scenario(["最初", "次"]))
    assert clock.now() == 5
    assert not result["passed"]
    assert not result["turns"][0]["complete"]
    assert len(result["turns"]) == 1
    assert len(judge.calls) == 1


def test_multiple_turns_and_partial_judge() -> None:
    judge = FakeJudge("partial")
    transport = FakeTransport([[[msg()]], [[msg()]], [[msg()]]])
    result = Runner("D1", transport, judge, clock=FakeClock(), quiet_s=1, timeout_s=3).run(
        scenario(["一", "二", "三"])
    )
    assert len(transport.sent) == 3
    assert len(judge.calls) == 1
    assert len(judge.calls[0]["conversation"]["turns"]) == 3
    assert not result["passed"]
    assert result["checks"][-1]["verdict"] == "partial"


@pytest.mark.parametrize(
    "verdict,passed", [("good", True), ("partial", False), ("bad", False), ("invalid", False)]
)
def test_judge_verdicts(verdict: str, passed: bool) -> None:
    result = Runner(
        "D1",
        FakeTransport([[[msg()]]]),
        FakeJudge(verdict),
        clock=FakeClock(),
        quiet_s=1,
        timeout_s=3,
    ).run(scenario())
    assert result["checks"][-1]["passed"] is passed


def test_markdown_escapes_and_absent_checks() -> None:
    text = markdown(
        [
            {"id": "a|b", "checks": [{"type": "x", "passed": False, "reason": "a|b\nc"}]},
            {"id": "two", "checks": []},
        ]
    )
    assert "× a\\|b<br>c" in text
    assert "| two | — |" in text


def test_scenario_corpus_and_local_mapping(tmp_path: Path) -> None:
    path = Path(__file__).resolve().parents[2] / "scripts/user_eval/scenarios.json"
    rows = json.loads(path.read_text())
    assert len(rows) == 13
    assert all("2026-10-" in row["_comment"] for row in rows)
    with pytest.raises(ValueError, match="未割り当て"):
        load_scenarios(path)
    local = tmp_path / "scenarios.local.json"
    local.write_text(
        json.dumps(
            {
                key: "FAKE_" + key
                for key in [
                    "client_a",
                    "brand_a",
                    "campaign_a",
                    "campaign_b",
                    "delivery_kanji",
                    "slack_only_topic",
                ]
            }
        )
    )
    resolved = load_scenarios(path, local)
    assert len(resolved) == 13
    assert all(r["checks"][-1]["type"] == "llm_judge" for r in resolved)
    assert "FAKE_client_a" in resolved[0]["utterances"][0]
    assert "暫定" in JUDGE_PROMPT


@pytest.mark.parametrize(
    "names", [["llm_judge", "must_not_ask_back"], ["llm_judge", "llm_judge"], ["typo", "llm_judge"]]
)
def test_invalid_checks_rejected(names: list[str]) -> None:
    row = scenario()
    row["checks"] = [{"type": n} for n in names]
    transport = FakeTransport([])
    with pytest.raises(ValueError):
        Runner("D1", transport, FakeJudge()).run(row)
    assert not transport.sent


def test_first_turn_ask_back_is_not_hidden_by_later_answer() -> None:
    transport = FakeTransport([[[msg("どのブランドですか？")]], [[msg("資料を添付しました")]]])
    result = Runner("D1", transport, FakeJudge(), clock=FakeClock(), quiet_s=1, timeout_s=3).run(
        scenario(["作って", "お任せ"])
    )
    assert not result["checks"][0]["passed"]
    assert "発話 1" in result["checks"][0]["reason"]


def test_adapter_exceptions_are_recorded_and_suite_continues(tmp_path: Path) -> None:
    class BrokenTransport(FakeTransport):
        def send(self, dm_id: str, text: str, *, timeout_s: float) -> str:
            raise TimeoutError("offline fixture")

    rows = run_suite(
        Runner("D1", BrokenTransport([]), FakeJudge(), clock=FakeClock()),
        [scenario(), scenario()],
        tmp_path,
    )
    assert len(rows) == 2
    assert all(not row["passed"] for row in rows)
    assert "TimeoutError" in rows[0]["checks"][-2]["reason"]
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 2


def test_log_and_judge_exceptions_are_visible() -> None:
    class BrokenLogs(FakeLogReader):
        def read(
            self, request_id: str, events: tuple[str, ...], *, timeout_s: float
        ) -> list[dict[str, Any]]:
            raise TimeoutError("log timeout")

    class BrokenJudge(FakeJudge):
        def judge(
            self, prompt: str, conversation: dict[str, Any], *, timeout_s: float
        ) -> dict[str, str]:
            raise TimeoutError("judge timeout")

    result = Runner(
        "D1",
        FakeTransport([[[msg(request_id="r1")]]]),
        BrokenJudge(),
        BrokenLogs(),
        FakeClock(),
        quiet_s=1,
        timeout_s=3,
    ).run(scenario())
    assert not result["passed"]
    assert "judge timeout" in result["checks"][-1]["reason"]
    assert result["log_errors"] == ["r1: log timeout"]
