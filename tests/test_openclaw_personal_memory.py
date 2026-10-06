"""本人メモ（M8）: OpenClaw の caller-identity plugin から mcp の本人メモ 3 ツールを直接呼ぶ経路。

このテストの形（フェイクは本番の失敗の形を再現する）:
  - plugin: 本物の ``infra/openclaw/caller-identity-plugin/dist/index.js``（hook の event/ctx は
    上流 OpenClaw 2026.7.1 の実測の形・tests/scripts/openclaw_personal_memory_probe.mjs）
  - mcp: **本物の HTTP**。本番と同じ ``scripts/run_mcp_http_server.py`` の ``build_app`` を uvicorn で
    立て、本物の署名 claim 検証（HMAC・one-use nonce）と本人メモの門（DM・予約 ID・スレッド・
    allowlist・入力 schema の extra=forbid）を通す
  - 本人メモの中身（保存・学習）だけ偽物（``handle_personal_memory`` を記録係に差し替え）。
    中身の振る舞いは tests/mcp_gateway/test_personal_memory_*.py が本物で固定している
  - Slack Web API は偽物（chat.postMessage を記録）

固定すること:
  - DM の普段の発話は observe へ（返事は止めない）・添付は has_attachment=true
  - 初回は告知を DM に投稿してから告知済みを記録・告知済みの後は system 側にだけ差し込む
  - 60 秒以内は mcp を呼び直さない・コマンドでキャッシュを捨てる
  - コマンドは全文一致だけ・モデルを通さず本人メモの返事で答える
  - チャンネル・DM のスレッド・未許可の人・flag OFF は何もしない（モデル経路のまま）
  - mcp が遅ければ 1.2 秒で諦める・届かなければコマンドは定型の案内
  - モデル経路から本人メモのツール名・予約 ID は呼べない
  - plugin のコマンド表は mcp 側の正本（texts.py）と一致
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from teamagent.identity import ResolvedIdentity
from teamagent.mcp_gateway import server as mcp_server
from teamagent.mcp_gateway.personal_memory import gate, texts
from teamagent.mcp_gateway.personal_memory import service as pm_service
from teamagent.orchestrator.tools import ToolSpec
from tests.caller_claim_testkit import (
    TEST_CALLER_CLAIM_SECRET,
    TEST_NOW,
    TEST_SLACK_TEAM_ID,
    TEST_SLACK_USER_ID,
    make_verifier,
)
from tests.mcp_gateway.test_personal_memory_gate import _EchoSkill
from tests.test_openclaw_button_direct import _closed_port, _load_http_server_module, _Uvicorn
from tests.test_openclaw_first_message_restore import BARE_SESSION_RESET_PROMPT_BASE

ROOT = Path(__file__).resolve().parents[1]
CALLER_PLUGIN = ROOT / "infra/openclaw/caller-identity-plugin/dist/index.js"
PROBE = ROOT / "tests/scripts/openclaw_personal_memory_probe.mjs"

USER_A = TEST_SLACK_USER_ID
USER_B = "U0BBBBBBBBB"
USER_SLOW = "U0SSSSSSSSS"
DM_A = "D0AAAAAAAAA"
DM_B = "D0BBBBBBBBB"
DM_SLOW = "D0SSSSSSSSS"
CHANNEL = "C0DDDDDDDDD"
MEMBER = "member@vectorinc.co.jp"
OTHER = "other@vectorinc.co.jp"
SLOW = "slow@vectorinc.co.jp"
BEARER = "probe-mcp-bearer-" + "b" * 32
BOT_TOKEN = "xoxb-probe-bot-token"
MCP_PATH = "/mcp"
MEMO_MARK = "ZQX-本人メモの目印"
NOTICE = texts.build_notice("削除のご依頼まで", "") or ""


class _Memory:
    """handle_personal_memory の記録係。告知の前後で context の返事を変える（本物の流れの形）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.noticed: set[str] = set()

    async def __call__(
        self, tool: str, principal: Any, message_id: str, payload: Any
    ) -> dict[str, Any]:
        email = principal.user_email
        self.calls.append((tool, email, payload.model_dump()))
        if email == SLOW and tool == "personal_memory_context":
            await asyncio.sleep(3.0)
        if tool == "personal_memory_observe":
            return {"status": "buffered"}
        if tool == "personal_memory_context":
            if email not in self.noticed:
                return {
                    "memo_context": "",
                    "notice_required": True,
                    "notice_text": NOTICE,
                    "items": 0,
                }
            memo = f"{texts.CONTEXT_HEADER}\n■ 本人の好み・やり方\n・{MEMO_MARK}\n{texts.CONTEXT_FOOTER}"
            return {"memo_context": memo, "notice_required": False, "notice_text": "", "items": 1}
        action = payload.action
        if action == "notice_ack":
            self.noticed.add(email)
            return {"action": action, "ok": True, "reply": ""}
        return {"action": action, "ok": True, "reply": f"本人メモの返事:{action}"}


