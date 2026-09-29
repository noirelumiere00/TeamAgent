"""朝ダイジェストのボタンを「AI を通さず直接処理する」経路（2026-09-29 裁定）を端から端まで通す。

本番の失敗: 📅・🗓・✏️・☑️ を押しても何も起きない。plugin 側の束縛（ACTION_BINDINGS）は直したが、
本番の ``agents.defaults.heartbeat.every: "0m"``（infra/openclaw/openclaw.config.json5）では
上流の heartbeat-runner に agent が載らず、押下の後に AI の run が起きない。

裁定: plugin が押下を捕捉したら ``{handled: true}`` を返し（system event も heartbeat も積まない）、
束縛先の 1 ツールを層1 と同じ MCP クライアント・同じ署名 claim で mcp へ直接呼び、結果を
保証経路と同じ chat.postMessage で押した本人の DM へ投稿する。

このテストの形（フェイクは本番の失敗の形を再現する）:
  - 押下: 上流 OpenClaw 2026.7.1 が interactive handler に渡す ctx の形（probe の冒頭に file:line）
  - plugin: 本物の ``infra/openclaw/caller-identity-plugin/dist/index.js``
  - mcp: **本物の HTTP**。本番と同じ ``scripts/run_mcp_http_server.py`` の ``build_app``
    （bearer 認証・streamable-http・SSE 応答）を uvicorn で立て、本物の ``dispatch_tool`` と
    caller claim 検証（HMAC・本人・one-use nonce）を通す。ツールは 📅（CalendarEventSkill）と
    ☑️（DigestAckSkill）が本物（Google カレンダーと確認状態の保存先だけ偽物）、🗓・✏️ は
    本物の入出力 schema・本物のトークン復号・本物の文言で組んだ代役（Gmail 側が重いため）。
  - Slack: 偽物（conversations.open / chat.postMessage）。失敗は本番の形
    （HTTP 200 + ``{"ok": false, "error": "channel_not_found"}``・HTTP 500・conversations.open の
    ``internal_error``・スレッドへの投稿の ``cannot_reply_to_message``・受け付けた後の時間切れ）。
  - 押した本人だけへの一時表示: 上流の ``ctx.respond.reply``（Slack の response_url・ephemeral）。
    失敗は上流と同じく例外のまま返る（response_url の失効）。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel

from teamagent.adapters.gcalendar_client import DuplicateEventError
from teamagent.connect_diagnostics import admin_forward_hint
from teamagent.identity import IdentityResolver, ResolvedIdentity
from teamagent.mcp_gateway import server as mcp_server
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills._shared.mail_compose import gmail_thread_url
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.calendar_event import skill as calendar_skill_module
from teamagent.skills.calendar_event.skill import CalendarEventSkill
from teamagent.skills.digest_ack.skill import DigestAckSkill
from teamagent.skills.mail_draft import skill as mail_draft_skill_module
from teamagent.skills.mail_draft.schema import MailDraftInput, MailDraftOutput
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
    encode_event_token,
)
from teamagent.skills.schedule_propose import skill as schedule_skill_module
from teamagent.skills.schedule_propose.schema import ScheduleProposeInput, ScheduleProposeOutput
from teamagent.skills.schedule_propose.slot_finder import format_candidates_ja
from tests.caller_claim_testkit import (
    TEST_CALLER_CLAIM_SECRET,
    TEST_NOW,
    TEST_SLACK_TEAM_ID,
    TEST_SLACK_USER_ID,
    make_verifier,
)

ROOT = Path(__file__).resolve().parents[1]
CALLER_PLUGIN = ROOT / "infra/openclaw/caller-identity-plugin/dist/index.js"
PROBE = ROOT / "tests/scripts/openclaw_button_direct_probe.mjs"
HTTP_SERVER_SCRIPT = ROOT / "scripts/run_mcp_http_server.py"

USER_A = TEST_SLACK_USER_ID
USER_B = "U0BBBBBBBBB"
USER_C = "U0CCCCCCCCC"  # 本人を解決できない利用者（resolver が None）
DM_A = "D0AAAAAAAAA"
DM_B = "D0BBBBBBBBB"
DM_C = "D0CCCCCCCCC"
CHANNEL = "C0DDDDDDDDD"
MEMBER_EMAIL = "member@vectorinc.co.jp"
OTHER_EMAIL = "other@vectorinc.co.jp"
BEARER = "probe-mcp-bearer-" + "b" * 32
BOT_TOKEN = "xoxb-probe-bot-token"
MCP_PATH = "/mcp"
# plugin の mcp 予算（initialize〜tools/call 全体）。initialize が遅い CI でも tools/call の前に
# 切れないだけの幅をとり、代役の ✏️ はそれより十分長く止める（ツールを渡した後に途切れる形）。
SHORT_TIMEOUT_MS = 2000
SLOW_THREAD = "199a1b2c3d4e5f99"
SLOW_SECONDS = 5.0
MAIL_THREAD = "199a1b2c3d4e5f60"

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
    "ENABLE_PROGRESS_NOTIFY",
    "USE_PAYLOAD_OFFLOAD",
)
_MAIL_ACTION_SECRET = "dedicated-mail-key-" + "k" * 40
# ダイジェストが載せる件名の上限（60 字）いっぱいの日本語の件名。
_MAX_TITLE = ("【定例】株式会社サンプルホールディングス様 週次営業打合せ" + "あ" * 40)[:60]

# plugin の文言（ACTION_BINDINGS）。テストでは実値を写して固定する（文言を変えたらここも変える）。
_CALENDAR_UNKNOWN = "予定を登録できたか確認できませんでした。カレンダーに入っていなければ『予定入れといて』と送ってください。"
_CALENDAR_RETRY = "予定の登録に失敗しました。もう一度押すか、『予定入れといて』と送ってください。"
_PENDING_MAIL_DRAFT = (
    "✏️ 返信下書きを作っています。できたらこの DM でお知らせします（数十秒かかることがあります）。"
)
_PENDING_SCHEDULE = "🗓 日程候補の下書きを作っています。できたらこの DM でお知らせします。"
_STALE_TEXT = "このボタンは使えなくなっています。最新の朝ダイジェストから押してください。"
_DM_ONLY_TEXT = "このボタンは Aico との DM に届いた朝ダイジェストでだけ使えます。DM のダイジェストから押してください。"
_MAIL_DRAFT_RETRY = "返信下書きを作れませんでした。もう一度押してください。"
_MAIL_DRAFT_UNKNOWN = "返信下書きを作れたか確認できませんでした。Gmail の下書きをご確認ください。"
# 台帳にある押下の押し直しへの一時表示（2026-09-29 レビュー指摘: 押し直しが無言だった）。
_RUNNING_TEXT = "このボタンはいま処理しています。終わったらこの DM でお知らせします。"
_ALREADY_PRESSED_TEXT = "このボタンはすでに押されています（同じボタンは 1 回だけ使えます）。結果はこの DM にお送りしています。"
# mcp の本人特定の拒否（server.py の _identity_rejected）の末尾の診断行（connect_diagnostics.format_diag_line）。
_IDENTITY_DIAG_RE = re.compile(
    r"^診断: CONNECT-I01(?P<sub>[abc]) \d{4}-\d{2}-\d{2} \d{2}:\d{2} JST (?P<subject>-|U[A-Z0-9]{8,})$"
)

# 利用者に出してはいけない内部語（ツール名・引数名・mcp の門のコード・英語の例外文）。
_INTERNAL_WORDS = (
    "calendar_event",
    "schedule_propose",
    "mail_draft",
    "digest_ack",
    "event_token",
    "schedule_token",
    "draft_token",
    "ack_token",
    "CALLER_IDENTITY",
    "Caller authorization",
    "CONNECT-",
    "unknown tool",
    "request_id",
    "job_id",
    "expired",
    "not_connected",
)


# ── mcp 側のツール（本物の skill ＋ 外部だけ偽物 / 本物の schema・文言の代役）──────────────
class _Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calendar_inserts: list[dict[str, str]] = []
        self.acks: list[tuple[str, str, int]] = []
        self.schedule_threads: list[str] = []
        self.mail_threads: list[str] = []


class _FakeCalendar:
    """Google カレンダーの偽物（本物の skill が呼ぶ insert_event だけ）。同じ id は 409 相当。"""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def insert_event(
        self,
        request_id: str,
        *,
        summary: str,
        start_iso: str,
        end_iso: str,
        location: str = "",
        event_id: str = "",
        **_: Any,
    ) -> SimpleNamespace:
        with self._recorder.lock:
            if any(row["event_id"] == event_id for row in self._recorder.calendar_inserts):
                raise DuplicateEventError(event_id)
            self._recorder.calendar_inserts.append(
                {"summary": summary, "start": start_iso, "end": end_iso, "event_id": event_id}
            )
        return SimpleNamespace(html_link=f"https://www.google.com/calendar/event?eid={event_id}")


class _FakeTokenStore:
    def get(self, email: str) -> SimpleNamespace | None:
        if email not in (MEMBER_EMAIL, OTHER_EMAIL):
            return None
        return SimpleNamespace(scopes=("https://www.googleapis.com/auth/calendar.events",))


class _FakeAckStore:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def ack(self, requester: str, items: Any, *, request_id: str) -> int:
        with self._recorder.lock:
            self._recorder.acks.append(("ack", requester, len(items)))
        return len(items)

    def unack(self, requester: str, items: Any, *, request_id: str) -> int:
        with self._recorder.lock:
            self._recorder.acks.append(("unack", requester, len(items)))
        return len(items)


_RECORDER = _Recorder()


class _SchedulePropose(BaseSkill[ScheduleProposeInput, ScheduleProposeOutput]):
    """🗓 の代役: 本物の schema・本物の復号・本物の文言（Gmail と空き枠の計算だけ省く）。"""

    name: ClassVar[str] = "schedule_propose"
    description: ClassVar[str] = "schedule_propose stand-in with the production schema."
    input_schema: ClassVar[type[BaseModel]] = ScheduleProposeInput
    output_schema: ClassVar[type[BaseModel]] = ScheduleProposeOutput

    def run(self, input: ScheduleProposeInput, ctx: SkillContext) -> ScheduleProposeOutput:
        requester = str(ctx.metadata["user_email"])
        thread_id = decode_draft_token(input.schedule_token, requester)
        if not thread_id:
            return ScheduleProposeOutput(
                error="expired", message=schedule_skill_module._ERR_MSG["expired"]
            )
        with _RECORDER.lock:
            _RECORDER.schedule_threads.append(thread_id)
        import datetime as dt

        jst = dt.timezone(dt.timedelta(hours=9))
        slots = [
            (dt.datetime(2026, 10, 1, 10, tzinfo=jst), dt.datetime(2026, 10, 1, 11, tzinfo=jst)),
            (dt.datetime(2026, 10, 2, 14, tzinfo=jst), dt.datetime(2026, 10, 2, 15, tzinfo=jst)),
        ]
        candidates = format_candidates_ja(slots).replace("\n", " / ")
        message = (
            f"🗓 候補 {len(slots)} 件入りの返信下書きを作成しました（未送信）: {candidates}"
            f"\nカレンダーに仮予定 {len(slots)} 件を置きました（透明・他の予定を邪魔しません）。"
        )
        return ScheduleProposeOutput(
            created=True,
            holds_created=len(slots),
            open_url=gmail_thread_url(thread_id),
            message=message,
        )


class _MailDraft(BaseSkill[MailDraftInput, MailDraftOutput]):
    """✏️ の代役: 本物の schema・本物の復号・本物の文言。SLOW_THREAD だけ遅い（LLM 起草の再現）。"""

    name: ClassVar[str] = "mail_draft"
    description: ClassVar[str] = "mail_draft stand-in with the production schema."
    input_schema: ClassVar[type[BaseModel]] = MailDraftInput
    output_schema: ClassVar[type[BaseModel]] = MailDraftOutput

    def run(self, input: MailDraftInput, ctx: SkillContext) -> MailDraftOutput:
        requester = str(ctx.metadata["user_email"])
        thread_id = decode_draft_token(input.draft_token, requester)
        if not thread_id:
            return MailDraftOutput(
                error="expired", message=mail_draft_skill_module._ERR_MSG["expired"]
            )
        if thread_id == SLOW_THREAD:
            time.sleep(SLOW_SECONDS)
        with _RECORDER.lock:
            _RECORDER.mail_threads.append(thread_id)
        return MailDraftOutput(
            created=True,
            open_url=gmail_thread_url(thread_id),
            message=mail_draft_skill_module._OK_MSG,
        )


def _specs(*, with_digest_ack: bool) -> list[ToolSpec]:
    specs = [
        ToolSpec(
            "calendar_event",
            CalendarEventSkill.description,
            CalendarEventSkill,
            factory=lambda: CalendarEventSkill(
                token_store=cast(Any, _FakeTokenStore()),
                gcalendar_factory=lambda _token: _FakeCalendar(_RECORDER),
            ),
        ),
        ToolSpec("schedule_propose", _SchedulePropose.description, _SchedulePropose),
        ToolSpec("mail_draft", _MailDraft.description, _MailDraft),
    ]
    if with_digest_ack:
        specs.append(
            ToolSpec(
                "digest_ack",
                DigestAckSkill.description,
                DigestAckSkill,
                factory=lambda: DigestAckSkill(store=_FakeAckStore(_RECORDER)),
            )
        )
    return specs


def _resolver() -> IdentityResolver:
    people = {
        USER_A: ResolvedIdentity(slack_user_id=USER_A, email=MEMBER_EMAIL),
        USER_B: ResolvedIdentity(slack_user_id=USER_B, email=OTHER_EMAIL),
    }

    async def resolve(slack_user_id: str) -> ResolvedIdentity | None:
        return people.get(slack_user_id)

    return resolve


# ── 本番と同じ streamable-http アプリを uvicorn で立てる ─────────────────────────────────
def _load_http_server_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "run_mcp_http_server_button_e2e", HTTP_SERVER_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Uvicorn:
    def __init__(self, app: Any) -> None:
        import uvicorn

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        self._thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self._sock]}, daemon=True
        )

    def __enter__(self) -> _Uvicorn:
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=15)
        self._sock.close()


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _legacy_escaped_event_shape(title: str) -> str:
    """修正前のエンコーダ（``\\uXXXX``）が件名 60 字で出していた形の value（677 字）。"""
    payload = {
        "v": 2,
        "typ": "event",
        "s": "2026-10-20T10:00:00+09:00",
        "n": "2026-10-20T11:00:00+09:00",
        "l": title,
        "o": _owner_hash(MEMBER_EMAIL),
        "e": int(time.time()) + 86_400,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + "A" * 22


def _tokens() -> dict[str, str]:
    # 本物の skill は壁時計で復号するので、トークンも壁時計で発行する
    # （caller claim の時計は TEST_NOW で固定＝別の時計）。
    now = int(time.time())

    def event(title: str, day: int, month: int = 10) -> str:
        token = encode_event_token(
            start_iso=f"2026-{month:02d}-{day:02d}T10:00:00+09:00",
            end_iso=f"2026-{month:02d}-{day:02d}T11:00:00+09:00",
            title=title,
            owner_email=MEMBER_EMAIL,
            now=now,
        )
        assert token
        return token

    draft = encode_draft_token(MAIL_THREAD, MEMBER_EMAIL, now=now)
    draft_slow = encode_draft_token(SLOW_THREAD, MEMBER_EMAIL, now=now)
    ack = encode_ack_token(
        [AckItem("m", "0123456789abcdef", 1_784_420_000_000)], MEMBER_EMAIL, now=now
    )
    ack_all = encode_ack_all_token(
        [AckItem("m", f"{i:016x}", 1_784_420_000_000) for i in range(10)],
        MEMBER_EMAIL,
        now=now,
    )
    assert draft and draft_slow and ack and ack_all
    return {
        "event": event("A社定例", 20),
        "eventDouble": event("B社定例", 21),
        "eventRetry": event("C社定例", 22),
        "eventSlackApiError": event("D社定例", 23),
        "eventSlack500": event("E社定例", 24),
        "eventLongTitle": event(_MAX_TITLE, 25),
        "eventForeign": event("A社だけの予定", 26),
        "eventUnknownUser": event("F社定例", 27),
        "eventLate": event("G社定例", 2, 11),
        "eventOpenFail": event("H社定例", 3, 11),
        "eventPostTimeout": event("I社定例", 4, 11),
        "eventThread": event("J社定例", 5, 11),
        "eventThreadRejected": event("K社定例", 6, 11),
        "eventThreadTimeout": event("L社定例", 9, 11),
        "eventBothFail": event("M社定例", 10, 11),
        "tooLong": _legacy_escaped_event_shape("あ" * 60),
        "draft": draft,
        "draftSlow": draft_slow,
        "ack": ack,
        "ackAll": ack_all,
    }


def _run_probe(*, mcp_url: str, no_ack_mcp_url: str, tokens: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        ["node", str(PROBE)],
        check=True,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            # loopback の mcp へは proxy を通さない（NODE_USE_ENV_PROXY が効いている環境でも）。
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PROBE_INPUT": json.dumps(
                {
                    "pluginUrl": CALLER_PLUGIN.as_uri(),
                    "secret": TEST_CALLER_CLAIM_SECRET,
                    "teamId": TEST_SLACK_TEAM_ID,
                    "bearer": BEARER,
                    "botToken": BOT_TOKEN,
                    "mcpUrl": mcp_url,
                    "noAckMcpUrl": no_ack_mcp_url,
                    "closedMcpUrl": f"http://127.0.0.1:{_closed_port()}{MCP_PATH}",
                    "userA": USER_A,
                    "userB": USER_B,
                    "userC": USER_C,
                    "dmA": DM_A,
                    "dmB": DM_B,
                    "dmC": DM_C,
                    "channel": CHANNEL,
                    "nowMs": TEST_NOW * 1000,
                    "shortTimeoutMs": SHORT_TIMEOUT_MS,
                    "tokens": tokens,
                }
            ),
        },
        timeout=90,
    )
    return cast(dict[str, Any], json.loads(completed.stdout))


@pytest.fixture(scope="module")
def e2e() -> Iterator[dict[str, Any]]:
    """mcp を 2 つ（全ツール / digest_ack 無し＝本番 OFF の形）立て、probe を 1 回流す。"""
    with pytest.MonkeyPatch.context() as mp:
        for name in _HMAC_ENVS:
            mp.delenv(name, raising=False)
        mp.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_ACTION_SECRET)
        module = _load_http_server_module()

        def app_for(*, with_digest_ack: bool) -> Any:
            mp.setattr(
                module,
                "build_production_server",
                lambda: mcp_server.build_server(
                    specs=_specs(with_digest_ack=with_digest_ack),
                    identity_resolver=_resolver(),
                    company_shared_groups=frozenset({"vectorinc.co.jp"}),
                    allowed_domains=frozenset({"vectorinc.co.jp"}),
                    caller_claim_verifier=make_verifier(),
                ),
            )
            return module.build_app(bearer=BEARER, path=MCP_PATH)

        tokens = _tokens()
        full_app = app_for(with_digest_ack=True)
        no_ack_app = app_for(with_digest_ack=False)
        with _Uvicorn(full_app) as full, _Uvicorn(no_ack_app) as no_ack:
            report = _run_probe(
                mcp_url=f"http://127.0.0.1:{full.port}{MCP_PATH}",
                no_ack_mcp_url=f"http://127.0.0.1:{no_ack.port}{MCP_PATH}",
                tokens=tokens,
            )
            # タイムアウトさせた ✏️ は mcp 側では走り続ける（打ち切りは plugin 側だけ）。
            deadline = time.monotonic() + SLOW_SECONDS + 10
            while SLOW_THREAD not in _RECORDER.mail_threads and time.monotonic() < deadline:
                time.sleep(0.05)
        yield {"report": report, "tokens": tokens, "recorder": _RECORDER}


# ── 共通の検査 ─────────────────────────────────────────────────────────────────────
def _handled_without_system_event(pressed: dict[str, Any]) -> None:
    assert pressed["matched"] is True
    assert pressed["handlerResult"] == {"handled": True}
    assert pressed["enqueued"] is False, "system event も heartbeat も積まない"


def _assert_user_facing(text: str, tokens: dict[str, str]) -> None:
    for word in _INTERNAL_WORDS:
        assert word not in text, word
    for token in tokens.values():
        assert token not in text
        assert token[:40] not in text
    # URL は <url|表示名> の中にだけ出す（生の URL を出さない）。
    outside_links = re.sub(r"<https://[^<>|\s]+\|[^<>]+>", "", text)
    assert "http" not in outside_links, text


def _tool_calls(case: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], case["toolCalls"])


def _split_identity_diagnostic(text: str) -> tuple[str, str]:
    """本人特定の拒否の返事を「本文」と「診断行」に分ける（案内と診断行は mcp の定型そのまま）。"""
    body, hint, diag = text.rsplit("\n", 2)
    assert hint == admin_forward_hint()
    return body, diag


def _ephemeral_texts(case: dict[str, Any]) -> list[str]:
    return [item["text"] for item in case["ephemerals"]]


# ── 1. 📅 ─────────────────────────────────────────────────────────────────────────
def test_calendar_press_runs_the_tool_once_and_posts_once_to_the_pressers_dm(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["calendar"]
    tokens = e2e["tokens"]
    _handled_without_system_event(case["first"])

    calls = _tool_calls(case)
    assert [call["name"] for call in calls] == ["calendar_event"]
    call = calls[0]
    # 引数は束縛のトークン引数 1 つだけ（捕捉した完全な value）＋ plugin が作る _user_context。
    assert call["argumentKeys"] == ["_user_context", "event_token"]
    assert call["tokenArgument"] == tokens["event"]
    # 押した人と会話は押下イベントの実際の値（claim と宣言の両方）。
    assert call["claim"]["tool"] == "calendar_event"
    assert call["claim"]["sub"] == USER_A
    assert call["claim"]["team"] == TEST_SLACK_TEAM_ID
    assert call["claim"]["channel"] == DM_A
    assert call["claim"]["message"] == "1784424000.000101"
    assert call["claim"]["thread"] is None
    assert call["context"] == {
        "slack_user_id": USER_A,
        "slack_team_id": TEST_SLACK_TEAM_ID,
        "channel_id": DM_A,
    }

    # 本物の CalendarEventSkill が本物の復号で登録した（件名・日時がトークンのとおり）。
    inserts = [row for row in e2e["recorder"].calendar_inserts if row["summary"] == "A社定例"]
    assert len(inserts) == 1
    assert inserts[0]["start"] == "2026-10-20T10:00:00+09:00"

    # 押した本人の DM に 1 通。文はツールの message のまま・リンクは <url|カレンダーで開く>。
    assert len(case["posts"]) == 1
    post = case["posts"][0]
    assert post["channel"] == DM_A
    assert "thread_ts" not in post
    assert post["unfurl_links"] is False
    event_url = f"https://www.google.com/calendar/event?eid={inserts[0]['event_id']}"
    assert post["text"] == f"{calendar_skill_module._OK_MSG}\n🔗 <{event_url}|カレンダーで開く>"
    _assert_user_facing(post["text"], tokens)

    # 同じボタンの再押下（trigger 違い）: 何も実行しない・DM へは何も投稿しない。
    _handled_without_system_event(case["replay"])
    assert case["mcpRequests"].count("tools/call") == 1
    assert case["slackCalls"].count("chat.postMessage") == 1
    # 📅 はすぐ返るので押した直後の一時表示は出さない。再押下には、押した本人にだけ一時表示で
    # 「すでに押されています」を返す（無言にしない・2026-09-29 レビュー指摘）。
    assert case["ephemerals"] == [
        {
            "text": _ALREADY_PRESSED_TEXT,
            "responseType": "ephemeral",
            "userId": USER_A,
            "channelId": DM_A,
        }
    ]
    assert any(
        "button repress action=calendar_event state=answered notice=ephemeral" in line
        for line in case["logs"]
    )


def test_register_banner_reports_the_direct_path(e2e: dict[str, Any]) -> None:
    assert "button_direct=yes" in e2e["report"]["mainBanner"]
    assert "button_direct=no" in e2e["report"]["legacy"]["banner"]


# ── 2. 🗓・✏️ ─────────────────────────────────────────────────────────────────────
def test_schedule_and_mail_draft_on_one_row_each_call_only_their_own_tool(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["sameRow"]
    tokens = e2e["tokens"]
    _handled_without_system_event(case["schedule"])
    _handled_without_system_event(case["mailDraft"])
    calls = _tool_calls(case)
    assert sorted(call["name"] for call in calls) == ["mail_draft", "schedule_propose"]
    by_name = {call["name"]: call for call in calls}
    assert by_name["schedule_propose"]["argumentKeys"] == ["_user_context", "schedule_token"]
    assert by_name["mail_draft"]["argumentKeys"] == ["_user_context", "draft_token"]
    assert by_name["schedule_propose"]["tokenArgument"] == tokens["draft"]
    assert by_name["mail_draft"]["tokenArgument"] == tokens["draft"]
    # 押下ごとに別の nonce（同じ行・同じ値でも action_id が違えば別の押下）。
    assert by_name["schedule_propose"]["claim"]["nonce"] != by_name["mail_draft"]["claim"]["nonce"]
    assert MAIL_THREAD in e2e["recorder"].schedule_threads
    assert MAIL_THREAD in e2e["recorder"].mail_threads

    gmail = f"<{gmail_thread_url(MAIL_THREAD)}|Gmailで開く>"
    texts = sorted(post["text"] for post in case["posts"])
    assert len(texts) == 2
    assert all(post["channel"] == DM_A for post in case["posts"])
    assert f"{mail_draft_skill_module._OK_MSG}\n🔗 {gmail}" in texts
    schedule_text = next(text for text in texts if text.startswith("🗓 候補 2 件"))
    assert schedule_text.endswith(f"\n🔗 {gmail}")
    for text in texts:
        _assert_user_facing(text, tokens)
    # 結果まで時間のかかる 🗓・✏️ は、押した直後に本人にだけ見える 1 行を出す
    # （上流の ctx.respond.reply＝response_url の ephemeral。押下の会話・押した本人）。
    assert sorted(
        (item["text"], item["responseType"], item["userId"], item["channelId"])
        for item in case["ephemerals"]
    ) == sorted(
        [
            (_PENDING_MAIL_DRAFT, "ephemeral", USER_A, DM_A),
            (_PENDING_SCHEDULE, "ephemeral", USER_A, DM_A),
        ]
    )
    for item in case["ephemerals"]:
        _assert_user_facing(item["text"], tokens)


# ── 3・4. ☑️ ──────────────────────────────────────────────────────────────────────
def test_digest_ack_posts_the_undo_button_and_the_undo_press_is_handled_directly(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["ackThenUndo"]
    _handled_without_system_event(case["ack"])
    _handled_without_system_event(case["undo"])
    assert [call["name"] for call in _tool_calls(case)] == ["digest_ack", "digest_ack"]
    assert [row for row in e2e["recorder"].acks if row[1] == MEMBER_EMAIL][:2] == [
        ("ack", MEMBER_EMAIL, 1),
        ("unack", MEMBER_EMAIL, 1),
    ]
    first, second = case["posts"]
    assert first["channel"] == second["channel"] == DM_A
    assert first["text"] == "☑️ 確認済みにしました。新しい返信が来たら、また表示します。"
    button = first["blocks"][0]["accessory"]
    assert button["action_id"] == "digest_ack"
    assert button["text"]["text"] == "↩︎ 取り消す"
    # 取り消しボタンの value は本物の unack トークン（本人の分だけ）。
    undo = decode_ack_token(button["value"], MEMBER_EMAIL)
    assert undo is not None and undo.kind == "unack"
    assert second["text"] == "↩︎ 取り消しました。次回の朝ダイジェストにまた表示されます。"
    assert "blocks" not in second
    # 取り消した後に元の ☑️ をもう一度押す: 同じ押下は 1 回だけ（mcp の nonce も同じ）なので実行しない。
    # 以前は無言だった。押した本人にだけ一時表示で返す。
    _handled_without_system_event(case["reAck"])
    assert len(_tool_calls(case)) == 2
    assert len(case["posts"]) == 2
    assert _ephemeral_texts(case) == [_ALREADY_PRESSED_TEXT]


def test_digest_ack_disabled_on_mcp_says_the_button_is_unavailable(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["ackDisabled"]
    _handled_without_system_event(case["pressed"])
    assert [call["name"] for call in _tool_calls(case)] == ["digest_ack"]
    assert [post["text"] for post in case["posts"]] == ["このボタンはいま使えません。"]
    assert case["posts"][0]["channel"] == DM_A


# ── 5. 件名 60 字の 📅 ───────────────────────────────────────────────────────────
def test_sixty_char_japanese_title_passes_the_mcp_schema_and_registers_the_full_title(
    e2e: dict[str, Any],
) -> None:
    tokens = e2e["tokens"]
    assert len(_MAX_TITLE) == 60
    assert 160 < len(tokens["eventLongTitle"]) <= EVENT_TOKEN_MAX_LENGTH
    case = e2e["report"]["longTitle"]
    _handled_without_system_event(case["pressed"])
    assert _tool_calls(case)[0]["tokenArgument"] == tokens["eventLongTitle"]
    # 本物の CalendarEventInput（max_length=500）を通り、件名が 1 字も欠けずに登録された。
    assert [row["summary"] for row in e2e["recorder"].calendar_inserts].count(_MAX_TITLE) == 1
    assert case["posts"][0]["text"].startswith(calendar_skill_module._OK_MSG)


# ── 6・7. 二重押下・再起動後の同じ押下 ─────────────────────────────────────────────
def test_double_press_while_running_executes_once(e2e: dict[str, Any]) -> None:
    case = e2e["report"]["doublePress"]
    _handled_without_system_event(case["first"])
    _handled_without_system_event(case["second"])
    assert case["mcpRequests"].count("tools/call") == 1
    assert len(case["posts"]) == 1
    assert [row["summary"] for row in e2e["recorder"].calendar_inserts].count("B社定例") == 1
    # 処理中の 2 回目には「いま処理しています」を本人にだけ一時表示（無言にしない）。
    assert _ephemeral_texts(case) == [_RUNNING_TEXT]


def test_same_press_after_a_plugin_restart_is_stopped_by_the_mcp_one_use_nonce(
    e2e: dict[str, Any],
) -> None:
    """plugin の台帳が空（再起動）でも、同じ押下は nonce が同じなので mcp が止める（二重の守り）。

    mcp はこの再生を CALLER_IDENTITY_REJECTED で返し、本人を確かめられない拒否と見分けられない。
    すでに登録済みでありうるので、自由文での頼み直しを勧める失敗文（二重登録の入口）ではなく、
    確かめてから頼み直す文（texts.unknown）にする（2026-09-29 レビュー指摘）。
    """
    first = _tool_calls(e2e["report"]["calendar"])[0]["claim"]
    case = e2e["report"]["restartReplay"]
    _handled_without_system_event(case["pressed"])
    again = _tool_calls(case)[0]["claim"]
    assert again["nonce"] == first["nonce"]
    assert again["run_id"] != first["run_id"]
    assert [row["summary"] for row in e2e["recorder"].calendar_inserts].count("A社定例") == 1
    assert len(case["posts"]) == 1
    # 本文は texts.unknown。mcp の診断行（I01a・本人不明なので識別子は "-"）を定型のまま後ろに添える。
    body, diag = _split_identity_diagnostic(case["posts"][0]["text"])
    assert body == _CALENDAR_UNKNOWN
    match = _IDENTITY_DIAG_RE.fullmatch(diag)
    assert match is not None and match["sub"] == "a" and match["subject"] == "-", diag
    assert any(
        "result=gateway_caller_identity_rejected diag=CONNECT-I01a" in line for line in case["logs"]
    )
    _assert_user_facing(body, e2e["tokens"])


def test_repress_after_ten_minutes_is_still_stopped_by_the_plugin_ledger(
    e2e: dict[str, Any],
) -> None:
    """押下の台帳はボタンの value の寿命（24h）より長く持つ（mcp の nonce より先に切れない）。"""
    case = e2e["report"]["lateRepress"]
    for name in ("first", "after11min", "after23h"):
        _handled_without_system_event(case[name])
    assert case["mcpRequests"].count("tools/call") == 1
    assert [row["summary"] for row in e2e["recorder"].calendar_inserts].count("G社定例") == 1
    assert len(case["posts"]) == 1
    assert case["posts"][0]["text"].startswith(calendar_skill_module._OK_MSG)
    assert sum("rejected replayed Slack button action" in line for line in case["logs"]) == 2


# ── 8. 他人の押下 ──────────────────────────────────────────────────────────────────
def test_another_users_token_is_rejected_by_mcp_and_answered_only_in_their_own_dm(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["crossUserToken"]
    _handled_without_system_event(case["pressed"])
    call = _tool_calls(case)[0]
    assert call["claim"]["sub"] == USER_B
    assert call["claim"]["channel"] == DM_B
    # 本物の CalendarEventSkill が本人照合で無効にした（A の予定は登録されない）。
    assert "A社だけの予定" not in [row["summary"] for row in e2e["recorder"].calendar_inserts]
    assert [(post["channel"], post["text"]) for post in case["posts"]] == [
        (DM_B, calendar_skill_module._ERR_MSG["expired"])
    ]


def test_press_from_someone_elses_dm_is_not_executed_and_is_answered_in_the_pressers_dm(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["foreignDm"]
    _handled_without_system_event(case["pressed"])
    assert case["mcpRequests"] == []
    assert [(post["channel"], post["text"]) for post in case["posts"]] == [
        (
            DM_B,
            "このボタンは Aico との DM に届いた朝ダイジェストでだけ使えます。"
            "DM のダイジェストから押してください。",
        )
    ]


def test_channel_press_is_not_executed_and_nothing_is_posted_to_the_channel(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["channelPress"]
    _handled_without_system_event(case["pressed"])
    assert case["mcpRequests"] == []
    assert [post["channel"] for post in case["posts"]] == [DM_A]
    assert "thread_ts" not in case["posts"][0]
    assert case["posts"][0]["text"].startswith("このボタンは Aico との DM に届いた朝ダイジェスト")


# ── 9. 形の違う値・組み替え・未認可 ──────────────────────────────────────────────────
def test_values_of_the_wrong_shape_are_not_executed_and_get_a_short_notice(
    e2e: dict[str, Any],
) -> None:
    shape = e2e["report"]["shape"]
    names = ("draftOnCalendar", "eventOnSchedule", "tooLong", "mailDraftOver160", "garbage")
    for name in names:
        _handled_without_system_event(shape[name])
    report = shape["report"]
    assert report["mcpRequests"] == []
    assert [(post["channel"], post["text"]) for post in report["posts"]] == [
        (DM_A, "このボタンは使えなくなっています。最新の朝ダイジェストから押してください。")
    ] * len(names)


def test_recombined_or_unauthorized_presses_do_nothing_at_all(e2e: dict[str, Any]) -> None:
    silent = e2e["report"]["silent"]
    for name in ("recombined", "unauthorized"):
        _handled_without_system_event(silent[name])
    assert silent["report"]["mcpRequests"] == []
    assert silent["report"]["slackCalls"] == []


def test_mcp_gate_rejection_is_answered_with_a_fixed_sentence(e2e: dict[str, Any]) -> None:
    """本人を解決できない拒否も CALLER_IDENTITY_REJECTED（再生と同じ code）なので、確かめる文にする。"""
    case = e2e["report"]["unknownUser"]
    _handled_without_system_event(case["pressed"])
    assert [call["name"] for call in _tool_calls(case)] == ["calendar_event"]
    assert [post["channel"] for post in case["posts"]] == [DM_C]
    # 本人の解決失敗（I01c）は利用者では直らない。管理者へ転送する診断行が本人に届く
    # （AI 経路の SOUL「診断: 行はそのまま出す」と同じ・2026-09-29 レビュー指摘）。
    body, diag = _split_identity_diagnostic(case["posts"][0]["text"])
    assert body == _CALENDAR_UNKNOWN
    match = _IDENTITY_DIAG_RE.fullmatch(diag)
    assert match is not None and match["sub"] == "c" and match["subject"] == USER_C, diag
    # 英語の理由・対処文（mcp の error の 1 行目）は出さない。
    assert "Caller authorization" not in case["posts"][0]["text"]
    assert "利用者側の操作では直りません" not in case["posts"][0]["text"]
    _assert_user_facing(body, e2e["tokens"])


# ── 11〜13. 失敗 ──────────────────────────────────────────────────────────────────
def test_mcp_down_before_the_tool_was_sent_allows_pressing_again(e2e: dict[str, Any]) -> None:
    case = e2e["report"]["mcpDown"]
    _handled_without_system_event(case["down"])
    # initialize で接続拒否＝tools/call は 1 度も出ていない。
    assert case["afterDown"]["mcpRequests"] == ["initialize"]
    assert [post["text"] for post in case["afterDown"]["posts"]] == [
        "予定の登録に失敗しました。もう一度押すか、『予定入れといて』と送ってください。"
    ]
    # 案内どおりもう一度押すと、今度は実行される（1 回だけ）。
    _handled_without_system_event(case["again"])
    assert case["mcpRequests"].count("tools/call") == 1
    assert [row["summary"] for row in e2e["recorder"].calendar_inserts].count("C社定例") == 1
    assert case["posts"][-1]["text"].startswith(calendar_skill_module._OK_MSG)
    assert len(case["posts"]) == 2


def test_timeout_after_the_tool_was_sent_does_not_invite_a_second_run(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["mcpTimeout"]
    _handled_without_system_event(case["slow"])
    assert [post["text"] for post in case["posts"]] == [
        "返信下書きを作れたか確認できませんでした。Gmail の下書きをご確認ください。"
    ]
    assert any("result=unknown_timeout" in line for line in case["logs"])
    # もう一度押しても実行しない（実行されたか分からない押下を 2 回目に回さない）。
    _handled_without_system_event(case["again"])
    assert case["mcpRequests"].count("tools/call") == 1
    # mcp 側では 1 回だけ実行された（打ち切ったのは plugin の待ちだけ）。
    assert e2e["recorder"].mail_threads.count(SLOW_THREAD) == 1
    # 「作っています」は 1 回だけ。押し直しには「すでに押されています（結果は DM）」を返す（無言にしない）。
    assert _ephemeral_texts(case) == [_PENDING_MAIL_DRAFT, _ALREADY_PRESSED_TEXT]


@pytest.mark.parametrize(
    ("name", "summary"), [("slackApiError", "D社定例"), ("slackHttp500", "E社定例")]
)
def test_slack_failures_do_not_raise_and_do_not_rerun_the_tool(
    e2e: dict[str, Any], name: str, summary: str
) -> None:
    case = e2e["report"][name]
    _handled_without_system_event(case["pressed"])
    assert case["mcpRequests"].count("tools/call") == 1
    assert [row["summary"] for row in e2e["recorder"].calendar_inserts].count(summary) == 1
    assert case["posts"] == []
    assert any(
        "outcome=post_failed" in line and "fallback=ephemeral" in line for line in case["logs"]
    )
    # 結果は押下の会話（本人の DM）で本人にだけ一時表示で届く（無言にしない・2026-09-29 レビュー指摘）。
    assert len(case["ephemerals"]) >= 1
    assert case["ephemerals"][0]["text"].startswith(calendar_skill_module._OK_MSG)
    assert (case["ephemerals"][0]["userId"], case["ephemerals"][0]["channelId"]) == (USER_A, DM_A)
    _assert_user_facing(case["ephemerals"][0]["text"], e2e["tokens"])
    if name == "slackHttp500":
        # 5xx は保証経路と同じく 2 回まで再送してから諦める（ツールは再実行しない）。
        assert case["slackCalls"].count("chat.postMessage") == 3
    else:
        # DM に結果が残っていない押下の押し直し: 実行せず「確かめてください」を一時表示。
        _handled_without_system_event(case["repressed"])
        assert _ephemeral_texts(case)[1:] == [_CALENDAR_UNKNOWN]
        assert any("state=undelivered notice=ephemeral" in line for line in case["logs"])
    assert e2e["report"]["unhandledRejections"] == 0


def test_result_post_and_ephemeral_both_failing_does_not_raise(e2e: dict[str, Any]) -> None:
    case = e2e["report"]["slackAndRespondFail"]
    _handled_without_system_event(case["pressed"])
    assert case["mcpRequests"].count("tools/call") == 1
    assert [row["summary"] for row in e2e["recorder"].calendar_inserts].count("M社定例") == 1
    assert case["posts"] == []
    assert case["ephemerals"] == []
    assert any(
        "outcome=post_failed" in line and "fallback=ephemeral_failed_unexpected" in line
        for line in case["logs"]
    )
    assert e2e["report"]["unhandledRejections"] == 0


# ── 17. 本人の DM を確かめられない（conversations.open の失敗）────────────────────────────
def test_dm_lookup_failure_is_not_silent_and_the_button_can_be_pressed_again(
    e2e: dict[str, Any],
) -> None:
    """conversations.open が ok:false（再試行しない）でも無言にしない。押した本人にだけ一時表示で返す。"""
    case = e2e["report"]["openApiError"]
    _handled_without_system_event(case["failed"])
    after = case["afterFail"]
    assert after["slackCalls"] == ["conversations.open"]
    assert after["mcpRequests"] == []
    assert after["posts"] == []
    # 押下の会話で押した本人にだけ見える（チャンネルにも他人の DM にも投稿しない）。
    assert after["ephemerals"] == [
        {"text": _CALENDAR_RETRY, "responseType": "ephemeral", "userId": USER_A, "channelId": DM_A}
    ]
    assert any(
        "outcome=dm_unresolved reason=slack_api_internal_error notice=ephemeral" in line
        for line in after["logs"]
    )
    # 何も実行していないので台帳から外れている＝Slack が戻ってから押し直すと 1 回だけ実行される。
    _handled_without_system_event(case["again"])
    assert case["mcpRequests"].count("tools/call") == 1
    assert [row["summary"] for row in e2e["recorder"].calendar_inserts].count("H社定例") == 1
    assert [post["channel"] for post in case["posts"]] == [DM_A]
    assert case["posts"][0]["text"].startswith(calendar_skill_module._OK_MSG)


def test_dm_lookup_failure_with_an_expired_response_url_does_not_raise(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["openHttp500"]
    _handled_without_system_event(case["pressed"])
    # 5xx は 2 回まで再送してから諦める。ツールは実行しない。
    assert case["slackCalls"] == ["conversations.open"] * 3
    assert case["mcpRequests"] == []
    assert case["posts"] == []
    assert case["ephemerals"] == []
    assert any(
        "outcome=dm_unresolved reason=slack_http_500 notice=ephemeral_failed_unexpected" in line
        for line in case["logs"]
    )
    assert e2e["report"]["unhandledRejections"] == 0


# ── 18・19. 投稿の時間切れ・スレッド ───────────────────────────────────────────────────
def test_result_post_accepted_then_timed_out_is_not_sent_twice(e2e: dict[str, Any]) -> None:
    """Slack が受け付けた後にこちらの待ちが切れても再送しない（同じ結果を DM に 2 通残さない）。

    届いたか分からないので、同じ文を押した本人にだけ一時表示でも返す（再読み込みで消える＝
    2 通目として残らない。無言より重複のほうがまし）。
    """
    case = e2e["report"]["postTimeout"]
    _handled_without_system_event(case["pressed"])
    assert case["mcpRequests"].count("tools/call") == 1
    assert case["slackCalls"].count("chat.postMessage") == 1
    assert len(case["posts"]) == 1
    assert _ephemeral_texts(case) == [case["posts"][0]["text"]]
    assert any(
        "outcome=post_failed" in line and "reason=slack_timeout" in line for line in case["logs"]
    )


def test_press_inside_a_thread_keeps_the_thread_in_claim_context_and_post(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["threadPress"]
    thread = case["thread"]
    _handled_without_system_event(case["pressed"])
    call = _tool_calls(case)[0]
    assert call["claim"]["thread"] == thread
    assert call["context"]["thread_ts"] == thread
    assert call["claim"]["channel"] == DM_A
    assert [row["summary"] for row in e2e["recorder"].calendar_inserts].count("J社定例") == 1
    assert [(post["channel"], post.get("thread_ts")) for post in case["posts"]] == [(DM_A, thread)]


def test_thread_post_rejected_falls_back_once_without_the_thread(e2e: dict[str, Any]) -> None:
    case = e2e["report"]["threadRejected"]
    _handled_without_system_event(case["pressed"])
    assert case["mcpRequests"].count("tools/call") == 1
    # スレッドへの投稿（ok:false は再試行しない）→ スレッド無しで 1 回だけ。
    assert case["slackCalls"].count("chat.postMessage") == 2
    assert [(post["channel"], "thread_ts" in post) for post in case["posts"]] == [(DM_A, False)]
    assert case["posts"][0]["text"].startswith(calendar_skill_module._OK_MSG)
    assert any("outcome=delivered result=tool_message" in line for line in case["logs"])


def test_thread_post_timeout_does_not_fall_back_without_the_thread(e2e: dict[str, Any]) -> None:
    case = e2e["report"]["threadTimeout"]
    _handled_without_system_event(case["pressed"])
    assert case["slackCalls"].count("chat.postMessage") == 1
    assert [post.get("thread_ts") for post in case["posts"]] == [case["thread"]]
    assert any(
        "outcome=post_failed" in line and "reason=slack_timeout" in line for line in case["logs"]
    )


# ── 20. 形の合わない値の案内の 1 回性 ──────────────────────────────────────────────────
def test_stale_value_notice_is_sent_once_per_button(e2e: dict[str, Any]) -> None:
    case = e2e["report"]["staleTwice"]
    _handled_without_system_event(case["first"])
    _handled_without_system_event(case["second"])
    assert case["mcpRequests"] == []
    assert [(post["channel"], post["text"]) for post in case["posts"]] == [(DM_A, _STALE_TEXT)]


def test_stale_value_notice_is_retried_after_a_failed_post(e2e: dict[str, Any]) -> None:
    case = e2e["report"]["staleNoticeRetry"]
    assert case["mcpRequests"] == []
    # 1 回目は DM（ok:false）にも一時表示（response_url の失効）にも届かない → 押し直しで DM へ届く
    # → 3 回目は DM へは出さず、一時表示で同じ案内だけ。
    assert case["slackCalls"].count("chat.postMessage") == 2
    assert [(post["channel"], post["text"]) for post in case["posts"]] == [(DM_A, _STALE_TEXT)]
    assert _ephemeral_texts(case) == [_STALE_TEXT]
    assert any(
        "outcome=post_failed notice=value_shape" in line
        and "fallback=ephemeral_failed_unexpected" in line
        for line in case["logs"]
    )


def test_stale_value_notice_falls_back_to_an_ephemeral_when_the_dm_post_fails(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["staleNoticeFallback"]
    assert case["mcpRequests"] == []
    # DM への案内は ok:false でも、押した本人にだけ一時表示で届く＝案内済み。押し直しは一時表示だけ。
    assert case["posts"] == []
    assert case["slackCalls"].count("chat.postMessage") == 1
    assert _ephemeral_texts(case) == [_STALE_TEXT, _STALE_TEXT]
    assert any("outcome=post_failed notice=value_shape" in line for line in case["logs"])
    assert any("button repress action=digest_ack state=notice" in line for line in case["logs"])


# ── 21. DM 以外で押された案内の投稿失敗（2026-09-29 レビュー指摘: 24h 無言になっていた）────────
def test_not_own_dm_notice_falls_back_to_an_ephemeral_in_the_pressed_conversation(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["notOwnDmFallback"]
    _handled_without_system_event(case["first"])
    _handled_without_system_event(case["again"])
    assert case["mcpRequests"] == []
    assert case["posts"] == []
    # 押下の会話（チャンネル）で押した本人にだけ見える一時表示。チャンネルへは投稿しない。
    assert [(item["text"], item["userId"], item["channelId"]) for item in case["ephemerals"]] == [
        (_DM_ONLY_TEXT, USER_A, CHANNEL),
        (_DM_ONLY_TEXT, USER_A, CHANNEL),
    ]
    # 案内は届いたので DM への投稿は繰り返さない（押し直しは一時表示だけ）。
    assert case["slackCalls"].count("chat.postMessage") == 1
    assert any(
        "outcome=post_failed result=not_own_dm" in line and "fallback=ephemeral" in line
        for line in case["logs"]
    )


def test_not_own_dm_notice_that_reached_nobody_is_retried_on_the_next_press(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["notOwnDmRetry"]
    for name in ("failed", "retried", "third"):
        _handled_without_system_event(case[name])
    assert case["afterFail"]["posts"] == []
    assert case["afterFail"]["ephemerals"] == []
    assert case["mcpRequests"] == []
    # どこにも届かなかったので印を外した → 押し直しで DM へ案内が届く → 3 回目は一時表示だけ。
    assert [(post["channel"], post["text"]) for post in case["posts"]] == [(DM_A, _DM_ONLY_TEXT)]
    assert [(item["text"], item["channelId"]) for item in case["ephemerals"]] == [
        (_DM_ONLY_TEXT, CHANNEL)
    ]
    assert any("state=not_own_dm notice=ephemeral" in line for line in case["logs"])


# ── 22. 「作っています」と結果の順序 ─────────────────────────────────────────────────
def test_pending_notice_is_shown_before_a_fast_result(e2e: dict[str, Any]) -> None:
    """結果がすぐ出る（mcp 停止）ときも、「作っています」が結果の後に届かない（2026-09-29 レビュー指摘）。"""
    case = e2e["report"]["pendingOrder"]
    _handled_without_system_event(case["pressed"])
    assert case["mcpRequests"] == ["initialize"]
    assert case["timeline"] == [
        {"kind": "ephemeral", "text": _PENDING_MAIL_DRAFT},
        {"kind": "post", "text": _MAIL_DRAFT_RETRY},
    ]


# ── 15. 文面の組み立ての境界 ─────────────────────────────────────────────────────────
def test_tool_text_cannot_inject_slack_markup(e2e: dict[str, Any]) -> None:
    render = e2e["report"]["render"]
    assert render["escaped"]["reply"] == {
        "text": "&lt;!here&gt; A&amp;B &lt;@U0123456789&gt; 登録しました"
    }


def test_only_safe_google_https_links_become_links(e2e: dict[str, Any]) -> None:
    render = e2e["report"]["render"]
    for name in ("pipeLink", "httpLink", "foreignHost"):
        assert "<" not in render[name]["reply"]["text"], name
        assert "🔗" not in render[name]["reply"]["text"], name
    assert render["ampLink"]["reply"]["text"] == (
        "作成しました\n🔗 <https://mail.google.com/mail/u/0/?a=1&amp;b=2#all/abc|Gmailで開く>"
    )


def test_links_whose_checked_form_differs_from_the_output_are_rejected(
    e2e: dict[str, Any],
) -> None:
    """検査した値（WHATWG の解釈）と Slack へ出す値がずれるリンクは通さない（2026-09-29 レビュー指摘）。"""
    render = e2e["report"]["render"]
    for name in ("backslashLink", "backslashQuery", "userinfoLink", "nonCanonicalLink"):
        assert render[name]["reply"] == {"text": "登録しました"}, name
    # リンクにできなかった URL は文からも消える（生の URL を Slack に自動リンクさせない）。
    assert render["rawUrlInMessage"]["reply"] == {"text": "📅 登録しました"}
    # URL の形でない値では文を削らない。
    assert render["notAUrl"]["reply"] == {"text": "登録しました"}


def test_undo_button_is_only_made_from_an_unack_token(e2e: dict[str, Any]) -> None:
    render = e2e["report"]["render"]
    for name in ("undoWrongType", "undoGarbage"):
        assert render[name]["reply"] == {"text": "☑️ 確認済みにしました。"}, name


def test_gateway_errors_and_broken_results_become_fixed_sentences(e2e: dict[str, Any]) -> None:
    render = e2e["report"]["render"]
    expected = {
        # CALLER_IDENTITY_REJECTED は one-use nonce の再生（実行済み）と見分けられない → 確かめる文。
        "gatewayError": "日程候補の下書きを作れたか確認できませんでした。Gmail の下書きをご確認ください。",
        # それ以外の門の拒否（ツールは走っていない）→ 別の頼み方を案内する文。
        "gatewayOther": "日程候補の下書きを作れませんでした。お手数ですが Gmail から直接ご返信ください。",
        "exception": (
            "返信下書きを作れませんでした。お手数ですが『（件名）の返信下書きを作って』と送ってください。"
        ),
        "otherUnknownTool": "予定の登録に失敗しました。『予定入れといて』と送ってください。",
        "isError": "予定の登録に失敗しました。『予定入れといて』と送ってください。",
        "brokenJson": "確認済みにできませんでした。次回の朝ダイジェストでもう一度お試しください。",
    }
    for name, text in expected.items():
        assert render[name]["reply"] == {"text": text}, name
        _assert_user_facing(render[name]["reply"]["text"], e2e["tokens"])
    assert render["gatewayError"]["result"] == "gateway_caller_identity_rejected"
    assert render["gatewayOther"]["result"] == "gateway_tool_input_invalid"


def test_identity_rejection_adds_only_the_canonical_diagnostic_lines(e2e: dict[str, Any]) -> None:
    """mcp の本人特定の拒否は、定型の案内と診断行（CONNECT-I01a/b/c）だけを添える。"""
    render = e2e["report"]["render"]
    assert render["identityDiag"]["reply"] == {
        "text": _CALENDAR_UNKNOWN
        + "\n解決しない場合は、次の 1 行をそのまま管理者（小俣）へ送ってください:"
        + "\n診断: CONNECT-I01c 2026-09-29 12:00 JST U0CCCCCCCCC"
    }
    assert render["identityDiag"]["result"] == "gateway_caller_identity_rejected diag=CONNECT-I01c"
    for name in ("identityDiagMarkup", "identityDiagLink", "identityDiagOtherCode"):
        assert render[name]["reply"] == {"text": _CALENDAR_UNKNOWN}, name
        assert render[name]["result"] == "gateway_caller_identity_rejected", name


def test_reply_fields_the_plugin_reads_exist_in_each_tools_output_schema() -> None:
    """plugin が読む欄（message・リンク欄・取り消しトークン欄）が、各ツールの本物の出力 schema にある。

    ツール側で欄の名前を変えると、直接実行の返事からリンクや取り消しボタンが黙って消える。
    """
    from teamagent.skills.calendar_event.schema import CalendarEventOutput
    from teamagent.skills.digest_ack.schema import DigestAckOutput

    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            f"const m = await import({json.dumps(CALLER_PLUGIN.as_uri())});"
            "process.stdout.write(JSON.stringify(m.ACTION_BINDINGS));",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    bindings = json.loads(completed.stdout)
    outputs: dict[str, type[BaseModel]] = {
        "calendar_event": CalendarEventOutput,
        "schedule_propose": ScheduleProposeOutput,
        "mail_draft": MailDraftOutput,
        "digest_ack": DigestAckOutput,
    }
    assert set(bindings) == set(outputs)
    for action_id, binding in bindings.items():
        fields = outputs[action_id].model_fields
        assert "message" in fields, action_id
        if binding["resultLink"] is not None:
            assert binding["resultLink"]["field"] in fields, action_id
        if binding["undoToken"] is not None:
            assert binding["undoToken"]["field"] in fields, action_id
        assert set(binding["texts"]) == {"retry", "failed", "unknown"}, action_id
    assert bindings["calendar_event"]["resultLink"]["field"] == "event_url"
    assert bindings["digest_ack"]["undoToken"] == {
        "field": "undo_token",
        "tokenType": "unack",
        "label": "↩︎ 取り消す",
    }


# ── 14. 直接実行が無効な環境 ──────────────────────────────────────────────────────────
def test_without_the_bot_token_the_press_keeps_the_legacy_heartbeat_path(
    e2e: dict[str, Any],
) -> None:
    case = e2e["report"]["legacy"]
    assert case["pressed"]["handlerResult"] == {"handled": False}
    assert case["pressed"]["enqueued"] is True
    assert case["mcpRequests"] == []
    assert case["posts"] == []
