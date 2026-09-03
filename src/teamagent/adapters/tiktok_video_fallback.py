"""TikTok 動画実体の二段構え（共有ヘルパー）: 取得経路の失敗分を mcp 側 Apify で補完する。

呼び出し元（両方とも opt-in env ``USE_TIKTOK_APIFY_FALLBACK=1``・既定 OFF＝従来挙動のまま）:
  (a) tiktok_acquire_status — media worker(yt-dlp → browser) が落とせなかった videos[] の不足分
  (b) video_algorithm — 動画DL経路の失敗（``video_algorithm_fetch_failed``）でサムネ縮退する前

規律（設計_TikTok動画取得の二段構え 2026-09-03・設計A）:
- 境界を動かさない: Apify token / egress を持つ mcp だけが Apify に触る。media worker は不変。
- URL は tiktok.com 配下の canonical HTTPS だけを渡す
  （ここで落とし、apify_client 側でも fail-close）。
- 取得物は ``media-jobs/<job_id>/input/apify-<key>.mp4`` へ ``stage_bytes``
  （sha256・サイズ上限 30MB・content_type 検査）で置く。同じ job_id で再実行しても
  S3 に既にあれば再利用し Apify を二重実行しない。
- CostGuard の記帳は ``ApifyClient.run_actor_sync`` が行う
  （COST_APIFY_MONTHLY_USD で fail-closed）。
- fail-open だが記録: どの段で失敗しても例外を外へ出さず、warnings に理由コードだけを残す
  （URL・トークン等の生文字列は載せない）。
- 出所の明示: 呼び出し元は ``acquired_via``（worker | apify）を成果物メタに記録する。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog

from teamagent.adapters.apify_client import tiktok_post_url_allowed

logger = structlog.get_logger(__name__)

ENV_FLAG = "USE_TIKTOK_APIFY_FALLBACK"
ENV_DEADLINE = "TIKTOK_APIFY_FALLBACK_DEADLINE_S"
ENV_MAX_VIDEOS = "TIKTOK_APIFY_FALLBACK_MAX_VIDEOS"
# 実測（2026-09-02）: run ≈ 68s + KVS GET（3.4MB/本）。MCP ツール呼び出し 300s 天井の内側に収める。
DEADLINE_DEFAULT_S = 150
_DEADLINE_MIN_S = 30
_DEADLINE_MAX_S = 240
# videos_per_kw(≤10) × KW(≤10) の理論上限 100 本に対する費用の安全弁（20本 = $0.08/ジョブ）。
MAX_VIDEOS_DEFAULT = 20
_MAX_VIDEOS_CEILING = 100
MAX_VIDEO_BYTES = 30 * 1024 * 1024
PRESIGN_S = 10 * 60
# S3 操作の締切は Apify の壁時計予算 + presign/stage の余裕。
_S3_MARGIN_S = 30
STAGED_NAME_PREFIX = "apify-"

ACQUIRED_VIA_WORKER = "worker"
ACQUIRED_VIA_APIFY = "apify"

_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,60}")
# stage_bytes の name 文字集合（小文字英数・-._）に収まる識別子だけを受ける。
_SAFE_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}$")


def apify_fallback_enabled() -> bool:
    """opt-in フラグ（既定 OFF）。両経路（tiktok_acquire_status / video_algorithm）共通。"""

    return os.environ.get(ENV_FLAG, "").strip().lower() in {"1", "true", "yes"}


def _envint(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def fallback_deadline_s() -> int:
    return _envint(
        ENV_DEADLINE, DEADLINE_DEFAULT_S, minimum=_DEADLINE_MIN_S, maximum=_DEADLINE_MAX_S
    )


def fallback_max_videos() -> int:
    return _envint(ENV_MAX_VIDEOS, MAX_VIDEOS_DEFAULT, minimum=0, maximum=_MAX_VIDEOS_CEILING)


def safe_reason(exc: BaseException) -> str:
    """警告に残す失敗理由（コードだけ・URL/トークン等の生文字列は載せない）。"""

    match = _REASON_CODE.match(str(exc))
    if match is not None:
        return match.group(0)
    return type(exc).__name__


def fallback_job_id(request_fingerprint: str) -> str:
    """MediaJobClient と同じ ``mj_<24hex>`` 形の job_id（stage_bytes の job_id 検査に通る）。"""

    digest = hashlib.sha256(request_fingerprint.encode("utf-8")).hexdigest()
    return f"mj_{digest[:24]}"


def staged_name(key: str) -> str:
    return f"{STAGED_NAME_PREFIX}{key}.mp4"


@dataclass(frozen=True)
class StagedVideo:
    """補完できた1本。``body`` は keep_body=True で取得直後のみ（S3 再利用時は None）。"""

    key: str
    post_url: str
    name: str
    body: bytes | None
    ref: Any | None  # S3ObjectRef（media_client 無しの時は None）
    url: str | None  # 署名URL（presign 失敗時は None）
    reused: bool
    content_type: str = "video/mp4"


@dataclass
class FallbackOutcome:
    """fill_missing_videos の結果。warnings は呼び出し元の status/ログへそのまま流せる。"""

    videos: list[StagedVideo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    est_cost_usd: float = 0.0
    requested: int = 0
    reused: int = 0
    fetched: int = 0


def _default_apify(cost_guard: Any | None) -> Any:
    from teamagent.adapters.apify_client import ApifyClient
    from teamagent.adapters.cost_guard import CostGuard

    ledger = cost_guard if cost_guard is not None else CostGuard.from_env()
    return ApifyClient.from_env(ledger=ledger)


def _presign(media_client: Any, ref: Any, *, deadline_epoch_s: int, presign_s: int) -> str | None:
    try:
        return str(
            media_client.presign_get(ref, deadline_epoch_s=deadline_epoch_s, expires_s=presign_s)
        )
    except Exception:
        return None


def fill_missing_videos(
    job_id: str,
    post_urls_missing: Mapping[str, str],
    *,
    media_client: Any | None,
    apify: Any | None = None,
    cost_guard: Any | None = None,
    deadline_s: int | None = None,
    request_id: str = "apify-fallback",
    user_email: str = "",
    max_videos: int | None = None,
    presign_s: int = PRESIGN_S,
    keep_body: bool = False,
    clock: Callable[[], float] = time.time,
) -> FallbackOutcome:
    """不足分 ``{key: 投稿URL}`` を Apify で取得し、S3(media-jobs/<job_id>/input/) へ置いて返す。

    - ``media_client``: MediaJobClient（None なら S3 に置かず bytes だけ返す＝ローカル runtime）。
    - ``apify``: ApifyClient（None なら env から生成。``cost_guard`` はその時の台帳）。
    - ``deadline_s`` / ``max_videos``: 未指定なら env（TIKTOK_APIFY_FALLBACK_DEADLINE_S /
      TIKTOK_APIFY_FALLBACK_MAX_VIDEOS）→ 既定 150s / 20本。
    - 例外は外へ出さない（fail-open）。理由は ``warnings`` に残す。
    """

    outcome = FallbackOutcome()
    deadline = fallback_deadline_s() if deadline_s is None else max(1, int(deadline_s))
    cap = fallback_max_videos() if max_videos is None else max(0, int(max_videos))
    deadline_epoch_s = int(clock()) + deadline + _S3_MARGIN_S

    wanted: list[tuple[str, str]] = []
    for raw_key, raw_url in post_urls_missing.items():
        key, url = str(raw_key), str(raw_url).strip()
        if not _SAFE_KEY.fullmatch(key):
            outcome.warnings.append(f"{key[:40]}:APIFY_FALLBACK_SKIPPED:KEY_INVALID")
            continue
        if not tiktok_post_url_allowed(url):
            outcome.warnings.append(f"{key}:APIFY_FALLBACK_SKIPPED:URL_NOT_ALLOWED")
            continue
        if len(wanted) >= cap:
            outcome.warnings.append(f"{key}:APIFY_FALLBACK_SKIPPED:CAP")
            continue
        wanted.append((key, url))
    outcome.requested = len(wanted)
    if not wanted:
        return outcome

    # 冪等: 既に S3 にある分は再利用して Apify を叩かない。
    pending: list[tuple[str, str]] = []
    for key, url in wanted:
        if media_client is None:
            pending.append((key, url))
            continue
        try:
            existing = media_client.find_staged(
                job_id=job_id, name=staged_name(key), deadline_epoch_s=deadline_epoch_s
            )
        except Exception as exc:
            outcome.warnings.append(f"{key}:APIFY_FALLBACK_FAILED:S3_HEAD:{safe_reason(exc)}")
            continue
        if existing is None:
            pending.append((key, url))
            continue
        outcome.videos.append(
            StagedVideo(
                key=key,
                post_url=url,
                name=staged_name(key),
                body=None,
                ref=existing,
                url=_presign(
                    media_client, existing, deadline_epoch_s=deadline_epoch_s, presign_s=presign_s
                ),
                reused=True,
                content_type=str(getattr(existing, "content_type", "") or "video/mp4"),
            )
        )
        outcome.reused += 1

    if pending:
        try:
            client = apify if apify is not None else _default_apify(cost_guard)
            got, cost = client.tiktok_download_videos(
                [url for _key, url in pending],
                max_videos=len(pending),
                deadline_s=deadline,
                max_bytes_per_video=MAX_VIDEO_BYTES,
                request_id=request_id,
                user_email=user_email,
            )
        except Exception as exc:
            logger.warning(
                "tiktok_apify_fallback_failed",
                job_id=job_id,
                requested=len(pending),
                error=safe_reason(exc),
            )
            outcome.warnings.append(f"APIFY_FALLBACK_FAILED:{safe_reason(exc)}")
            got, cost = [], 0.0
        outcome.est_cost_usd = float(cost)
        key_by_url = {url: key for key, url in pending}
        done: set[str] = set()
        for item in got:
            matched = key_by_url.get(str(item.post_url))
            if matched is None or matched in done:
                continue
            key = matched
            body = bytes(item.body)
            content_type = str(getattr(item, "content_type", "") or "video/mp4")
            ref: Any | None = None
            signed_url: str | None = None
            if media_client is not None:
                # worker 取得物と同じ検査（sha256・サイズ上限・content_type）を stage_bytes で通す。
                # 通らなければ bytes も渡さない（検査を素通りする経路を作らない）。
                try:
                    ref = media_client.stage_bytes(
                        job_id=job_id,
                        name=staged_name(key),
                        body=body,
                        content_type=content_type,
                        deadline_epoch_s=deadline_epoch_s,
                        max_bytes=MAX_VIDEO_BYTES,
                    )
                except Exception as exc:
                    outcome.warnings.append(
                        f"{key}:APIFY_FALLBACK_FAILED:S3_STAGE:{safe_reason(exc)}"
                    )
                    continue
                signed_url = _presign(
                    media_client, ref, deadline_epoch_s=deadline_epoch_s, presign_s=presign_s
                )
            outcome.videos.append(
                StagedVideo(
                    key=key,
                    post_url=str(item.post_url),
                    name=staged_name(key),
                    body=body if keep_body else None,
                    ref=ref,
                    url=signed_url,
                    reused=False,
                    content_type=content_type,
                )
            )
            done.add(key)
            outcome.fetched += 1
        for key, _url in pending:
            if key not in done:
                outcome.warnings.append(f"{key}:APIFY_FALLBACK_MISSING")

    if outcome.fetched or outcome.reused:
        outcome.warnings.append(
            f"APIFY_FALLBACK_OK:fetched={outcome.fetched},reused={outcome.reused},"
            f"est_cost_usd={outcome.est_cost_usd:.4f}"
        )
    logger.info(
        "tiktok_apify_fallback_applied",
        job_id=job_id,
        requested=outcome.requested,
        fetched=outcome.fetched,
        reused=outcome.reused,
        est_cost_usd=round(outcome.est_cost_usd, 4),
    )
    return outcome


__all__ = [
    "ACQUIRED_VIA_APIFY",
    "ACQUIRED_VIA_WORKER",
    "DEADLINE_DEFAULT_S",
    "ENV_DEADLINE",
    "ENV_FLAG",
    "ENV_MAX_VIDEOS",
    "MAX_VIDEOS_DEFAULT",
    "MAX_VIDEO_BYTES",
    "PRESIGN_S",
    "STAGED_NAME_PREFIX",
    "FallbackOutcome",
    "StagedVideo",
    "apify_fallback_enabled",
    "fallback_deadline_s",
    "fallback_job_id",
    "fallback_max_videos",
    "fill_missing_videos",
    "safe_reason",
    "staged_name",
]
