"""tiktok_acquire_status の二段構え: worker(yt-dlp/browser) が落とせなかった動画を Apify で補完。

実体は共有ヘルパー ``adapters.tiktok_video_fallback.fill_missing_videos``
（video_algorithm と共用）。本モジュールは「status(done) の videos[] から不足を決める →
補完結果を videos[] に合流する」だけを担う。

- 発火は env ``USE_TIKTOK_APIFY_FALLBACK=1`` の opt-in
  （既定 OFF＝従来出力と 1 バイトも変わらない）。
- 不足の定義: 台帳の当初要求（videos_per_kw）× KW − worker が落とせた本数。候補は表示順で
  ``downloaded=False`` の投稿（worker の sort=save_rate/recent の選抜順は台帳から再現できないため
  表示順で補う・既知の制約）。1ジョブ上限 = 不足本数（最大 videos_per_kw × KW・env で更に頭打ち）。
- 冪等 / fail-open / allowlist / CostGuard / 出所（acquired_via）は共有ヘルパーの規律に従う。
"""

from __future__ import annotations

import copy
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from teamagent.adapters.apify_client import tiktok_post_url_allowed
from teamagent.adapters.tiktok_video_fallback import (
    ACQUIRED_VIA_APIFY,
    ACQUIRED_VIA_WORKER,
    ENV_FLAG,
    apify_fallback_enabled,
    fallback_deadline_s,
    fallback_max_videos,
    fill_missing_videos,
    safe_reason,
)

logger = structlog.get_logger(__name__)

_S3_MARGIN_S = 30
_SAFE_PID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


@dataclass(frozen=True)
class FallbackCandidate:
    pid: str
    kw: str
    url: str


def plan_fallback(
    videos: Sequence[dict[str, Any]],
    *,
    videos_per_kw: int,
    keywords: Sequence[str],
    hard_cap: int,
) -> list[FallbackCandidate]:
    """不足本数と候補（KW順 → 表示順・downloaded=False・allowlist 内URL）を決定論で決める。"""

    by_kw: dict[str, list[dict[str, Any]]] = {}
    for row in videos:
        by_kw.setdefault(str(row.get("kw") or ""), []).append(row)
    out: list[FallbackCandidate] = []
    for kw in keywords:
        rows = by_kw.get(kw, [])
        need = max(0, videos_per_kw - sum(1 for row in rows if row.get("downloaded")))
        for row in rows:
            if need <= 0:
                break
            if row.get("downloaded"):
                continue
            pid = str(row.get("pid") or "")
            url = str(row.get("tiktok_url") or "")
            if not _SAFE_PID.fullmatch(pid) or not url or not tiktok_post_url_allowed(url):
                continue
            out.append(FallbackCandidate(pid=pid, kw=kw, url=url))
            need -= 1
    cap = min(hard_cap, videos_per_kw * len(keywords))
    return out[: max(0, cap)]


class ApifyVideoFallback:
    """status(done) の videos[] を見て不足分だけ共有ヘルパーで補完し、videos[] に合流させる。"""

    def __init__(
        self,
        *,
        apify: Any | None = None,
        cost_guard: Any | None = None,
        media_client_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._apify = apify
        self._cost_guard = cost_guard
        self._media_client_factory = media_client_factory
        self._clock = clock

    def _get_media_client(self) -> Any:
        if self._media_client_factory is None:
            raise RuntimeError("media client factory is not configured")
        return self._media_client_factory()

    def apply(
        self,
        status: dict[str, Any],
        *,
        job_id: str,
        audit_principal_hash: str,
        request_id: str,
        user_email: str,
        log: Any,
    ) -> dict[str, Any]:
        """done の status dict に不足動画を合流させた新しい dict を返す（入力は変更しない）。"""

        if status.get("status") != "done":
            return status
        out = copy.deepcopy(status)
        videos: list[dict[str, Any]] = [
            dict(row) for row in (out.get("videos") or []) if isinstance(row, dict)
        ]
        for row in videos:
            row["acquired_via"] = ACQUIRED_VIA_WORKER if row.get("downloaded") else None
        out["videos"] = videos
        warnings: list[str] = list(out.get("warnings") or [])
        out["warnings"] = warnings

        deadline_s = fallback_deadline_s()
        hard_cap = fallback_max_videos()
        deadline_epoch_s = int(self._clock()) + deadline_s + _S3_MARGIN_S

        try:
            client = self._get_media_client()
            request = client.get_request(
                job_id,
                deadline_epoch_s=deadline_epoch_s,
                expected_audit_principal_hash=audit_principal_hash,
            )
        except Exception as exc:
            warnings.append(f"APIFY_FALLBACK_SKIPPED:REQUEST_UNAVAILABLE:{safe_reason(exc)}")
            return out
        operation = getattr(request, "operation", None)
        videos_per_kw = int(getattr(operation, "videos_per_kw", 0) or 0)
        keywords = [str(kw) for kw in (getattr(operation, "keywords", ()) or ())]
        if request is None or videos_per_kw <= 0 or not keywords:
            warnings.append("APIFY_FALLBACK_SKIPPED:NO_TARGET")
            return out

        candidates = plan_fallback(
            videos, videos_per_kw=videos_per_kw, keywords=keywords, hard_cap=hard_cap
        )
        if not candidates:
            return out
        by_pid = {str(row.get("pid") or ""): row for row in videos}

        outcome = fill_missing_videos(
            job_id,
            {candidate.pid: candidate.url for candidate in candidates},
            media_client=client,
            apify=self._apify,
            cost_guard=self._cost_guard,
            deadline_s=deadline_s,
            request_id=request_id,
            user_email=user_email,
            max_videos=hard_cap,
            clock=self._clock,
        )
        for staged in outcome.videos:
            target = by_pid.get(staged.key)
            if target is None:
                continue
            target["downloaded"] = True
            target["s3_key"] = staged.ref.key if staged.ref is not None else None
            target["url"] = staged.url
            target["acquired_via"] = ACQUIRED_VIA_APIFY
        warnings.extend(outcome.warnings)

        counts = dict(out.get("counts") or {})
        counts["videos"] = sum(1 for row in videos if row.get("downloaded"))
        counts["videos_apify"] = outcome.fetched + outcome.reused
        out["counts"] = counts
        log.info(
            "tiktok_apify_fallback_merged",
            job_id=job_id,
            candidates=len(candidates),
            fetched=outcome.fetched,
            reused=outcome.reused,
            est_cost_usd=round(outcome.est_cost_usd, 4),
        )
        return out


__all__ = [
    "ACQUIRED_VIA_APIFY",
    "ACQUIRED_VIA_WORKER",
    "ENV_FLAG",
    "ApifyVideoFallback",
    "FallbackCandidate",
    "apify_fallback_enabled",
    "plan_fallback",
]
