"""回答評価ボタン（AF）: caller-identity plugin → mcp の answer_feedback_record → search_feedback。

このテストの形（フェイクは本番の失敗の形を再現する）:
  - plugin: 本物の ``infra/openclaw/caller-identity-plugin/dist/index.js``（hook の event/ctx は
    上流 OpenClaw 2026.7.1 の実測の形・tests/scripts/openclaw_answer_feedback_probe.mjs）
  - mcp: **本物の HTTP**（``scripts/run_mcp_http_server.py`` の ``build_app`` を uvicorn で立てる）。
    本物の署名 claim 検証（HMAC・one-use nonce）と評価トークンの検証を通す
  - 保存先だけ偽物（記録係）。fail 用の利用者では本番の失敗（DB 例外）を投げる
  - Slack Web API は偽物（chat.postMessage / chat.update を記録）

固定すること:
  - メッセージ起点の返信にはツール有無によらず評価が 1 通付く（silent・action・flag OFF は除外）
  - 押下で mcp が呼ばれ、search_feedback に記録され、メッセージが「ありがとうございます」に置き換わる
  - 二度押しは追記（最後の値で上書き）・別の人・改ざん・保存失敗は記録も置き換えもしない
  - チャンネルでは返信と同じスレッドへ出す・モデル経路から評価のツールは呼べない
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from teamagent.adapters.answer_feedback_store import AnswerFeedbackRow
from teamagent.identity import ResolvedIdentity
from teamagent.mcp_gateway import answer_feedback as afb
from teamagent.mcp_gateway import server as mcp_server
from teamagent.orchestrator.tools import ToolSpec
from tests.caller_claim_testkit import (
    TEST_CALLER_CLAIM_SECRET,
    TEST_NOW,
    TEST_SLACK_TEAM_ID,
    TEST_SLACK_USER_ID,
    make_verifier,
)
from tests.mcp_gateway.test_personal_memory_gate import _EchoSkill
from tests.test_openclaw_button_direct import _load_http_server_module, _Uvicorn

ROOT = Path(__file__).resolve().parents[1]
CALLER_PLUGIN = ROOT / "infra/openclaw/caller-identity-plugin/dist/index.js"
PROBE = ROOT / "tests/scripts/openclaw_answer_feedback_probe.mjs"

USER_A = TEST_SLACK_USER_ID
USER_B = "U0BBBBBBBBB"
USER_FAIL = "U0FFFFFFFFF"
CHANNEL = "C0DDDDDDDDD"
MEMBER = "member@vectorinc.co.jp"
FAIL_EMAIL = "fail@vectorinc.co.jp"
BEARER = "probe-mcp-bearer-" + "b" * 32
BOT_TOKEN = "xoxb-probe-bot-token"
MCP_PATH = "/mcp"


class _Store:
    def __init__(self) -> None:
        self.rows: list[AnswerFeedbackRow] = []

    def insert(self, row: AnswerFeedbackRow) -> None:
        if row.user_email == FAIL_EMAIL:
            raise RuntimeError("could not connect to server")
        self.rows.append(row)


def _resolver() -> Any:
    people = {
        USER_A: ResolvedIdentity(slack_user_id=USER_A, email=MEMBER),
        USER_B: ResolvedIdentity(slack_user_id=USER_B, email="other@vectorinc.co.jp"),
        USER_FAIL: ResolvedIdentity(slack_user_id=USER_FAIL, email=FAIL_EMAIL),
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
                    "userA": USER_A,
                    "userB": USER_B,
                    "userFail": USER_FAIL,
                    "channel": CHANNEL,
                    "nowMs": TEST_NOW * 1000,
                }
            ),
        },
        timeout=120,
    )
    return cast(dict[str, Any], json.loads(completed.stdout))


@pytest.fixture(scope="module")
def e2e() -> Iterator[tuple[dict[str, Any], _Store]]:
    store = _Store()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(afb.ANSWER_FEEDBACK_FLAG_ENV, "1")
        mp.setenv("USAGE_EVENTS_DISABLE", "1")
        for name in ("DATABASE_URL", "SLACK_BOT_TOKEN", "ENABLE_PROGRESS_NOTIFY"):
            mp.delenv(name, raising=False)
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
                answer_feedback_store=store,
            ),
        )
        app = module.build_app(bearer=BEARER, path=MCP_PATH)
        with _Uvicorn(app) as srv:
            report = _run_probe(f"http://127.0.0.1:{srv.port}{MCP_PATH}")
    yield report, store


def _token_payload(token: str) -> dict[str, Any]:
    segment = token.split(".")[0]
    return dict(json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))))


def test_search_reply_gets_one_feedback_message_after_the_reply(
    e2e: tuple[dict[str, Any], _Store],
) -> None:
    report, _ = e2e
    dm = report["dmSearch"]
    assert dm["turn"]["delivered"] is None  # 返信の中身は変えない
    assert len(dm["posts"]) == 1  # 分割 payload でも 1 通
    body = dm["posts"][0]["body"]
    assert body["channel"] == "D" + USER_A[1:]  # 質問した本人の DM（conversations.open）
    assert "thread_ts" not in body
    assert body["text"] == "この回答は役に立ちましたか？"
    actions = body["blocks"][1]["elements"]
    assert [a["action_id"] for a in actions] == ["answer_feedback_up", "answer_feedback_down"]
    assert actions[0]["value"] == actions[1]["value"]
    # 返信の配信より後に届くよう待ってから投稿する
    assert dm["sleeps"] and dm["sleeps"][0] >= 2000
    payload = _token_payload(actions[0]["value"])
    assert payload["q"] == "JAL 過去提案 事例"  # 改行は空白へ
    assert payload["k"] == ["search"]
    assert payload["u"] == USER_A
    assert payload["e"] == TEST_NOW + afb.TOKEN_TTL_S
    # G7: 検索語はログに出さない
    assert not any("JAL" in line for line in dm["logs"])


def test_owner_press_records_and_replaces_message(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, store = e2e
    pressed = report["pressOwner"]
    assert [call["name"] for call in pressed["mcpCalls"]] == [afb.ANSWER_FEEDBACK_TOOL_NAME] * 2
    assert [call["arguments"]["rating"] for call in pressed["mcpCalls"]] == [1, -1]
    member_rows = [
        row for row in store.rows if row.user_email == MEMBER and row.query == "JAL 過去提案 事例"
    ]
    assert [row.rating for row in member_rows] == [1, -1]  # 追記＝最後の値が有効
    assert member_rows[0].query == "JAL 過去提案 事例"
    assert member_rows[0].answer_id == member_rows[1].answer_id
    assert member_rows[0].search_session_id == f"slack-{member_rows[0].answer_id}"
    assert member_rows[0].note == '{"tools":["search"]}'
    updates = pressed["updates"]
    assert [u["body"]["text"] for u in updates] == [
        "ありがとうございます（👍 を記録しました）",
        "ありがとうございます（👎 を記録しました）",
    ]
    assert all(u["body"]["ts"] == "1784424100.000100" for u in updates)
    assert all("actions" not in json.dumps(u["body"]["blocks"]) for u in updates)
    assert pressed["ephemeral"] == []


def test_other_person_cannot_vote(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, store = e2e
    other = report["pressOther"]
    assert other["mcpCallsAdded"] == 0
    assert other["ephemeral"] == [
        {"text": "この評価ボタンは質問した方だけが押せます。", "responseType": "ephemeral"}
    ]
    assert all(row.user_email != "other@vectorinc.co.jp" for row in store.rows)


def test_forged_token_is_not_sent_to_mcp(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, store = e2e
    assert report["pressForged"]["mcpCallsAdded"] == 0
    assert report["pressForged"]["ephemeral"][0]["text"].startswith("評価を記録できませんでした")
    assert all(row.query != "改ざんした質問" for row in store.rows)


def test_store_failure_keeps_buttons_and_tells_presser(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, store = e2e
    failure = report["storeFailure"]
    assert failure["updatesAdded"] == 0
    assert failure["ephemeral"][0]["text"].startswith("評価を記録できませんでした")
    assert any("code=AFB_STORE_FAILED" in line for line in failure["logs"])
    assert all(row.user_email != FAIL_EMAIL for row in store.rows)


def test_reply_with_non_search_tool_gets_one_button_and_original_message(
    e2e: tuple[dict[str, Any], _Store],
) -> None:
    report, _ = e2e
    no_search = report["noSearch"]
    assert len(no_search["posts"]) == len(no_search["turns"]) == 3
    assert no_search["turns"][0]["toolResults"][0].get("block") is not True
    payload = _token_payload(no_search["posts"][0]["body"]["blocks"][1]["elements"][0]["value"])
    assert payload["q"] == "元の発言 資料を送って"
    assert payload["k"] == ["knowledge_deliver"]
    assert not any("元の発言" in line for line in no_search["logs"])


def test_tool_free_reply_gets_one_button_and_normalizes_original_message(
    e2e: tuple[dict[str, Any], _Store],
) -> None:
    report, _ = e2e
    posts = report["noSearch"]["posts"]
    assert len(posts) == 3
    for post, expected in zip(posts[1:], ["こんにちは", "😀" * 300], strict=True):
        token = post["body"]["blocks"][1]["elements"][0]["value"]
        payload = _token_payload(token)
        assert payload["q"] == expected
        assert "k" not in payload
        assert len(token) <= afb.MAX_TOKEN_CHARS


def test_non_search_and_tool_free_votes_are_recorded(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, store = e2e
    assert len(report["noSearch"]["mcpCalls"]) == 2
    rows = {row.query: row for row in store.rows}
    assert rows["元の発言 資料を送って"].note == '{"tools":["knowledge_deliver"]}'
    assert rows["こんにちは"].note is None


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "silent",
        "commentary",
        "reasoning",
        "payloadCommentary",
        "feedbackPrompt",
        "feedbackThanks",
        "feedbackFailed",
        "feedbackNotOwner",
        "feedbackStale",
        "feedbackBlocks",
    ],
)
def test_non_answer_payload_gets_no_feedback(e2e: tuple[dict[str, Any], _Store], case: str) -> None:
    report, _ = e2e
    assert report["excluded"][case] == []


def test_visible_final_error_reply_also_gets_one_feedback(
    e2e: tuple[dict[str, Any], _Store],
) -> None:
    report, _ = e2e
    assert len(report["errorReply"]["posts"]) == 1


def test_action_origin_reply_and_vote_get_no_feedback(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, _ = e2e
    assert report["action"]["pressed"]["result"] == {"handled": True}
    assert report["action"]["postsAdded"] == 0
    assert any(
        "heartbeat has no exact authoritative Slack button action" in line
        for line in report["action"]["logs"]
    )


def test_first_search_query_and_up_to_five_unique_short_tools(
    e2e: tuple[dict[str, Any], _Store],
) -> None:
    report, _ = e2e
    post = report["toolLimit"]["posts"][0]
    payload = _token_payload(post["body"]["blocks"][1]["elements"][0]["value"])
    assert payload["q"] == "最初の検索"
    assert payload["k"] == [
        "knowledge_deliver",
        "search",
        "oauth_connect",
        "calendar_event",
        "lookup",
    ]


def test_optional_tools_fit_existing_token_size_limit(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, _ = e2e
    token = report["tokenLimit"]["posts"][0]["body"]["blocks"][1]["elements"][0]["value"]
    assert len(token) <= afb.MAX_TOKEN_CHARS
    claim = afb.verify_feedback_token(
        token,
        key=make_verifier().derive_purpose_key(afb.KEY_LABEL),
        now=TEST_NOW,
        presser_user_id=USER_A,
        team_id=TEST_SLACK_TEAM_ID,
    )
    assert claim.query == "😀" * 300
    assert 0 < len(claim.tools) < 5


def test_old_v1_without_tools_passes_plugin_and_mcp(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, store = e2e
    legacy = report["oldV1"]
    assert "k" not in _token_payload(legacy["token"])
    assert len(legacy["mcpCalls"]) == 1
    rows = [row for row in store.rows if row.query == "旧 v1 の検索"]
    assert len(rows) == 1
    assert rows[0].note is None


def test_channel_feedback_goes_to_the_reply_thread(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, _ = e2e
    channel = report["channel"]
    assert channel["top"]["toolBlocked"] is False
    posts = channel["posts"]
    assert [p["body"]["channel"] for p in posts] == [CHANNEL, CHANNEL]
    assert posts[0]["body"]["thread_ts"] == channel["top"]["messageId"]
    assert posts[1]["body"]["thread_ts"] == "1784423000.000100"


def test_flag_off_does_nothing(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, _ = e2e
    disabled = report["disabled"]
    assert disabled["posts"] == []
    assert disabled["interactive"] == [
        "mail_draft",
        "calendar_event",
        "schedule_propose",
        "digest_ack",
    ]
    assert "answer_feedback=off" in disabled["banner"]


def test_model_path_cannot_call_feedback_tool(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, _ = e2e
    assert report["llmPath"]["byName"]["block"] is True
    assert report["llmPath"]["byId"]["block"] is True


def test_post_failure_does_not_touch_reply(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, _ = e2e
    failure = report["postFailure"]
    assert failure["delivered"] is None
    assert any("outcome=post_failed" in line for line in failure["logs"])


def test_banner_and_handlers_when_enabled(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, _ = e2e
    assert "answer_feedback=on" in report["banner"]
    assert {"answer_feedback_up", "answer_feedback_down"} <= set(report["interactive"])


def test_allowlist_limits_buttons_to_listed_askers(e2e: tuple[dict[str, Any], _Store]) -> None:
    report, _ = e2e
    allowlist = report["allowlist"]
    assert allowlist["outside"] == 0  # 一覧に無い人（A）の返信には付かない
    assert len(allowlist["posts"]) == 1  # 一覧の人（B・小文字と空白で書いても）には付く
    assert allowlist["posts"][0]["body"]["channel"] == "D" + USER_B[1:]
    assert {"answer_feedback_up", "answer_feedback_down"} <= set(allowlist["interactive"])
    assert "answer_feedback=list:2" in allowlist["banner"]


@pytest.mark.parametrize("flag", [f"{USER_A},bad-id", "0", f"{USER_A},,{USER_B}", "yes"])
def test_invalid_flag_is_off(e2e: tuple[dict[str, Any], _Store], flag: str) -> None:
    report, _ = e2e
    result = report["invalidFlags"][flag]
    assert result["posts"] == 0
    assert result["interactive"] == []
    assert "answer_feedback=off" in result["banner"]


def test_plugin_and_mcp_share_the_token_contract() -> None:
    """plugin の定数（鍵ラベル・TTL・予約 ID・ツール名）が mcp 側の正本と一致する。"""
    source = CALLER_PLUGIN.read_text(encoding="utf-8")
    assert f'ANSWER_FEEDBACK_KEY_LABEL = "{afb.KEY_LABEL.decode()}"' in source
    assert "ANSWER_FEEDBACK_TOKEN_TTL_S = 7 * 24 * 60 * 60" in source
    assert afb.TOKEN_TTL_S == 7 * 24 * 60 * 60
    assert f'ANSWER_FEEDBACK_TOOL = "{afb.ANSWER_FEEDBACK_TOOL_NAME}"' in source
    assert 'AF_INVOCATION_PREFIX = "aico-fb-"' in source
    assert f"ANSWER_FEEDBACK_QUERY_MAX = {afb.MAX_QUERY_CHARS}" in source
    assert f"ANSWER_FEEDBACK_TOOLS_MAX = {afb.MAX_TOOL_NAMES}" in source
    assert f"ANSWER_FEEDBACK_TOOL_NAME_MAX = {afb.MAX_TOOL_NAME_CHARS}" in source
    assert f'ANSWER_FEEDBACK_SEARCH_TOOL = "{mcp_server.SEARCH_TOOL_NAME}"' in source
