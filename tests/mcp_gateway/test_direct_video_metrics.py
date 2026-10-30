"""動画数字の直接投稿。gateway・実投稿経路を通し、Slack/検索/usage はフェイク。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from structlog.testing import capture_logs

from teamagent.mcp_gateway import detached_jobs, direct_summary, server
from teamagent.mcp_gateway import direct_video_metrics as video
from teamagent.skills._shared.next_step import DELIVER_SUGGESTION
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.search import campaign_files, composite
from teamagent.skills.search.alias_expansion import scope_footer
from teamagent.skills.search.schema import SearchInput, SearchOutput
from tests.mcp_gateway.test_surface_video_followup import (
    CHANNEL,
    DM,
    ME,
    OTHER,
    _claim,
    _FakeSlack,
    _resolver_for,
    _spec,
    _Verifier,
)
from tests.skills.search.test_campaign_files import _video, _video_campaign

HEADING = "📊 動画ごとの数字（ショート動画データベース）"
SECTION = (
    HEADING + "\n食品 & 飲料 / <テーマ>\n"
    "1. 507,000 再生・保存 120　洋食ランチ https://example.test/video/1\n\n"
    "B社 / 夏\n2. 不明 再生・保存 80　試食 https://example.test/video/2"
)
PREFIX = "回答です。\n\n📎 施策のレポート・提案書\nhttps://example.test/report\n\n"
SUFFIX = "\n\n📎 *資料リンク*\nhttps://example.test/file"
ANSWER = PREFIX + SECTION + SUFFIX


def _posted_slack_text(section: str) -> str:
    """投稿関数が Slack 用に 1 回だけエスケープした後の SECTION（実投稿経路の出力）。"""
    return (
        section.replace("&", "&amp;")
        .replace("<テーマ>", "&lt;テーマ&gt;")
        .replace("https://example.test/video/1", "<https://example.test/video/1>")
        .replace("https://example.test/video/2", "<https://example.test/video/2>")
    )


def _slack_status_lines() -> list[str]:
    """search が 📊 の後へ "\\n\\n" でつなぐ状態行。composite.status_note の実出力をそのまま使う。"""
    lookups = [
        composite.SlackLookup(status="not_connected", reason="not_connected"),
        composite.SlackLookup(status="not_connected", reason="reconnect_required"),
        composite.SlackLookup(status="error", reason="search_failed"),
        composite.SlackLookup(status="ok", reason="ok", hidden_count=2),
    ]
    notes = [composite.status_note(lookup) for lookup in lookups]
    assert all(notes), "状態行が空になった（テストの前提が崩れた）"
    return notes


STATUS_IDS = ["not_connected", "reconnect_required", "search_failed", "hidden"]


class _Search(BaseSkill[SearchInput, SearchOutput]):
    input_schema = SearchInput
    output_schema = SearchOutput

    def __init__(self, answer: str = ANSWER) -> None:
        self.answer = answer

    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        return SearchOutput(answer=self.answer, total_cost_usd=0)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in (video.ENABLED_ENV, video.ALLOWED_EMAILS_ENV, "ENABLE_PROGRESS_NOTIFY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("USE_PAYLOAD_OFFLOAD", "false")
    monkeypatch.setattr(server, "_record_usage", lambda **kwargs: None)
    monkeypatch.setattr(server, "_inject_search_web_links", lambda data: None)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0)


def _enable(monkeypatch: pytest.MonkeyPatch, emails: str = ME) -> None:
    monkeypatch.setenv(video.ENABLED_ENV, "true")
    monkeypatch.setenv(video.ALLOWED_EMAILS_ENV, emails)


async def _dispatch(answer: str = ANSWER, *, email: str = ME, channel: str = DM) -> dict[str, Any]:
    claim = _claim(channel=channel, thread_ts="123.456" if channel == CHANNEL else None)
    contents = await server.dispatch_tool(
        _spec(_Search(answer), "search"),
        "search",
        {"query": "食品の動画事例"},
        identity_resolver=_resolver_for(email),
        caller_claim_verifier=_Verifier(claim),  # type: ignore[arg-type]
    )
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


@pytest.mark.parametrize("emails", [ME, "*"])
async def test_posts_exact_section_and_replaces_only_section(
    monkeypatch: pytest.MonkeyPatch, emails: str
) -> None:
    _enable(monkeypatch, emails)
    slack = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: slack)
    with capture_logs() as logs:
        result = await _dispatch()
    assert result["answer"] == PREFIX + video.POSTED_TEXT + SUFFIX
    assert SECTION not in result["answer"] and HEADING not in result["answer"]
    assert slack.posts == [
        {"channel": DM, "thread_ts": None, "blocks": None, "text": _posted_slack_text(SECTION)}
    ]
    event = next(log for log in logs if log["event"] == "search_video_metrics_direct_post")
    assert event["outcome"] == "posted" and event["reason"] == "post_status"
    assert event["section_chars"] == len(SECTION)
    assert "食品" not in json.dumps(event, ensure_ascii=False) and "https://" not in str(event)


@pytest.mark.parametrize("note", _slack_status_lines(), ids=STATUS_IDS)
async def test_slack_status_line_stays_in_answer_without_source_links(
    monkeypatch: pytest.MonkeyPatch, note: str
) -> None:
    # 10-14 の穴①（本番 USE_DIRECT_VIDEO_METRICS_POST=1）: 資料リンクの節が無い回は、
    # "\n\n" でつながる ℹ️・🔒 の状態行が 📊 の節に巻き込まれ、DM への直接投稿の側へ移っていた。
    _enable(monkeypatch)
    slack = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: slack)
    result = await _dispatch(PREFIX + SECTION + "\n\n" + note)
    assert [post["text"] for post in slack.posts] == [_posted_slack_text(SECTION)]
    assert result["answer"] == PREFIX + video.POSTED_TEXT + "\n\n" + note


@pytest.mark.parametrize("failure", ["not_ok", "timeout", "exception"])
async def test_delivery_status_preserves_or_removes_section(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _enable(monkeypatch)
    slack = _FakeSlack()
    calls: list[dict[str, Any]] = []

    async def post_message(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if failure == "timeout":
            raise TimeoutError
        if failure == "exception":
            raise RuntimeError("本文はログに出さない")
        return None

    monkeypatch.setattr(slack, "post_message", post_message)
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: slack)
    result = await _dispatch()
    assert result["answer"] == (
        PREFIX + video.POSTED_TEXT + SUFFIX if failure == "timeout" else ANSWER
    )
    assert len(calls) == (1 if failure == "timeout" else 2)


@pytest.mark.parametrize("case", ["off", "outside", "empty", "channel", "no_section"])
async def test_gateway_passthrough(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    _enable(monkeypatch)
    if case == "off":
        monkeypatch.delenv(video.ENABLED_ENV)
    if case == "empty":
        monkeypatch.delenv(video.ALLOWED_EMAILS_ENV)
    slack = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: slack)
    answer = PREFIX + SUFFIX if case == "no_section" else ANSWER
    result = await _dispatch(
        answer,
        email=OTHER if case == "outside" else ME,
        channel=CHANNEL if case == "channel" else DM,
    )
    assert result["answer"] == answer and slack.posts == []


@pytest.mark.parametrize("case", ["legacy", "unverified", "no_destination"])
def test_untrusted_caller_passthrough(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    _enable(monkeypatch, "*")
    slack = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: slack)
    data = {"answer": ANSWER}
    video.maybe_post(
        data,
        verified_caller=None
        if case == "legacy"
        else _claim(channel="bad" if case == "no_destination" else DM),
        metadata={"identity_verified": case != "unverified", "user_email": ME},
        request_id="r",
    )
    assert data == {"answer": ANSWER} and slack.posts == []


def test_unexpected_post_exception_preserves_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)

    def fail(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("本文 https://private.example")

    monkeypatch.setattr(detached_jobs, "post_to_origin_status", fail)
    data = {"answer": ANSWER}
    with capture_logs() as logs:
        video.maybe_post(
            data,
            verified_caller=_claim(),
            metadata={"identity_verified": True, "user_email": ME},
            request_id="r",
        )
    assert data["answer"] == ANSWER
    assert logs[-1]["reason"] == "post_exception"
    assert "private.example" not in str(logs)


@pytest.mark.parametrize("suffix", ["", SUFFIX, "\n\n\n次の節", "\n\n探した範囲: データベース"])
def test_extract_preserves_all_characters_and_boundaries(suffix: str) -> None:
    assert video.extract_section(PREFIX + SECTION + suffix) == (PREFIX, SECTION, suffix)


def _producer_lines_after_metrics() -> list[str]:
    """search が 📊 の後ろへつなぎうる実際の行（作り手の関数・定数の出力をそのまま使う）。"""
    return [
        *_slack_status_lines(),
        DELIVER_SUGGESTION,
        scope_footer(["食品 動画"], slack=None),
        scope_footer(
            ["食品 動画"], slack=composite.SlackLookup(status="error", reason="search_failed")
        ),
    ]


@pytest.mark.parametrize(
    "line",
    _producer_lines_after_metrics(),
    ids=[*STATUS_IDS, "deliver", "scope_footer", "scope_footer_slack_failed"],
)
def test_producer_lines_after_metrics_stay_outside_section(line: str) -> None:
    # 作り手が行頭の記号を変えたら、ここで赤になる（📊 の節への巻き込みを防ぐ）。
    suffix = "\n\n" + line
    assert video.extract_section(PREFIX + SECTION + suffix) == (PREFIX, SECTION, suffix)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("📎 実ファイルをお送りしますか？", id="clip"),
        pytest.param("📁 データベースの資料（2件）", id="folder"),
        pytest.param("🏆 事例・施策実績（1件）", id="trophy"),
        pytest.param("🤝 案件（決定・進行中の案件。実績ではありません）", id="handshake"),
        pytest.param("🗂 A社のカルテ（最新2件）", id="dividers"),
        pytest.param("🗂️ A社のカルテ（異体字セレクタ付き）", id="dividers_vs16"),
        pytest.param("💬 Slack（あなたの Slack・3件）", id="speech"),
        pytest.param("🗃 過去の調べもの（X・TikTok の調査結果）", id="filebox"),
        pytest.param("🗃️ 過去の調べもの（異体字セレクタ付き）", id="filebox_vs16"),
        pytest.param("🔎 近い業種・言い換え（『宅配』そのものの資料ではありません）", id="search"),
        pytest.param("🌐 公開Web（他社・一般の情報で、自社の実績ではありません）", id="globe"),
        pytest.param("ℹ️ Slack は未連携のため、データベースだけを探しました", id="info_vs16"),
        pytest.param("ℹ Slack は未連携（異体字セレクタ無し）", id="info_bare"),
        pytest.param("🔒 Slack の非公開の場所に一致が 1 件あります", id="lock"),
        pytest.param("探した範囲: データベース ✓3件", id="scope_colon"),
        pytest.param("探した範囲（コロン無し）", id="scope_bare"),
    ],
)
@pytest.mark.parametrize("sep", ["\n", "\n\n"], ids=["one_newline", "blank_line"])
def test_extract_stops_at_every_allowed_heading(line: str, sep: str) -> None:
    suffix = sep + line + "\n・本文"
    assert video.extract_section(PREFIX + SECTION + suffix) == (PREFIX, SECTION, suffix)


@pytest.mark.parametrize(
    "label",
    [
        pytest.param("C社 / 秋（投稿 3 本）", id="campaign"),
        pytest.param("1. 12 再生・保存 3", id="row"),
        pytest.param("⭐ 星印の施策", id="unlisted_emoji"),
    ],
)
def test_extract_keeps_campaign_blocks_after_one_blank_line(label: str) -> None:
    # 施策間の 1 空行は節の内側（見出しの許可リストに無い行は次の施策のブロック）。
    section = SECTION + "\n\n" + label + "\n3. 10 再生・保存 1"
    assert video.extract_section(PREFIX + section + SUFFIX) == (PREFIX, section, SUFFIX)


@pytest.mark.parametrize("answer", ["", "数字なし", "説明文 " + SECTION, HEADING + "違う\n本文"])
def test_extract_requires_exact_heading_line(answer: str) -> None:
    assert video.extract_section(answer) is None


def test_producer_heading_and_extractor_stay_in_sync() -> None:
    hit = _video_campaign(
        1, "食品メーカー", "試食", [_video("https://example.test/v", "試食", 100, 2)]
    )
    hit.metadata["related_files"] = []
    footer = campaign_files.video_stats_footer([hit], "回答")
    assert video.extract_section("回答\n\n" + footer) == ("回答\n\n", footer, "")
    assert footer.startswith(HEADING + "\n")


def test_env_defaults_and_independence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(direct_summary.ENABLED_ENV, "true")
    monkeypatch.setenv(direct_summary.ALLOWED_EMAILS_ENV, "*")
    assert video.load_policy() == direct_summary.DirectPolicy()
    _enable(monkeypatch, f" {ME.upper()} ,, {OTHER} ")
    assert video.load_policy() == direct_summary.DirectPolicy(True, frozenset({ME, OTHER}))
    monkeypatch.delenv(direct_summary.ENABLED_ENV)
    assert video.load_policy().enabled


def test_posted_text_tells_aico_not_to_repeat_the_numbers() -> None:
    # 10-13 の DM 実機: 別の投稿の後に Aico が同じ数字を丸めて並べ直した。置き換えの 1 行で止める。
    assert video.POSTED_TEXT.startswith("📊 ")
    assert "説明では数字を並べ直しません" in video.POSTED_TEXT


def _campaign_hit(chunk: int, advertiser: str, campaign: str, base: int) -> Any:
    hit = _video_campaign(
        chunk,
        advertiser,
        campaign,
        [
            _video(
                f"https://www.tiktok.com/@a/video/{base}",
                f"{advertiser}の投稿{base}、アレンジ",
                507700,
                129,
            ),
            _video(
                f"https://www.tiktok.com/@b/video/{base + 1}",
                f"{advertiser}の投稿{base + 1}",
                331300,
                73,
            ),
        ],
    )
    hit.metadata["related_files"] = []
    return hit


def _account_content() -> str:
    from teamagent.ingest.campaign_aggregate import AccountAggregate, format_account_document

    return format_account_document(
        AccountAggregate(
            account="acc0",
            videos=(
                _video("https://www.tiktok.com/@acc0/video/99", "通常投稿のレシピ", 400000, 10),
            ),
        )
    )


def _posted_case(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> tuple[dict[str, Any], Any, Any, str]:
    _enable(monkeypatch)
    monkeypatch.setattr(detached_jobs, "post_to_origin_status", lambda *a, **k: outcome)
    ham = _campaign_hit(10, "伊藤ハム", "ハンバーグ", 1)
    other = _campaign_hit(11, "別の会社", "別の施策", 50)  # 📊 に載らない施策（例: 4 件目以降）
    section = campaign_files.video_stats_footer([ham], "")
    assert section.startswith(HEADING) and "@b/video/50" not in section
    account = _account_content()
    data: dict[str, Any] = {
        "answer": PREFIX + section + SUFFIX,
        "hits": [
            {"content": ham.content},
            {"content": other.content},
            {"content": account},
            {"chunk_id": 3},
        ],
    }
    video.maybe_post(
        data,
        verified_caller=_claim(),
        metadata={"identity_verified": True, "user_email": ME},
        request_id="r",
    )
    return data, ham, other, account


@pytest.mark.parametrize("outcome", [direct_summary.POSTED, direct_summary.UNCERTAIN])
def test_posted_strips_only_the_rows_in_the_posted_section(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    # 10-14 の DM 実機（r55）: answer の節を置き換えても、hits の本文から数字を拾って並べ直した
    data, _, other, account = _posted_case(monkeypatch, outcome)
    stripped = data["hits"][0]["content"]
    for gone in (
        "再生 507,700",
        "保存 129",
        "再生 331,300",
        "https://www.tiktok.com",
        "合計 839,000",
    ):
        assert gone not in stripped, gone
    assert "伊藤ハムの投稿1、アレンジ" in stripped and "伊藤ハムの投稿2" in stripped
    assert campaign_files.VIDEO_ROWS_POSTED_HEADING in stripped
    # 📊 に無い中央値・平均・最大とアカウント名は残す
    assert "中央値" in stripped and "最大" in stripped and "たべもの" in stripped
    assert "投稿本数: 2 本" in stripped and "投稿文の特徴" in stripped
    # 📊 に載らなかった施策と投稿アカウント実績は、数字もリンクもそのまま
    assert data["hits"][1] == {"content": other.content}
    assert data["hits"][2] == {"content": account}
    assert data["hits"][3] == {"chunk_id": 3}


def test_failed_post_keeps_hit_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    data, ham, other, account = _posted_case(monkeypatch, direct_summary.FAILED)
    assert [h.get("content") for h in data["hits"]] == [ham.content, other.content, account, None]


def test_partially_posted_campaign_keeps_heading_and_total() -> None:
    ham = _campaign_hit(10, "伊藤ハム", "ハンバーグ", 1)
    only_first = "📊\n1. https://www.tiktok.com/@a/video/1"
    out = campaign_files.strip_video_numbers(ham.content, only_first)
    assert "https://www.tiktok.com/@a/video/1" not in out
    assert "https://www.tiktok.com/@b/video/2" in out and "再生 331,300" in out
    assert campaign_files.VIDEO_ROWS_POSTED_HEADING not in out and "合計 839,000" in out


def test_strip_video_numbers_is_idempotent_and_leaves_other_docs() -> None:
    ham = _campaign_hit(10, "伊藤ハム", "ハンバーグ", 1)
    posted = campaign_files.video_stats_footer([ham], "")
    once = campaign_files.strip_video_numbers(ham.content, posted)
    assert campaign_files.strip_video_numbers(once, posted) == once
    account = _account_content()
    assert (
        campaign_files.strip_video_numbers(
            account, posted + " https://www.tiktok.com/@acc0/video/99"
        )
        == account
    )


def test_graph_heading_ends_video_section() -> None:
    from teamagent.mcp_gateway.direct_video_metrics import extract_section
    from teamagent.skills.search.campaign_files import VIDEO_METRICS_HEADING

    metrics = VIDEO_METRICS_HEADING + "\n動画: 123再生"
    graph = "\n\n🕸 関係図で見る: <https://connect.newstv.co.jp/search/graph/focus?seed=id>"
    answer = metrics + graph + "\n\n探した範囲: データベース"
    assert extract_section(answer) == ("", metrics, graph + "\n\n探した範囲: データベース")