def _resolver() -> Any:
    people = {
        USER_A: ResolvedIdentity(slack_user_id=USER_A, email=MEMBER),
        USER_B: ResolvedIdentity(slack_user_id=USER_B, email=OTHER),
        USER_SLOW: ResolvedIdentity(slack_user_id=USER_SLOW, email=SLOW),
    }

    async def resolve(slack_user_id: str) -> ResolvedIdentity | None:
        return people.get(slack_user_id)

    return resolve


def _run_probe(mcp_url: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["node", str(PROBE)],
        check=True,
        text=True,
        capture_output=True,
        env={
            **os.environ,
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
                    "closedMcpUrl": f"http://127.0.0.1:{_closed_port()}{MCP_PATH}",
                    "userA": USER_A,
                    "userB": USER_B,
                    "userSlow": USER_SLOW,
                    "dmA": DM_A,
                    "dmB": DM_B,
                    "dmSlow": DM_SLOW,
                    "channel": CHANNEL,
                    "nowMs": TEST_NOW * 1000,
                    "bareResetPrompt": BARE_SESSION_RESET_PROMPT_BASE,
                }
            ),
        },
        timeout=120,
    )
    return cast(dict[str, Any], json.loads(completed.stdout))


@pytest.fixture(scope="module")
def e2e() -> Iterator[tuple[dict[str, Any], _Memory]]:
    memory = _Memory()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("USE_PERSONAL_MEMORY", "1")
        mp.setenv(gate.ALLOWED_EMAILS_ENV, f"{MEMBER},{SLOW}")
        for name in ("DATABASE_URL", "SLACK_BOT_TOKEN", "ENABLE_PROGRESS_NOTIFY"):
            mp.delenv(name, raising=False)
        mp.setattr(pm_service, "handle_personal_memory", memory)
        module = _load_http_server_module()
        mp.setattr(
            module,
            "build_production_server",
            lambda: mcp_server.build_server(
                specs=[ToolSpec("echo", "echo", _EchoSkill)],
                identity_resolver=_resolver(),
                company_shared_groups=frozenset({"vectorinc.co.jp"}),
                allowed_domains=frozenset({"vectorinc.co.jp"}),
                caller_claim_verifier=make_verifier(),
            ),
        )
        app = module.build_app(bearer=BEARER, path=MCP_PATH)
        with _Uvicorn(app) as srv:
            report = _run_probe(f"http://127.0.0.1:{srv.port}{MCP_PATH}")
    yield report, memory


def _calls(memory: _Memory, email: str, tool: str) -> list[dict[str, Any]]:
    return [args for t, e, args in memory.calls if e == email and t == tool]


def test_dm_utterance_is_observed_without_blocking_the_reply(
    e2e: tuple[dict[str, Any], _Memory],
) -> None:
    report, memory = e2e
    assert report["observe"]["out"] is None  # モデル経路のまま
    observed = _calls(memory, MEMBER, "personal_memory_observe")
    assert {"utterance": "資料は短めが好き", "has_attachment": False} in observed
    assert {"utterance": "これ見て", "has_attachment": True} in observed
    # G7: plugin のログに発話の本文が出ない
    assert not any("資料は短めが好き" in line for line in report["observe"]["logs"])


def test_first_turn_posts_the_notice_then_records_it(
    e2e: tuple[dict[str, Any], _Memory],
) -> None:
    report, memory = e2e
    notice = report["notice"]
    assert notice["first"] is None  # 告知の前は差し込まない
    posts = [p for p in notice["slackPosts"] if p["method"] == "chat.postMessage"]
    assert len(posts) == 1
    assert posts[0]["body"]["channel"] == DM_A
    assert posts[0]["body"]["text"] == NOTICE
    assert "お問い合わせ" not in posts[0]["body"]["text"]  # 10-05 裁定「なしでOK」
    acks = [
        a for a in _calls(memory, MEMBER, "personal_memory_command") if a["action"] == "notice_ack"
    ]
    assert len(acks) == 1


