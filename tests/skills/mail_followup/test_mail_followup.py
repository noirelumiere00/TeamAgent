"""mail_followup Skill のオフラインテスト（課金0・外部I/O無し）。

fake GmailClient / InMemoryTokenStore を注入し、死守ライン（G1 本人限定 / G2 連携必須 /
G3 マスク / G5 クエリ限定）と放置日数・並び順・正直ラベリングを検証する。実 Gmail 不要。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from teamagent.adapters.oauth_token_store import InMemoryTokenStore
from teamagent.skills.base import SkillContext
from teamagent.skills.mail_followup.schema import MailFollowupInput
from teamagent.skills.mail_followup.skill import (
    MailFollowupSkill,
    _hash_id,
    _idle_days,
    _mask_email,
)

OWNER = "s-komata@vectorinc.co.jp"
NOW_MS = 1_700_000_000_000  # 固定 now（テスト決定性）
MS_PER_DAY = 86_400_000


# ── fakes ─────────────────────────────────────────────────────────────────


@dataclass
class _Ref:
    id: str
    thread_id: str = "t"


@dataclass
class _Msg:
    headers: dict[str, str]
    internal_date_ms: int | None
    id: str = "m"
    thread_id: str = "t"


class FakeGmail:
    """list_messages / get_message(metadata) だけを持つ最小 fake。"""

    def __init__(self, msgs: list[_Msg]) -> None:
        self._msgs = msgs
        self.last_query: str | None = None
        self.last_format: str | None = None

    def list_messages(
        self,
        query: str | None,
        request_id: str,
        *,
        label_ids: Any = None,
        max_results: int = 50,
        **kw: Any,
    ) -> tuple[list[_Ref], None]:
        self.last_query = query
        n = min(len(self._msgs), max_results)
        return ([_Ref(id=f"m{i}") for i in range(n)], None)

    def get_message(
        self, msg_id: str, request_id: str, *, format: str = "full", user_id: str = "me"
    ) -> _Msg:
        self.last_format = format
        return self._msgs[int(msg_id[1:])]


def _ctx(user_email: str | None = OWNER) -> SkillContext:
    return SkillContext(request_id="r-test", user_id="U1", metadata={"user_email": user_email})


def _msg(sender: str, subject: str, days_ago: int) -> _Msg:
    return _Msg(
        headers={"From": sender, "Subject": subject},
        internal_date_ms=NOW_MS - days_ago * MS_PER_DAY,
    )


# ── G1 / G2 fail-closed ────────────────────────────────────────────────────


def test_g1_requires_user_email() -> None:
    skill = MailFollowupSkill(gmail=FakeGmail([]), now_ms=NOW_MS)
    with pytest.raises(PermissionError):
        skill.run(MailFollowupInput(client_name="森ビル"), _ctx(user_email=None))


def test_g1_blank_user_email_fails_closed() -> None:
    skill = MailFollowupSkill(gmail=FakeGmail([]), now_ms=NOW_MS)
    with pytest.raises(PermissionError):
        skill.run(MailFollowupInput(client_name="森ビル"), _ctx(user_email="   "))


def test_g2_unconnected_fails_closed() -> None:
    # gmail 未注入 + 空 TokenStore → 本人トークン無し → fail-closed。
    skill = MailFollowupSkill(token_store=InMemoryTokenStore(), now_ms=NOW_MS)
    with pytest.raises(PermissionError):
        skill.run(MailFollowupInput(client_name="森ビル"), _ctx())


def test_g2_no_token_store_fails_closed() -> None:
    skill = MailFollowupSkill(now_ms=NOW_MS)  # token_store も gmail も無し
    with pytest.raises(PermissionError):
        skill.run(MailFollowupInput(client_name="森ビル"), _ctx())


# ── happy path / 並び順 / マスク / 正直ラベル ──────────────────────────────


def test_happy_path_sorted_and_masked() -> None:
    msgs = [
        _msg("田中 <tanaka@moribuild.co.jp>", "ご提案の件", days_ago=2),
        _msg("佐藤 <sato@moribuild.co.jp>", "請求書送付", days_ago=10),
        _msg("鈴木 <suzuki@moribuild.co.jp>", "日程調整", days_ago=5),
    ]
    skill = MailFollowupSkill(gmail=FakeGmail(msgs), now_ms=NOW_MS)
    out = skill.run(MailFollowupInput(client_name="森ビル", lookback_days=30), _ctx())

    assert out.scanned_count == 3
    assert out.total_cost_usd == 0.0
    assert out.inbox_owner_masked == "s***@vectorinc.co.jp"
    assert out.note  # 正直な但し書きが入る
    # 放置日数が大きい順
    assert [it.idle_days for it in out.items] == [10, 5, 2]
    # 相手アドレスはマスクされ、生アドレスは出ない
    for it in out.items:
        assert it.counterpart_masked.endswith("@moribuild.co.jp")
        assert "@moribuild.co.jp" in it.counterpart_masked
        assert "tanaka@" not in it.counterpart_masked
        assert it.evidence_ref and "@" not in it.evidence_ref


def test_idle_days_filter() -> None:
    msgs = [
        _msg("a@x.co.jp", "件名A", days_ago=2),
        _msg("b@x.co.jp", "件名B", days_ago=9),
    ]
    skill = MailFollowupSkill(gmail=FakeGmail(msgs), now_ms=NOW_MS)
    out = skill.run(MailFollowupInput(client_name="X社", idle_days=5, lookback_days=30), _ctx())
    assert [it.idle_days for it in out.items] == [9]


def test_query_is_client_scoped_and_metadata_only() -> None:
    fake = FakeGmail([_msg("a@x.co.jp", "件名", days_ago=1)])
    skill = MailFollowupSkill(gmail=fake, now_ms=NOW_MS)
    skill.run(MailFollowupInput(client_name="INPEX", lookback_days=7), _ctx())
    # G5: client 名 + 期間 + 受信限定がクエリに入る
    assert '"INPEX"' in (fake.last_query or "")
    assert "newer_than:7d" in (fake.last_query or "")
    assert "-in:sent" in (fake.last_query or "")
    # G6 構造的: 本文は読まない（metadata のみ）
    assert fake.last_format == "metadata"


def test_subject_is_scrubbed_and_truncated() -> None:
    long_subject = "重要 " + "x" * 200
    msgs = [_msg("a@x.co.jp", long_subject, days_ago=1)]
    skill = MailFollowupSkill(gmail=FakeGmail(msgs), now_ms=NOW_MS)
    out = skill.run(MailFollowupInput(client_name="X社"), _ctx())
    assert len(out.items[0].subject_scrubbed) <= 80


# ── 純粋関数 ────────────────────────────────────────────────────────────────


def test_mask_email() -> None:
    assert _mask_email("tanaka@moribuild.co.jp") == "t***@moribuild.co.jp"
    assert _mask_email("garbage") == "***"


def test_hash_id_is_not_raw() -> None:
    h = _hash_id("18f0a1b2c3")
    assert h != "18f0a1b2c3"
    assert len(h) == 12


def test_idle_days_clamps_negative() -> None:
    assert _idle_days(NOW_MS + MS_PER_DAY, NOW_MS) == 0  # 未来日時は 0 に丸める
    assert _idle_days(None, NOW_MS) == 0
    assert _idle_days(NOW_MS - 3 * MS_PER_DAY, NOW_MS) == 3


# ── レビュー指摘の回帰テスト ────────────────────────────────────────────────


def test_idle_days_widens_scan_window() -> None:
    """idle_days > lookback_days のとき走査窓を広げる（広げないと post-filter で全滅・誤答）。"""
    fake = FakeGmail([])
    skill = MailFollowupSkill(gmail=fake, now_ms=NOW_MS)
    skill.run(MailFollowupInput(client_name="森ビル", idle_days=30, lookback_days=14), _ctx())
    assert "newer_than:33d" in (fake.last_query or "")  # min(90, max(14, 30+3))


def test_credential_error_becomes_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """認証情報の解決失敗(ValueError)は PermissionError に変換（dispatch が連携案内に出せる）。"""
    from teamagent.adapters import gmail_client as gc
    from teamagent.adapters.oauth_token_store import OAuthToken

    def _boom(token: Any, *, readonly: bool = True) -> Any:
        raise ValueError("GOOGLE_CLIENT_ID 未設定")

    monkeypatch.setattr(gc.GmailClient, "from_user_token", staticmethod(_boom))
    store = InMemoryTokenStore({OWNER: OAuthToken(refresh_token="x")})
    skill = MailFollowupSkill(token_store=store, now_ms=NOW_MS)
    with pytest.raises(PermissionError):
        skill.run(MailFollowupInput(client_name="森ビル"), _ctx())
