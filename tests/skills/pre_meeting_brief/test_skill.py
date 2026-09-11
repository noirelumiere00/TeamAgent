"""pre_meeting_brief Skill の引き当て・RLS・面ガード・2 経路一致。

フェイク DB は **本番の失敗モード** を再現する:
  - ``user_groups`` を渡さないと ``_apply_session`` が GUC を立てず、domain 共有の
    事例が 1 件も返らない（policy 0010 の acl_groups 分岐に当たらない）
  - 母集団が ``case_corpus='true'`` で絞られていないと全社資料が混ざる
  - ``%`` ``_`` ``\\`` を含む社名は ILIKE のエスケープが無いと全件マッチする
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from teamagent.adapters.gcalendar_client import extract_events
from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest.schema import CalendarEventItem
from teamagent.skills.pre_meeting_brief.schema import PreMeetingBriefInput
from teamagent.skills.pre_meeting_brief.signals import build_signal_input
from teamagent.skills.pre_meeting_brief.skill import (
    SURFACE_BLOCKED_MESSAGE,
    PreMeetingBriefSkill,
)

USER = "komata@vectorinc.co.jp"

# domain 共有（acl_groups）でしか見えない事例。user_groups を落とすと返らない。
CASE_ROWS: list[dict[str, Any]] = [
    {
        "title": "260706_事業本部ショート動画事例_v2.pptx",
        "source_uri": "https://drive.google.com/file/d/abc/view",
        "owner_email": "mochizuki@vectorinc.co.jp",
        "case_client": "北都カード×みなとランド",
        "case_product": "切り抜き165本",
        "case_effect": "視聴800万回、指名検索が前月比5倍。",
        "case_owner": "佐藤",
        "case_external_use": "NG",
        "case_external_use_note": "対外利用NG（事例集フォルダが展開NG）",
        "case_industry": "金融／テーマパーク文脈",
        "updated_at": "2026-07-06",
        "acl_groups": ["vectorinc.co.jp"],
        "case_corpus": "true",
    },
    {
        "title": "施策事例集",
        "source_uri": "https://docs.google.com/spreadsheets/d/xyz",
        "owner_email": "shimizu@vectorinc.co.jp",
        "case_client": "東光製作所",
        "case_product": "新卒採用ショート",
        "case_effect": "指名検索+180%。",
        "case_owner": "高橋",
        "case_external_use": "OK",
        "case_external_use_note": "",
        "case_industry": "防災機器",
        "updated_at": "2026-07-08",
        "acl_groups": ["vectorinc.co.jp"],
        "case_corpus": "true",
    },
    {
        "title": "無関係な提案書.pptx",
        "source_uri": "https://drive.google.com/file/d/zzz/view",
        "owner_email": "x@vectorinc.co.jp",
        "case_client": "花王",
        "case_product": "",
        "case_effect": "",
        "case_owner": "",
        "case_external_use": "",
        "case_external_use_note": "",
        "case_industry": "",
        "updated_at": "2026-06-01",
        "acl_groups": ["vectorinc.co.jp"],
        # ⚠️ 事例集ではない＝母集団に入ってはいけない
        "case_corpus": None,
    },
]


class _FakeConn:
    def __init__(self, groups: list[str] | None) -> None:
        self.groups = groups


class _FakePg:
    """``connection`` の kwargs をそのまま記録するフェイク（RLS の穴を検出する）。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = CASE_ROWS if rows is None else rows
        self.connect_kwargs: list[dict[str, Any]] = []
        self.stage_calls: list[int] = []

    class _Ctx:
        def __init__(self, conn: _FakeConn) -> None:
            self.conn = conn

        def __enter__(self) -> _FakeConn:
            return self.conn

        def __exit__(self, *a: object) -> None:
            return None

    def connection(self, **kwargs: Any) -> Any:
        self.connect_kwargs.append(kwargs)
        return self._Ctx(_FakeConn(kwargs.get("user_groups")))

    # --- ここから下が「本番の失敗モード」の再現 ---
    def _visible(self, conn: _FakeConn) -> list[dict[str, Any]]:
        """user_groups が無ければ domain 共有資料は 1 件も見えない（policy 0010）。"""
        if not conn.groups:
            return []
        return [
            r
            for r in self.rows
            if set(r.get("acl_groups") or []) & set(conn.groups)
            # 母集団は case_corpus='true' のみ
            and r.get("case_corpus") == "true"
        ]

    def case_corpus_available(self, conn: _FakeConn, request_id: str | None = None) -> bool:
        return bool(self._visible(conn))

    def get_industry_for_client(
        self, conn: _FakeConn, client_name: str, request_id: str | None = None
    ) -> str | None:
        for r in self._visible(conn):
            if r["case_client"] == client_name and r["case_industry"]:
                return str(r["case_industry"])
        return None

    def list_case_studies(
        self,
        conn: _FakeConn,
        *,
        client_name: str = "",
        industry: str | None = None,
        product: str | None = None,
        limit: int = 3,
        stage: int = 1,
        request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.stage_calls.append(stage)
        rows = self._visible(conn)
        if stage == 1:
            return [r for r in rows if r["case_client"] == client_name][:limit]
        if stage == 2:
            # ILIKE のエスケープを再現: % _ \ はリテラル扱い
            needle = re.escape(client_name)
            return [r for r in rows if re.search(needle, r["case_client"])][:limit]
        if stage == 3:
            if not industry or not product:
                return []
            return [
                r
                for r in rows
                if r["case_industry"] == industry and product in (r["case_product"] or "")
            ][:limit]
        if stage == 4:
            if not industry:
                return []
            return [r for r in rows if r["case_industry"] == industry][:limit]
        return []


def _ctx(**meta: Any) -> SkillContext:
    base: dict[str, Any] = {"user_email": USER, "channel_id": "D001"}
    base.update(meta)
    return SkillContext(request_id="req-test", metadata=base)


def _event_item(**kw: Any) -> CalendarEventItem:
    base: dict[str, Any] = {
        "summary_display": "【社外】初田様_東光製作所",
        "summary_scrubbed": "【社外】初田様_東光製作所",
        "start_at": "2026-09-11T14:00:00+09:00",
        "end_at": "2026-09-11T15:00:00+09:00",
        "attendee_list_available": True,
    }
    base.update(kw)
    return CalendarEventItem(**base)


# ── G1 fail-closed ────────────────────────────────────────────────────
def test_missing_user_email_raises_permission_error() -> None:
    skill = PreMeetingBriefSkill(pg=_FakePg(), events=[])
    with pytest.raises(PermissionError):
        skill.run(PreMeetingBriefInput(), SkillContext(request_id="r", metadata={}))


# ── 宛先 deny-by-default（tool 経路）──────────────────────────────────
@pytest.mark.parametrize("channel_id", ["", "C0AB", "G0AB", "W0AB"])
def test_tool_path_blocks_non_dm_surfaces(channel_id: str) -> None:
    """変異: ``is_private_surface`` を空文字許容へ戻すと件数・社名が出て赤。"""
    pg = _FakePg()
    skill = PreMeetingBriefSkill(pg=pg)
    out = skill.run(PreMeetingBriefInput(), _ctx(channel_id=channel_id))
    assert out.message == SURFACE_BLOCKED_MESSAGE
    assert out.items == []
    assert out.external_count == 0
    assert out.scanned is False
    assert pg.connect_kwargs == []  # 金庫にも触らない


# ── RLS ───────────────────────────────────────────────────────────────
def test_rls_metadata_always_carries_user_groups_and_member_role() -> None:
    """変異: ``user_groups=rls["user_groups"]`` を落とすと事例が 0 件になり赤。"""
    pg = _FakePg()
    skill = PreMeetingBriefSkill(pg=pg, events=[_event_item()])
    out = skill.run(PreMeetingBriefInput(), _ctx())
    assert pg.connect_kwargs, "connection が呼ばれていない"
    kwargs = pg.connect_kwargs[0]
    assert kwargs["app_role"] == "teamagent_app"
    assert kwargs["user_email"] == USER
    assert kwargs["user_groups"] == ["vectorinc.co.jp"]
    assert kwargs["user_role"] == "member"
    assert out.items and out.items[0].cases, "user_groups があれば事例が引ける"


def test_admin_role_is_never_passed() -> None:
    """この経路の connection kwargs に admin が現れない（昇格口が存在しない）。"""
    pg = _FakePg()
    PreMeetingBriefSkill(pg=pg, events=[_event_item()]).run(PreMeetingBriefInput(), _ctx())
    assert all(k.get("user_role") != "admin" for k in pg.connect_kwargs)


# ── 引き当て ──────────────────────────────────────────────────────────
def test_stage1_hit_does_not_run_lower_stages() -> None:
    pg = _FakePg()
    PreMeetingBriefSkill(pg=pg, events=[_event_item()]).run(PreMeetingBriefInput(), _ctx())
    assert pg.stage_calls == [1]


def test_no_industry_means_stage3_and_4_never_run() -> None:
    """業種が金庫に無ければ段3/4 を走らせない（業種を推測しない）。

    変異: ``if not industry: return`` を外すと stage 3/4 が呼ばれて赤。
    """
    pg = _FakePg()
    item = _event_item(summary_display="【社外】未知株式会社様 打合せ")
    PreMeetingBriefSkill(pg=pg, events=[item]).run(PreMeetingBriefInput(), _ctx())
    assert 3 not in pg.stage_calls
    assert 4 not in pg.stage_calls


def test_corpus_population_is_limited_to_case_corpus() -> None:
    """変異: 母集団から ``case_corpus='true'`` を外すと無関係な提案書が混じって赤。"""
    pg = _FakePg()
    item = _event_item(summary_display="【社外】花王様 打合せ")
    out = PreMeetingBriefSkill(pg=pg, events=[item]).run(PreMeetingBriefInput(), _ctx())
    titles = [c.company_display for i in out.items for c in i.cases]
    assert "花王" not in titles


def test_like_metacharacters_in_company_name_do_not_match_everything() -> None:
    pg = _FakePg()
    item = _event_item(summary_display="【社外】100%_青葉広告様 打合せ")
    out = PreMeetingBriefSkill(pg=pg, events=[item]).run(PreMeetingBriefInput(), _ctx())
    assert out.items[0].cases == []


def test_case_without_effect_uses_fixed_text_not_chunk_body() -> None:
    """``case_effect`` が無い document で chunk 本文が混入しない。"""
    rows = [dict(CASE_ROWS[1], case_effect="", case_corpus="true")]
    pg = _FakePg(rows)
    out = PreMeetingBriefSkill(pg=pg, events=[_event_item()]).run(PreMeetingBriefInput(), _ctx())
    assert out.items[0].cases[0].effect_display == ""


def test_ng_case_carries_reason_note() -> None:
    pg = _FakePg()
    item = _event_item(summary_display="【社外】北都カード×みなとランド様 打合せ")
    out = PreMeetingBriefSkill(pg=pg, events=[item]).run(PreMeetingBriefInput(), _ctx())
    note = out.items[0].cases[0].external_use_note
    assert note.startswith("⚠")
    assert "展開NG" in note


def test_corpus_missing_reports_unavailable() -> None:
    """事例集が 1 件も無ければ ``corpus_available=False``（節ごと出さない側へ倒す）。"""
    pg = _FakePg(rows=[])
    out = PreMeetingBriefSkill(pg=pg, events=[_event_item()]).run(PreMeetingBriefInput(), _ctx())
    assert out.corpus_available is False
    assert out.items == []


def test_client_direct_lookup_does_not_touch_calendar() -> None:
    pg = _FakePg()
    skill = PreMeetingBriefSkill(pg=pg)  # calendar=None
    out = skill.run(PreMeetingBriefInput(client="東光製作所"), _ctx())
    assert out.items and out.items[0].cases[0].company_display == "東光製作所"


# ── 2 経路の一致（構造で止めた事故の証明）────────────────────────────
def test_runner_and_tool_paths_produce_identical_items() -> None:
    """同一の生 ``events.list`` item を 2 経路に流すと結果が完全一致する。

    変異: ``signals_from_item`` に再抽出ロジックを入れる／片方だけ別関数にすると赤。
    """
    raw = {
        "id": "e9",
        "summary": "【社外】青葉広告山田様",
        "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "description": "クライアント：東光製作所／代理店：青葉広告（山田様）",
        "organizer": {"email": "boss@vectorinc.co.jp"},
        "attendees": [
            {"email": "me@vectorinc.co.jp", "self": True},
            {"email": "yoshida@dentsu.co.jp"},
        ],
    }
    (detail,) = extract_events([raw], want_description=True)

    class _FakeCal:
        def list_events(self, request_id: str, **kw: object) -> list[object]:
            return [detail]

    tool_out = PreMeetingBriefSkill(pg=_FakePg(), calendar=_FakeCal()).run(
        PreMeetingBriefInput(), _ctx()
    )

    # 定期便経路: morning_digest と同じ写し替えを build_signal_input 経由で行う
    sig = build_signal_input(detail)
    item = CalendarEventItem(
        summary_display=str(detail.summary),
        summary_scrubbed=str(detail.summary),
        start_at=detail.start,
        end_at=detail.end,
        all_day=detail.all_day,
        attendee_domains=list(sig.attendee_domains),
        attendee_list_available=sig.attendee_list_available,
        has_client_line=sig.has_client_line,
        client_hint_display=sig.client_hint,
        agency_display=sig.agency_hint,
    )
    runner_out = PreMeetingBriefSkill(pg=_FakePg(), events=[item]).run(
        PreMeetingBriefInput(), _ctx()
    )

    assert tool_out.items == runner_out.items
    assert tool_out.items[0].clients_display == ["東光製作所"]
    assert tool_out.items[0].agency_display == "青葉広告(山田様)"


# ── ログ ─────────────────────────────────────────────────────────────
def test_done_log_kwargs_are_exactly_the_allowed_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pre_meeting_brief_done`` のログ kwargs 集合を厳密一致で固定する。

    変異: 社名・MTG 名・URL・ドメインのどれかを 1 つ足すと赤。
    """
    captured: list[dict[str, Any]] = []

    import teamagent.skills.pre_meeting_brief.skill as mod

    class _Spy:
        def info(self, event: str, **kw: Any) -> None:
            if event == "pre_meeting_brief_done":
                captured.append(kw)

        def warning(self, *a: Any, **k: Any) -> None:
            return None

    monkeypatch.setattr(mod, "logger", _Spy())
    PreMeetingBriefSkill(pg=_FakePg(), events=[_event_item()]).run(PreMeetingBriefInput(), _ctx())
    assert captured
    assert set(captured[0]) == {
        "request_id",
        "external",
        "uncertain",
        "items",
        "cases",
        "corpus",
    }


# ── 複数クライアント: 2 社目が丸ごと消えない（社ごとの枠）───────────────
MULTI_ROWS: list[dict[str, Any]] = [
    {
        "title": "施策事例集",
        "source_uri": "https://docs.google.com/spreadsheets/d/xyz",
        "owner_email": "a@vectorinc.co.jp",
        "case_client": "緑川フーズ",
        "case_product": f"施策{i}",
        "case_effect": "目標再生数120%超で着地。",
        "case_owner": "中村",
        "case_external_use": "NG",
        "case_external_use_note": "数値は開示NG",
        "case_industry": "外食",
        "updated_at": "2026-07-08",
        "acl_groups": ["vectorinc.co.jp"],
        "case_corpus": "true",
    }
    for i in range(3)
] + [
    {
        "title": "施策事例集",
        "source_uri": "https://docs.google.com/spreadsheets/d/xyz",
        "owner_email": "b@vectorinc.co.jp",
        "case_client": "白水飲料",
        "case_product": "長尺＋切り抜き30本",
        "case_effect": "ゼロだった指名検索を期間中継続的に創出。",
        "case_owner": "小林",
        "case_external_use": "confidential",
        "case_external_use_note": "開示NG（confidential）",
        "case_industry": "飲料/健康",
        "updated_at": "2026-07-08",
        "acl_groups": ["vectorinc.co.jp"],
        "case_corpus": "true",
    }
]


def test_second_client_is_never_dropped_entirely() -> None:
    """1 社目が枠を食い尽くして 2 社目が丸ごと消える、が起きない。

    実測の欠陥: 緑川フーズ 3 件・白水飲料 1 件のとき、社ごとの枠が無いと
    [緑川フーズ系] 3 行だけになり、白水飲料の事例も接頭辞も出ない。

    変異: ``_build_item`` の ``per_client`` を ``input.max_cases`` に戻すと赤。
    """
    pg = _FakePg(MULTI_ROWS)
    item = _event_item(
        summary_display="【外出】青葉広告鈴木さま",
        summary_scrubbed="【外出】青葉広告鈴木さま",
        has_client_line=True,
        client_hint_display="緑川フーズ／白水飲料",
    )
    out = PreMeetingBriefSkill(pg=pg, events=[item]).run(PreMeetingBriefInput(), _ctx())
    (brief,) = out.items
    assert brief.clients_display == ["緑川フーズ", "白水飲料"]
    groups = [c.client_group for c in brief.cases]
    assert "緑川フーズ" in groups
    assert "白水飲料" in groups
    # 既定 max_cases=3・2 社 → 各 1 件（DELTA §3 の実物例と同じ形）
    assert len(brief.cases) == 2


def test_single_client_still_gets_the_full_quota() -> None:
    """1 社だけの MTG は従来どおり max_cases まで並ぶ（枠の導入で減らさない）。"""
    pg = _FakePg(MULTI_ROWS)
    item = _event_item(
        summary_display="【社外】緑川フーズ様",
        summary_scrubbed="【社外】緑川フーズ様",
        has_client_line=True,
        client_hint_display="緑川フーズ",
    )
    out = PreMeetingBriefSkill(pg=pg, events=[item]).run(PreMeetingBriefInput(), _ctx())
    (brief,) = out.items
    assert len(brief.cases) == 3


# ── match_stage（どの段で当たったかを出力に残す）──────────────────────
def test_match_stage_records_the_stage_that_hit() -> None:
    """段3/4 に落ちて精度が悪化したことを後追いできるようにする。

    変異: ``_to_case`` の ``match_stage=stage`` を ``0`` に戻すと赤。
    """
    pg = _FakePg()
    item = _event_item(has_client_line=True, client_hint_display="東光製作所")
    out = PreMeetingBriefSkill(pg=pg, events=[item]).run(PreMeetingBriefInput(), _ctx())
    (brief,) = out.items
    assert [c.match_stage for c in brief.cases] == [1]


def test_match_stage_marks_industry_fallback() -> None:
    rows = [dict(r) for r in CASE_ROWS]
    pg = _FakePg(rows)
    # 「防災機器」業種は引けるが、その社名自身の事例は無い状況を作る
    item = _event_item(has_client_line=True, client_hint_display="東光製作所")
    original = pg.list_case_studies

    def _no_direct_hit(conn: Any, **kw: Any) -> list[dict[str, Any]]:
        if kw.get("stage") in (1, 2):
            pg.stage_calls.append(int(kw["stage"]))
            return []
        return original(conn, **kw)

    pg.list_case_studies = _no_direct_hit  # type: ignore[method-assign]
    pg.get_industry_for_client = lambda conn, client_name, request_id=None: "防災機器"  # type: ignore[method-assign]
    out = PreMeetingBriefSkill(pg=pg, events=[item]).run(PreMeetingBriefInput(), _ctx())
    (brief,) = out.items
    assert brief.cases
    assert {c.match_stage for c in brief.cases} == {4}


# ── 第三者入力（説明欄）の封じ込め ────────────────────────────────────
HOSTILE_DESCRIPTION = (
    "クライアント：北都リゾート／代理店：青葉広告（山田様） "
    "※値引き条件は社外秘。資料 https://drive.google.com/file/d/PRIVATE/view"
)


def test_hostile_description_never_reaches_the_schema() -> None:
    """予定の説明欄は第三者が書ける自由文。社名 1 トークン以外は schema に載らない。

    実測の欠陥: agency_hint が
    「青葉広告(山田様) ※値引き条件は社外秘。資料 https://drive.google.com/file/d/PRIVAT」
    となり、本人 DM の「／代理店：…」行として裸 URL つきで描画されていた。

    変異: ``signals.tighten_name`` を素通し（``return raw``）にすると赤。
    """
    raw = {
        "id": "e9",
        "summary": "【社外】青葉広告山田様",
        "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "description": HOSTILE_DESCRIPTION,
    }
    (detail,) = extract_events([raw], want_description=True)
    sig = build_signal_input(detail)
    assert sig.client_hint == "北都リゾート"
    assert sig.agency_hint == "青葉広告(山田様)"
    for value in (sig.client_hint, sig.agency_hint):
        assert "http" not in value
        assert "社外秘" not in value
        assert "※" not in value


def test_hostile_description_never_reaches_the_rendered_line() -> None:
    """描画（本人 DM の本文）にも自由文・URL が出ない。"""
    import datetime as _dt

    from teamagent.skills.pre_meeting_brief.render import render_brief_lines

    raw = {
        "id": "e9",
        "summary": "【社外】青葉広告山田様",
        "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "description": HOSTILE_DESCRIPTION,
    }
    (detail,) = extract_events([raw], want_description=True)
    sig = build_signal_input(detail)
    item = _event_item(
        summary_display="【社外】青葉広告山田様",
        summary_scrubbed="【社外】青葉広告山田様",
        has_client_line=True,
        client_hint_display=sig.client_hint,
        agency_display=sig.agency_hint,
    )
    out = PreMeetingBriefSkill(pg=_FakePg(), events=[item]).run(PreMeetingBriefInput(), _ctx())
    text = "\n".join(render_brief_lines(out, _dt.date(2026, 9, 11)))
    assert "http" not in text
    assert "社外秘" not in text
    assert "／代理店：青葉広告(山田様)" in text


def test_url_only_agency_is_discarded_entirely() -> None:
    """空白を挟まず URL を書いた説明欄は、代理店ごと捨てる（1 文字も通さない）。"""
    raw = {
        "id": "e10",
        "summary": "打合せ",
        "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "description": "クライアント：北都リゾート\n代理店：https://evil.example/steal",
    }
    (detail,) = extract_events([raw], want_description=True)
    sig = build_signal_input(detail)
    assert sig.agency_hint == ""
