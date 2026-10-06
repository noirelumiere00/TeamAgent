"""新しい会話の 1 通目を戻す（FM・2026-10-06 全 DM 判定のテーマ P7）。

事故: 新しい会話の 1 通目（/new・「新しい会話」・時間切れの区切り・初めての人）で、利用者が
「トレンダーズ」「連携」や依頼文を送ったのに、Aico が時刻つきの挨拶だけを返した。上流 openclaw
2026.7.1 は isBareSessionReset が真だと本文を捨て、BARE_SESSION_RESET_PROMPT に置き換える
（get-reply-CknL88Yv.js:3301/3328）。plugin は受信で本文を控え、before_prompt_build で
bare reset 文を見たら appendContext で戻す。

このテストの形（フェイクは本番の失敗の形を再現する）:
  - plugin: 本物の ``infra/openclaw/caller-identity-plugin/dist/index.js``
  - prompt: 上流 dist の BARE_SESSION_RESET_PROMPT_BASE をバイト単位で写したものを、上流の組み立て
    （inboundUserContext ＋ 空行 ＋ bare 文 ＋ 時刻行）と同じ形で渡す
  - mcp・Slack は使わない（fetch は呼ばれたら失敗する）

本人メモ（appendSystemContext）との合成は本物の mcp が要るので tests/test_openclaw_personal_memory.py。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from tests.caller_claim_testkit import (
    TEST_CALLER_CLAIM_SECRET,
    TEST_NOW,
    TEST_SLACK_TEAM_ID,
    TEST_SLACK_USER_ID,
)
from tests.scripts.test_openclaw_runtime_contract import CONFIG, _load_reviewed_json5

ROOT = Path(__file__).resolve().parents[1]
CALLER_PLUGIN = ROOT / "infra/openclaw/caller-identity-plugin/dist/index.js"
PROBE = ROOT / "tests/scripts/openclaw_first_message_restore_probe.mjs"
LOCK = ROOT / "infra/openclaw/plugins-lock.json"

USER_A = TEST_SLACK_USER_ID
USER_B = "U0BBBBBBBBB"
DM_A = "D0AAAAAAAAA"
DM_B = "D0BBBBBBBBB"
CHANNEL = "C0DDDDDDDDD"
BOT_USER = "U0AICOAICO1"

# 上流 openclaw@2026.7.1 get-reply-CknL88Yv.js:2704 の BARE_SESSION_RESET_PROMPT_BASE（バイト単位の写し）。
BARE_SESSION_RESET_PROMPT_BASE = (
    "A new session was started via /new or /reset. Execute your Session Startup sequence now"
    " - read the required files before responding to the user. If BOOTSTRAP.md exists in the"
    " provided Project Context, read it and follow its instructions first. Then greet the user"
    " in your configured persona, if one is provided. Be yourself - use your defined voice,"
    " mannerisms, and mood. Keep it to 1-3 sentences and ask what they want to do. If the"
    " runtime model differs from default_model in the system prompt, mention the default model."
    " Do not mention internal steps, files, tools, or reasoning."
)
INBOUND_USER_CONTEXT = (
    'Conversation info (untrusted metadata):\n```json\n{"chat_type": "direct", "sender": "U…"}\n```'
)


def _run_probe() -> dict[str, Any]:
    completed = subprocess.run(
        ["node", str(PROBE)],
        check=True,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PROBE_INPUT": json.dumps(
                {
                    "pluginUrl": CALLER_PLUGIN.as_uri(),
                    "secret": TEST_CALLER_CLAIM_SECRET,
                    "teamId": TEST_SLACK_TEAM_ID,
                    "userA": USER_A,
                    "userB": USER_B,
                    "dmA": DM_A,
                    "dmB": DM_B,
                    "channel": CHANNEL,
                    "botUser": BOT_USER,
                    "nowMs": TEST_NOW * 1000,
                    "bareResetPrompt": BARE_SESSION_RESET_PROMPT_BASE,
                    "inboundUserContext": INBOUND_USER_CONTEXT,
                }
            ),
        },
        timeout=60,
    )
    return cast(dict[str, Any], json.loads(completed.stdout))


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    return _run_probe()


def _quoted(result: dict[str, Any] | None) -> str:
    """戻した appendContext の引用部分（<<< と >>> の間）。"""
    assert result is not None
    assert set(result) == {"appendContext"}  # 利用者発話の後ろだけ。system 側・prepend には入れない
    text = result["appendContext"]
    start = text.rindex("<<<\n") + len("<<<\n")
    end = text.rindex("\n>>>")
    return cast(str, text[start:end])


def test_bare_reset_prompt_gets_the_users_first_message_back(report: dict[str, Any]) -> None:
    restored = report["restored"]
    assert _quoted(restored["bound"]) == "トレンダーズ"
    assert _quoted(restored["unbound"]) == "トレンダーズの資料ある？"
    context = restored["bound"]["appendContext"]
    assert "挨拶・自己紹介・時刻の話はせず" in context
    assert "資料" in context and "決まりは変わらない" in context
    # G7: 本文はログに出さない（結果と長さだけ）。mcp・Slack には触れない
    assert not any("トレンダーズ" in line for line in restored["logs"])
    assert any(
        line.endswith("first message restore outcome=restored len=6") for line in restored["logs"]
    )
    assert restored["fetches"] == []


def test_reset_phrase_alone_is_not_restored(report: dict[str, Any]) -> None:
    assert report["phraseOnly"] == {
        "/new": None,
        "/reset": None,
        "新しい会話": None,
        "新しい会話。": None,
        f"<@{BOT_USER}> /new": None,
    }


def test_reset_phrase_with_a_tail_restores_only_the_tail(report: dict[str, Any]) -> None:
    for text, result in report["phraseWithTail"].items():
        assert _quoted(result) == "トレンダーズ", text


def test_normal_prompt_and_soft_reset_tail_are_left_alone(report: dict[str, Any]) -> None:
    assert report["notBare"] == {"normal": None, "softTail": None}


def test_another_senders_message_is_never_used(report: dict[str, Any]) -> None:
    other = report["otherSender"]
    assert other["dmOther"] is None
    assert (
        other["channelOther"] is None
    )  # 同じチャンネル（同じセッション鍵）でも送信者が違えば使わない
    assert _quoted(other["channelA"]) == "Aさんの依頼"
    assert _quoted(other["channelB"]) == "Bさんの依頼"
    assert other["spoofedRun"] is None  # 他人の run id を名乗っても送信者が合わなければ使わない


def test_switch_off_only_with_zero(report: dict[str, Any]) -> None:
    flag = report["flag"]
    assert flag["off"] is None
    assert "first_message_restore=off" in flag["offBanner"]
    assert _quoted(flag["on1"]) == "トレンダーズ"
    assert "first_message_restore=on" in flag["defaultBanner"]  # 既定 ON


def test_expiry_boundary_escape_and_length_cap(report: dict[str, Any]) -> None:
    edges = report["edges"]
    assert edges["expired"] is None  # 120 秒を過ぎた控えは使わない
    quoted = _quoted(edges["escape"])
    assert ">>>" not in quoted and "<<<" not in quoted  # 枠から抜け出せない
    assert "›››" in quoted and "‹‹‹" in quoted
    frame = len(_frame_only())
    assert edges["longLen"] == frame + 2000  # 2000 字まで


def _frame_only() -> str:
    script = (
        f"const m = await import({json.dumps(CALLER_PLUGIN.as_uri())});"
        'process.stdout.write(m.buildFirstMessageRestoreContext(""));'
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return out.stdout


def _plugin_value(expr: str) -> Any:
    script = (
        f"const m = await import({json.dumps(CALLER_PLUGIN.as_uri())});"
        f"process.stdout.write(JSON.stringify({expr}));"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return json.loads(out.stdout)


def test_reset_triggers_match_the_shipped_config() -> None:
    """plugin の合言葉の表が openclaw.config.json5 の session.resetTriggers と一致する。"""
    config = _load_reviewed_json5(CONFIG)
    assert _plugin_value("m.FIRST_MESSAGE_RESET_TRIGGERS") == config["session"]["resetTriggers"]


def test_bare_reset_marker_is_a_prefix_of_the_copied_upstream_prompt() -> None:
    marker = _plugin_value("m.BARE_SESSION_RESET_MARKER")
    assert BARE_SESSION_RESET_PROMPT_BASE.startswith(marker)


def test_markers_exist_in_shipped_dist() -> None:
    """照合文と写した bare 文が上流 dist に実在する。任意実行: OPENCLAW_DIST_DIR を付けたときだけ。"""
    raw = os.environ.get("OPENCLAW_DIST_DIR")
    if not raw:
        pytest.skip("OPENCLAW_DIST_DIR（openclaw@<plugins-lock の版> の package/dist）が無い")
    dist = Path(raw)
    package = json.loads((dist.parent / "package.json").read_text())
    assert package["version"] == json.loads(LOCK.read_text())["openclaw"]["version"]
    shipped = (dist / "get-reply-CknL88Yv.js").read_text(encoding="utf-8")
    assert BARE_SESSION_RESET_PROMPT_BASE in shipped
    # bare 文の 3 種（BASE・BOOTSTRAP_PENDING・BOOTSTRAP_LIMITED）すべてに照合文が入る
    marker = _plugin_value("m.BARE_SESSION_RESET_MARKER")
    assert shipped.count(f'"{marker}') >= 3
    assert _plugin_value("m.SOFT_RESET_TAIL_MARKER") in shipped
