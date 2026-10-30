"""Thumbnail access is confined to the persistent prefix of the existing media bucket."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "infra/terraform"


@pytest.mark.parametrize(
    "filename,actions",
    [
        ("ingest_schedule.tf", '["s3:PutObject", "s3:GetObject"]'),
        ("connect_web.tf", '["s3:GetObject"]'),
    ],
)
def test_library_thumbnail_iam(filename: str, actions: str) -> None:
    text = (ROOT / filename).read_text()
    block = text[text.index('sid       = "VideoLibraryThumbnails"') :].split("\n    }")[0]
    assert f"actions   = {actions}" in block
    assert 'resources = ["${aws_s3_bucket.media_jobs[0].arn}/video-library/thumbs/*"]' in block
    assert "for_each = local.tk_enabled == 1 ? [1] : []" in text
    assert 'resource "aws_s3_bucket"' not in text


def test_library_thumbnails_do_not_expire_with_job_artifacts() -> None:
    text = (ROOT / "tiktok_acquire.tf").read_text()
    lifecycle = text[
        text.index('resource "aws_s3_bucket_lifecycle_configuration" "media_jobs"') :
    ].split("\nresource ")[0]
    assert 'prefix = "media-jobs/"' in lifecycle
    assert "video-library/" not in lifecycle
