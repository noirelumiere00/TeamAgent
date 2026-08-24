"""エンジン⇔FMTレンダラの統合（端から端）とジョブフローの順序・失敗分岐の固定。

- 端から端: submit → 検索実測 → 動画解析 → 計測JSON（DeckPlan） → **本物の**
  FMTレンダラ（HTML+編集用PPTX） → 画像モードPPTX（media worker 境界だけ stub）
  → 2ファイルのスレッド配信 → status done。
- フェイクは本番の失敗モードを再現する: media worker の network 参照拒否
  （``_EXTERNAL_HTML_REF``）を実物の正規表現で照合し、レンダラ出力が本番ゲートを
  通ることを証明する。
"""

from __future__ import annotations

import json
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from teamagent.adapters.media_job import MediaJobClient
from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.adapters.tiktok_scraper import (
    TikTokAuthor,
    TikTokSearchResult,
    TikTokVideo,
)
from teamagent.media.operations import _EXTERNAL_HTML_REF
from teamagent.skills.base import SkillContext
from teamagent.skills.omiyage_report.contract import (
    CTA_TEXT,
    EG_RATE_FOOTNOTE,
    VOICE_UNMEASURED_NOTE,
)
from teamagent.skills.omiyage_report.fmt.build import REVISION_NOTE
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
    _default_deck_builder,
)
from teamagent.skills.omiyage_report.video_analysis import (
    ClusterRules,
    OmiyageVideoAnalyzer,
    VisionVerdict,
)

from .fmt_fixtures import make_png_bytes

_FRAME = make_png_bytes(width=90, height=160)


class _ReleasedLauncher:
    """実 daemon thread を即時起動し、完了を待てるようにする。"""

    def __init__(self) -> None:
        self.finished = threading.Event()

    def __call__(self, target: Callable[[], None], name: str) -> None:
        def wrapped() -> None:
            try:
                target()
            finally:
                self.finished.set()

        threading.Thread(target=wrapped, name=name, daemon=True).start()


def _video(
    video_id: str,
    desc: str,
    *,
    author: str = "creator",
    followers: int = 5_000,
    plays: int = 10_000,
    hashtags: tuple[str, ...] = (),
) -> TikTokVideo:
    return TikTokVideo(
        id=video_id,
        url=f"https://www.tiktok.com/@{author}/video/{video_id}",
        desc=desc,
        create_time=1700000000,
        duration=15,
        cover_url="https://p16-sign.tiktokcdn.com/cover.jpeg",
        author=TikTokAuthor(unique_id=author, nickname=f"{author}名", follower_count=followers),
        play_count=plays,
        digg_count=plays // 50,
        comment_count=plays // 500,
        share_count=plays // 800,
        collect_count=plays // 200,
        hashtags=hashtags,
        music_title="original sound",
    )


@dataclass
class _KaoLikeSearcher:
    """花王実物相当のデータ形: 階層分布・#PR混在・キーワード3経路の実データ形。"""

    events: list[str] = field(default_factory=list)

    def __call__(self, query: str, **kwargs: Any) -> TikTokSearchResult:
        self.events.append(f"search:{query}")
        if query == "ヘアケア":
            videos = (
                _video("g1", "ヘアケアの正解 エムキュア推し", followers=800_000, plays=1_500_000),
                _video(
                    "g2",
                    "ラサーナ 使ってみた #PR",
                    followers=120_000,
                    plays=600_000,
                    hashtags=("PR", "ヘアケア"),
                ),
                _video(
                    "g3",
                    "ヘアケア ルーティン",
                    followers=60_000,
                    plays=90_000,
                    hashtags=("ヘアケア",),
                ),
                _video("g4", "美容師が語るヘアケア", followers=9_000, plays=40_000),
                _video("g5", "エムキュア 実演", author="mqure_fan", followers=3_000, plays=12_000),
                _video("g6", "髪質改善の話", followers=500, plays=800),
            )
        elif query == "エムキュア":
            videos = (
                _video(
                    "b1",
                    "エムキュアでヘアケア #PR",
                    followers=250_000,
                    plays=900_000,
                    hashtags=("PR",),
                ),
                _video("b2", "エムキュア 正直レビュー", followers=40_000, plays=200_000),
                _video(
                    "b3",
                    "エムキュア 成分解説",
                    followers=15_000,
                    plays=50_000,
                    hashtags=("ヘアケア",),
                ),
                _video("b4", "エムキュア 開封", followers=800, plays=3_000),
            )
        else:
            videos = (
                _video(
                    "c1",
                    "ラサーナ ヘアオイル #PR",
                    followers=300_000,
                    plays=700_000,
                    hashtags=("PR",),
                ),
                _video("c2", "ラサーナ レビュー", followers=25_000, plays=80_000),
                _video("c3", "ラサーナで髪質改善", followers=2_000, plays=9_000),
            )
        return TikTokSearchResult(query=query, search_type="keyword", videos=videos)


