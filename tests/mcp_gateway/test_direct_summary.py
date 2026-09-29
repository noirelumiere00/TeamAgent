"""検索上位チェックの結果を mcp が DM へ直接投稿するテスト（server.dispatch_tool 経由）。

本物の SearchSurfaceCheckSkill・dispatch_tool・post_to_origin を通す。Slack だけ偽物。
09-28 本番: 「そのまま返す」規約と MCP 返却の絞り込みの後も、Aico が文面を組み直して
詳細レポートの URL を落とした（「上記リンクで確認できます」とだけ残った）。確かめること:
- 対象（ON・allowlist 内・署名検証済み・1 対 1 DM）なら、結果が Block Kit（見出し・結論・数字の欄・
  1 本 1 行・レポートの文字リンク）で DM に届き、Aico への返却には集計も URL も載らない
  （組み直しの材料を渡さない）。最上位の text には結論と blocks の全文（レポートのリンク含む）
- 依頼の中身（クライアント名）が server から直接投稿まで届き、クライアントの節と照合の範囲が出る
- 事前の取得ジョブ（acquire_job_id）の結果は「依頼のたびに検索し直した値」と書かない
- 対象外（OFF・allowlist 外・空の allowlist・チャンネル・LEGACY）は今と同じ返却で、投稿しない
- 投稿に失敗したら今と同じ返却に戻す（結果を消さない）。タイムアウトは届いた可能性があるので戻さない
- 2 段目（動画の中身）の予告行も直接投稿に入り、追記はその後に届く
- 費用の記録は投稿の成否に関係なく残る
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from teamagent.adapters.slack_client import SlackPostResult
from teamagent.mcp_gateway import detached_jobs, direct_summary, server, surface_video_followup
from teamagent.mcp_gateway.server import USER_CONTEXT_KEY, dispatch_tool
from teamagent.skills._shared.slack_blocks import validate
from teamagent.skills.search_surface_check.summary import followup_notice_line
from teamagent.skills.video_algorithm import thumbnails
from tests.mcp_gateway.test_surface_video_followup import (
    ARGS,
    CHANNEL,
    DM,
    KEYWORD,
    ME,
    OTHER,
    RELAY_KEYS,
    TOOL,
    USER_ID,
    _baseline_summary,
    _blocks_text,
    _call,
    _claim,
    _eventually,
    _FakeSlack,
    _skill,
    _spec,
)


def _direct(monkeypatch: pytest.MonkeyPatch, *, emails: frozenset[str] = frozenset({ME})) -> None:
    monkeypatch.setattr(
        direct_summary,
        "load_policy",
        lambda: direct_summary.DirectPolicy(enabled=True, allowed_emails=emails),
    )


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> _FakeSlack:
    fake = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: fake)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0.0)
    return fake


@pytest.fixture(autouse=True)
def usage(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    for name in (
        direct_summary.ENABLED_ENV,
        direct_summary.ALLOWED_EMAILS_ENV,
        surface_video_followup.ENABLED_ENV,
        surface_video_followup.ALLOWED_EMAILS_ENV,
        surface_video_followup.MAX_VIDEOS_ENV,
        detached_jobs.MAX_BACKGROUND_ENV,
        "ENABLE_PROGRESS_NOTIFY",
        "USE_PAYLOAD_OFFLOAD",
        "VIDEO_QUOTA_ENABLED",
        "SEARCH_SURFACE_ALLOWED_EMAILS",
        "USE_TIKTOK_APIFY_FALLBACK",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")
    monkeypatch.setattr(thumbnails, "fetch_cover", lambda *a, **k: None)
    monkeypatch.setattr(detached_jobs, "REGISTRY", detached_jobs.DetachedJobRegistry())
    monkeypatch.setattr(surface_video_followup, "CACHE", surface_video_followup.FollowupCache())
    monkeypatch.setattr(surface_video_followup, "REUSE_POST_DELAY_S", 0.0)
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(server, "_record_usage", lambda **kw: records.append(kw))
    return records


# ── 対象なら DM へそのまま届き、Aico には中身を渡さない ─────────────────────


async def test_eligible_posts_blocks_and_returns_no_material(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack, usage: list[dict[str, Any]]
) -> None:
    _direct(monkeypatch)
    skill, _g, _d = _skill()
    out = await _call(_spec(skill))

    assert len(slack.posts) == 1
    post = slack.posts[0]
    assert post["channel"] == DM and post["thread_ts"] is None
    baseline = _baseline_summary()
    blocks = post["blocks"]
    validate(blocks)  # Block Kit の上限の内側
    body = _blocks_text(blocks)
    assert blocks[0]["text"]["text"] == f"検索上位チェック「{KEYWORD}」"
    # 標準 Markdown（**太字**・[@id](<url>)・行頭の「- 」）は Block Kit に出ない
    assert "**" not in body and "](" not in body
    assert not any(line.startswith("- ") for line in body.splitlines())
    report_url = "https://s3.example/surface-1"
    assert report_url in baseline
    assert f"<{report_url}|レポートを開く>" in body
    # 通知文（プレビュー）: 結論の 1 行とレポートの URL
    headline = next(
        line.removeprefix("**結論** ")
        for line in baseline.splitlines()
        if line.startswith("**結論**")
    )
    assert headline in post["text"] and f"<{report_url}|レポートを開く>" in post["text"]
    assert headline in body
    # ARGS は acquire_job_id あり＝事前の取得ジョブを読んだだけ。検索し直したとは書かない
    assert "この依頼では検索し直していません" in body
    assert "依頼のたびに検索し直した値" not in body + post["text"]

    # Aico への返却: 投稿済みの一言だけ。集計・URL・本文を載せない
    assert out["status"] == "posted" and out["delivered"] is True
    assert out["slack_summary"] == direct_summary.POSTED_TEXT
    assert set(out) == {"status", "delivered", "slack_summary", "note"}
    assert "http" not in json.dumps(out, ensure_ascii=False)
    assert "report_url" not in out

    # 費用は記録される（投稿の成否と関係なく）
    assert len(usage) == 1 and usage[0]["skill"] == TOOL
    assert usage[0]["cost_usd"] > 0


async def test_client_name_reaches_the_direct_post(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    """server → deliver へ依頼の中身（skill_input）を渡す配線。渡らないとクライアントの節が消える。"""
    _direct(monkeypatch)
    skill, _g, _d = _skill()
    await _call(_spec(skill), args={**ARGS, "client_name": "GABAN"})
    assert len(slack.posts) == 1
    post = slack.posts[0]
    body = _blocks_text(post["blocks"])
    client = next(line for line in body.splitlines() if line.startswith(":office:"))
    assert client == ":office: *GABAN の現状*"
    assert "「GABAN」に触れた投稿" in body
    scope = (
        "照合したのは本文・タグにある「GABAN」の表記だけです"
        "（ほかの表記・公式アカウントは照合していません）"
    )
    assert scope in body and scope in post["text"]


@pytest.mark.parametrize(
    ("email", "claim", "emails"),
    [
        (OTHER, _claim(), frozenset({ME})),  # allowlist 外
        (ME, _claim(), frozenset()),  # 空の allowlist は全員拒否
        (ME, _claim(channel=CHANNEL, thread_ts="1784424000.000009"), frozenset({ME})),  # チャンネル
    ],
)
async def test_not_eligible_returns_as_today_and_posts_nothing(
    monkeypatch: pytest.MonkeyPatch,
    slack: _FakeSlack,
    email: str,
    claim: Any,
    emails: frozenset[str],
) -> None:
    _direct(monkeypatch, emails=emails)
    skill, _g, _d = _skill()
    out = await _call(_spec(skill), claim=claim, email=email)
    assert set(out) <= RELAY_KEYS
    assert out["slack_summary"] == _baseline_summary()
    assert slack.posts == []


async def test_flag_off_returns_as_today(slack: _FakeSlack) -> None:
    skill, _g, _d = _skill()
    out = await _call(_spec(skill))
    assert out["slack_summary"] == _baseline_summary()
    assert slack.posts == []


async def test_legacy_caller_is_not_posted(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    """resolver 無し（LEGACY）＝宛先を信用できないので直接投稿しない。"""
    _direct(monkeypatch)
    skill, _g, _d = _skill()
    contents = await dispatch_tool(
        _spec(skill), TOOL, {**ARGS, USER_CONTEXT_KEY: {"user_email": ME, "channel_id": DM}}
    )
    out = json.loads(contents[0].text)
    assert out["slack_summary"] == _baseline_summary()
    assert slack.posts == []


# ── 投稿の失敗とタイムアウト ─────────────────────────────────────────────


class _NotOkSlack(_FakeSlack):
    async def post_message(self, channel: str, text: str, request_id: str, **kw: Any) -> Any:
        with self.lock:
            self.posts.append({"channel": channel, "text": text, "thread_ts": kw.get("thread_ts")})
        return SlackPostResult(channel=channel, ts=None, ok=False)


class _TimeoutSlack(_FakeSlack):
    async def post_message(self, channel: str, text: str, request_id: str, **kw: Any) -> Any:
        with self.lock:
            self.posts.append({"channel": channel, "text": text, "thread_ts": kw.get("thread_ts")})
        raise TimeoutError


async def test_failed_post_falls_back_to_normal_return(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _NotOkSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: fake)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0.0)
    _direct(monkeypatch)
    skill, _g, _d = _skill()
    out = await _call(_spec(skill))
    # 2 回試して届かなかった → 結果を消さないよう今と同じ返却
    assert len(fake.posts) == 2
    assert set(out) <= RELAY_KEYS
    assert out["slack_summary"] == _baseline_summary()


async def test_timeout_is_treated_as_maybe_posted(monkeypatch: pytest.MonkeyPatch) -> None:
    """タイムアウトは Slack 側で届いた可能性がある → 同じ文面を Aico から出し直させない。"""
    fake = _TimeoutSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: fake)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0.0)
    _direct(monkeypatch)
    skill, _g, _d = _skill()
    out = await _call(_spec(skill))
    assert len(fake.posts) == 1  # 再試行もしない
    assert out["status"] == "posted" and out["delivered"] is False
    assert out["slack_summary"] == direct_summary.UNCERTAIN_TEXT
    assert "http" not in json.dumps(out, ensure_ascii=False)


# ── 2 段目と一緒に使う ──────────────────────────────────────────────────


async def test_followup_notice_is_in_the_direct_post_and_followup_comes_after(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    _direct(monkeypatch)
    monkeypatch.setattr(
        surface_video_followup,
        "load_policy",
        lambda: surface_video_followup.FollowupPolicy(enabled=True, allowed_emails=frozenset({ME})),
    )
    skill, _g, _d = _skill()
    out = await _call(_spec(skill))
    assert out["status"] == "posted"
    first = _blocks_text(slack.posts[0]["blocks"])
    assert followup_notice_line(5) in first  # 予告の行も Block Kit の注記に入る
    await _eventually(lambda: len(slack.posts) == 2, timeout=10.0)
    assert slack.posts[1]["channel"] == DM
    assert slack.posts[1]["blocks"] and _blocks_text(slack.posts[1]["blocks"]) != first


# ── 決め方（単体）──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("policy", "tool", "caller", "meta", "reason"),
    [
        (
            direct_summary.DirectPolicy(True, frozenset({ME})),
            "video_algorithm",
            _claim(),
            {},
            "tool",
        ),
        (direct_summary.DirectPolicy(False, frozenset({ME})), TOOL, _claim(), {}, "disabled"),
        (direct_summary.DirectPolicy(True, frozenset({ME})), TOOL, None, {}, "legacy"),
        (
            direct_summary.DirectPolicy(True, frozenset({ME})),
            TOOL,
            _claim(),
            {"identity_verified": False, "user_email": ME},
            "unverified",
        ),
        (
            direct_summary.DirectPolicy(True, frozenset({ME})),
            TOOL,
            _claim(),
            {"identity_verified": True, "user_email": OTHER},
            "not_allowed",
        ),
        (
            direct_summary.DirectPolicy(True, frozenset({ME})),
            TOOL,
            _claim(channel=CHANNEL, thread_ts="1784424000.000009"),
            {"identity_verified": True, "user_email": ME.upper()},
            "not_dm",
        ),
        (
            direct_summary.DirectPolicy(True, frozenset({ME})),
            TOOL,
            _claim(),
            {"identity_verified": True, "user_email": f" {ME.upper()} "},
            "ok",
        ),
    ],
)
def test_decide_reasons(
    policy: direct_summary.DirectPolicy, tool: str, caller: Any, meta: dict[str, Any], reason: str
) -> None:
    destination, got = direct_summary.decide(
        policy, tool=tool, verified_caller=caller, metadata=meta
    )
    assert got == reason
    assert (destination is not None) == (reason == "ok")


def test_policy_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(direct_summary.ENABLED_ENV, raising=False)
    monkeypatch.delenv(direct_summary.ALLOWED_EMAILS_ENV, raising=False)
    assert direct_summary.load_policy() == direct_summary.DirectPolicy()
    monkeypatch.setenv(direct_summary.ENABLED_ENV, "1")
    monkeypatch.setenv(direct_summary.ALLOWED_EMAILS_ENV, f" {ME.upper()} ,,{OTHER}")
    assert direct_summary.load_policy() == direct_summary.DirectPolicy(
        enabled=True, allowed_emails=frozenset({ME, OTHER})
    )


def test_deliver_without_summary_is_failed(slack: _FakeSlack) -> None:
    dest = detached_jobs.Destination(channel_id=DM, thread_ts=None)
    assert direct_summary.deliver({"slack_summary": "  "}, dest, request_id="r") == "failed"
    assert direct_summary.deliver({}, dest, request_id="r") == "failed"
    assert slack.posts == []


def test_post_to_origin_keeps_its_bool_contract(slack: _FakeSlack) -> None:
    dest = detached_jobs.Destination(channel_id=DM, thread_ts=None)
    assert detached_jobs.post_to_origin("a", dest, request_id="r") is True
    assert detached_jobs.post_to_origin_status("b", dest, request_id="r") == "posted"


def test_post_to_origin_status_timeout_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    dest = detached_jobs.Destination(channel_id=DM, thread_ts=None)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0.0)
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: _TimeoutSlack())
    assert detached_jobs.post_to_origin_status("a", dest, request_id="r") == "uncertain"
    assert detached_jobs.post_to_origin("a", dest, request_id="r") is False
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: _NotOkSlack())
    assert detached_jobs.post_to_origin_status("a", dest, request_id="r") == "failed"
    assert (
        detached_jobs.post_to_origin_status("a", dest, request_id="r", fallback_user_id=USER_ID)
        == "failed"
    )
