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


# ---- 複数 KW 入りの取得ジョブ: 分析する KW の投稿だけを読む ------------------------------
# tiktok_acquire は KW の数や本数によって、1ジョブに複数 KW をまとめることがある。
# 先頭から読むと別 KW の上位投稿でこの KW を分析してしまう（黙って別の結果になる）。


def _kw_post(kw: str, rank: int) -> dict[str, Any]:
    return {
        **_POST,
        "id": f"{kw}-{rank}",
        "kw": kw,
        "rank_display": rank,
        "url": f"https://www.tiktok.com/@u/video/{hashlib.sha256(f'{kw}:{rank}'.encode()).hexdigest()[:12]}",
    }


class _PostsOnlyMediaClient:
    """posts.json だけを返す取得結果（manifest なし＝指標だけの取得と同じ形）。"""

    def __init__(self, posts: list[dict[str, Any]]) -> None:
        body = json.dumps({"posts": posts}, ensure_ascii=False).encode()
        self._ref = _ref("posts.normalized.json", body, "application/json")
        self._body = body

    def get_result(
        self,
        job_id: str,
        *,
        deadline_epoch_s: int,
        expected_audit_principal_hash: str | None = None,
    ) -> MediaJobResult:
        return MediaJobResult(
            job_id=job_id,
            status="done",
            artifacts=(MediaArtifact(name="posts.json", object=self._ref),),
        )

    def download(self, ref: S3ObjectRef, *, deadline_epoch_s: int) -> bytes:
        return self._body


def _run_with_job(
    monkeypatch: pytest.MonkeyPatch,
    posts: list[dict[str, Any]],
    query: str,
) -> tuple[Any, list[list[str]]]:
    """本物の TikTokS3Source で run し、分析に渡った上位ボードの URL を記録する。

    深掘り（DL・Gemini）に進ませないよう、記録したあと空の結果を返して打ち切る。
    """

    from teamagent.adapters import tiktok_s3_source
    from teamagent.skills.base import SkillContext
    from teamagent.skills.video_algorithm.schema import VideoAlgorithmInput

    real_source = tiktok_s3_source.TikTokS3Source
    client = _PostsOnlyMediaClient(posts)
    monkeypatch.setattr(
        tiktok_s3_source,
        "TikTokS3Source",
        lambda job_id, *, audit_principal_hash: real_source(
            job_id,
            audit_principal_hash=audit_principal_hash,
            client=client,  # type: ignore[arg-type]
            clock=lambda: 100,
        ),
    )
    skill = VideoAlgorithmSkill(gemini=None)
    boards: list[list[str]] = []
    original_search = skill._search

    def _spy(query: str, n: int, request_id: str, searcher: Any = None) -> list[Any]:
        boards.append([meta.url for meta in original_search(query, n, request_id, searcher)])
        return []

    monkeypatch.setattr(skill, "_search", _spy)
    out = skill.run(
        VideoAlgorithmInput(query=query, acquire_job_id=_JOB_ID),
        ctx=SkillContext(metadata={"user_email": "a@vectorinc.co.jp"}),
    )
    return out, boards


def test_multi_keyword_job_reads_only_the_queried_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seven = [_kw_post("セブン", rank) for rank in (1, 2)]
    famima = [_kw_post("ファミマ", rank) for rank in (1, 2)]

    _out, boards = _run_with_job(monkeypatch, seven + famima, "ファミマ")

    assert boards == [[post["url"] for post in famima]]


def test_keyword_match_absorbs_width_case_and_space() -> None:
    from teamagent.skills.video_algorithm.skill import _posts_for_query

    posts = [_kw_post("ＵＮＩＱＬＯ 新作", 1), _kw_post("GU", 1)]

    selected, job_keywords = _posts_for_query(posts, " uniqlo  新作 ")

    assert [post["kw"] for post in selected] == ["ＵＮＩＱＬＯ 新作"]
    assert job_keywords == ["ＵＮＩＱＬＯ 新作", "GU"]


def test_keyword_absent_from_multi_keyword_job_is_not_substituted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts = [_kw_post("セブン", 1), _kw_post("ファミマ", 1)]

    out, boards = _run_with_job(monkeypatch, posts, "ローソン")

    assert boards == [[]]  # 別 KW の投稿で代用しない
    assert out.videos == []
    assert "「ローソン」は渡された取得結果に入っていません" in out.slack_summary
    assert "入っているKW: セブン・ファミマ" in out.slack_summary
    # slack_summary は利用者へそのまま出る。引数名・内部IDは出さない（SOUL の禁止語）。
    assert "job_id" not in out.slack_summary and _JOB_ID not in out.slack_summary


def test_single_keyword_job_still_reads_all_posts_when_phrasing_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1KW のジョブは従来どおり（依頼の言い回しがジョブの KW と少し違っても読む）。
    posts = [_kw_post("セブン", rank) for rank in (1, 2, 3)]

    _out, boards = _run_with_job(monkeypatch, posts, "セブンイレブン 新作")

    assert boards == [[post["url"] for post in posts]]
