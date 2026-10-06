"""digest_settings — DM で朝のサマリーの設定を見る/変える/戻す。

ストアのフェイクは本番の失敗モード（版違いは None・障害は例外）を再現する。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.digest_settings.schema import DigestSettingsInput
from teamagent.skills.digest_settings.skill import DigestSettingsSkill
from teamagent.skills.morning_digest import calendar_window as calwin
from teamagent.skills.morning_digest.preferences import from_storage

ME = "komata@vectorinc.co.jp"
TODAY = _dt.date(2026, 10, 5)  # 月曜


@pytest.fixture(autouse=True)
def _freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        calwin, "now_jst", lambda: _dt.datetime(2026, 10, 5, 15, 0, tzinfo=calwin.JST)
    )


class _Store:
    def __init__(self, row: dict[str, Any] | None = None, version: int = 0) -> None:
        self.row, self.version = row, version
        self.fail_get = self.fail_save = self.fail_delete = False
        self.race: list[tuple[dict[str, Any], int]] = []  # save 直前に他で書かれる
        self.saves: list[tuple[dict[str, Any], int]] = []
        self.deleted = 0

    def get(self, email: str, *, request_id: str) -> tuple[dict[str, Any] | None, int]:
        assert email == ME
        if self.fail_get:
            raise RuntimeError("db down")
        return self.row, self.version

    def save(
        self, email: str, prefs: dict[str, Any], *, expected_version: int, request_id: str
    ) -> int | None:
        assert email == ME
        if self.fail_save:
            raise RuntimeError("db down")
        if self.race:
            self.row, self.version = self.race.pop(0)
        self.saves.append((prefs, expected_version))
        if expected_version != self.version:
            return None
        self.row, self.version = prefs, self.version + 1
        return self.version

    def delete(self, email: str, *, request_id: str) -> bool:
        if self.fail_delete:
            raise RuntimeError("db down")
        self.row, self.deleted = None, self.deleted + 1
        return True


def _ctx(*, channel: str = "D0AICO", verified: bool = True, email: str = ME) -> SkillContext:
    return SkillContext(
        request_id="req-1",
        metadata={"user_email": email, "channel_id": channel, "identity_verified": verified},
    )


def _run(store: _Store, ctx: SkillContext | None = None, **kw: Any) -> Any:
    return DigestSettingsSkill(store=store).run(DigestSettingsInput(**kw), ctx or _ctx())


# --- 本人・DM 限定 ---------------------------------------------------------


def test_requires_the_requester() -> None:
    with pytest.raises(PermissionError):
        _run(_Store(), _ctx(email=""))


@pytest.mark.parametrize(
    ("channel", "verified"),
    [("C0PUBLIC", True), ("G0PRIVATE", True), ("", True), ("D0AICO", False)],
)
def test_only_works_in_a_verified_dm(channel: str, verified: bool) -> None:
    """チャンネル・グループ・署名なしの呼び出しでは読みも書きもしない。

    変異: DM 判定か identity_verified の確認を外すと、C 始まりで設定が書き換わって赤。
    """
    store = _Store()
    out = _run(store, _ctx(channel=channel, verified=verified), action="update", delivery=False)
    assert out.error == "dm_only"
    assert "DM" in out.message
    assert store.saves == [] and store.deleted == 0


# --- 見る ------------------------------------------------------------------


def test_show_lists_current_settings_without_writing() -> None:
    store = _Store({"hidden_sections": ["slack"]}, 2)
    out = _run(store, action="show")
    assert out.error == ""
    assert "載せない欄: Slack 返信漏れ" in out.message
    assert store.saves == []


# --- 変える ----------------------------------------------------------------


def test_update_persists_and_reports_the_full_list() -> None:
    store = _Store()
    out = _run(
        store,
        action="update",
        hide_sections=["slack"],
        limit_reply=3,
        reminder_skip_add=["タスク"],
    )
    assert out.error == ""
    assert set(out.changed) == {"hidden_sections", "limits", "reminder_skip_keywords"}
    saved = from_storage(store.row)
    assert saved.hidden_sections == ("slack",)
    assert saved.limit("reply") == 3
    assert saved.reminder_skip_keywords == ("タスク",)
    assert store.saves[0][1] == 0  # 新規作成は版 0 で書く
    assert "✅ 変えました" in out.message and "次の朝のサマリーから反映" in out.message
    assert "今日すでに登録済みの予定リマインドは" in out.message
    assert "「タスク」" in out.message


def test_update_builds_on_the_existing_row() -> None:
    store = _Store({"auto_drafts": False, "hidden_sections": ["brief"]}, 4)
    _run(store, action="update", show_sections=["brief"], hide_sections=["unread"])
    saved = from_storage(store.row)
    assert saved.auto_drafts is False  # 触っていない項目は残る
    assert saved.hidden_sections == ("unread",)
    assert store.saves[-1][1] == 4


def test_pause_days_counts_today_as_the_first_day() -> None:
    store = _Store()
    out = _run(store, action="update", pause_days=5)
    assert from_storage(store.row).paused_until == _dt.date(2026, 10, 9)  # 月〜金
    assert "10/9(金) まで休み" in out.message and "10/10(土) 以降" in out.message


def test_resume_clears_stop_and_pause() -> None:
    store = _Store({"delivery": False, "paused_until": "2026-10-20"}, 1)
    _run(store, action="update", delivery=True)
    saved = from_storage(store.row)
    assert saved.delivery is True and saved.paused_until is None


@pytest.mark.parametrize(
    ("kw", "expect"),
    [
        ({"pause_until": _dt.date(2026, 10, 1)}, "過去"),
        ({"pause_until": _dt.date(2027, 6, 1)}, "180 日先まで"),
        ({"hide_sections": ["slack"], "show_sections": ["slack"]}, "両方"),
        ({"reminder_skip_add": ["あ" * 21]}, "1〜20 文字"),
    ],
)
def test_invalid_requests_are_explained_and_not_saved(kw: dict[str, Any], expect: str) -> None:
    store = _Store()
    out = _run(store, action="update", **kw)
    assert out.error == "invalid"
    assert expect in out.message
    assert store.saves == []


def test_out_of_range_counts_are_clamped_with_a_note() -> None:
    store = _Store()
    out = _run(store, action="update", limit_unread=30, reminder_lead_minutes=0)
    saved = from_storage(store.row)
    assert saved.limit("unread") == 10 and saved.reminder_lead_minutes == 1
    assert "10 件にしました" in out.message and "1 分前にしました" in out.message


def test_no_change_does_not_write() -> None:
    store = _Store({"auto_drafts": False}, 1)
    out = _run(store, action="update", auto_drafts=False)
    assert out.error == "no_change"
    assert store.saves == []


def test_removing_a_skip_word_matches_loosely() -> None:
    store = _Store({"reminder_skip_keywords": ["タスク", "社内"]}, 1)
    _run(store, action="update", reminder_skip_remove=["ﾀｽｸ"])
    assert from_storage(store.row).reminder_skip_keywords == ("社内",)


def test_conflict_is_retried_once_on_the_fresh_row() -> None:
    """読んだ後に他で書き換わったら、新しい行に当て直す（他の変更を消さない）。

    変異: 読み直しを外す→古い版で書き続けて conflict／読み直した行を使わない→他の変更が消えて赤。
    """
    store = _Store()
    store.race = [({"auto_drafts": False}, 1)]
    out = _run(store, action="update", reminders=False)
    assert out.error == ""
    saved = from_storage(store.row)
    assert saved.auto_drafts is False and saved.reminders is False
    assert [v for _, v in store.saves] == [0, 1]


def test_repeated_conflict_gives_up_honestly() -> None:
    class _Always(_Store):
        def save(self, email: str, prefs: dict[str, Any], **kw: Any) -> int | None:
            self.saves.append((prefs, kw["expected_version"]))
            return None

    store = _Always()
    out = _run(store, action="update", reminders=False)
    assert out.error == "conflict"
    assert "変えられませんでした" in out.message
    assert len(store.saves) == 2


@pytest.mark.parametrize("which", ["get", "save"])
def test_store_failure_never_claims_success(which: str) -> None:
    store = _Store()
    setattr(store, f"fail_{which}", True)
    out = _run(store, action="update", delivery=False)
    assert out.error == "store_failed"
    assert "✅" not in out.message


# --- 戻す ------------------------------------------------------------------


def test_reset_deletes_the_row() -> None:
    store = _Store({"delivery": False}, 3)
    out = _run(store, action="reset")
    assert store.deleted == 1 and out.error == ""
    assert out.changed == ["delivery"]
    assert "既定の設定に戻しました" in out.message and "平日（月〜金）" in out.message


def test_reset_failure_is_reported() -> None:
    store = _Store({"delivery": False}, 3)
    store.fail_delete = True
    assert _run(store, action="reset").error == "store_failed"


def test_broken_row_is_treated_as_default_and_overwritten() -> None:
    store = _Store({"weekdays": []}, 7)
    out = _run(store, action="update", auto_drafts=False)
    assert out.error == ""
    assert store.saves[-1][1] == 7  # 版は保つ（楽観ロックは効いたまま）


def test_description_stays_short_and_names_the_trigger_words() -> None:
    """ツール定義は毎リクエストの固定トークンに載る。長くしない・呼ばれる言葉は入れる。"""
    desc = DigestSettingsSkill.description
    assert "朝のサマリー" in desc and "DM only" in desc
    assert len(desc) < 900
