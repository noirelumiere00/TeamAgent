"""メール／カレンダー系 description のルーティング硬化リグレッション（P1-1）。

背景（実測 2026-08-20）: OC の外側ルーター(Haiku)は name+description だけでツールを選ぶ。
OC へ露出する 35 本の description 全文を走査したところ、利用者が実際に使う語
（「要返信」「返信が必要」「返信漏れ」「返信待ち」「今日」「スケジュール」）が
**1 本もヒットしなかった**。X/TikTok 系は tests/skills/test_routing_descriptions_catalog.py で
相互排他注記まで硬化済みなのに、メール/カレンダー系だけが未硬化だった。

ここは「トリガー語がある」だけでなく **「他ツールに同じ語が出るときは必ず正しい先を指す
ポインタになっている」** ことまで固定する（語をばら撒くと逆に混同が増えるため）。
corpus 突き合わせ（tests/routing/）は LLM 依存で非決定的なので、pytest はこの
decision-substring 固定のみを担う。
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from pathlib import Path

import teamagent.skills as _skills_pkg
from teamagent.skills.base import SkillRegistry
from teamagent.skills.calendar_event.skill import CalendarEventSkill
from teamagent.skills.calendar_freebusy.skill import CalendarFreeBusySkill
from teamagent.skills.mail_followup.skill import MailFollowupSkill
from teamagent.skills.mail_summary.skill import MailSummarySkill
from teamagent.skills.oauth_connect.skill import OAuthConnectSkill
from teamagent.skills.video.skill import VideoAnalysisSkill

_SCOPE = Path(__file__).resolve().parents[2] / "infra" / "openclaw" / "effective-tool-scope.json"


def _exposed_descriptions() -> dict[str, str]:
    """OC へ実際に露出するツールの name→description（真実源は effective-tool-scope.json）。"""
    for mod in pkgutil.iter_modules(_skills_pkg.__path__):
        if mod.ispkg:
            try:
                importlib.import_module(f"teamagent.skills.{mod.name}.skill")
            except ModuleNotFoundError:
                continue  # skill.py を持たないパッケージ（vseo 等）
    scope = json.loads(_SCOPE.read_text(encoding="utf-8"))
    names = [t["name"] if isinstance(t, dict) else str(t) for t in scope["tools"]]
    registered = set(SkillRegistry.list_all())
    out: dict[str, str] = {}
    for name in names:
        if name not in registered:
            continue  # env フラグ次第で未 import のもの（実害は他テストが担保）
        out[name] = str(getattr(SkillRegistry.get(name), "description", ""))
    return out


# ── mail_followup: 利用者語彙の受け口 ───────────────────────────────────────


def test_mail_followup_has_user_vocabulary() -> None:
    d = MailFollowupSkill.description
    for w in ("要返信", "返信が必要", "返信漏れ", "返信待ち", "放置"):
        assert w in d, f"mail_followup description に『{w}』が無い"


def test_mail_followup_points_to_the_other_surfaces() -> None:
    d = MailFollowupSkill.description
    assert "mail_summary" in d  # 内容の横断要約は summary
    assert "calendar_freebusy" in d  # 空き時間・予定一覧はカレンダー側
    assert "mode='agenda'" in d  # 予定一覧の呼び方まで明示
    # 顧客名が取れない依頼で断片を詰めない（P0-2 のガードと description を一致させる）
    assert "client_name を**空のまま呼ぶ**" in d
    # 2026-08-21 裁定: 空で呼んだ先は「聞き返し」ではなく候補一覧。外側に文面を自作させない。
    assert "inbox_triage" in d
    assert "聞き返しの文面を自作しない" in d


# ── mail_summary: 相互排他と client_name の規律 ────────────────────────────


def test_mail_summary_defers_followup_to_mail_followup() -> None:
    d = MailSummarySkill.description
    assert "mail_followup" in d
    assert "要返信" in d
    assert "mode='agenda'" in d


def test_mail_summary_forbids_request_fragments_in_client_name() -> None:
    d = MailSummarySkill.description
    assert "依頼文の断片" in d
    assert "空にして呼ぶ" in d


def test_mail_tools_do_not_share_a_boilerplate_opening() -> None:
    """冒頭がボイラープレート共通だと Haiku が識別できない（実測の混同要因）。"""
    a = MailSummarySkill.description[:40]
    b = MailFollowupSkill.description[:40]
    assert a != b
    assert not a.startswith("本人の受信箱")  # 旧: 両方が同じ書き出しだった
    assert not b.startswith("本人の受信箱")


# ── calendar_freebusy: 予定一覧の受け口（P1-2） ────────────────────────────


def test_calendar_freebusy_advertises_agenda_mode() -> None:
    d = CalendarFreeBusySkill.description
    for w in ("明日の予定", "今日の予定", "スケジュール", "mode='agenda'"):
        assert w in d, f"calendar_freebusy description に『{w}』が無い"
    assert "mode='free'" in d  # 既定側も明示（既存の空き時間照会を潰さない）


def test_calendar_freebusy_forbids_llm_date_arithmetic() -> None:
    """日付は relative_day で渡させる（LLM に今日の日付を計算させない）。"""
    d = CalendarFreeBusySkill.description
    assert "日付は自分で計算しないこと" in d
    assert "relative_day='today'" in d


def test_calendar_event_defers_reads_to_agenda() -> None:
    """『予定入れて』と『予定教えて』の 1 語差で登録ツールへ流れるのを止める。"""
    d = CalendarEventSkill.description
    assert "mode='agenda'" in d
    assert "登録専用" in d


# ── oauth_connect: 一語『連携』 ────────────────────────────────────────────


def test_oauth_connect_fires_on_single_word() -> None:
    d = OAuthConnectSkill.description
    assert "『連携』の一語だけでも呼ぶ" in d


# ── 露出 35 本の横断不変量 ─────────────────────────────────────────────────


def test_reply_vocabulary_elsewhere_is_only_a_pointer_to_mail_followup() -> None:
    """要返信系の語を持つ他ツールは、必ず mail_followup を指していること。

    語をばら撒くと混同が増える。mail_followup 以外に出てよいのは
    「それは mail_followup へ」という相互排他注記としてだけ。
    """
    descs = _exposed_descriptions()
    assert "mail_followup" in descs, "露出セットの解決に失敗（テスト自身の前提が壊れている）"
    for word in ("要返信", "返信が必要", "返信漏れ", "返信待ち", "放置しているメール"):
        for name, d in descs.items():
            if name == "mail_followup" or word not in d:
                continue
            assert "mail_followup" in d, (
                f"{name} が『{word}』を持つのに mail_followup を指していない"
            )


def test_agenda_vocabulary_is_owned_by_calendar_freebusy() -> None:
    """『明日の予定』を名乗ってよいのは calendar_freebusy だけ。"""
    descs = _exposed_descriptions()
    owners = [n for n, d in descs.items() if "明日の予定" in d]
    assert "calendar_freebusy" in owners
    for name in owners:
        if name == "calendar_freebusy":
            continue
        assert "calendar_freebusy" in descs[name], f"{name} が『明日の予定』を横取りしている"


def test_video_approval_is_a_live_tool_not_a_dangling_reference() -> None:
    """video_analysis→video_approval の相互排他注記は **消してはいけない**。

    P1-1 の依頼書は「video_approval は未登録の幽霊ツール」として参照撤去を求めていたが、
    実測は逆: video_approval は SkillRegistry にも effective-tool-scope.json の 35 本にも
    factory（USE_VIDEO_APPROVAL）にも存在する実ツール。撤去すると
    tests/skills/video_approval/test_routing_descriptions.py が赤くなり、
    「納品物の合否チェック」が競合分析ツールへ化ける既知の納品事故が復活する。
    """
    assert "video_approval" in SkillRegistry.list_all()
    assert "video_approval" in _exposed_descriptions()
    assert "video_approval" in VideoAnalysisSkill.description
