"""tiktok_acquire / tiktok_acquire_status の単体テスト（store をモック）。

AWS(SQS/DynamoDB/S3)に触れず、submit投函と status整形の配線を検証する。
"""

from __future__ import annotations

import hashlib
from typing import Any

from teamagent.skills.base import SkillContext
from teamagent.skills.tiktok_acquire.schema import (
    TikTokAcquireInput,
    TikTokAcquireStatusInput,
)
from teamagent.skills.tiktok_acquire.skill import (
    TikTokAcquireSkill,
    TikTokAcquireStatusSkill,
)


class _FakeStore:
    def __init__(self, submit_ok: bool = True, status: dict[str, Any] | None = None) -> None:
        self._submit_ok = submit_ok
        self._status = status
        self.last_spec: dict[str, Any] | None = None
        self.status_audit_hash: str | None = None

    def submit(self, spec: dict[str, Any]) -> bool:
        self.last_spec = spec
        return self._submit_ok

    def get_status(
        self,
        job_id: str,
        *,
        audit_principal_hash: str,
    ) -> dict[str, Any] | None:
        self.status_audit_hash = audit_principal_hash
        return self._status


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="req-test", user_id="U123", metadata={"user_email": "a@vectorinc.co.jp"}
    )


def test_acquire_submits_and_returns_job_id() -> None:
    store = _FakeStore(submit_ok=True)
    skill = TikTokAcquireSkill(store=store)  # type: ignore[arg-type]
    out = skill.run(
        TikTokAcquireInput(
            keywords=["コンビニスイーツ", "セブンスイーツ"],
            n_per_kw=30,
            videos_per_kw=6,
            sort="save_rate",
            client_name="セブンイレブン",
            competitors=["ローソン", "ファミマ"],
            industry="コンビニ",
        ),
        _ctx(),
    )
    assert out.status == "queued"
    assert out.job_id.startswith("tk_")
    assert out.poll_after_s == 75
    # 投函specの中身
    spec = store.last_spec
    assert spec is not None
    assert spec["keywords"] == ["コンビニスイーツ", "セブンスイーツ"]
    assert spec["n_per_kw"] == 30
    assert spec["videos_per_kw"] == 6
    assert spec["sort"] == "save_rate"
    assert spec["audit_principal_hash"] == hashlib.sha256(b"a@vectorinc.co.jp").hexdigest()
    assert "requested_by" not in spec
    assert len(spec["request_fingerprint"]) == 64
    assert spec["client"]["client"] == "セブンイレブン"
    assert spec["client"]["competitors"] == ["ローソン", "ファミマ"]
    assert spec["client"]["industry"] == "コンビニ"


def test_acquire_clamps_via_schema() -> None:
    # videos_per_kw は schema で 0..10、n_per_kw は 1..30 に制約される
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TikTokAcquireInput(keywords=["x"], n_per_kw=31)
    with pytest.raises(ValidationError):
        TikTokAcquireInput(keywords=["x"], videos_per_kw=11)
    with pytest.raises(ValidationError):
        TikTokAcquireInput(keywords=[])  # 1件以上必須


def test_acquire_submit_failure_returns_failed() -> None:
    store = _FakeStore(submit_ok=False)
    skill = TikTokAcquireSkill(store=store)  # type: ignore[arg-type]
    out = skill.run(TikTokAcquireInput(keywords=["x"]), _ctx())
    assert out.status == "failed"
    assert out.poll_after_s == 0


def test_status_running() -> None:
    store = _FakeStore(status={"status": "running", "progress": {"kw_done": 1, "kw_total": 2}})
    skill = TikTokAcquireStatusSkill(store=store)  # type: ignore[arg-type]
    out = skill.run(TikTokAcquireStatusInput(job_id="tk_abc"), _ctx())
    assert out.status == "running"
    assert out.progress == {"kw_done": 1, "kw_total": 2}
    assert store.status_audit_hash == hashlib.sha256(b"a@vectorinc.co.jp").hexdigest()


def test_status_done_maps_videos_and_urls() -> None:
    store = _FakeStore(
        status={
            "status": "done",
            "counts": {"kw": 2, "posts": 60, "videos": 12},
            "s3_prefix": "tiktok-acquire/tk_abc/",
            "posts_json_url": "https://signed/posts",
            "manifest_url": "https://signed/manifest",
            "videos": [
                {
                    "pid": "p0001",
                    "kw": "x",
                    "downloaded": True,
                    "s3_key": "k1",
                    "url": "https://signed/v1",
                }
            ],
        }
    )
    skill = TikTokAcquireStatusSkill(store=store)  # type: ignore[arg-type]
    out = skill.run(TikTokAcquireStatusInput(job_id="tk_abc"), _ctx())
    assert out.status == "done"
    assert out.counts == {"kw": 2, "posts": 60, "videos": 12}
    assert out.posts_json_url == "https://signed/posts"
    assert out.manifest_url == "https://signed/manifest"
    assert len(out.videos) == 1
    assert out.videos[0]["s3_key"] == "k1"  # 機械処理用のS3キーが残る


def test_status_unknown_when_not_found() -> None:
    store = _FakeStore(status=None)
    skill = TikTokAcquireStatusSkill(store=store)  # type: ignore[arg-type]
    out = skill.run(TikTokAcquireStatusInput(job_id="tk_missing"), _ctx())
    assert out.status == "unknown"