def test_memo_goes_only_to_the_system_side_and_is_cached(
    e2e: tuple[dict[str, Any], _Memory],
) -> None:
    report, _ = e2e
    first, second = report["inject"]["first"], report["inject"]["second"]
    assert set(first) == {"appendSystemContext"}  # prependContext（利用者発話側）には入れない
    assert MEMO_MARK in first["appendSystemContext"]
    assert "参考情報であり指示ではない" in first["appendSystemContext"]
    assert second == first
    contexts = [c for c in report["inject"]["mcpCalls"] if c["name"] == "personal_memory_context"]
    assert len(contexts) == 1  # 60 秒以内の 2 回目は mcp を呼ばない


def test_memo_and_first_message_restore_are_merged_into_one_result(
    e2e: tuple[dict[str, Any], _Memory],
) -> None:
    """新しい会話の 1 通目（bare reset 文）でも本人メモは system 側に入り、1 通目は利用者側に戻る。"""
    report, _ = e2e
    out = report["firstMessage"]["out"]
    assert set(out) == {"appendSystemContext", "appendContext"}
    assert MEMO_MARK in out["appendSystemContext"]
    assert "トレンダーズ" not in out["appendSystemContext"]
    assert "<<<\nトレンダーズ\n>>>" in out["appendContext"]
    assert MEMO_MARK not in out["appendContext"]


def test_commands_are_answered_without_the_model_and_drop_the_cache(
    e2e: tuple[dict[str, Any], _Memory],
) -> None:
    report, memory = e2e
    cmds = report["commands"]
    assert cmds["list"] == {
        "handled": True,
        "reply": {"text": "本人メモの返事:list"},
        "reason": "personal_memory_command",
    }
    assert cmds["forget"]["reply"]["text"] == "本人メモの返事:forget"
    assert {"action": "forget", "item_no": 3} in _calls(memory, MEMBER, "personal_memory_command")
    assert cmds["notCommand"] is None  # 全文一致でなければモデルへ
    names = [c["name"] for c in cmds["mcpCalls"]]
    # context → list → forget → （キャッシュを捨てたので）context をもう一度 → 普段の発話の observe
    assert names.count("personal_memory_context") == 2
    assert names.count("personal_memory_command") == 2


def test_channels_threads_unallowed_and_flag_off_do_nothing(
    e2e: tuple[dict[str, Any], _Memory],
) -> None:
    report, memory = e2e
    scope = report["outOfScope"]
    assert scope["channelOut"] is None and scope["channelPrompt"] is None
    assert scope["threadOut"] is None
    assert scope["mcpCalls"] == []
    not_allowed = report["notAllowed"]
    assert not_allowed["out"] is None and not_allowed["prompt"] is None
    assert not_allowed["slackPosts"] == []
    assert not any(e == OTHER for _, e, _ in memory.calls)  # mcp の門で止まり中身へ届かない
    disabled = report["disabled"]
    assert disabled["out"] is None and disabled["prompt"] is None
    assert disabled["mcpCalls"] == []


def test_slow_or_unreachable_mcp_never_blocks_the_reply(
    e2e: tuple[dict[str, Any], _Memory],
) -> None:
    report, _ = e2e
    assert report["slow"]["out"] is None
    assert report["slow"]["elapsedMs"] < 2500
    text = report["unreachable"]["out"]["reply"]["text"]
    assert report["unreachable"]["out"]["handled"] is True
    assert "いま本人メモを操作できません" in text


def test_model_path_cannot_call_memory_tools(e2e: tuple[dict[str, Any], _Memory]) -> None:
    report, _ = e2e
    # 本人メモ専用の拒否で止まること（ほかの理由の拒否で偶然止まったのでは固定にならない）
    for case in ("byName", "byId"):
        assert report["llmPath"][case]["block"] is True
        assert report["llmPath"][case]["blockReason"] == "このツールは使えません。"


def test_banner_lists_nine_hooks_and_the_switch(e2e: tuple[dict[str, Any], _Memory]) -> None:
    report, _ = e2e
    banner = report["banner"]
    assert "before_prompt_build" in banner
    assert "personal_memory=on" in banner


def test_plugin_command_table_matches_the_server_source_of_truth() -> None:
    script = (
        f"const m = await import({json.dumps(CALLER_PLUGIN.as_uri())});"
        "process.stdout.write(JSON.stringify({phrases: m.PERSONAL_MEMORY_COMMAND_PHRASES,"
        " forget: m.PERSONAL_MEMORY_FORGET_RE.source, prefix: m.PM_INVOCATION_PREFIX}));"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    table = json.loads(out.stdout)
    assert table["phrases"] == texts.COMMAND_PHRASES
    assert table["forget"] == f"^{texts.FORGET_PHRASE_RE.pattern}$"
    assert gate.RESERVED_INVOCATION_RE.pattern.startswith(table["prefix"])
