"""朝ダイジェストのボタン 4 種を、上流 OpenClaw の実際の渡し方で plugin → mcp まで通す。

本番の失敗（2026-09-29 裁定の対象）:
  📅 予定登録（calendar_event）・🗓 日程候補（schedule_propose）は表示されているのに、
  押しても何も起きない（本番 30 日でボタン経由の実行 0 件）。原因は plugin 側に 3 つ重なっていた:
    1. interactive handler が namespace ``mail_draft`` にしか登録されておらず、
       ボタン経由の実行で呼べるツールが ``mail_draft`` に固定されていた
       （``Slack mail action cannot authorize another tool``）。
    2. 上流は system event の文字列を 160 字で切る（159 字＋「…」）。📅 の event トークンは
       最短でも 197 字・☑️ 一括（ackall）は数百字あり、heartbeat 側の値と押下時の値が一致しない。
    3. DM の heartbeat run は hook ctx の channelId が ``U…``（messageTo 由来）で、
       押下の ``D…`` と一致せず run の束縛が拒否される（CONNECT-P03）。

このテストは上流の渡し方（``tests/scripts/openclaw_action_bindings_probe.mjs`` の冒頭に
file:line）をそのまま再現した入力を実物の plugin に通し、署名済みの引数を本物の
``dispatch_tool``（caller claim 検証）へ流して、各ツールが**本物の HMAC 復号**で
トークンを受理できるところまで確かめる（plugin と mcp の二重の守り）。

⚠️ 本番の経路はこのファイルではない: 本番設定 ``agents.defaults.heartbeat.every: "0m"``
（infra/openclaw/openclaw.config.json5）では heartbeat runner に agent が載らず、押下後の
heartbeat は ``skipped reason=disabled`` で捨てられる（openclaw@2026.7.1 dist/heartbeat-runner-*.js）。
09-29 裁定で、bearer と bot token がある環境（＝本番）の押下は plugin が直接 mcp へ呼んで本人の DM へ
投稿する（``tests/test_openclaw_button_direct.py``）。このファイルは、その 2 つが無い環境で残る
従来の経路（handled:false → system event → heartbeat run の束縛）と、メッセージ由来の run の門を確かめる。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel

from teamagent.identity import IdentityResolver, ResolvedIdentity
from teamagent.mcp_gateway.server import dispatch_tool
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.morning_digest.ack_token import (
    AckItem,
    decode_ack_token,
    encode_ack_all_token,
    encode_ack_token,
)
from teamagent.skills.morning_digest.draft_token import (
    _owner_hash,
    decode_draft_token,
    encode_draft_token,
)
from teamagent.skills.morning_digest.event_token import (
    EVENT_TOKEN_MAX_LENGTH,
    decode_event_token,
    encode_event_token,
)
from tests.caller_claim_testkit import (
    TEST_CALLER_CLAIM_SECRET,
    TEST_NOW,
    TEST_SLACK_TEAM_ID,
    TEST_SLACK_USER_ID,
    make_verifier,
)

ROOT = Path(__file__).resolve().parents[1]
CALLER_PLUGIN = ROOT / "infra/openclaw/caller-identity-plugin/dist/index.js"
PROBE = ROOT / "tests/scripts/openclaw_action_bindings_probe.mjs"

MEMBER_EMAIL = "member@vectorinc.co.jp"
USER_B = "U0BBBBBBBBB"
DM_A = "D0AAAAAAAAA"
CHANNEL = "C0CCCCCCCCC"

_HMAC_ENVS = (
    "MAIL_ACTION_HMAC_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY",
    "MAIL_ACTION_HMAC_PRIMARY_GENERATION",
    "MAIL_ACTION_HMAC_PREVIOUS_GENERATION",
    "MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET",
    "MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
    "MAIL_ACTION_TTL_S",
    "DATABASE_URL",
    "SLACK_BOT_TOKEN",
)
_MAIL_ACTION_SECRET = "dedicated-mail-key-" + "k" * 40
# 本番のダイジェストが実際に出す長さの件名（同じ件名の定例が 2 件並ぶと、切り詰め後の
# system event の value が一致する＝曖昧になる）。トークンの payload は UTF-8 なので、
# 件名がおよそ 28 字を超えると日付の違いが先頭 159 字の外に出る。
_LONG_TITLE = "【定例】株式会社サンプルホールディングス様 週次営業打合せ（オンライン）"
# ダイジェストが載せる件名の上限（60 字）いっぱいの件名。
_MAX_TITLE = ("【定例】株式会社サンプルホールディングス様 週次営業打合せ" + "あ" * 32)[:60]


# ── mcp 側の受け手（本物の復号で「トークンが無傷で届いた」ことを確かめる）─────────────
class _ButtonOutput(BaseModel):
    email: str
    verified: bool
    token: str
    decoded: str


class _CalendarInput(BaseModel):
    event_token: str = ""
    title: str = ""
    start: str = ""


class _ScheduleInput(BaseModel):
    schedule_token: str


class _AckInput(BaseModel):
    ack_token: str = ""


class _MailDraftInput(BaseModel):
    draft_token: str = ""


class _CalendarSkill(BaseSkill[_CalendarInput, _ButtonOutput]):
    name: ClassVar[str] = "calendar_event"
    description: ClassVar[str] = "calendar_event stand-in that decodes the real token."
    input_schema: ClassVar[type[BaseModel]] = _CalendarInput
    output_schema: ClassVar[type[BaseModel]] = _ButtonOutput

    def run(self, input: _CalendarInput, ctx: SkillContext) -> _ButtonOutput:
        email = str(ctx.metadata["user_email"])
        decoded = ""
        if input.event_token:
            event = decode_event_token(input.event_token, email, now=TEST_NOW)
            decoded = "INVALID" if event is None else f"{event.start_iso}|{event.title}"
        else:
            decoded = f"freeform|{input.title}|{input.start}"
        return _ButtonOutput(
            email=email,
            verified=bool(ctx.metadata["identity_verified"]),
            token=input.event_token,
            decoded=decoded,
        )


class _ScheduleSkill(BaseSkill[_ScheduleInput, _ButtonOutput]):
    name: ClassVar[str] = "schedule_propose"
    description: ClassVar[str] = "schedule_propose stand-in that decodes the real token."
    input_schema: ClassVar[type[BaseModel]] = _ScheduleInput
    output_schema: ClassVar[type[BaseModel]] = _ButtonOutput

    def run(self, input: _ScheduleInput, ctx: SkillContext) -> _ButtonOutput:
        email = str(ctx.metadata["user_email"])
        thread_id = decode_draft_token(input.schedule_token, email, now=TEST_NOW)
        return _ButtonOutput(
            email=email,
            verified=bool(ctx.metadata["identity_verified"]),
            token=input.schedule_token,
            decoded=thread_id or "INVALID",
        )


class _AckSkill(BaseSkill[_AckInput, _ButtonOutput]):
    name: ClassVar[str] = "digest_ack"
    description: ClassVar[str] = "digest_ack stand-in that decodes the real token."
    input_schema: ClassVar[type[BaseModel]] = _AckInput
    output_schema: ClassVar[type[BaseModel]] = _ButtonOutput

    def run(self, input: _AckInput, ctx: SkillContext) -> _ButtonOutput:
        email = str(ctx.metadata["user_email"])
        payload = decode_ack_token(input.ack_token, email, now=TEST_NOW)
        return _ButtonOutput(
            email=email,
            verified=bool(ctx.metadata["identity_verified"]),
            token=input.ack_token,
            decoded="INVALID" if payload is None else f"{payload.kind}|{len(payload.items)}",
        )


class _MailDraftSkill(BaseSkill[_MailDraftInput, _ButtonOutput]):
    name: ClassVar[str] = "mail_draft"
    description: ClassVar[str] = "mail_draft stand-in that decodes the real token."
    input_schema: ClassVar[type[BaseModel]] = _MailDraftInput
    output_schema: ClassVar[type[BaseModel]] = _ButtonOutput

    def run(self, input: _MailDraftInput, ctx: SkillContext) -> _ButtonOutput:
        email = str(ctx.metadata["user_email"])
        thread_id = decode_draft_token(input.draft_token, email, now=TEST_NOW)
        return _ButtonOutput(
            email=email,
            verified=bool(ctx.metadata["identity_verified"]),
            token=input.draft_token,
            decoded=thread_id or "INVALID",
        )


_BY_NAME = {
    skill.name: ToolSpec(skill.name, skill.description, skill)
    for skill in (_CalendarSkill, _ScheduleSkill, _AckSkill, _MailDraftSkill)
}


def _resolver() -> IdentityResolver:
    member = ResolvedIdentity(slack_user_id=TEST_SLACK_USER_ID, email=MEMBER_EMAIL)

    async def resolve(slack_user_id: str) -> ResolvedIdentity | None:
        return member if slack_user_id == member.slack_user_id else None

    return resolve


async def _dispatch(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    contents = await dispatch_tool(
        _BY_NAME,
        tool,
        arguments,
        identity_resolver=_resolver(),
        company_shared_groups=frozenset({"vectorinc.co.jp"}),
        caller_claim_verifier=make_verifier(),
        allowed_domains=frozenset({"vectorinc.co.jp"}),
        require_rls=True,
    )
    assert len(contents) == 1
    return cast(dict[str, Any], json.loads(contents[0].text))


def _legacy_escaped_event_shape(title: str) -> str:
    """修正前のエンコーダ（``\\uXXXX``）が件名 60 字で出していた形の value（677 字）。

    今のエンコーダは上限 500 字に収めて出すので、上限超えの押下は旧版のダイジェストが
    残っているときにしか起きない。plugin は署名を検証しない（鍵は mcp にだけある）ので、
    形と typ だけ本物に揃え、署名は 22 字のダミーにする。
    """
    payload = {
        "v": 2,
        "typ": "event",
        "s": "2026-07-20T10:00:00+09:00",
        "n": "2026-07-20T11:00:00+09:00",
        "l": title,
        "o": _owner_hash(MEMBER_EMAIL),
        "e": TEST_NOW + 86_400,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + "A" * 22


def _tokens() -> dict[str, str]:
    def event(title: str, start: str) -> str:
        token = encode_event_token(
            start_iso=start,
            end_iso=start.replace("T10:", "T11:"),
            title=title,
            owner_email=MEMBER_EMAIL,
            now=TEST_NOW,
        )
        assert token
        return token

    draft = encode_draft_token("199a1b2c3d4e5f60", MEMBER_EMAIL, now=TEST_NOW)
    ack = encode_ack_token(
        [AckItem("m", "0123456789abcdef", 1_784_420_000_000)], MEMBER_EMAIL, now=TEST_NOW
    )
    ack_all = encode_ack_all_token(
        [AckItem("m", f"{i:016x}", 1_784_420_000_000) for i in range(10)],
        MEMBER_EMAIL,
        now=TEST_NOW,
    )
    assert draft and ack and ack_all
    return {
        "event": event("A社定例", "2026-07-20T10:00:00+09:00"),
        "eventTwinA": event(_LONG_TITLE, "2026-07-20T10:00:00+09:00"),
        "eventTwinB": event(_LONG_TITLE, "2026-07-21T10:00:00+09:00"),
        "eventLongTitle": event(_MAX_TITLE, "2026-07-20T10:00:00+09:00"),
        "tooLong": _legacy_escaped_event_shape("あ" * 60),
        "draft": draft,
        "ack": ack,
        "ackAll": ack_all,
    }


def _run_probe(tokens: dict[str, str], plugin: Path = CALLER_PLUGIN) -> dict[str, Any]:
    completed = subprocess.run(
        ["node", str(PROBE)],
        check=True,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PROBE_INPUT": json.dumps(
                {
                    "pluginUrl": plugin.as_uri(),
                    "secret": TEST_CALLER_CLAIM_SECRET,
                    "teamId": TEST_SLACK_TEAM_ID,
                    "userA": TEST_SLACK_USER_ID,
                    "userB": USER_B,
                    "dmA": DM_A,
                    "channel": CHANNEL,
                    "nowMs": TEST_NOW * 1000,
                    "tokens": tokens,
                }
            ),
        },
        timeout=60,
    )
    return cast(dict[str, Any], json.loads(completed.stdout))


@pytest.fixture
def mail_action_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _HMAC_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_ACTION_SECRET)


@pytest.fixture
def tokens(mail_action_hmac: None) -> dict[str, str]:
    return _tokens()


@pytest.fixture
def probe(tokens: dict[str, str]) -> dict[str, Any]:
    return _run_probe(tokens)


def _signed(result: dict[str, Any]) -> dict[str, Any]:
    assert result["block"] is False, result["blockReason"]
    assert result["params"] is not None
    assert result["claim"] is not None
    return cast(dict[str, Any], result["params"])


def test_fixture_tokens_reproduce_the_production_lengths(tokens: dict[str, str]) -> None:
    """前提の固定: 本物のエンコーダで作ったトークンが、本番の失敗の形（160 字超）であること。"""
    assert len(tokens["event"]) > 160
    assert len(tokens["ackAll"]) > 160
    assert len(tokens["draft"]) <= 160
    assert len(tokens["ack"]) <= 160
    assert len(tokens["tooLong"]) > EVENT_TOKEN_MAX_LENGTH
    # 件名 60 字（ダイジェストの上限）でも、今のエンコーダは上限内に収めて出す。
    assert 160 < len(tokens["eventLongTitle"]) <= EVENT_TOKEN_MAX_LENGTH
    # 同じ件名の 2 件は、上流の切り詰め（159 字）までが一致する。
    assert tokens["eventTwinA"][:159] == tokens["eventTwinB"][:159]
    assert tokens["eventTwinA"] != tokens["eventTwinB"]


def test_binding_table_is_one_to_one_and_registers_every_namespace(
    probe: dict[str, Any],
) -> None:
    assert probe["registeredNamespaces"] == [
        "calendar_event",
        "digest_ack",
        "mail_draft",
        "schedule_propose",
    ]
    bindings = probe["exportedBindings"]
    assert set(bindings) == set(probe["registeredNamespaces"])
    tools = [binding["tool"] for binding in bindings.values()]
    assert len(tools) == len(set(tools)), "束縛表は action_id → ツールの 1 対 1"
    assert {action: binding["tool"] for action, binding in bindings.items()} == {
        "mail_draft": "mail_draft",
        "calendar_event": "calendar_event",
        "schedule_propose": "schedule_propose",
        "digest_ack": "digest_ack",
    }
    assert {action: binding["tokenParam"] for action, binding in bindings.items()} == {
        "mail_draft": "draft_token",
        "calendar_event": "event_token",
        "schedule_propose": "schedule_token",
        "digest_ack": "ack_token",
    }
    # mail_draft の値の上限は従来どおり 160 のまま（弱めない）。
    assert bindings["mail_draft"]["maxLength"] == 160


async def test_calendar_button_in_dm_runs_once_with_the_full_signed_token(
    probe: dict[str, Any], tokens: dict[str, str]
) -> None:
    case = probe["dmCalendar"]
    assert case["press"]["matched"] is True
    assert case["press"]["handlerResult"] == {"handled": False}
    assert case["press"]["enqueued"] is True
    # 上流は system event の value を切る。モデルに見えるのはこの切れた値だけ。
    assert case["press"]["systemEventValue"] == tokens["event"][:159] + "…"

    params = _signed(case["first"])
    # plugin が押下時に捕捉した**完全な**トークンで上書きする（モデルの値は使わない）。
    assert params["event_token"] == tokens["event"]
    claim = case["first"]["claim"]
    assert claim["tool"] == "calendar_event"
    assert claim["sub"] == TEST_SLACK_USER_ID
    assert claim["channel"] == DM_A  # mcp が受理する正準の D…（DM:U… ではない）
    assert claim["message"] == "1784424000.000101"

    # 1 回だけ: 同じ run の 2 回目・同じ押下の再押下・別 run での使い回しはすべて止まる。
    assert case["second"]["block"] is True
    assert "already consumed" in case["second"]["blockReason"]
    assert case["replayPress"] == {"handlerResult": {"handled": True}, "enqueued": False}
    assert case["crossRun"]["block"] is True

    out = await _dispatch("calendar_event", params)
    assert out == {
        "email": MEMBER_EMAIL,
        "verified": True,
        "token": tokens["event"],
        "decoded": "2026-07-20T10:00:00+09:00|A社定例",
    }


def test_calendar_button_in_dm_also_binds_when_the_run_names_the_d_channel(
    probe: dict[str, Any], tokens: dict[str, str]
) -> None:
    params = _signed(probe["dmCalendarDirect"])
    assert params["event_token"] == tokens["event"]
    assert probe["dmCalendarDirect"]["claim"]["channel"] == DM_A


def test_calendar_button_cannot_authorize_another_tool(probe: dict[str, Any]) -> None:
    case = probe["wrongTool"]
    for name in ("mailDraft", "schedulePropose", "search"):
        assert case[name]["block"] is True, name
        assert "another tool" in case[name]["blockReason"], name
    # 止められた呼び出しは押下を消費しない（正しいツールは 1 回呼べる）。
    assert _signed(case["thenRight"])["event_token"]


def test_value_with_the_wrong_token_shape_is_not_captured(probe: dict[str, Any]) -> None:
    for name in ("draftTypedOnCalendar", "eventTypedOnSchedule", "draftTypedOnAck", "garbage"):
        case = probe["shape"][name]
        assert case["handlerResult"] == {"handled": True}, name
        # 捕捉されていない押下の system event では、どのツールも署名されない。
        assert case["call"]["block"] is True, name
        assert "missing or stale" in case["call"]["blockReason"], name
    assert probe["shape"]["tooLong"]["handlerResult"] == {"handled": True}
    assert probe["shape"]["tooLong"]["call"]["block"] is True
    # mail_draft の値の上限（160）は弱めていない。
    assert probe["shape"]["mailDraftOver160"]["handlerResult"] == {"handled": True}
    assert probe["shape"]["mailDraftOver160"]["call"]["block"] is True
    assert probe["shape"]["unauthorized"]["handlerResult"] == {"handled": True}


async def test_message_origin_calendar_freeform_still_works_and_button_tools_do_not(
    probe: dict[str, Any], tokens: dict[str, str]
) -> None:
    case = probe["message"]
    plain = _signed(case["freeformPlain"])
    assert "event_token" not in plain, "自由文の引数は 1 バイトも変えない"
    assert plain["title"] == "A社と打合せ"
    assert case["freeformPlain"]["claim"]["channel"] == DM_A

    forged = _signed(case["freeformForgedToken"])
    # メッセージ由来ではボタンのトークンを使えない（自由文としてだけ通る）。
    assert forged["event_token"] == ""
    assert forged["title"] == "A社と打合せ"

    for name in ("schedulePropose", "digestAck", "mailDraft"):
        assert case[name]["block"] is True, name
        assert "requires an authoritative Slack button action" in case[name]["blockReason"], name

    out = await _dispatch("calendar_event", forged)
    assert out["decoded"] == "freeform|A社と打合せ|2026-07-20T15:00:00+09:00"
    assert out["token"] == ""


async def test_schedule_and_mail_draft_on_the_same_row_are_separate_presses(
    probe: dict[str, Any], tokens: dict[str, str]
) -> None:
    case = probe["sameRow"]
    assert case["scheduleHandler"] == {"handled": False}
    assert case["mailHandler"] == {"handled": False}
    schedule = _signed(case["schedule"])
    assert schedule["schedule_token"] == tokens["draft"]
    assert case["schedule"]["claim"]["tool"] == "schedule_propose"
    mail = _signed(case["mailDraft"])
    assert mail["draft_token"] == tokens["draft"]
    assert case["mailDraft"]["claim"]["tool"] == "mail_draft"
    # 押下ごとに別の束縛（nonce も別）。
    assert case["schedule"]["claim"]["nonce"] != case["mailDraft"]["claim"]["nonce"]

    out = await _dispatch("schedule_propose", schedule)
    assert out["decoded"] == "199a1b2c3d4e5f60"
    out = await _dispatch("mail_draft", mail)
    assert out["decoded"] == "199a1b2c3d4e5f60"


async def test_digest_ack_all_button_carries_the_full_token_and_disabled_tool_does_nothing(
    probe: dict[str, Any], tokens: dict[str, str]
) -> None:
    assert probe["ackAll"]["systemEventValue"].endswith("…")
    params = _signed(probe["ackAll"]["call"])
    assert params["ack_token"] == tokens["ackAll"]
    out = await _dispatch("digest_ack", params)
    assert out["decoded"] == "ackall|10"

    # digest_ack が無効（tools/list に無い）でも、押下の run は他のツールを 1 つも呼べない。
    for name in ("search", "calendar"):
        assert probe["ackDisabled"][name]["block"] is True, name
        assert "another tool" in probe["ackDisabled"][name]["blockReason"], name


def test_channel_thread_button_keeps_the_existing_channel_path(
    probe: dict[str, Any], tokens: dict[str, str]
) -> None:
    params = _signed(probe["channelThread"])
    assert params["event_token"] == tokens["event"]
    claim = probe["channelThread"]["claim"]
    assert claim["channel"] == CHANNEL
    assert claim["thread"] == "1784423000.000001"


def test_another_users_dm_run_cannot_use_the_press(probe: dict[str, Any]) -> None:
    case = probe["crossUser"]
    assert case["foreignRun"]["block"] is True
    assert case["forgedSender"]["block"] is True
    # 他人の run に拒否されても、本人の押下は本人の DM の run で使える。
    assert _signed(case["own"])["event_token"]
    assert case["own"]["claim"]["sub"] == TEST_SLACK_USER_ID


def test_same_title_meetings_on_two_rows_bind_to_their_own_row(
    probe: dict[str, Any], tokens: dict[str, str]
) -> None:
    """同じ件名の定例が 2 行あると、system event の value（159 字＋…）は同じになる。

    行（Slack の block_id）で見分けて、押した行の完全なトークンだけが渡る（取り違えない）。
    block_id でも見分けられないときは、どちらとも決めずに止める。
    """
    case = probe["twins"]
    assert case["sameSystemEventValue"] is True
    assert _signed(case["first"])["event_token"] == tokens["eventTwinB"]
    assert _signed(case["second"])["event_token"] == tokens["eventTwinA"]
    assert case["ambiguous"]["block"] is True
    assert "missing or stale" in case["ambiguous"]["blockReason"]


async def test_schedule_and_mail_draft_pending_together_are_told_apart_by_action_id(
    probe: dict[str, Any], tokens: dict[str, str]
) -> None:
    """同じ行の 🗓 と ✏️ は block も value（draft トークン）も同じで、見分ける手掛かりは
    action_id だけ。両方の押下が待っている状態でも、それぞれ自分のツールに束縛される。"""
    case = probe["pendingPair"]
    assert case["sameSystemEventValue"] is True
    schedule = _signed(case["schedule"])
    assert case["schedule"]["claim"]["tool"] == "schedule_propose"
    assert schedule["schedule_token"] == tokens["draft"]
    mail = _signed(case["mailDraft"])
    assert case["mailDraft"]["claim"]["tool"] == "mail_draft"
    assert mail["draft_token"] == tokens["draft"]
    assert case["schedule"]["claim"]["nonce"] != case["mailDraft"]["claim"]["nonce"]

    out = await _dispatch("schedule_propose", schedule)
    assert out["decoded"] == "199a1b2c3d4e5f60"


def test_two_presses_in_one_heartbeat_prompt_sign_nothing(probe: dict[str, Any]) -> None:
    """1 回の heartbeat に押下が 2 行載ると、どちらの run か決められない＝何も署名しない。"""
    case = probe["twoInteractions"]
    for name in ("calendar", "mailDraft"):
        assert case[name]["block"] is True, name
        assert case[name]["claim"] is None, name


def test_repeated_heartbeat_keeps_the_binding_only_for_the_same_conversation(
    probe: dict[str, Any], tokens: dict[str, str]
) -> None:
    """同じ run に heartbeat の通知が 2 回来たとき、run が名乗る会話が同じなら束縛を保つ。
    途中で変わったら（同じ本人の DM の別名でも・他人の DM でも）run ごと捨てる。"""
    case = probe["repeatedHeartbeat"]
    assert _signed(case["sameName"]["asFirst"])["event_token"] == tokens["event"]
    assert case["sameName"]["asFirst"]["claim"]["channel"] == DM_A
    for name in ("renamedMidRun", "otherUsersDm"):
        # 最初に束縛した会話名で呼んでも、変わった後の名前で呼んでも止まる（run ごと捨てた）。
        for called_as in ("asFirst", "asSecond"):
            result = case[name][called_as]
            assert result["block"] is True, (name, called_as)
            assert result["claim"] is None, (name, called_as)


async def test_calendar_button_with_a_sixty_char_title_reaches_mcp_intact(
    probe: dict[str, Any], tokens: dict[str, str]
) -> None:
    """件名 60 字（ダイジェストの上限）の 📅 も捕捉され、完全なトークンが mcp の復号まで届く。

    09-29 レビュー: 以前のエンコーダでは日本語の件名が 38 字前後を超えると 500 字を超え、
    plugin が捕捉せず（handled:true で終わる）押しても無反応だった。
    """
    case = probe["longTitle"]
    assert case["handlerResult"] == {"handled": False}
    params = _signed(case["call"])
    assert params["event_token"] == tokens["eventLongTitle"]
    out = await _dispatch("calendar_event", params)
    assert out["decoded"] == f"2026-07-20T10:00:00+09:00|{_MAX_TITLE}"
