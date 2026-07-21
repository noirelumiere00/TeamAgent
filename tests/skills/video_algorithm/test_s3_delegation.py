"""Owner-bound TikTok acquisition delegation tests."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from teamagent.adapters.tiktok_s3_source import TikTokS3Source
from teamagent.media.contracts import MediaArtifact, MediaJobResult, S3ObjectRef
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

_JOB_ID = "tk_0123456789ab"
_AUDIT_HASH = "a" * 64
_URL = "https://www.tiktok.com/@u/video/1"
_POST = {
    "id": "p0001",
    "rank_display": 1,
    "url": _URL,
    "account_id": "u",
    "account_name": "U",
    "followers": 100,
    "title": "テスト動画",
    "plays": 1000,
    "likes": 10,
    "shares": 2,
    "comments": 3,
    "saves": 50,
    "eg_rate": 6.5,
}


def _ref(name: str, body: bytes, content_type: str) -> S3ObjectRef:
    return S3ObjectRef(
        bucket="teamagent-media-test",
        key=f"media-jobs/{_JOB_ID}/attempts/1/attempt/output/{name}",
        version_id=f"version-{name.replace('/', '-')}",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        content_type=content_type,
    )


class _FakeMediaClient:
    def __init__(self) -> None:
        posts = json.dumps({"posts": [_POST]}).encode()
        manifest = json.dumps(
            {
                "items": [
                    {
                        "pid": "p0001",
                        "tiktok_url": _URL,
                        "downloaded": True,
                    }
                ]
            }
        ).encode()
        video = b"FAKE_MP4_BYTES" * 100
        self.refs = {
            "posts.json": _ref("posts.normalized.json", posts, "application/json"),
            "manifest.json": _ref("videos/manifest.json", manifest, "application/json"),
            "video-p0001": _ref("videos/p0001.mp4", video, "video/mp4"),
        }
        self.bodies = {
            ref.version_id: body
            for ref, body in (
                (self.refs["posts.json"], posts),
                (self.refs["manifest.json"], manifest),
                (self.refs["video-p0001"], video),
            )
        }
        self.owner_reads: list[tuple[str, str | None]] = []
        self.downloaded_versions: list[str] = []

    def get_result(
        self,
        job_id: str,
        *,
        deadline_epoch_s: int,
        expected_audit_principal_hash: str | None = None,
    ) -> MediaJobResult:
        assert deadline_epoch_s == 130
        self.owner_reads.append((job_id, expected_audit_principal_hash))
        return MediaJobResult(
            job_id=job_id,
            status="done",
            artifacts=tuple(
                MediaArtifact(name=name, object=ref) for name, ref in self.refs.items()
            ),
        )

    def download(self, ref: S3ObjectRef, *, deadline_epoch_s: int) -> bytes:
        assert deadline_epoch_s == 130
        self.downloaded_versions.append(ref.version_id)
        return self.bodies[ref.version_id]


def _fake_src() -> tuple[TikTokS3Source, _FakeMediaClient]:
    client = _FakeMediaClient()
    source = TikTokS3Source(
        _JOB_ID,
        audit_principal_hash=_AUDIT_HASH,
        client=client,  # type: ignore[arg-type]
        clock=lambda: 100,
    )
    return source, client


def test_s3_source_binds_owner_and_exact_artifact_versions() -> None:
    source, client = _fake_src()
    posts = source.posts()
    assert len(posts) == 1 and posts[0]["saves"] == 50
    data, mime = source.download(_URL)
    assert mime == "video/mp4" and len(data) > 1000
    assert client.owner_reads == [(_JOB_ID, _AUDIT_HASH)]
    assert client.downloaded_versions == [
        client.refs["posts.json"].version_id,
        client.refs["manifest.json"].version_id,
        client.refs["video-p0001"].version_id,
    ]
    with pytest.raises(FileNotFoundError):
        source.download("https://www.tiktok.com/@x/video/999")


def test_s3_source_rejects_unscoped_job_or_owner() -> None:
    client = _FakeMediaClient()
    with pytest.raises(ValueError, match="job ID"):
        TikTokS3Source(
            "media-jobs/arbitrary-prefix",
            audit_principal_hash=_AUDIT_HASH,
            client=client,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="audit principal"):
        TikTokS3Source(
            _JOB_ID,
            audit_principal_hash="attacker-selected",
            client=client,  # type: ignore[arg-type]
        )


def test_posts_to_metas_mapping() -> None:
    skill = VideoAlgorithmSkill()
    metas = skill._posts_to_metas([_POST])
    assert len(metas) == 1
    meta = metas[0]
    assert meta.rank == 1
    assert meta.url == _URL
    assert meta.author == "u"
    assert meta.follower_count == 100
    assert meta.collect_count == 50
    assert meta.play_count == 1000
    assert abs(meta.engagement_rate - 6.5) < 1e-9
    assert abs(meta.save_rate() - 5.0) < 1e-9


def test_search_uses_s3_searcher_override() -> None:
    skill = VideoAlgorithmSkill()
    sentinel = skill._posts_to_metas([_POST])

    def fake_searcher(query: str, count: int, request_id: str) -> list[Any]:
        del query, count, request_id
        return sentinel

    assert skill._search("q", 5, "req", searcher=fake_searcher) is sentinel
