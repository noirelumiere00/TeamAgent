"""朝の自動下書きから社内の差出人を外す（2026-10-01・BU1 ヒアリング）。

声: 「メール下書きが不便（自分で下書き管理しているのとバッティング）」
    「社内向けのメールへの返信は不要では？」

固定すること:
- 社内ドメインの差出人には朝の自動下書きを作らない（既定 ON）
- 社外の差出人は従来どおり作る
- env MORNING_DIGEST_DRAFT_SKIP_INTERNAL=false で従来挙動（社内も作る）に戻せる
- 社内の VIP（IMPORTANT_SENDERS）でも、ドメインが社内なら外す（表示ラベルとは独立）
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest.schema import MailDigestItem, MorningDigestInput
from teamagent.skills.morning_digest.skill import MorningDigestSkill, _is_internal_only

ME = "me@vectorinc.co.jp"


DOMAINS = frozenset({"vectorinc.co.jp", "newstv.co.jp"})


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        # 社内だけのやり取り（グループ会社 NewsTV を含む）
        ({"From": "佐藤 <sato@vectorinc.co.jp>", "To": ME}, True),
        ({"From": "r@newstv.co.jp", "To": ME, "Cc": "SUZUKI@VECTORINC.CO.JP"}, True),
        # 顧客スレッドに同僚が全員返信した（最新の From は社内だが Cc に顧客）→ 外さない
        ({"From": "sato@vectorinc.co.jp", "To": ME, "Cc": "client <a@client.co.jp>"}, False),
        # 顧客からのメール
        ({"From": "client <a@client.co.jp>", "To": ME}, False),
        # サブドメインは社内扱いしない（完全一致）
        ({"From": "a@sub.vectorinc.co.jp", "To": ME}, False),
        # 本人以外が誰もいない・壊れたヘッダ
        ({"From": ME, "To": ME}, False),
        ({"From": "not an address", "To": ""}, False),
    ],
)
def test_is_internal_only(headers: dict[str, str], expected: bool) -> None:
    assert _is_internal_only(headers, ME, DOMAINS) is expected


def test_is_internal_only_without_domains_is_false() -> None:
    assert _is_internal_only({"From": "sato@vectorinc.co.jp", "To": ME}, ME, frozenset()) is False


def test_button_issuance_is_not_affected() -> None:
    """外すのは朝の自動下書きだけ。✏️ ボタン（encode_draft_token）を出す側はこの判定を使わない。

    判定（_draft_skip_internal / _is_internal_only）を参照してよいのは __init__ と
    _create_drafts だけ。ほかのメソッドが参照し始めたら（ボタンにも効かせる変更）赤くする。
    """
    import inspect

    allowed = {"__init__", "_create_drafts"}
    # 末尾の説明文へ「外したか」を渡すこの 1 行だけは、他のメソッドにあってよい（判定には使わない）。
    footer_feed = "out.draft_skip_internal = self._draft_skip_internal"
    users = {
        name
        for name, fn in inspect.getmembers(MorningDigestSkill, predicate=inspect.isfunction)
        if any(
            token in inspect.getsource(fn).replace(footer_feed, "")
            for token in ("_draft_skip_internal", "_is_internal_only")
        )
    }
    assert users == allowed
    assert any(
        "encode_draft_token" in inspect.getsource(fn)
        for _, fn in inspect.getmembers(MorningDigestSkill, predicate=inspect.isfunction)
    )


class _Msg:
    def __init__(self, sender: str, thread_id: str) -> None:
        self.headers: dict[str, str] = {"From": sender, "To": ME, "Subject": "s"}
        self.thread_id = thread_id


class _FakeGmail:
    def __init__(self) -> None:
        self.list_calls = 0

    def list_drafts(self, rid: str, **_: Any) -> list[Any]:
        self.list_calls += 1
        return []


def _run(monkeypatch: pytest.MonkeyPatch, senders: list[str], *, skip_env: str | None) -> list[str]:
    if skip_env is None:
        monkeypatch.delenv("MORNING_DIGEST_DRAFT_SKIP_INTERNAL", raising=False)
    else:
        monkeypatch.setenv("MORNING_DIGEST_DRAFT_SKIP_INTERNAL", skip_env)
    monkeypatch.setenv("DIGEST_INTERNAL_DOMAIN", "vectorinc.co.jp")
    monkeypatch.setenv("IMPORTANT_SENDERS", "boss@vectorinc.co.jp")
    skill = MorningDigestSkill(token_store=None)
    gmail = _FakeGmail()
    drafted: list[str] = []

    def _fake_gmail_for(token: Any, *, readonly: bool) -> _FakeGmail:
        return gmail

    def _fake_single(gmail_rw: Any, msg: _Msg, requester: str, ctx: Any) -> tuple[bool, float]:
        drafted.append(msg.headers["From"])
        return True, 0.0

    monkeypatch.setattr(skill, "_gmail_for", _fake_gmail_for)
    monkeypatch.setattr(skill, "_create_single_draft", _fake_single)
    msgs = [_Msg(s, f"t{i}") for i, s in enumerate(senders)]
    items = [
        MailDigestItem(counterpart_masked="x", importance="high", to_self=True) for _ in senders
    ]
    skill._create_drafts(
        object(),
        ME,
        MorningDigestInput(max_drafts=5),
        msgs,
        items,
        SkillContext(request_id="r", metadata={"user_email": ME}),
    )
    return drafted


def test_internal_senders_are_skipped_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    drafted = _run(
        monkeypatch,
        [
            "佐藤 <sato@vectorinc.co.jp>",
            "client <a@client.co.jp>",
            "boss@vectorinc.co.jp",  # 社内の VIP も外す
            "r@newstv.co.jp",  # グループ会社も社内
        ],
        skip_env=None,
    )
    assert drafted == ["client <a@client.co.jp>"]


def test_internal_domains_env_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MORNING_DIGEST_DRAFT_INTERNAL_DOMAINS", "vectorinc.co.jp")
    drafted = _run(monkeypatch, ["sato@vectorinc.co.jp", "r@newstv.co.jp"], skip_env=None)
    assert drafted == ["r@newstv.co.jp"]


def test_env_false_restores_previous_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    drafted = _run(
        monkeypatch,
        ["佐藤 <sato@vectorinc.co.jp>", "client <a@client.co.jp>"],
        skip_env="false",
    )
    assert drafted == ["佐藤 <sato@vectorinc.co.jp>", "client <a@client.co.jp>"]


def test_only_internal_senders_never_touches_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    """社内だけなら対象ゼロ＝ Gmail に触れずに 0 件で返る（無駄な API 呼び出しをしない）。"""
    monkeypatch.delenv("MORNING_DIGEST_DRAFT_SKIP_INTERNAL", raising=False)
    monkeypatch.setenv("DIGEST_INTERNAL_DOMAIN", "vectorinc.co.jp")
    skill = MorningDigestSkill(token_store=None)

    class _GmailNoCall:
        def list_drafts(self, rid: str, **_: Any) -> list[Any]:
            raise AssertionError("対象ゼロなら gmail に触れないはず")

    class _M:
        headers: ClassVar[dict[str, str]] = {
            "From": "sato@vectorinc.co.jp",
            "To": ME,
            "Subject": "s",
        }

    monkeypatch.setattr(skill, "_gmail_for", lambda token, *, readonly: _GmailNoCall())
    created, cost = skill._create_drafts(
        object(),
        ME,
        MorningDigestInput(max_drafts=3),
        [_M()],
        [MailDigestItem(counterpart_masked="x", importance="high", to_self=True)],
        SkillContext(request_id="r", metadata={"user_email": ME}),
    )
    assert (created, cost) == (0, 0.0)


def test_footer_flag_follows_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """末尾の説明文に使う draft_skip_internal は env と一致する（env の読み方は skill の 1 か所）。"""
    monkeypatch.delenv("MORNING_DIGEST_DRAFT_SKIP_INTERNAL", raising=False)
    assert MorningDigestSkill(token_store=None)._draft_skip_internal is True
    monkeypatch.setenv("MORNING_DIGEST_DRAFT_SKIP_INTERNAL", "false")
    assert MorningDigestSkill(token_store=None)._draft_skip_internal is False
