"""事例ブリーフの本人 DM で、絵文字（書記素クラスタ）を途中で切らないことのテスト。

予定の説明欄・予定名は第三者も書ける自由文で、絵文字が入りうる。コードポイント単位で
切ると、切り口に片割れ（孤立した地域指示子は□で囲んだ英字に見える・宙ぶらりんの ZWJ・
肌色の抜けた 👍 など）が DM に残る。

本番と同じ変換を通す: Google の生の予定 → ``extract_events`` → 朝ダイジェストの
``_collect_calendar``（``build_signal_input`` で社名・代理店を ``tighten_name`` の 40 字に
絞る）→ ``PreMeetingBriefSkill`` → ``render_brief_lines``（``harden`` で予定名を 60 字・
社名/代理店を 40 字に切る）。

- 予定名: 描画の ``harden`` の 60 字で切れる
- クライアント・代理店: ``tighten_name`` の 40 字で切れる（以降の ``harden`` は既に収まっている）

⚠️ 不可視文字（ZWJ・VS16 など）はソースに直接書かず ``\\u`` エスケープで書く。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest

from teamagent.adapters.gcalendar_client import extract_events
from teamagent.skills.morning_digest import calendar_window as calwin
from teamagent.skills.morning_digest.schema import MorningDigestInput
from teamagent.skills.morning_digest.skill import MorningDigestSkill
from teamagent.skills.pre_meeting_brief.render import render_brief_lines
from teamagent.skills.pre_meeting_brief.schema import PreMeetingBriefInput
from teamagent.skills.pre_meeting_brief.skill import PreMeetingBriefSkill
from tests.skills.pre_meeting_brief.test_skill import _ctx, _FakePg

# NFKC を通るので、合成済みの 1 字に畳まれる「か＋結合濁点」は入れない（割れようがない）。
_CLUSTERS = {
    "family": "\U0001f468‍\U0001f469‍\U0001f467",  # 👨 ZWJ 👩 ZWJ 👧
    "flag": "\U0001f1ef\U0001f1f5",  # 地域指示子 2 個（🇯🇵）
    "skin_tone": "\U0001f44d\U0001f3fd",  # 👍 肌色
    "keycap": "1️⃣",  # 1 VS16 囲み
    "tag_flag": "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
    "ivs_kanji": "葛\U000e0100",  # 異体字セレクタ付きの漢字
    "cjk": "定",
}

_PREFIX = "【社外】"  # 社外判定に確実に乗せる（判定はこのテストの対象外）
_TITLE_MAX = 60  # render._MAX_TITLE
_NAME_MAX = 40  # signals._MAX_NAME


class _GCal:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def list_events(self, request_id: str, **kwargs: Any) -> list[Any]:
        return self._events


def _brief_lines(*, summary: str, client: str, agency: str) -> list[str]:
    raw = {
        "id": "e1",
        "summary": summary,
        "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "description": f"クライアント：{client}\n代理店：{agency}",
    }
    (detail,) = extract_events([raw], want_description=True)
    digest = MorningDigestSkill(gcalendar=_GCal([detail]))
    items = digest._collect_calendar(object(), MorningDigestInput(), _ctx())
    out = PreMeetingBriefSkill(pg=_FakePg(), events=items).run(PreMeetingBriefInput(), _ctx())
    return render_brief_lines(out, _dt.date(2026, 9, 11))


def _cases(cluster: str) -> list[tuple[dict[str, str], dict[str, str]]]:
    """(入力, 期待する表示) の組。上限がクラスタの内側に来る全並び＋ちょうど終わる並び。"""
    out: list[tuple[dict[str, str], dict[str, str]]] = []
    for inside in range(1, len(cluster)):
        title = _PREFIX + "定" * (_TITLE_MAX - len(_PREFIX) - inside)
        client = "北" * (_NAME_MAX - inside)
        agency = "青" * (_NAME_MAX - inside)
        raw = {
            "summary": title + cluster + "例",
            "client": client + cluster + "例",
            "agency": agency + cluster + "例",
        }
        out.append((raw, {"title": title, "client": client, "agency": agency}))
    title = _PREFIX + "例" * (_TITLE_MAX - len(_PREFIX) - len(cluster)) + cluster
    client = "北" * (_NAME_MAX - len(cluster)) + cluster
    agency = "青" * (_NAME_MAX - len(cluster)) + cluster
    raw = {"summary": title + "例", "client": client + "例", "agency": agency + "例"}
    out.append((raw, {"title": title, "client": client, "agency": agency}))
    return out


@pytest.mark.parametrize("name", sorted(_CLUSTERS))
def test_brief_title_client_and_agency_do_not_split_a_cluster(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        calwin, "now_jst", lambda: _dt.datetime(2026, 9, 11, 9, 0, tzinfo=calwin.JST)
    )
    for raw, want in _cases(_CLUSTERS[name]):
        lines = _brief_lines(**raw)
        title_lines = [ln for ln in lines if ln.startswith("▶️ ")]
        assert len(title_lines) == 1, lines
        head = f"▶️ 14:00–15:00  {want['title']}"
        # 判定が uncertain なら「（社外か要確認）」が続く。それ以外の文字が続けば割れている。
        assert title_lines[0] in (head, head + "（社外か要確認）"), title_lines[0]
        assert f"  クライアント：{want['client']}／代理店：{want['agency']}" in lines, lines