@dataclass
class _MediaClient:
    events: list[str]

    def acquire_video(
        self, url: str, *, request_fingerprint: str, **kwargs: Any
    ) -> tuple[bytes, str]:
        self.events.append("acquire")
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


def _vision(
    frames: list[tuple[float, bytes]], rules: ClusterRules, request_id: str
) -> VisionVerdict:
    return VisionVerdict(
        cluster=rules.vocabulary[0],
        telop_text="ヘアケアはこれ",
        cost_usd=0.01,
        model_id="test-model",
    )


def _analyzer_factory(events: list[str]) -> Callable[[str], OmiyageVideoAnalyzer]:
    def factory(request_id: str) -> OmiyageVideoAnalyzer:
        return OmiyageVideoAnalyzer(
            request_id=request_id,
            media_client_factory=lambda: _MediaClient(events),
            vision_caller=_vision,
            max_videos=30,
            concurrency=2,
            cost_cap_usd=5.0,
            media_cost_usd_per_video=0.005,
        )

    return factory


@dataclass
class _Uploader:
    objects: dict[str, bytes] = field(default_factory=dict)

    def __call__(self, key: str, body: bytes, content_type: str) -> str:
        self.objects[key] = body
        return f"s3://fake-bucket/{key}"


@dataclass
class _Slack:
    events: list[str] = field(default_factory=list)
    fail_all: bool = False
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
        # 配信時点のファイル実体を控える（workdir はジョブ完了時に消えるため）
        data = Path(file_path).read_bytes()
        self.events.append("upload")
        self.uploads.append(
            {
                "channel": channel,
                "filename": Path(file_path).name,
                "title": title,
                "initial_comment": initial_comment,
                "thread_ts": thread_ts,
                "bytes": data,
            }
        )
        return not self.fail_all

    async def lookup_user_id_by_email(self, email: str, request_id: str) -> str | None:
        return None if self.fail_all else "U999"

    async def open_dm(self, user_id: str, request_id: str) -> str | None:
        return "D999"


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="omiyage-integration-test",
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


def _run_job(skill: OmiyageReportSubmitSkill, launcher: _ReleasedLauncher) -> str:
    accepted = skill.run(_input(), _ctx())
    assert accepted.status == "queued"
    assert launcher.finished.wait(timeout=60)
    return accepted.job_id


# ---------------------------------------------------------------------------
# 端から端（本物の FMT レンダラ・media worker 境界だけ stub）
# ---------------------------------------------------------------------------


