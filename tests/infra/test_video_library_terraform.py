"""Thumbnails live in the always-present raw_files bucket under a non-expiring prefix."""

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
    block = text[text.index('sid       = "VideoLibraryThumbnails"') :].split("\n  }")[0]
    assert f"actions   = {actions}" in block
    assert 'resources = ["${aws_s3_bucket.raw_files.arn}/video-library/thumbs/*"]' in block
    # media_jobs は media 機能 OFF の本番に存在しない（2026-10-30 head-bucket 404）。条件付きにしない
    assert "media_jobs" not in block and "tk_enabled" not in block
    assert 'resource "aws_s3_bucket"' not in text


@pytest.mark.parametrize("filename", ["ingest_schedule.tf", "connect_web.tf"])
def test_library_bucket_env_is_unconditional(filename: str) -> None:
    text = (ROOT / filename).read_text()
    line = '{ name = "VIDEO_LIBRARY_S3_BUCKET", value = aws_s3_bucket.raw_files.bucket },'
    assert line in text
    before = text[: text.index(line)].splitlines()[-1]
    assert "tk_enabled" not in before


def test_library_thumbnails_do_not_expire() -> None:
    text = (ROOT / "lambda_iam.tf").read_text()
    lifecycle = text[
        text.index('resource "aws_s3_bucket_lifecycle_configuration" "raw_files"') :
    ].split("\nresource ")[0]
    assert "video-library/" not in lifecycle
