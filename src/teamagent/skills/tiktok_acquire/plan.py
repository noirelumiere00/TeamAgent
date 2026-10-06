"""tiktok_acquire の投函計画: 1ジョブの実行上限に収まる形へ組み直す（断らずに実行する）。

1ジョブは ``TIKTOK_OPERATION_EXECUTION_LIMIT_SECONDS``（870秒）の見積もり以内でないと
dispatcher / worker が受け付けない。見積もりの大半はサムネ保存（1本50秒）と動画保存
（1本120秒）で、既定値（10本/KW・動画2本/KW）では 2KW 以上が1ジョブに入らない。
以前は入力検証で拒否しており、本番では 2026-08-28〜09-29 に17回拒否・通ったのは1KWだけ
だった（検索面チェックのレシピ「3KW以上は先に videos_per_kw=0 で取得」も必ず拒否）。

組み直しの順番（要求をなるべく削らない順）:
1. そのまま1ジョブに収まる → そのまま（従来と同じ job_id・同じ中身）。
2. 動画なし（videos_per_kw=0）→ 指標だけのジョブ（metadata_only）に切り替える。
   検索面チェック・動画分析が読むのは posts（表示順・指標・cover_url）だけなので、
   省くのは S3 へのサムネ画像の控えと config.json。1ジョブ7KWまで入り、それを超える
   ときだけ均等に分ける。
3. 動画あり → KW をまとめられるだけまとめ、残りは別ジョブへ分けて並べる（要求どおりの
   本数で）。1KW でも収まらないとき、または分けると ``MAX_JOBS_PER_REQUEST`` を超えるときに
   限り、動画本数を保ったまま取得本数を縮め、それでも足りなければ動画本数も縮める。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from teamagent.media.contracts import (
    TIKTOK_OPERATION_EXECUTION_LIMIT_SECONDS,
    estimate_tiktok_operation_seconds,
)

ArtifactMode = Literal["metadata_only", "full"]

# 1回の依頼で同時に投函するジョブの上限。取得タスクは1本 16 vCPU（taskdef
# teamagent-dev-tiktok-acquire:45）で、アカウントの Fargate 上限は 140 vCPU・平常時の使用は
# 約7 vCPU（いずれも 2026-09-30 実測）＝同時に動かせる取得は約8本。dispatcher は RunTask が
# 上限で断られるとそのジョブを失敗で確定する（再試行しない）ので、1依頼で使い切ると他の人の
# 動画系ジョブまで落ちる。5本（80 vCPU）に抑え、他の依頼に3本分を残す。
MAX_JOBS_PER_REQUEST = 5


@dataclass(frozen=True)
class AcquireJobPlan:
    """1ジョブ分の投函内容。各ジョブは単独で実行上限に収まる。"""

    keywords: tuple[str, ...]
    n_per_kw: int
    videos_per_kw: int
    artifact_mode: ArtifactMode


@dataclass(frozen=True)
class AcquirePlan:
    jobs: tuple[AcquireJobPlan, ...]
    # 要求から変えた点（利用者にそのまま伝えられる文面）。結果が要求と変わらない
    # 組み直し（取得本数を超える動画本数を取得本数に揃えるだけ等）は書かない。
    adjustments: tuple[str, ...] = ()


def _as_requested(keywords: tuple[str, ...], n_per_kw: int, videos_per_kw: int) -> AcquirePlan:
    return AcquirePlan(jobs=(AcquireJobPlan(keywords, n_per_kw, videos_per_kw, "full"),))


def _fits(keyword_count: int, n_per_kw: int, videos_per_kw: int, mode: ArtifactMode) -> bool:
    return (
        estimate_tiktok_operation_seconds(
            keyword_count=keyword_count,
            n_per_kw=n_per_kw,
            videos_per_kw=videos_per_kw,
            artifact_mode=mode,
        )
        <= TIKTOK_OPERATION_EXECUTION_LIMIT_SECONDS
    )


def _keywords_per_job(limit: int, n_per_kw: int, videos_per_kw: int, mode: ArtifactMode) -> int:
    count = 0
    while count < limit and _fits(count + 1, n_per_kw, videos_per_kw, mode):
        count += 1
    return count


def _chunk(keywords: tuple[str, ...], per_job: int) -> tuple[tuple[str, ...], ...]:
    """KW の順を保ったまま、ジョブ数を最小にしつつ件数を均等に分ける。"""

    job_count = math.ceil(len(keywords) / per_job)
    base, extra = divmod(len(keywords), job_count)
    chunks: list[tuple[str, ...]] = []
    start = 0
    for index in range(job_count):
        size = base + (1 if index < extra else 0)
        chunks.append(keywords[start : start + size])
        start += size
    return tuple(chunks)


def _shrink_to_fit(keywords_per_job: int, n_per_kw: int, videos_per_kw: int) -> tuple[int, int]:
    """1ジョブに ``keywords_per_job`` KW 入る (取得本数, 動画本数)。

    動画本数を優先して残し、その上で取得本数を最大にする。取得本数は動画本数以上
    （動画は取得した投稿の中から選ぶので、それを超える動画本数は意味がない）。
    """

    for videos in range(min(videos_per_kw, n_per_kw), -1, -1):
        for n in range(n_per_kw, max(videos, 1) - 1, -1):
            if _fits(keywords_per_job, n, videos, "full"):
                return n, videos
    # 2KW×1本×動画0本（340秒）は常に収まるので到達しない。定数が変わったときの保険。
    raise ValueError("TikTok acquisition cannot fit the keywords in the job limit")


def plan_acquire_jobs(
    keywords: list[str] | tuple[str, ...],
    *,
    n_per_kw: int,
    videos_per_kw: int,
) -> AcquirePlan:
    kws = tuple(keywords)
    if _fits(len(kws), n_per_kw, videos_per_kw, "full"):
        return _as_requested(kws, n_per_kw, videos_per_kw)

    if videos_per_kw == 0:
        per_job = _keywords_per_job(len(kws), n_per_kw, 0, "metadata_only")
        chunks = _chunk(kws, per_job)
        adjustments = [
            "動画の保存がないので、表示順と指標だけを取る軽い取得に切り替えました"
            "（サムネ画像の保存は省略。サムネのURLは投稿データに入っています）。"
        ]
        if len(chunks) > 1:
            adjustments.append(
                f"KWが多いので、1回の実行時間に収まるよう{len(chunks)}件の取得に分けて並べました。"
            )
        return AcquirePlan(
            jobs=tuple(AcquireJobPlan(chunk, n_per_kw, 0, "metadata_only") for chunk in chunks),
            adjustments=tuple(adjustments),
        )

    adjustments = []
    n, videos = n_per_kw, videos_per_kw
    per_job = _keywords_per_job(len(kws), n, videos, "full")
    needed_per_job = math.ceil(len(kws) / MAX_JOBS_PER_REQUEST)
    if per_job < needed_per_job:
        n, videos = _shrink_to_fit(needed_per_job, n_per_kw, videos_per_kw)
        per_job = _keywords_per_job(len(kws), n, videos, "full")
        reduced = []
        if n < n_per_kw:
            reduced.append(f"取得本数を{n_per_kw}本→{n}本")
        if videos < min(videos_per_kw, n_per_kw):
            reduced.append(f"動画保存本数を{videos_per_kw}本→{videos}本")
        if reduced:
            reason = (
                f"KWが多く、同時に動かせる取得は{MAX_JOBS_PER_REQUEST}件までなので"
                if needed_per_job > 1
                else "1KWだけでも1回の実行時間に収まらないので"
            )
            adjustments.append(f"{reason}、各KWの" + "、".join(reduced) + "に減らしました。")
    chunks = _chunk(kws, per_job)
    if len(chunks) > 1:
        note = (
            f"動画も保存するので、1回の実行時間に収まるよう{len(chunks)}件の取得に分けて並べました"
        )
        adjustments.insert(0, note + ("。" if adjustments else "（本数は要求どおり）。"))
    return AcquirePlan(
        jobs=tuple(AcquireJobPlan(chunk, n, videos, "full") for chunk in chunks),
        adjustments=tuple(adjustments),
    )


__all__ = ["MAX_JOBS_PER_REQUEST", "AcquireJobPlan", "AcquirePlan", "plan_acquire_jobs"]
