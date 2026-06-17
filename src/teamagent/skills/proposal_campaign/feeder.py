"""proposal_campaign の純粋関数群（KW解決・スロット割当・1KW取得・evidence 組成）。

すべてネット非依存にテストできるよう、検索/取得/正規化は引数で受ける（DI）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from teamagent.skills.proposal_campaign.adapters import Fetcher, Normalizer, Searcher
from teamagent.skills.proposal_campaign.schema import KWThumbnailResult
from teamagent.skills.proposal_deck.contract import EvidenceImage

# {58-92} マトリクスの割当スロット（左→右）。メディア枠 → 界隈枠の順。
# 由来: 02_fmt_placeholder_map（D_publicity→メディア枠 / E_community→界隈枠）。
MEDIA_SLOTS: list[int] = [58, 60, 62, 64, 67, 68, 70, 71]
COMMUNITY_SLOTS: list[int] = [72, 74, 75, 77, 78, 81, 82, 84, 85, 88, 89, 91, 92]
_ALL_SLOTS: list[int] = MEDIA_SLOTS + COMMUNITY_SLOTS


def _dedupe(items: list[str]) -> list[str]:
    """空白除去・空文字除外・順序保持で重複排除。"""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        s = it.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def extract_keywords_from_dr(dr: dict[str, Any]) -> list[str]:
    """GeminiDRJSON → KW（D_publicity.trend_word / E_community.tiktok_tags / C_tiktok.tag）。"""
    kws: list[str] = []
    for item in dr.get("D_publicity", []) or []:
        word = (item or {}).get("trend_word")
        if word:
            kws.append(str(word))
    for item in dr.get("E_community", []) or []:
        for tag in (item or {}).get("tiktok_tags", []) or []:
            if tag:
                kws.append(str(tag).lstrip("#"))
    for item in dr.get("C_tiktok", []) or []:
        tag = (item or {}).get("tag")
        if tag:
            kws.append(str(tag).lstrip("#"))
    return _dedupe(kws)


def extract_keywords_from_composer(placeholders: dict[int, str]) -> list[str]:
    """ComposerOutput.placeholders の {58-92} マトリクスセルから KW を抽出（best-effort）。"""
    vals = [placeholders[pid] for pid in _ALL_SLOTS if placeholders.get(pid)]
    return _dedupe([str(v).lstrip("#") for v in vals])


def resolve_keywords(
    *,
    keywords: list[str],
    gemini_dr_json_path: str | None,
    composer_output_json_path: str | None,
) -> list[str]:
    """直接KW > DR JSON > ComposerOutput の優先で KW を解決する。"""
    if keywords:
        return _dedupe(keywords)
    if gemini_dr_json_path:
        dr = json.loads(Path(gemini_dr_json_path).read_text(encoding="utf-8"))
        return extract_keywords_from_dr(dr)
    if composer_output_json_path:
        data = json.loads(Path(composer_output_json_path).read_text(encoding="utf-8"))
        raw_ph = data.get("placeholders", {}) or {}
        placeholders = {int(k): str(v) for k, v in raw_ph.items()}
        return extract_keywords_from_composer(placeholders)
    return []


def assign_placeholder_ids(keywords: list[str]) -> list[int]:
    """各 KW に {58-92} スロットを左→右で割当（上限＝スロット数）。"""
    return _ALL_SLOTS[: len(keywords)]


def _write_image(cache_dir: Path, request_id: str, placeholder_id: int, data: bytes) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{request_id}_{placeholder_id}.jpg"
    path.write_bytes(data)
    return path


def _evidence(
    placeholder_id: int, keyword: str, image_path: str, cover_url: str | None, video_url: str | None
) -> EvidenceImage:
    return EvidenceImage(
        placeholder_id=placeholder_id,
        rank=1,
        keyword=keyword,
        source_url=cover_url,
        image_path=image_path,
        video_url=video_url,
    )


def fetch_one(
    *,
    keyword: str,
    placeholder_id: int,
    searcher: Searcher,
    fetcher: Fetcher,
    normalizer: Normalizer,
    fallback_bytes: bytes | None,
    cache_dir: Path,
    request_id: str,
) -> tuple[KWThumbnailResult, EvidenceImage | None]:
    """1 KW → (監査結果, EvidenceImage|None)。

    検索→1位→cover取得→正規化→保存→EvidenceImage。例外/0件/取得失敗は fallback、
    fallback も無ければ error（None）。KW 単位で隔離し、他 KW を巻き込まない。
    """
    video_url: str | None = None
    cover_url: str | None = None
    error: str = "no_result"
    try:
        videos = searcher(keyword, 1, request_id)
        if videos:
            top = videos[0]
            video_url = top.url or None
            cover_url = top.cover_url or None
            raw = fetcher(cover_url, request_id)
            if raw:
                path = _write_image(cache_dir, request_id, placeholder_id, normalizer(raw))
                result = KWThumbnailResult(
                    keyword=keyword,
                    placeholder_id=placeholder_id,
                    rank=1,
                    success=True,
                    source="tiktok_1st",
                    video_url=video_url,
                    cover_url=cover_url,
                    image_path=str(path),
                )
                return result, _evidence(placeholder_id, keyword, str(path), cover_url, video_url)
    except Exception as exc:  # TikTokScrapeError/ネット/parse 等は握って KW 単位 graceful
        error = type(exc).__name__

    # fallback（代替画像があれば）→ 無ければ error
    if fallback_bytes:
        path = _write_image(cache_dir, request_id, placeholder_id, fallback_bytes)
        result = KWThumbnailResult(
            keyword=keyword,
            placeholder_id=placeholder_id,
            rank=1,
            success=True,
            source="fallback",
            video_url=video_url,
            cover_url=cover_url,
            image_path=str(path),
        )
        return result, _evidence(placeholder_id, keyword, str(path), cover_url, video_url)
    return (
        KWThumbnailResult(
            keyword=keyword,
            placeholder_id=placeholder_id,
            rank=1,
            success=False,
            source="error",
            video_url=video_url,
            cover_url=cover_url,
            error=error,
        ),
        None,
    )


def build_evidence_images(evidences: list[EvidenceImage]) -> dict[int, list[EvidenceImage]]:
    """EvidenceImage 群を placeholder_id でまとめる。"""
    out: dict[int, list[EvidenceImage]] = {}
    for ev in evidences:
        out.setdefault(ev.placeholder_id, []).append(ev)
    return out
