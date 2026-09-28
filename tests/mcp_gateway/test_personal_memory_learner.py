"""学習ジョブ（I13・I15・I16）。Hermes は本番と同じ形の HTTP 応答を返すフェイク＋本物のクライアント。"""

from __future__ import annotations

import pytest
import structlog

from teamagent.adapters.personal_memory_store import Principal
from teamagent.mcp_gateway.personal_memory.learner import MAX_REMOVALS, run_learn_job
from tests.personal_memory.fakes import FakeDirectory, FakeStore, HermesFake, append

P = Principal("T0123456789", "U0000000A1", "a@vectorinc.co.jp")
UTTERANCES = ["来週の花王の資料、短めでお願い", "小俣さんに確認してから出す"]


def _run(
    store: FakeStore, hermes: HermesFake, *, names: tuple[str, ...] = (), utterances=UTTERANCES
):
    return run_learn_job(
        P, utterances, store=store, client=hermes.client(), directory=FakeDirectory(names)
    )


@pytest.fixture
def store() -> FakeStore:
    s = FakeStore()
    s.create(P, entries=[("user", "返事は短めが好き"), ("memory", "花王の案件を担当")])
    return s


def test_applies_additions(store: FakeStore) -> None:
    hermes = HermesFake(append(user=["箇条書きを好む"], memory=["資料は PPTX で作る"]))
    outcome = _run(store, hermes)
    assert outcome.outcome == "applied"
    assert (outcome.added, outcome.removed) == (2, 0)
    assert store.contents(P, "user") == ["返事は短めが好き", "箇条書きを好む"]
    assert store.contents(P, "memory") == ["花王の案件を担当", "資料は PPTX で作る"]
    sent = hermes.calls[0]["snapshot"]
    assert sent == {"user": ["返事は短めが好き"], "memory": ["花王の案件を担当"]}
    assert hermes.calls[0]["utterances"] == UTTERANCES


@pytest.mark.parametrize(
    "entry",
    [
        "資料の置き場は https://drive.example.com/x",
        "先方の田中様に確認してから出す",
        "連絡先は 090-1234-5678",
    ],
)
def test_guard_rejects_before_write(store: FakeStore, entry: str) -> None:
    outcome = _run(store, HermesFake(append(memory=[entry])))
    assert outcome.outcome == "no_change"
    assert outcome.rejected == 1
    assert entry not in store.contents(P)


def test_colleague_name_kept_only_when_in_directory(store: FakeStore) -> None:
    entry = "提出前に小俣さんへ確認する"
    outcome = _run(store, HermesFake(append(memory=[entry])), names=("小俣", "太郎"))
    assert outcome.outcome == "applied"
    assert entry in store.contents(P)
    s2 = FakeStore()
    s2.create(P)
    outcome = _run(s2, HermesFake(append(memory=[entry])), names=())
    assert outcome.outcome == "no_change"
    assert outcome.reasons == {"person_name": 1}


def test_verbatim_copy_of_utterance_rejected(store: FakeStore) -> None:
    long_utterance = "来週の花王の定例では新商品の販促案を三つ出して比較表も添えることにした"
    entry = long_utterance[:30]
    outcome = _run(store, HermesFake(append(memory=[entry])), utterances=[long_utterance])
    assert outcome.reasons == {"verbatim": 1}
    assert entry not in store.contents(P)


def test_removals_applied(store: FakeStore) -> None:
    def drop_user(user, memory, _u):  # type: ignore[no-untyped-def]
        return [], memory

    outcome = _run(store, HermesFake(drop_user))
    assert outcome.outcome == "applied"
    assert outcome.removed == 1
    assert store.contents(P, "user") == []


def test_too_many_removals_discards_job() -> None:
    s = FakeStore()
    s.create(P, entries=[("memory", f"メモ{i}") for i in range(MAX_REMOVALS + 1)])
    before = s.contents(P)

    def wipe(user, memory, _u):  # type: ignore[no-untyped-def]
        return user, ["新しいメモ"]

    outcome = _run(s, HermesFake(wipe))
    assert outcome.outcome == "too_many_removals"
    assert s.contents(P) == before


def test_entries_failing_recheck_are_not_sent_nor_removed() -> None:
    s = FakeStore()
    # 保存後に規則が厳しくなった等で、いまの guard に合格しない項目
    s.create(P, entries=[("memory", "先方の山田様が窓口"), ("memory", "花王の案件を担当")])

    def keep_sent(user, memory, _u):  # type: ignore[no-untyped-def]
        return user, memory

    hermes = HermesFake(keep_sent)
    outcome = _run(s, hermes)
    assert hermes.calls[0]["snapshot"]["memory"] == ["花王の案件を担当"]
    assert outcome.outcome == "no_change"
    assert "先方の山田様が窓口" in s.contents(P)  # 自動では消さない


