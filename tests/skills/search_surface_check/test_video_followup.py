"""検索上位チェックの 2 段目（上位の動画の中身）の skill 層のテスト。

本物の VideoAlgorithmSkill（取得→圧縮→Gemini→縮退）を、Gemini・取得・サムネ取得だけ偽物にして
回す。確かめること:
- 1 段目の上位（順位順）の動画を渡す（検索し直さない・カルーセル/尺 0 を除く）
- 月間上限が部分・0 本のときの動き
- 取得できない動画はサムネだけの分析へ縮退し、集計には入れない・全滅なら「分析できません」
- LLM の読みの数字照合（入力に無い数字の文を捨てる）
- Slack 追記の文面（表なし・1 本 1 行・第三者文字列の無害化）
- レポートに章が足され、新しい URL になる
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from teamagent.adapters import quota_store
from teamagent.adapters.quota_store import QuotaResult
from teamagent.skills.base import SkillContext
from teamagent.skills.search_surface_check.schema import (
    SearchSurfaceCheckInput,
    SearchSurfaceCheckOutput,
)
from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill
from teamagent.skills.search_surface_check.summary import (
    followup_notice_line,
    insert_before_report_line,
)
from teamagent.skills.search_surface_check.video_digest import (
    digest_videos,
    ground_digest_conclusion,
    is_analyzable,
    post_to_meta,
    select_followup_videos,
)
from teamagent.skills.search_surface_check.video_render import (
    ALL_FAILED_TEXT,
    COVER_ONLY_NOTE,
    REPORT_FAILED_LINE,
)
from teamagent.skills.video_algorithm import thumbnails
from tests.skills.search_surface_check.fixtures import KEYWORD, NOW, s3_rows
from tests.skills.search_surface_check.video_fakes import (
    ANALYSES,
    FakeDownloader,
    FakeGemini,
    VideoBedrock,
    make_engine,
)

_JOB_ID = "tk_0123456789ab"
ME = "a@vectorinc.co.jp"


@pytest.fixture(autouse=True)
def _local_media(monkeypatch: pytest.MonkeyPatch) -> None:
    """media job を使わない（ローカルのサムネ取得経路）。quota は既定 OFF。"""
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")
    monkeypatch.delenv("VIDEO_QUOTA_ENABLED", raising=False)
    monkeypatch.delenv("USE_TIKTOK_APIFY_FALLBACK", raising=False)
    monkeypatch.setattr(thumbnails, "fetch_cover", lambda *a, **k: None)


def _ctx() -> SkillContext:
    return SkillContext(request_id="req-video", user_id="U1", metadata={"user_email": ME})


class _Source:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def posts(self, n: int | None = None) -> list[dict[str, Any]]:
        return self._rows[:n] if n else self._rows


class _Publisher:
    def __init__(self, *, fail_from: int | None = None) -> None:
        self.htmls: list[str] = []
        self.fail_from = fail_from

    def __call__(self, path: str, *, request_id: str, query: str) -> str | None:
        with open(path, encoding="utf-8") as f:
            self.htmls.append(f.read())
        n = len(self.htmls)
        if self.fail_from is not None and n >= self.fail_from:
            return None
        return f"https://s3.example/surface-{n}"


def _skill(
    *,
    gemini: FakeGemini | None = None,
    downloader: FakeDownloader | None = None,
    bedrock: VideoBedrock | None = None,
    publisher: _Publisher | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> tuple[SearchSurfaceCheckSkill, FakeGemini, FakeDownloader, _Publisher, VideoBedrock]:
    gem = gemini or FakeGemini()
    dl = downloader or FakeDownloader()
    pub = publisher or _Publisher()
    bed = bedrock or VideoBedrock()
    source_rows = rows if rows is not None else s3_rows()
    skill = SearchSurfaceCheckSkill(
        bedrock=bed,
        publisher=pub,
        tiktok_source_factory=lambda job_id, audit_hash: _Source(source_rows),
        clock=lambda: NOW,
        video_engine=make_engine(gem, dl),
    )
    return skill, gem, dl, pub, bed


def _input() -> SearchSurfaceCheckInput:
    return SearchSurfaceCheckInput(
        keywords=[KEYWORD], platforms=["tiktok"], acquire_job_id=_JOB_ID, client_name="花王"
    )


def _first_stage(skill: SearchSurfaceCheckSkill) -> SearchSurfaceCheckOutput:
    return skill.run(_input(), _ctx())


def _followup(
    skill: SearchSurfaceCheckSkill, out: SearchSurfaceCheckOutput, max_videos: int = 5
) -> Any:
    videos = select_followup_videos(out, max_videos)
    return skill.run_video_followup(out, _input(), _ctx(), videos=videos)


# ── 選ぶ（同じ上位・検索し直さない）──────────────────────────────────────


def test_selects_top_ranked_videos_of_the_first_keyword_tiktok_surface() -> None:
    rows = s3_rows()
    rows[1]["duration"] = 0  # 2 位はカルーセル（尺 0）
    skill, *_ = _skill(rows=rows)
    out = _first_stage(skill)
    picked = select_followup_videos(out, 5)
    assert [p.rank for p in picked] == [1, 3, 4, 5, 6]
    assert out.measured_epoch == NOW


def test_analyzable_excludes_non_video_urls_and_missing_duration() -> None:
    skill, *_ = _skill()
    post = _first_stage(skill).surfaces[0].posts[0]
    assert is_analyzable(post)
    assert not is_analyzable(post.model_copy(update={"url": "https://www.tiktok.com/@a/photo/1"}))
    assert not is_analyzable(post.model_copy(update={"url": "javascript:alert(1)"}))
    assert not is_analyzable(post.model_copy(update={"duration_sec": 0}))


def test_meta_carries_first_stage_numbers_and_cover() -> None:
    rows = s3_rows()
    rows[0]["cover_url"] = "https://p16-sign.tiktokcdn.com/cover1.jpg"
    skill, *_ = _skill(rows=rows)
    post = _first_stage(skill).surfaces[0].posts[0]
    assert post.thumb_url == "https://p16-sign.tiktokcdn.com/cover1.jpg"
    meta = post_to_meta(post)
    assert (meta.rank, meta.url, meta.author) == (1, post.url, "gonosara")
    assert meta.collect_count == 11_200 and meta.play_count == 351_000
    assert meta.cover_url == "https://p16-sign.tiktokcdn.com/cover1.jpg"
    assert meta.duration_sec == 58.0


def test_followup_analyzes_the_same_top5_without_searching_again() -> None:
    skill, gem, dl, _pub, _bed = _skill()
    out = _first_stage(skill)
    result = _followup(skill, out)
    urls = [p.url for p in out.surfaces[0].posts[:5]]
    assert sorted(dl.urls) == sorted(urls)  # 1 段目の上位 5 本の URL だけを取得
    assert gem.ranks == [1, 2, 3, 4, 5]
    assert all(mime == "video/mp4" for _, mime in gem.calls)
    assert result.status == "ok"
    assert result.digest.watched == 5


# ── 数える ───────────────────────────────────────────────────────────


def test_digest_counts_short_video_axes() -> None:
    skill, *_ = _skill()
    d = _followup(skill, _first_stage(skill)).digest
    assert (d.hook_types[0].label, d.hook_types[0].count) == ("数字", 2)
    assert d.opening_telop == 3  # 5 位のテロップは 5 秒（冒頭ではない）
    assert d.telop_kw == 3
    assert d.spoken_kw == 2
    assert d.median_duration_sec == 62
    assert d.median_cut_count == 12
    assert [(p.label, p.count) for p in d.pacing] == [("速い", 2), ("ふつう", 2), ("とても速い", 1)]
    assert d.cta == 4
    assert [(c.label, c.count) for c in d.cta_types] == [("保存", 3), ("フォロー", 2)]
    assert d.narration == 4
    assert d.trending_sound == 1
    assert d.median_coherence == 75
    # 保存率の高い 2 本（2 位 6.8%・4 位 4.5%）に共通すること（相関は出さない）
    assert d.save_top_ranks == [2, 4]
    assert d.save_top_common == ["冒頭にテロップ", "CTA: フォロー", "ナレーションあり"]


# ── 月間上限 ─────────────────────────────────────────────────────────


def _quota(monkeypatch: pytest.MonkeyPatch, remaining: int) -> list[int]:
    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    asked: list[int] = []

    def _consume(self: Any, email: str, count: int, *, request_id: str) -> QuotaResult:
        asked.append(count)
        if count <= remaining:
            return QuotaResult(allowed=True, used=50 - remaining + count, limit=50)
        return QuotaResult(allowed=False, used=50 - remaining, limit=50, requested=count)

    monkeypatch.setattr(quota_store.VideoQuotaStore, "try_consume", _consume)
    return asked


def test_partial_quota_analyzes_only_the_reserved_top_ranks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _quota(monkeypatch, remaining=3)
    skill, gem, *_ = _skill()
    result = _followup(skill, _first_stage(skill))
    assert asked == [5, 3]  # 5 本は断られ、残り 3 本に丸めて確保
    assert gem.ranks == [1, 2, 3]
    assert result.digest.requested == 5 and result.digest.reserved == 3
    assert "5本のうち3本だけ分析しました" in result.slack_text
    assert "**上位3本の動画の中身**" in result.slack_text


def test_zero_quota_posts_the_limit_message_and_analyzes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _quota(monkeypatch, remaining=0)
    skill, gem, dl, pub, _ = _skill()
    out = _first_stage(skill)
    published_before = len(pub.htmls)
    result = _followup(skill, out)
    assert result.status == "quota_exhausted"
    assert "今月の動画分析の上限に達したため、動画の中身は分析しませんでした（残り 0 本）" in (
        result.slack_text
    )
    assert gem.calls == [] and dl.urls == []
    assert len(pub.htmls) == published_before  # レポートも作り直さない


def test_quota_without_email_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _quota(monkeypatch, remaining=50)
    skill, gem, *_ = _skill()
    out = _first_stage(skill)
    ctx = SkillContext(request_id="r", metadata={})
    with pytest.raises(RuntimeError, match="VIDEO_QUOTA_IDENTITY_REQUIRED"):
        skill.run_video_followup(out, _input(), ctx, videos=select_followup_videos(out, 5))
    assert gem.calls == []


# ── 失敗・縮退 ───────────────────────────────────────────────────────


def test_unfetchable_video_degrades_to_cover_only_and_is_left_out_of_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = s3_rows()
    rows[2]["cover_url"] = "https://p16-sign.tiktokcdn.com/cover3.jpg"
    monkeypatch.setattr(thumbnails, "fetch_cover", lambda *a, **k: b"\xff\xd8\xff\xe0jpegdata")
    skill, gem, *_ = _skill(rows=rows)
    out = _first_stage(skill)
    third_url = out.surfaces[0].posts[2].url
    skill, gem, _dl, _, _ = _skill(rows=rows, downloader=FakeDownloader(fail_urls={third_url}))
    out = _first_stage(skill)
    result = _followup(skill, out)
    assert (3, "image/jpeg") in gem.calls  # サムネ 1 枚で分析（既存の縮退）
    d = result.digest
    assert d.cover_only_ranks == [3] and d.watched == 4 and d.failed_ranks == []
    assert d.trending_sound == 0  # 3 位（サムネのみ）は集計に入れない
    line3 = next(ln for ln in result.slack_text.splitlines() if ln.startswith("- 3位"))
    assert COVER_ONLY_NOTE in line3
    assert "3位は動画を取得できず、サムネだけの分析です" in result.slack_text


def test_one_broken_video_does_not_discard_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    """取得失敗の縮退（サムネ取得）自体が例外でも、その 1 本だけを失敗にする。"""

    def _boom(*a: Any, **k: Any) -> bytes:
        raise RuntimeError("MEDIA_COVER_JOB_FAILED")

    monkeypatch.setattr(thumbnails, "fetch_cover", _boom)
    skill, *_ = _skill()
    out = _first_stage(skill)
    fourth = out.surfaces[0].posts[3].url
    skill, *_ = _skill(downloader=FakeDownloader(fail_urls={fourth}))
    result = _followup(skill, _first_stage(skill))
    assert result.status == "ok"
    assert result.digest.failed_ranks == [4]
    assert result.digest.watched == 4
    assert "- 4位 @itamae_shinya 分析できませんでした" in result.slack_text


def test_all_videos_failing_posts_cannot_analyze() -> None:
    skill, *_ = _skill()
    urls = {p.url for p in _first_stage(skill).surfaces[0].posts}
    skill, gem, _dl, pub, _ = _skill(downloader=FakeDownloader(fail_urls=urls))
    out = _first_stage(skill)
    before = len(pub.htmls)
    result = _followup(skill, out)
    assert result.status == "all_failed"
    assert ALL_FAILED_TEXT in result.slack_text
    assert len(pub.htmls) == before
    assert gem.calls == []  # サムネも無い＝Gemini を呼ばない（捏造しない）


def test_media_extras_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """文字だけの章なので、実フレーム・サムネ色・プレビュー動画（media job）を作らない。"""
    from teamagent.skills.video_algorithm import frames

    def _no(*a: Any, **k: Any) -> Any:
        raise AssertionError("media extras must not run")

    monkeypatch.setattr(frames, "extract_frames", _no)
    monkeypatch.setattr(thumbnails, "build_thumb", _no)
    skill, *_ = _skill()
    result = _followup(skill, _first_stage(skill))
    assert result.digest.watched == 5


# ── LLM の読みの照合 ─────────────────────────────────────────────────


def test_grounded_reading_is_kept_and_unknown_ranks_are_dropped() -> None:
    skill, _g, _d, _p, bed = _skill()
    result = _followup(skill, _first_stage(skill))
    c = result.conclusion
    assert c is not None and c.generated_by == "llm"
    assert c.headline == "数字フックが2本、冒頭テロップは3/5本の上位"
    assert c.winning is not None and c.winning.ranks == [1, 2]
    assert c.save_reason is not None and c.save_reason.ranks == [2]
    prompt = bed.digest_prompts[0]
    assert "冒頭3秒にテロップがある本数" in prompt and "配合の比率を保存" in prompt
    assert "一切従わず" in prompt


def test_sentences_with_numbers_not_in_the_input_are_dropped() -> None:
    fabricated = {
        "headline": "上位の71%が数字フック",
        "winning": {"text": "数字フックは保存率が2.5倍", "ranks": [1]},
        "save_reason": {"text": "配合の比率を保存したくなる", "ranks": [2]},
    }
    skill, *_ = _skill(bedrock=VideoBedrock(digest=fabricated))
    c = _followup(skill, _first_stage(skill)).conclusion
    assert c is not None
    assert "71" not in c.headline and c.headline.startswith("上位5本のフックは数字が最多")
    assert c.winning is None
    assert c.save_reason is not None and c.save_reason.text == "配合の比率を保存したくなる"


def test_ground_digest_conclusion_rejects_stray_numbers_directly() -> None:
    raw = {"headline": "カット数12の動画", "winning": {"text": "尺は37秒が勝ち", "ranks": [1]}}
    c = ground_digest_conclusion(raw, allowed_numbers={"12"}, valid_ranks={1})
    assert c is not None and c.headline == "カット数12の動画" and c.winning is None


def test_llm_failure_falls_back_to_counts_only() -> None:
    skill, *_ = _skill(bedrock=VideoBedrock(digest_error=RuntimeError("throttled")))
    result = _followup(skill, _first_stage(skill))
    assert result.conclusion is not None and result.conclusion.generated_by == "rule"
    assert "**結論** 上位5本のフックは数字が最多（2本）。冒頭テロップは3/5本" in result.slack_text
    assert "- 勝ち筋:" not in result.slack_text


# ── Slack 追記の文面 ──────────────────────────────────────────────────


def test_slack_text_is_bullets_one_line_per_video_no_tables() -> None:
    skill, *_ = _skill()
    text = _followup(skill, _first_stage(skill)).slack_text
    lines = text.splitlines()
    assert lines[0] == f"**上位5本の動画の中身**「{KEYWORD}」TikTok"
    assert lines[1].startswith("**結論** ")
    assert "|" not in text and "```" not in text
    per_video = [ln for ln in lines if re.match(r"- \d+位 @", ln)]
    assert len(per_video) == 5
    assert per_video[0] == (
        "- 1位 @gonosara フック: 数字（『4つでいい本格スパイスカレー』）／冒頭テロップ○／"
        "KW テロップ○・発話○／58秒・12カット・速い／CTA: 保存"
    )
    for label in (
        "- 勝ち筋:",
        "- フック:",
        "- テロップと KW:",
        "- 構成:",
        "- CTA:",
        "- 保存の理由:",
    ):
        assert any(ln.startswith(label) for ln in lines), label
    assert (
        "- 保存率の高い2本（2・4位）に共通: 冒頭にテロップ・CTA: フォロー・ナレーションあり"
        in lines
    )
    assert lines[-2] == "レポート（上位5本の動画の中身つき・7日有効）: https://s3.example/surface-2"
    assert lines[-1].startswith("_概算 $")


def test_third_party_strings_cannot_inject_slack_markup() -> None:
    evil = {
        **ANALYSES,
        1: {
            **ANALYSES[1],
            "hook_summary": "<!channel> *太字* [罠](https://evil.example)",
        },
    }
    rows = s3_rows()
    rows[0]["account_id"] = "<@U999>"
    skill, *_ = _skill(gemini=FakeGemini(evil), rows=rows)
    text = _followup(skill, _first_stage(skill)).slack_text
    assert "<!channel>" not in text and "<@U999>" not in text
    assert "[罠](" not in text
    line1 = next(ln for ln in text.splitlines() if ln.startswith("- 1位"))
    assert "＜!channel＞" in line1 and "＊太字＊" in line1


# ── レポート ─────────────────────────────────────────────────────────


def test_report_gets_the_chapter_and_a_new_url() -> None:
    skill, _g, _d, pub, _b = _skill()
    out = _first_stage(skill)
    assert out.report_url == "https://s3.example/surface-1"
    result = _followup(skill, out)
    assert result.report_url == "https://s3.example/surface-2"
    first, second = pub.htmls
    assert "top-videos" not in first
    assert "id='top-videos'" in second
    assert second.index("id='surface-1'") < second.index("id='top-videos'")
    for axis in (
        "フック",
        "冒頭テロップ",
        "テロップに KW",
        "発話に KW",
        "カット数",
        "テンポ",
        "CTA",
    ):
        assert f"<th scope='row'>{axis}</th>" in second
    for axis in ("ナレーション", "流行の音源", "一致度", "保存の理由"):
        assert f"<th scope='row'>{axis}</th>" in second
    assert "class='vcard'" in second and "材料4つのメモとして見返す" in second
    assert "2026-09-25" in second  # 1 段目と同じ実測日


def test_report_chapter_escapes_third_party_text() -> None:
    evil = {**ANALYSES, 2: {**ANALYSES[2], "main_message": "<script>alert(1)</script>"}}
    skill, _g, _d, pub, _b = _skill(gemini=FakeGemini(evil))
    _followup(skill, _first_stage(skill))
    assert "<script>alert(1)</script>" not in pub.htmls[-1]
    assert "&lt;script&gt;" in pub.htmls[-1]


def test_report_publish_failure_says_so() -> None:
    skill, *_ = _skill(publisher=_Publisher(fail_from=2))
    result = _followup(skill, _first_stage(skill))
    assert result.report_url is None
    assert REPORT_FAILED_LINE in result.slack_text
    assert "7日有効" not in result.slack_text


# ── 1 段目の予告行 ────────────────────────────────────────────────────


def test_notice_line_goes_right_before_the_report_line() -> None:
    skill, *_ = _skill()
    summary = _first_stage(skill).slack_summary
    line = followup_notice_line(5)
    updated = insert_before_report_line(summary, line)
    lines = updated.splitlines()
    i = lines.index(line)
    assert lines[i + 1].startswith("レポート（全")
    assert updated.replace(line + "\n", "") == summary


def test_notice_line_without_report_goes_before_cost() -> None:
    summary = "**検索上位チェック**「x」TikTok\n- a\n\n_概算 $0.0010_"
    updated = insert_before_report_line(summary, "予告")
    assert updated.splitlines()[-2:] == ["予告", "_概算 $0.0010_"]


def test_digest_of_empty_list_is_zero() -> None:
    d = digest_videos([], keyword="x", requested=0, reserved=0)
    assert d.watched == 0 and d.hook_types == []


def test_save_top_common_needs_both_videos() -> None:
    """保存率の高い 2 本（2・4 位）の片方にしか無い特徴は「共通」にしない。"""
    analyses = {**ANALYSES, 4: {**ANALYSES[4], "telops": [], "has_narration": False}}
    skill, *_ = _skill(gemini=FakeGemini(analyses))
    d = _followup(skill, _first_stage(skill)).digest
    assert d.save_top_ranks == [2, 4]
    assert d.save_top_common == ["CTA: フォロー"]


def test_report_chapter_escapes_the_llm_reading() -> None:
    reading = {"headline": "<img src=x onerror=alert(1)>数字フックの上位", "winning": None}
    skill, _g, _d, pub, _b = _skill(bedrock=VideoBedrock(digest=reading))
    _followup(skill, _first_stage(skill))
    assert "<img src=x" not in pub.htmls[-1]
    assert "&lt;img src=x" in pub.htmls[-1]
