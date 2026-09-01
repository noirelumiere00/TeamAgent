"""tiktok_search の「該当秒の実フレーム」を video_algorithm から取り込む（後段連携）。

## なぜ後段で呼ぶか

``tiktok_search`` はメタデータしか見ていない（system prompt にも「映像の中身は説明文と
エンゲージ指標から**推測**で補う」と明記されている）。したがって「どの秒が効いているか」を
自力で言うことはできない。実際に動画を視聴して秒単位の分析とフレーム抽出を行うのは
``video_algorithm`` で、既に本番で動いている。**分析ロジックは複製せず、その出力を写す**。

## なぜ既定では呼ばないか

``video_algorithm`` は取得→DL→Gemini マルチモーダルまで走る重い処理（数十秒〜数分・課金あり）。
軽い検索ツールが毎回それを引きずるのは誤り。``outputs`` に ``"frames"`` を明示した時だけ実行する。

失敗は握って ``[]`` を返す（フレームが無いだけで、検索結果とレポートは必ず出る）。
"""

from __future__ import annotations

import os

import structlog

from teamagent.skills._html.report import Filmstrip, Frame
from teamagent.skills.base import SkillContext

logger = structlog.get_logger(__name__)

# 1 レポートに載せる動画本数と、1 本あたりのコマ数。増やすほど重く・長くなる。
_MAX_VIDEOS = 3
_MAX_FRAMES = 5

_HOOK_JP = {
    "question": "問いかけ",
    "number": "数字提示",
    "shock": "驚き",
    "visual": "画で見せる",
    "pov": "POV",
    "dialogue": "会話",
    "problem": "問題提起",
    "other": "その他",
}


def frames_enabled() -> bool:
    """``USE_HTML_REPORT_FRAMES`` が真のときだけ後段連携する（既定 OFF・段階ゲート）。"""
    return (os.environ.get("USE_HTML_REPORT_FRAMES") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _subtitle(analysis: object) -> str:
    """「フック: 数字提示 — 19時帰宅でも10分」の 1 行を組む。分析が無ければ空。"""
    if analysis is None:
        return ""
    hook_type = str(getattr(analysis, "hook_type", "") or "")
    summary = str(getattr(analysis, "hook_summary", "") or "").strip()
    label = _HOOK_JP.get(hook_type, hook_type)
    if label and summary:
        return f"冒頭フック: {label} — {summary}"
    return f"冒頭フック: {label or summary}" if (label or summary) else ""


def build_filmstrips(query: str, ctx: SkillContext) -> list[Filmstrip]:
    """``video_algorithm`` を実行し、該当秒のコマ送りを返す。無効・失敗は ``[]``。"""
    if not frames_enabled():
        return []
    try:
        from teamagent.skills.video_algorithm.schema import VideoAlgorithmInput
        from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

        # outputs=[] ＝ あちら側の HTML/slides/pptx は作らせない（欲しいのは分析とフレームだけ）。
        result = VideoAlgorithmSkill().run(
            VideoAlgorithmInput(query=query, max_videos=_MAX_VIDEOS, outputs=[]),
            ctx,
        )
    except Exception as e:
        logger.warning("report_frames_failed", request_id=ctx.request_id, error=type(e).__name__)
        return []

    strips: list[Filmstrip] = []
    for video in getattr(result, "videos", [])[:_MAX_VIDEOS]:
        frames = [
            Frame(
                label=f"{float(getattr(f, 'sec', 0.0)):.1f}秒",
                image=str(getattr(f, "data_uri", "") or ""),
                caption=str(getattr(f, "caption", "") or ""),
            )
            for f in (getattr(video, "frames", []) or [])[:_MAX_FRAMES]
        ]
        frames = [f for f in frames if f.image]
        if not frames:
            continue
        meta = getattr(video, "meta", None)
        author = str(getattr(meta, "author", "") or "").lstrip("@")
        strips.append(
            Filmstrip(
                title=f"@{author}" if author else "動画",
                subtitle=_subtitle(getattr(video, "analysis", None)),
                frames=frames,
                href=str(getattr(meta, "url", "") or "") or None,
            )
        )
    logger.info("report_frames_built", request_id=ctx.request_id, videos=len(strips))
    return strips


__all__ = ["build_filmstrips", "frames_enabled"]