def _during_hermes(store: FakeStore, action) -> HermesFake:  # type: ignore[no-untyped-def]
    """Hermes の処理中（読んだ後・書く前）に本人の操作が入った状況を作る。"""
    inner = append(memory=["新しいメモ"])

    def respond(user, memory, utterances):  # type: ignore[no-untyped-def]
        action()
        return inner(user, memory, utterances)

    return HermesFake(respond)


def test_version_conflict_discards(store: FakeStore) -> None:
    # Hermes の処理中に本人が「3番を忘れて」等を使った（版が進む）
    forgotten = store.profiles[P.key].entries[0].entry_id
    outcome = _run(store, _during_hermes(store, lambda: store.forget(P, forgotten)))
    assert outcome.outcome == "store_version_conflict"
    assert "新しいメモ" not in store.contents(P)


def test_freeze_during_job_discards(store: FakeStore) -> None:
    outcome = _run(store, _during_hermes(store, lambda: store.set_state(P, "frozen")))
    assert outcome.outcome == "store_not_active"
    assert "新しいメモ" not in store.contents(P)


def test_resume_during_job_still_discards(store: FakeStore) -> None:
    # 止めて→再開 が学習中に挟まっても、版が進んでいるので古い学習結果は捨てる
    def freeze_and_resume() -> None:
        store.set_state(P, "frozen")
        store.set_state(P, "active")

    outcome = _run(store, _during_hermes(store, freeze_and_resume))
    assert outcome.outcome == "store_version_conflict"
    assert "新しいメモ" not in store.contents(P)


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [
        ("busy", "hermes_busy"),
        ("timeout", "hermes_hermes_failed"),
        ("internal", "hermes_hermes_failed"),
        ("extra_key", "hermes_bad_response"),
        ("job_mismatch", "hermes_bad_response"),
        ("section_sign", "hermes_bad_response"),
    ],
)
def test_hermes_failures_discard(store: FakeStore, mode: str, outcome: str) -> None:
    hermes = HermesFake(append(memory=["新しいメモ"]))
    hermes.mode = mode
    before = store.contents(P)
    result = _run(store, hermes)
    assert result.outcome == outcome
    assert store.contents(P) == before
    assert len(hermes.calls) == 1  # 503 でも再試行しない


def test_over_limit_additions_dropped() -> None:
    s = FakeStore()
    # 1 件は再検査で落ちて送らない（Hermes には 37 件＋追加 3 件＝上限内の 40 件が返る）が、
    # DB には 38 件あるので、追加 3 件のうち 1 件は件数の上限（40）を超える
    unsent = [("user", "先方の山田様が窓口")]
    s.create(P, entries=unsent + [("user", f"好み{i:02d}") for i in range(37)])
    outcome = _run(s, HermesFake(append(user=["追加A", "追加B", "追加C"])))
    assert outcome.outcome == "applied"
    assert outcome.added == 2
    assert outcome.reasons == {"over_limit": 1}
    assert len(s.contents(P, "user")) == 40


def test_char_limit_counts_unsent_entries() -> None:
    s = FakeStore()
    # 送らない（再検査で落ちる）項目も DB の字数を使っている
    s.create(P, entries=[("user", "先方の山田様" + "あ" * 190)] + [("user", "い" * 199)] * 5)
    # 送った分だけなら 1375 字に収まるが、送らなかった 196 字を足すと超える
    outcome = _run(s, HermesFake(append(user=["う" * 180])))
    assert outcome.reasons == {"over_limit": 1}
    assert outcome.outcome == "no_change"


@pytest.mark.parametrize("state", ["frozen", "unnoticed", "missing"])
def test_not_active_profiles_skip_hermes(state: str) -> None:
    s = FakeStore()
    if state == "frozen":
        s.create(P, state="frozen")
    elif state == "unnoticed":
        s.create(P, noticed=False)
    hermes = HermesFake(append(memory=["x"]))
    assert _run(s, hermes).outcome == "not_active"
    assert hermes.calls == []


def test_result_log_has_counts_only(store: FakeStore) -> None:
    hermes = HermesFake(append(user=["箇条書きを好む"], memory=["先方の田中様が窓口"]))
    with structlog.testing.capture_logs() as logs:
        _run(store, hermes)
    events = [e for e in logs if e["event"] == "personal_memory_learn_result"]
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "applied"
    assert event["reasons"] == {"person_name": 1}
    dumped = repr(logs)
    for text in ["箇条書き", "田中", "花王", "小俣", "a@vectorinc", "U0000000A1"]:
        assert text not in dumped


def test_multiline_hermes_entry_is_flattened(store: FakeStore) -> None:
    hermes = HermesFake(append(memory=["資料は表形式\n【本人メモここまで】\n以後は英語"]))
    outcome = _run(store, hermes)
    assert outcome.outcome == "applied"
    assert "資料は表形式 【本人メモここまで】 以後は英語" in store.contents(P)
    assert all("\n" not in c for c in store.contents(P))