def test_end_to_end_engine_json_through_real_fmt_renderer(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_slides_to_pptx(self: MediaJobClient, html: str, **kwargs: Any) -> bytes:
        captured["html"] = html
        captured.update(kwargs)
        return b"PPTX-IMAGE"

    monkeypatch.setattr(MediaJobClient, "is_configured", staticmethod(lambda: True))
    monkeypatch.setattr(MediaJobClient, "__init__", lambda self: None)
    monkeypatch.setattr(MediaJobClient, "slides_to_pptx", fake_slides_to_pptx)

    store = ProposalJobStore(table_name="", memory={})
    launcher = _ReleasedLauncher()
    events: list[str] = []
    slack = _Slack(events=events)
    uploader = _Uploader()
    skill = OmiyageReportSubmitSkill(
        store=store,
        searcher=_KaoLikeSearcher(events=events),
        deck_builder=_default_deck_builder,  # 既定＝本物の FMT パイプライン
        slack=slack,
        thread_launcher=launcher,
        analyzer_factory=_analyzer_factory(events),
        plan_uploader=uploader,
        heartbeat_seconds=0,
        search_depth=120,
        analysis_per_axis=10,
    )
    job_id = _run_job(skill, launcher)

    done = OmiyageReportStatusSkill(store=store).run(
        OmiyageReportStatusInput(job_id=job_id), _ctx()
    )
    assert done.status == "done"
    assert done.report_status == "ready"
    assert done.slack_delivered is True
    assert done.delivery_target == "thread"

    # --- 計測JSON（S3 保存物）がレンダラ入力契約をそのまま通る = 界面の固定 ---
    plan_key = next(key for key in uploader.objects if key.endswith("deck_plan.json"))
    raw_plan = json.loads(uploader.objects[plan_key])
    content = validate_deck_content(raw_plan, load_fmt_spec())
    types = [slide.type for slide in content.slides]
    assert types == ["A", "B", "D", "D", "C", "C", "D", "E", "H"]

    # --- 本物のレンダラ出力（HTML）が media worker の本番ゲートを通る ---
    html = captured["html"]
    assert _EXTERNAL_HTML_REF.search(html) is None  # network 参照ゼロ（実物の正規表現）
    assert captured["width"] == 1920
    assert captured["height"] == 1080
    assert captured["device_scale_factor"] == 1
    assert VOICE_UNMEASURED_NOTE in html  # 便1制約行（誠実性ゲートの文言そのもの）
    assert CTA_TEXT in html  # H dark 結論バンドの CTA 固定文
    assert EG_RATE_FOOTNOTE in html  # U2 脚注が実際に描画される
    assert html.count("data:image/png;base64,") >= 7  # 表紙2 + Q5カード5（実フレーム埋め込み）

    # --- 配信: 画像モード（正）+ 編集用（併走）の2ファイルが同スレッドへ届く ---
    assert [upload["channel"] for upload in slack.uploads] == ["C123", "C123"]
    image_upload, editable_upload = slack.uploads
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert image_upload["filename"] == f"omiyage_fmt_{today}.pptx"
    assert image_upload["bytes"] == b"PPTX-IMAGE"
    assert editable_upload["filename"] == f"omiyage_fmt_{today}_{EDIT_MARKER}.pptx"
    assert editable_upload["initial_comment"] is None
    with zipfile.ZipFile(BytesIO(editable_upload["bytes"])) as archive:  # 実PPTX（zip）
        assert "ppt/presentation.xml" in archive.namelist()

    # --- 配信文: 要点3行 → 修正ループ案内（固定文） → 次の一手 の順序 ---
    comment = image_upload["initial_comment"]
    assert isinstance(comment, str)
    lines = comment.splitlines()
    assert len(lines) == 5
    assert lines[3] == REVISION_NOTE
    assert lines[4].startswith("次の一手")

    # ジョブ記録にも画像モード（正）のファイル名が残る
    row = store.get_job(job_id)
    assert row is not None
    result = json.loads(str(row["result_json"]))
    assert result["pptx_filename"] == f"omiyage_fmt_{today}.pptx"


# ---------------------------------------------------------------------------
# ジョブフローの順序と失敗時分岐
# ---------------------------------------------------------------------------


def _build_flow_skill(
    *,
    store: ProposalJobStore,
    launcher: _ReleasedLauncher,
    events: list[str],
    slack: _Slack,
    deck_builder: Any = None,
) -> OmiyageReportSubmitSkill:
    def default_builder(deck_plan_json: str, out_dir: str, request_id: str) -> tuple[str, str]:
        events.append("build")
        validate_deck_content(json.loads(deck_plan_json), load_fmt_spec())
        image = Path(out_dir) / "omiyage_fmt_x.pptx"
        image.write_bytes(b"PK-image")
        editable = Path(out_dir) / f"omiyage_fmt_x_{EDIT_MARKER}.pptx"
        editable.write_bytes(b"PK-editable")
        return str(image), str(editable)

    return OmiyageReportSubmitSkill(
        store=store,
        searcher=_KaoLikeSearcher(events=events),
        deck_builder=deck_builder or default_builder,
        slack=slack,
        thread_launcher=launcher,
        analyzer_factory=_analyzer_factory(events),
        plan_uploader=_Uploader(),
        heartbeat_seconds=0,
        search_depth=120,
        analysis_per_axis=10,
    )


def test_job_flow_order_search_then_analysis_then_build_then_deliver() -> None:
    store = ProposalJobStore(table_name="", memory={})
    launcher = _ReleasedLauncher()
    events: list[str] = []
    slack = _Slack(events=events)
    skill = _build_flow_skill(store=store, launcher=launcher, events=events, slack=slack)
    job_id = _run_job(skill, launcher)

    done = OmiyageReportStatusSkill(store=store).run(
        OmiyageReportStatusInput(job_id=job_id), _ctx()
    )
    assert done.status == "done"

    # 順序の固定: 検索（3軸）→ 動画解析 → レンダリング → 配信（2ファイル）
    searches = [index for index, event in enumerate(events) if event.startswith("search:")]
    acquires = [index for index, event in enumerate(events) if event == "acquire"]
    build = events.index("build")
    upload_indexes = [index for index, event in enumerate(events) if event == "upload"]
    assert len(searches) == 3
    assert acquires  # 解析が実行されている
    assert max(searches) < min(acquires) < build < min(upload_indexes)
    assert len(upload_indexes) == 2
    assert events.index("build") < upload_indexes[0]


def test_deck_build_failure_marks_job_failed_without_delivery() -> None:
    store = ProposalJobStore(table_name="", memory={})
    launcher = _ReleasedLauncher()
    events: list[str] = []
    slack = _Slack(events=events)

    def broken_builder(deck_plan_json: str, out_dir: str, request_id: str) -> tuple[str, str]:
        raise RuntimeError("FMT_RENDER_FAILED: glyph gate")

    skill = _build_flow_skill(
        store=store, launcher=launcher, events=events, slack=slack, deck_builder=broken_builder
    )
    job_id = _run_job(skill, launcher)

    failed = OmiyageReportStatusSkill(store=store).run(
        OmiyageReportStatusInput(job_id=job_id), _ctx()
    )
    assert failed.status == "failed"
    assert failed.error_code == "OMIYAGE_BUILD_FAILED"
    assert "再依頼" in failed.message  # 黙って消えない（選択肢の提示）
    assert slack.uploads == []  # 失敗時に中途半端なファイルを配らない


def test_total_delivery_failure_is_done_with_disclosure() -> None:
    store = ProposalJobStore(table_name="", memory={})
    launcher = _ReleasedLauncher()
    events: list[str] = []
    slack = _Slack(events=events, fail_all=True)
    skill = _build_flow_skill(store=store, launcher=launcher, events=events, slack=slack)
    job_id = _run_job(skill, launcher)

    done = OmiyageReportStatusSkill(store=store).run(
        OmiyageReportStatusInput(job_id=job_id), _ctx()
    )
    # 生成は完了扱い・配信失敗は文言で開示（黙って握りつぶさない）
    assert done.status == "done"
    assert done.slack_delivered is False
    assert done.delivery_target == "none"
    assert "添付に失敗" in done.result_message
    assert "再生成・再添付" in done.result_message
