"""submit→背景実行→status の非同期境界と配信・失敗時挙動（場面3・場面4）。"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.adapters.tiktok_scraper import (
    TikTokAuthor,
    TikTokScrapeError,
    TikTokSearchResult,
    TikTokVideo,
)
from teamagent.media.contracts import TIKTOK_N_PER_KW_MAX
from teamagent.skills.base import SkillContext
from teamagent.skills.omiyage_report.contract import DeckPlan
from teamagent.skills.omiyage_report.fmt.contract import validate_deck_content
from teamagent.skills.omiyage_report.fmt.editable import EDIT_MARKER
from teamagent.skills.omiyage_report.fmt.spec import load_fmt_spec
from teamagent.skills.omiyage_report.schema import (
    OmiyageReportStatusInput,
    OmiyageReportSubmitInput,
)
from teamagent.skills.omiyage_report.skill import (
    OmiyageReportStatusSkill,
    OmiyageReportSubmitSkill,
)
from teamagent.skills.omiyage_report.video_analysis import (
    ClusterRules,
    OmiyageVideoAnalyzer,
    VisionVerdict,
)

from .fmt_fixtures import make_png_bytes

_FRAME = make_png_bytes()


class _GateThreadLauncher:
    """実 daemon thread を起動しつつ、本処理を Event で堰き止める。"""

    def __init__(self, *, released: bool = False) -> None:
        self.gate = threading.Event()
        self.finished = threading.Event()
        if released:
            self.gate.set()

    def __call__(self, target: Callable[[], None], name: str) -> None:
        def gated_target() -> None:
            if not self.gate.wait(timeout=10):
                self.finished.set()
                return
            try:
                target()
            finally:
                self.finished.set()

        threading.Thread(target=gated_target, name=name, daemon=True).start()


def _video(video_id: str, desc: str, *, author: str = "someone") -> TikTokVideo:
    return TikTokVideo(
        id=video_id,
        url=f"https://www.tiktok.com/@{author}/video/{video_id}",
        desc=desc,
        create_time=1700000000,
        duration=21,
        cover_url="https://p16-sign.tiktokcdn.com/cover.jpeg",
        author=TikTokAuthor(unique_id=author, nickname=author, follower_count=1000),
        play_count=10000,
        digg_count=100,
        comment_count=10,
        share_count=5,
        collect_count=50,
        hashtags=("ヘアケア",),
        music_title="original sound",
    )


@dataclass
class _FakeSearcher:
    fail_queries: set[str] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, query: str, **kwargs: Any) -> TikTokSearchResult:
        self.calls.append({"query": query, **kwargs})
        if query in self.fail_queries:
            raise TikTokScrapeError("MEDIA_TIKTOK_BOT_WALL: blocked")
        return TikTokSearchResult(
            query=query,
            search_type="keyword",
            videos=(
                _video(f"{abs(hash(query)) % 10**6}1", f"{query}の紹介 #PR"),
                _video(f"{abs(hash(query)) % 10**6}2", f"{query}レビュー"),
            ),
        )


@dataclass
class _FakeDeckBuilder:
    calls: list[str] = field(default_factory=list)
    plans: list[DeckPlan] = field(default_factory=list)

    def __call__(self, deck_plan_json: str, out_dir: str, request_id: str) -> tuple[str, str]:
        # レンダラ境界: 入力は契約 DeckPlan の JSON だけ。エンジン契約と
        # レンダラ入力契約（fmt）の両方を毎回照合し、界面の割れを検出する
        plan = DeckPlan.model_validate_json(deck_plan_json)
        validate_deck_content(json.loads(deck_plan_json), load_fmt_spec())
        self.plans.append(plan)
        image = Path(out_dir) / f"omiyage_fmt_{plan.generated_on}.pptx"
        image.write_bytes(b"PK-image")
        editable = Path(out_dir) / f"omiyage_fmt_{plan.generated_on}_{EDIT_MARKER}.pptx"
        editable.write_bytes(b"PK-editable")
        self.calls.append(str(image))
        return str(image), str(editable)


class _FakeMediaClient:
    """acquire→frames をローカルで即応する media client 代替。"""

    def acquire_video(
        self, url: str, *, request_fingerprint: str, **kwargs: Any
    ) -> tuple[bytes, str]:
        return b"video-bytes", "video/mp4"

    def extract_frames(
        self,
        data: bytes,
        mime: str,
        timecodes: list[float],
        *,
        request_fingerprint: str,
        width: int = 480,
        **kwargs: Any,
    ) -> list[tuple[float, bytes]]:
        return [(second, _FRAME) for second in timecodes]


def _fake_vision(
    frames: list[tuple[float, bytes]],
    rules: ClusterRules,
    request_id: str,
) -> VisionVerdict:
    return VisionVerdict(
        cluster=rules.vocabulary[0],
        telop_text="ヘアケアのテロップ",
        cost_usd=0.01,
        model_id="test-model",
    )


def _fake_analyzer_factory(request_id: str) -> OmiyageVideoAnalyzer:
    return OmiyageVideoAnalyzer(
        request_id=request_id,
        media_client_factory=_FakeMediaClient,
        vision_caller=_fake_vision,
        max_videos=20,
        concurrency=2,
        cost_cap_usd=5.0,
        media_cost_usd_per_video=0.005,
    )


@dataclass
class _RecordingUploader:
    objects: dict[str, bytes] = field(default_factory=dict)

    def __call__(self, key: str, body: bytes, content_type: str) -> str:
        self.objects[key] = body
        return f"s3://fake-bucket/{key}"


@dataclass
class _FakeSlack:
    fail_channel_upload: bool = False
    uploads: list[dict[str, Any]] = field(default_factory=list)

    async def upload_file(
        self,
        channel: str,
        file_path: str,
        request_id: str,
        *,
        title: str | None = None,
        initial_comment: str | None = None,
        thread_ts: str | None = None,
    ) -> bool:
        self.uploads.append(
            {
                "channel": channel,
                "file_path": file_path,
                "title": title,
                "initial_comment": initial_comment,
                "thread_ts": thread_ts,
            }
        )
        return not (self.fail_channel_upload and channel == "C123")

    async def lookup_user_id_by_email(self, email: str, request_id: str) -> str | None:
        return "U999" if email else None

    async def open_dm(self, user_id: str, request_id: str) -> str | None:
        return "D999"


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="omiyage-async-test",
        user_id="U123",
        metadata={
            "channel_id": "C123",
            "thread_ts": "123.456",
            "user_email": "sales@example.com",
        },
    )


def _input() -> OmiyageReportSubmitInput:
    return OmiyageReportSubmitInput(
        brand="エムキュア",
        competitors=["ラサーナ"],
        keywords=["ヘアケア"],
    )


def _build(
    *,
    store: ProposalJobStore,
    launcher: _GateThreadLauncher,
    searcher: _FakeSearcher | None = None,
    slack: _FakeSlack | None = None,
    uploader: _RecordingUploader | None = None,
) -> tuple[OmiyageReportSubmitSkill, _FakeSearcher, _FakeDeckBuilder, _FakeSlack]:
    searcher = searcher or _FakeSearcher()
    builder = _FakeDeckBuilder()
    slack = slack or _FakeSlack()
    skill = OmiyageReportSubmitSkill(
        store=store,
        searcher=searcher,
        deck_builder=builder,
        slack=slack,
        thread_launcher=launcher,
        analyzer_factory=_fake_analyzer_factory,
        plan_uploader=uploader or _RecordingUploader(),
        heartbeat_seconds=0,
        retry_after_seconds=30,
        search_depth=120,
        search_timeout_seconds=300,
        analysis_per_axis=10,
    )
    return skill, searcher, builder, slack


def test_submit_runs_background_and_delivers_to_request_thread() -> None:
    store = ProposalJobStore(table_name="", memory={})
    launcher = _GateThreadLauncher()
    uploader = _RecordingUploader()
    skill, searcher, builder, slack = _build(store=store, launcher=launcher, uploader=uploader)
    status = OmiyageReportStatusSkill(store=store, retry_after_seconds=30)

    accepted = skill.run(_input(), _ctx())
    assert accepted.status == "queued"
    assert accepted.job_id.startswith("omy_")

    queued = status.run(OmiyageReportStatusInput(job_id=accepted.job_id), _ctx())
    assert queued.status == "queued"
    assert queued.retry_after_seconds == 30

    launcher.gate.set()
    assert launcher.finished.wait(timeout=10)

    done = status.run(OmiyageReportStatusInput(job_id=accepted.job_id), _ctx())
    assert done.status == "done"
    assert done.report_status == "ready"
    assert done.slack_delivered is True
    assert done.delivery_target == "thread"
    assert len(done.summary_lines) == 3
    assert done.next_step.startswith("次の一手")
    assert {axis.query for axis in done.axes} == {"ヘアケア", "エムキュア", "ラサーナ"}
    assert all(axis.fetched == 2 and not axis.failed for axis in done.axes)

    # 検索は各軸1回。明示 search_depth=120 でも dispatcher 上限（30）へ clamp されて送られる
    # （120 を送ると Lambda が n_per_kw を拒否して全軸 TIKTOK_MEDIA_JOB_FAILED になる）
    assert [call["query"] for call in searcher.calls] == ["ヘアケア", "エムキュア", "ラサーナ"]
    assert all(call["max_videos"] == TIKTOK_N_PER_KW_MAX for call in searcher.calls)
    assert all(1 <= call["max_videos"] <= TIKTOK_N_PER_KW_MAX for call in searcher.calls)
    assert len(builder.calls) == 1

    # 配信は画像モード（正・コメント付き）→ 編集用（同スレッド・コメント無し）の2ファイル
    assert len(slack.uploads) == 2
    upload = slack.uploads[0]
    assert upload["channel"] == "C123"
    assert upload["thread_ts"] == "123.456"
    assert upload["title"] == "エムキュア様 TikTok検索データ確認資料"
    assert Path(upload["file_path"]).name.startswith("omiyage_fmt_")
    comment = upload["initial_comment"]
    assert isinstance(comment, str)
    assert "次の一手" in comment
    assert "修正はこのスレッドで再依頼" in comment  # 固定の修正ループ案内
    editable_upload = slack.uploads[1]
    assert editable_upload["channel"] == "C123"
    assert editable_upload["thread_ts"] == "123.456"
    assert editable_upload["initial_comment"] is None
    assert EDIT_MARKER in editable_upload["title"]
    assert EDIT_MARKER in Path(editable_upload["file_path"]).name

    # 動画解析: ブランド軸2本 + 競合軸2本 + Q5候補（一般KW軸2本）を実施しコストをジョブ記録へ
    analysis = done.video_analysis
    assert analysis.executed is True
    assert analysis.requested == 6
    assert analysis.analyzed == 6
    assert analysis.failed == 0
    assert analysis.cost_usd_estimate > 0

    # 計測JSON: 契約検証済み DeckPlan がレンダラ境界へ渡り、S3 相当へも保存される
    assert len(builder.plans) == 1
    plan = builder.plans[0]
    types = [slide.type for slide in plan.slide_plan]
    assert types[0] == "A" and types[-1] == "H"
    q_numbers = [slide.q_number for slide in plan.slide_plan]
    assert "Q3" in q_numbers  # 動画解析由来クラスタ収録
    assert "Q5" in q_numbers  # 実フレーム埋め込みの TOP カード収録
    assert plan.slide_plan[0].data is not None  # 表紙の段差サムネ2枚
    assert done.deck_plan_s3_uri.startswith("s3://fake-bucket/")
    assert done.audit_s3_uri.startswith("s3://fake-bucket/")
    stored_keys = sorted(uploader.objects)
    assert [key.rsplit("/", 1)[-1] for key in stored_keys] == ["audit.json", "deck_plan.json"]


def test_partial_failure_builds_deck_and_offers_rerun() -> None:
    store = ProposalJobStore(table_name="", memory={})
    launcher = _GateThreadLauncher(released=True)
    searcher = _FakeSearcher(fail_queries={"ラサーナ"})
    skill, _searcher, _builder, slack = _build(store=store, launcher=launcher, searcher=searcher)
    status = OmiyageReportStatusSkill(store=store)

    accepted = skill.run(_input(), _ctx())
    assert launcher.finished.wait(timeout=10)

    done = status.run(OmiyageReportStatusInput(job_id=accepted.job_id), _ctx())
    assert done.status == "done"
    assert done.report_status == "partial"
    failed = [axis for axis in done.axes if axis.failed]
    assert [axis.query for axis in failed] == ["ラサーナ"]
    assert failed[0].failure_code == "MEDIA_TIKTOK_BOT_WALL"
    # 場面4: 黙って消えず、部分結果+再実行の選択肢を文言で提示
    assert "一部の検索" in done.result_message
    assert "部分結果" in done.result_message
    assert "再実行" in done.result_message
    assert len(slack.uploads) == 2  # 部分結果でも資料（画像+編集用）は届ける


def test_all_axes_failed_marks_job_failed_with_choices() -> None:
    store = ProposalJobStore(table_name="", memory={})
    launcher = _GateThreadLauncher(released=True)
    searcher = _FakeSearcher(fail_queries={"ヘアケア", "エムキュア", "ラサーナ"})
    skill, _searcher, builder, slack = _build(store=store, launcher=launcher, searcher=searcher)
    status = OmiyageReportStatusSkill(store=store)

    accepted = skill.run(_input(), _ctx())
    assert launcher.finished.wait(timeout=10)

    failed = status.run(OmiyageReportStatusInput(job_id=accepted.job_id), _ctx())
    assert failed.status == "failed"
    assert failed.error_code == "OMIYAGE_SEARCH_FAILED"
    assert "再依頼" in failed.message  # 選択肢の提示
    assert "検索語・競合名を変えて" in failed.message
    assert builder.calls == []
    assert slack.uploads == []


def test_thread_upload_failure_falls_back_to_dm() -> None:
    store = ProposalJobStore(table_name="", memory={})
    launcher = _GateThreadLauncher(released=True)
    slack = _FakeSlack(fail_channel_upload=True)
    skill, _searcher, _builder, slack = _build(store=store, launcher=launcher, slack=slack)
    status = OmiyageReportStatusSkill(store=store)

    accepted = skill.run(_input(), _ctx())
    assert launcher.finished.wait(timeout=10)

    done = status.run(OmiyageReportStatusInput(job_id=accepted.job_id), _ctx())
    assert done.status == "done"
    assert done.slack_delivered is True
    assert done.delivery_target == "dm"
    # スレッド1枚目で失敗 → DM へ切替え、DM には画像+編集用の2ファイルを届ける
    assert [upload["channel"] for upload in slack.uploads] == ["C123", "D999", "D999"]


def test_stale_running_job_fails_closed_as_mcp_restarted() -> None:
    store = ProposalJobStore(table_name="", memory={})
    job_id = "omy_" + "0" * 32
    store.create_job(job_id, {"kind": "omiyage_report"})
    assert store.mark_running(job_id)

    later = datetime.now(UTC) + timedelta(seconds=3600)
    status = OmiyageReportStatusSkill(
        store=store,
        stale_after_seconds=180,
        clock=lambda: later,
    )
    out = status.run(OmiyageReportStatusInput(job_id=job_id), _ctx())
    assert out.status == "failed"
    assert out.error_code == "MCP_RESTARTED"


def test_status_rejects_unknown_job() -> None:
    store = ProposalJobStore(table_name="", memory={})
    status = OmiyageReportStatusSkill(store=store)
    out = status.run(OmiyageReportStatusInput(job_id="omy_" + "f" * 32), _ctx())
    assert out.status == "failed"
    assert out.error_code == "JOB_NOT_FOUND"


def test_status_input_schema_rejects_non_omiyage_job_ids() -> None:
    with pytest.raises(ValueError):
        OmiyageReportStatusInput(job_id="pb_" + "a" * 32)
    with pytest.raises(ValueError):
        OmiyageReportStatusInput(job_id="omy_INVALID")
