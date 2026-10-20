from pathlib import Path
from types import SimpleNamespace

from tests.adapters.test_dm_quality_reader import END, START, ReadOnlySlack, extract


def test_denominator_includes_no_reply_but_not_non_requests() -> None:
    from teamagent.dm_quality import Judgement, summarize

    data = extract(ReadOnlySlack())
    decisions = {
        x.input_id: Judgement(verdict=v)
        for x, v in zip(data.exchanges, ["good", "no_reply", "not_a_request"], strict=True)
    }
    report = summarize(data, decisions, start=START, end=END, observed_until=END)
    assert report.complete
    assert report.request_count == 2
    assert report.good_rate == 0.5


def test_missing_data_never_has_a_confirmed_rate() -> None:
    from teamagent.dm_quality import Judgement, summarize

    data = extract(ReadOnlySlack())
    decisions = {x.input_id: Judgement(verdict="good") for x in data.exchanges}
    data.failures.append("history_failed")
    report = summarize(data, decisions, start=START, end=END, observed_until=END)
    assert not report.complete and report.good_rate is None
    data.failures.clear()
    decisions.pop(next(iter(decisions)))
    report = summarize(data, decisions, start=START, end=END, observed_until=END)
    assert not report.complete and report.good_rate is None


def test_haiku_uses_fixed_rubric_caching_and_rejects_unstructured_output() -> None:
    import pytest

    from teamagent.dm_quality import HAIKU_MODEL, HaikuJudge
    from teamagent.prompts.loader import load_prompt

    calls = []

    def converse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            text='{"verdict":"good"}', stop_reason="end_turn", usage=SimpleNamespace(cost_usd=0.001)
        )

    judge = HaikuJudge(SimpleNamespace(model_id=HAIKU_MODEL, converse=converse))
    assert judge.judge(extract(ReadOnlySlack()).exchanges[0]).verdict == "good"
    assert calls[0]["cache_system"] is True
    assert "rubric_v1" in calls[0]["system"]
    assert calls[0]["system"] == load_prompt("dm_quality", "v1", "system")
    judge.client.converse = lambda **_: SimpleNamespace(
        text="private prose", stop_reason="end_turn", usage=SimpleNamespace(cost_usd=0.001)
    )
    with pytest.raises(ValueError):
        judge.judge(extract(ReadOnlySlack()).exchanges[0])


def test_checkpoint_replay_is_private_and_avoids_second_judgement(tmp_path: Path) -> None:
    from teamagent.dm_quality import Judgement, measure, write_report

    class Judge:
        model_id = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"

        def __init__(self):
            self.calls = 0

        def judge(self, exchange):
            self.calls += 1
            return Judgement(verdict="good", cost_usd=0.001)

    judge = Judge()
    data = extract(ReadOnlySlack())
    path = tmp_path / "checkpoint.json"
    first = measure(
        data, judge, start=START, end=END, observed_until=END, checkpoint=path, budget_usd=1
    )
    second = measure(
        data, judge, start=START, end=END, observed_until=END, checkpoint=path, budget_usd=1
    )
    assert judge.calls == len(data.exchanges)
    assert first.good_rate == second.good_rate == 1.0
    report_path = tmp_path / "report.json"
    write_report(report_path, second)
    for artifact in (path, report_path):
        assert artifact.stat().st_mode & 0o777 == 0o600
        assert "合成" not in artifact.read_text()
        assert "U0123456789" not in artifact.read_text()


def test_report_validation_cannot_label_incomplete_data_complete() -> None:
    import pytest
    from pydantic import ValidationError

    from teamagent.dm_quality import QualityReport, summarize

    data = extract(ReadOnlySlack())
    report = summarize(data, {}, start=START, end=END, observed_until=END)
    raw = report.model_dump(mode="json")
    raw.update(complete=True, good_rate=1.0)
    with pytest.raises(ValidationError):
        QualityReport.model_validate(raw)


def test_budget_limit_and_judge_failure_are_missing_and_not_retried(tmp_path) -> None:
    from teamagent.dm_quality import HAIKU_MODEL, measure

    class FailedJudge:
        model_id = HAIKU_MODEL
        calls = 0

        def judge(self, exchange):
            self.calls += 1
            raise ValueError("sensitive model output")

    judge = FailedJudge()
    data = extract(ReadOnlySlack())
    kwargs = dict(start=START, end=END, observed_until=END, checkpoint=tmp_path / "checkpoint.json")
    report = measure(data, judge, budget_usd=0.0000001, **kwargs)
    assert judge.calls == 0 and not report.complete and report.good_rate is None
    report = measure(data, judge, budget_usd=1, **kwargs)
    calls = judge.calls
    assert calls == 3 and not report.complete and report.cost_usd > 0
    measure(data, judge, budget_usd=1, **kwargs)
    assert judge.calls == calls
    assert "sensitive" not in (tmp_path / "checkpoint.json").read_text()
