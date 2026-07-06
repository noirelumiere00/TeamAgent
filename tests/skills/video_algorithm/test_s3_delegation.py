"""video_algorithm の取得委譲（tiktok_acquire S3 出力読み）の単体テスト。

boto3/S3 に触れず、TikTokS3Source の posts/download マッピングと、
VideoAlgorithmSkill._posts_to_metas の写像を検証する。
"""

from __future__ import annotations

import json

import pytest

from teamagent.adapters.tiktok_s3_source import TikTokS3Source
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

_PREFIX = "tiktok-acquire/tk_test/"
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


class _FakeBody:
    def __init__(self, b: bytes) -> None:
        self._b = b

    def read(self) -> bytes:
        return self._b


class _FakeS3:
    def __init__(self, objs: dict[str, bytes]) -> None:
        self._objs = objs

    def get_object(self, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        if Key not in self._objs:
            raise KeyError("NoSuchKey")
        return {"Body": _FakeBody(self._objs[Key])}


def _fake_src() -> TikTokS3Source:
    objs = {
        f"{_PREFIX}posts.normalized.json": json.dumps({"posts": [_POST]}).encode(),
        f"{_PREFIX}videos/manifest.json": json.dumps(
            {
                "items": [
                    {
                        "pid": "p0001",
                        "tiktok_url": _URL,
                        "downloaded": True,
                        "video_path": "videos/p0001.mp4",
                        "thumb_path": "thumbs/p0001.jpg",
                    }
                ]
            }
        ).encode(),
        f"{_PREFIX}videos/p0001.mp4": b"FAKE_MP4_BYTES" * 100,
    }
    src = TikTokS3Source(_PREFIX)
    src._s3 = lambda: _FakeS3(objs)  # type: ignore[method-assign]
    return src


def test_s3_source_posts_and_download() -> None:
    src = _fake_src()
    posts = src.posts()
    assert len(posts) == 1 and posts[0]["saves"] == 50
    data, mime = src.download(_URL)
    assert mime == "video/mp4" and len(data) > 1000
    # 未保存/不明URLは例外（スキル側で cover-only へ縮退）
    with pytest.raises(FileNotFoundError):
        src.download("https://www.tiktok.com/@x/video/999")


def test_posts_to_metas_mapping() -> None:
    skill = VideoAlgorithmSkill()
    metas = skill._posts_to_metas([_POST])
    assert len(metas) == 1
    m = metas[0]
    assert m.rank == 1
    assert m.url == _URL
    assert m.author == "u"  # account_id 優先
    assert m.follower_count == 100
    assert m.collect_count == 50  # saves
    assert m.play_count == 1000
    assert abs(m.engagement_rate - 6.5) < 1e-9  # eg_rate は既に%単位＝そのまま保持（審査所見R1）
    assert abs(m.save_rate() - 5.0) < 1e-9  # 50/1000*100


def test_search_uses_s3_searcher_override() -> None:
    # _search に searcher override を渡すと、それが使われる（self._searcher 非依存）
    skill = VideoAlgorithmSkill()
    sentinel = skill._posts_to_metas([_POST])

    def fake_searcher(q: str, n: int, rid: str) -> list:
        return sentinel

    out = skill._search("q", 5, "req", searcher=fake_searcher)
    assert out is sentinel
