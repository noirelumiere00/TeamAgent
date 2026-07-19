"""Core routes must delegate media work or fail before touching stripped binaries."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from teamagent.adapters.media_job import MediaJobError
from teamagent.adapters.tiktok_scraper import TikTokScrapeError, search_tiktok
from teamagent.adapters.video_download import VideoDownloadError, download_video
from teamagent.adapters.video_proxy import VideoProxyError, ensure_under_limit
from teamagent.skills.proposal_campaign.adapters import default_normalizer
from teamagent.skills.proposal_deck.contract import ComposerOutput
from teamagent.skills.proposal_deck.skill import ProposalDeckSkill

ROOT = Path(__file__).resolve().parents[2]
MEDIA_ENV = ("MEDIA_TASK_QUEUE", "MEDIA_JOBS_TABLE", "MEDIA_JOB_BUCKET")


@pytest.fixture(autouse=True)
def _unconfigured_core(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*MEDIA_ENV, "TEAMAGENT_LOCAL_MEDIA_RUNTIME"):
        monkeypatch.delenv(name, raising=False)


def test_video_download_fails_before_importing_ytdlp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported_ytdlp = False
    original_import = __import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        nonlocal imported_ytdlp
        if name == "yt_dlp":
            imported_ytdlp = True
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    with pytest.raises(VideoDownloadError, match="VIDEO_MEDIA_JOB_NOT_CONFIGURED"):
        download_video("https://www.youtube.com/watch?v=BaW_jenozKc")
    assert imported_ytdlp is False


def test_video_transform_fails_before_looking_for_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "teamagent.adapters.video_proxy.shutil.which",
        lambda _name: (_ for _ in ()).throw(AssertionError("ffmpeg lookup reached")),
    )
    with pytest.raises(VideoProxyError, match="VIDEO_MEDIA_JOB_NOT_CONFIGURED"):
        ensure_under_limit(b"video", "video/mp4", limit_mb=0)


def test_tiktok_search_fails_before_resolving_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teamagent.adapters.tiktok_scraper._node_bin",
        lambda: (_ for _ in ()).throw(AssertionError("node lookup reached")),
    )
    with pytest.raises(TikTokScrapeError, match="TIKTOK_MEDIA_JOB_NOT_CONFIGURED"):
        search_tiktok("安全な検索語")


def test_proposal_render_and_image_normalization_fail_at_media_boundary(
    tmp_path: Path,
) -> None:
    with pytest.raises(MediaJobError, match="MEDIA_JOB_NOT_CONFIGURED"):
        ProposalDeckSkill._render_pptx(
            ComposerOutput.model_construct(placeholders={}),
            tmp_path / "missing-template.pptx",
            tmp_path / "out.pptx",
            request_id="request",
        )
    with pytest.raises(MediaJobError, match="MEDIA_JOB_NOT_CONFIGURED"):
        default_normalizer(b"\x89PNG\r\n\x1a\npayload")


def test_core_skill_modules_have_no_top_level_heavy_media_imports() -> None:
    paths = (
        ROOT / "src/teamagent/skills/proposal_deck/skill.py",
        ROOT / "src/teamagent/skills/proposal_campaign/skill.py",
        ROOT / "src/teamagent/skills/video_algorithm/skill.py",
    )
    forbidden = {"pptx", "playwright", "weasyprint", "yt_dlp"}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        top_level = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        imported = {alias.name.split(".", 1)[0] for node in top_level for alias in node.names}
        assert forbidden.isdisjoint(imported), (path, forbidden & imported)


def test_iac_injects_complete_media_service_contract_atomically() -> None:
    fargate = (ROOT / "infra/terraform/fargate.tf").read_text(encoding="utf-8")
    media = (ROOT / "infra/terraform/tiktok_acquire.tf").read_text(encoding="utf-8")
    for name in (
        "MEDIA_TASK_QUEUE",
        "MEDIA_JOBS_TABLE",
        "MEDIA_JOB_BUCKET",
        "MEDIA_ARTIFACT_TTL_SECONDS",
    ):
        assert f'{{ name = "{name}"' in fargate
    assert "local.media_enabled == 1" in fargate
    assert "role   = aws_iam_role.mcp_task.name" in media
    assert 'actions   = ["sqs:SendMessage"]' in media
    assert 'actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]' in media
    assert 'sid = "S3JobArtifactsRead"' in media
    assert '"s3:GetObjectVersion"' in media
    assert 'sid = "S3JobInputsWrite"' in media
    assert "media-jobs/*/input/*" in media
    assert "MEDIA_ARTIFACT_TTL_SECONDS = tostring(var.media_artifact_ttl_seconds)" in media
    assert "!var.enable_scrape_tools || local.media_enabled == 1" in fargate
    assert "hardened core contains no browser/ffmpeg/yt-dlp fallback" in fargate
