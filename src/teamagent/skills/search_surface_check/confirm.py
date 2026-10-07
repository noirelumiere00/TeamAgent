"""取得の前の確認（10-05 小俣さん裁定: KW・分析本数・競合を聞いてから作る）。

対象は「検索上位と動画の中身を 1 通で届ける」人（2 段目＝mcp_gateway/surface_video_followup の
対象と同じ: ``USE_SURFACE_VIDEO_FOLLOWUP`` と ``SURFACE_VIDEO_ONE_SHOT`` が ON・
``SURFACE_VIDEO_FOLLOWUP_ALLOWED_EMAILS`` に載っている・本人確認済みの DM）。
それ以外は今までどおり確認なしで作る。

確認は取得（3 KW 以上なら tiktok_acquire）の**前**に返す。依頼者が答えてから
``confirmed=true`` で呼び直す（SOUL）。KW・本数・競合をすべて依頼文で指定済みなら、Aico が
最初から ``confirmed=true`` で呼ぶ（指定に従う）。
"""

from __future__ import annotations

import os
from typing import Any

from teamagent.mcp_gateway.allowlist import email_allowed
from teamagent.skills._shared.private_surface import is_private_surface
from teamagent.skills.search_surface_check.schema import SearchSurfaceCheckInput

# 2 段目の対象と同じ env（mcp_gateway/surface_video_followup.py の ENABLED_ENV /
# ALLOWED_EMAILS_ENV）。skill から gateway を import しない（層の向き）ので名前だけ持つ。
# 一致はテストで固定する。
FOLLOWUP_ENABLED_ENV = "USE_SURFACE_VIDEO_FOLLOWUP"
FOLLOWUP_ALLOWED_EMAILS_ENV = "SURFACE_VIDEO_FOLLOWUP_ALLOWED_EMAILS"
ONE_SHOT_ENV = "SURFACE_VIDEO_ONE_SHOT"

DEFAULT_VIDEOS = 5
# 動画 1 本あたりの目安（3 並列で取得・分析＋まとめ）。5 本で約 10 分・10 本で約 20 分
# （実測 11 分/5 本）。
MINUTES_PER_VIDEO = 2


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes"}


def one_shot_enabled(user_email: str | None) -> bool:
    """この人は「検索上位＋動画の中身を 1 通」の対象か（空の allowlist は誰も対象にしない）。"""
    if not (
        _truthy(os.environ.get(FOLLOWUP_ENABLED_ENV)) and _truthy(os.environ.get(ONE_SHOT_ENV))
    ):
        return False
    allowed = {
        e.strip().lower()
        for e in os.environ.get(FOLLOWUP_ALLOWED_EMAILS_ENV, "").split(",")
        if e.strip()
    }
    # 2 段目の本体（surface_video_followup）と同じ照合にする。「*」＝本人確認済みの全員
    # （10-06 夜の全員開放で、ここだけ完全一致のままだったため確認と 1 通化が誰にも効かなかった）。
    return email_allowed(user_email, allowed)


def confirm_required(input: SearchSurfaceCheckInput, metadata: dict[str, Any]) -> bool:
    """取得の前に確認文を返すか。確認済み（confirmed）・対象外・DM 以外は返さない。"""
    if input.confirmed or input.acquire_job_id:
        # 取得ジョブを持ってきた＝確認の後（SOUL の順序: 確認 → tiktok_acquire → 本ツール）。
        # confirmed の付け忘れで確認が繰り返されないようにする。
        return False
    if not one_shot_enabled(str(metadata.get("user_email") or "")):
        return False
    return is_private_surface(metadata.get("channel_id"), metadata.get("identity_verified") is True)


def estimate_minutes(videos: int) -> int:
    return max(5, videos * MINUTES_PER_VIDEO)


def build_confirm_message(input: SearchSurfaceCheckInput) -> str:
    """確認文（Aico がそのまま返す）。決まっている値は既定として見せ、変えたい所だけ聞く。"""
    videos = input.max_videos or DEFAULT_VIDEOS
    kws = "／".join(input.keywords)
    platforms = "・".join("TikTok" if p == "tiktok" else "Instagram" for p in input.platforms)
    competitors = (
        "・".join(a if a.startswith("@") else f"@{a}" for a in input.competitor_accounts)
        if input.competitor_accounts
        else "なし（比べたい競合の公式アカウントがあれば @ で教えてください）"
    )
    lines = [
        "この内容で検索上位チェックを作ります。よければ「OK」、変えたい所があれば返信してください。",
        f"• キーワード: {kws}（{platforms}）",
        f"• 動画の中身の分析: 全キーワードの上位から {videos}本"
        "（複数のキーワードに出る動画・表示順位の高い動画を優先。5本か10本を選べます）",
        f"• 比較する競合: {competitors}",
    ]
    if input.client_accounts:
        lines.append(
            "• 自社の公式: "
            + "・".join(a if a.startswith("@") else f"@{a}" for a in input.client_accounts)
        )
    minutes = estimate_minutes(videos)
    lines.append(
        f"検索上位と動画の中身を、全部そろってから 1 通でお届けします（目安 約{minutes}分）。"
    )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_VIDEOS",
    "FOLLOWUP_ALLOWED_EMAILS_ENV",
    "FOLLOWUP_ENABLED_ENV",
    "ONE_SHOT_ENV",
    "build_confirm_message",
    "confirm_required",
    "estimate_minutes",
    "one_shot_enabled",
]
