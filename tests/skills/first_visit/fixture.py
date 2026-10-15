"""Independently authored synthetic evidence; no client data or kit fixture imports."""

from __future__ import annotations

import hashlib
import json
from base64 import b64decode
from collections.abc import Mapping
from pathlib import Path
from typing import Any

VID = "7646064651251371272"
SYNTHETIC_JPEG = b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwg"
    "JC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAYABADASIAAhEBAxEB/8QA"
    "FQABAQAAAAAAAAAAAAAAAAAAAAb/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/"
    "xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCIAWEsAB//2Q=="
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_fixture(root: Path, inputs: Mapping[str, Any], kit_path: Path) -> Path:
    """Complete one converted case with clearly synthetic, first-visit-only evidence."""
    root.mkdir(parents=True, exist_ok=True)
    for relative, value in inputs.items():
        write_json(root / relative, value)
    cfg = inputs["case.json"]
    selection = inputs["selection.json"]
    assert [row["id"] for row in selection] == [VID], "fixture expects one synthetic video"
    axes = [("category", row) for row in cfg["keywords"]] + [
        ("own" if row.get("own") else "competitor", row) for row in cfg["brands"]
    ]
    windows = {"category": 30, "own": 20, "brand": 20}
    windows.update((cfg.get("first_visit") or {}).get("window") or {})
    posts: dict[str, dict[str, Any]] = {}
    raw: dict[str, dict[str, Any]] = {}
    cover = root / "assets" / "covers" / f"{VID}.jpg"
    cover.parent.mkdir(parents=True)
    cover.write_bytes(SYNTHETIC_JPEG)
    for kind, axis in axes:
        for rank, video in enumerate(inputs[axis["file"]]["videos"], start=1):
            video_id = video["id"]
            raw.setdefault(video_id, video)
            post = posts.setdefault(
                video_id,
                {
                    "video_id": video_id,
                    "axes": [],
                    "angle": "demo",
                    "appeal": "none",
                    "is_pr": video.get("isAd") is True,
                    "cover": f"assets/covers/{VID}.jpg" if video_id == VID else None,
                },
            )
            post["axes"].append(
                {
                    "kind": kind,
                    "name": axis["name"],
                    "rank": rank,
                    "in_window": rank <= windows["brand" if kind == "competitor" else kind],
                    "relevance": "relevant" if kind == "category" else "irrelevant",
                    "confirmed": True,
                    "cover_sha": hashlib.sha256(SYNTHETIC_JPEG).hexdigest()
                    if video_id == VID
                    else None,
                }
            )
    assert raw[VID].get("mediaType") == "video", "fixture expects explicit video metadata"
    assert isinstance(raw[VID].get("isAd"), bool), "fixture expects a measured advertisement flag"
    write_json(root / "labels.json", {"vocab": "general", "posts": list(posts.values())})
    frames: list[dict[str, Any]] = [
        {
            "file": f"frames/{VID}/{index + 1:03d}.jpg",
            "sec": float(index * 4),
            "caption": f"合成手順{index + 1}",
        }
        for index in range(5)
    ]
    frame_dir = root / "frames" / VID
    frame_dir.mkdir(parents=True)
    for frame in frames:
        (root / frame["file"]).write_bytes(SYNTHETIC_JPEG)
    (frame_dir / "contact.jpg").write_bytes(SYNTHETIC_JPEG)
    write_json(
        frame_dir / "frames.json",
        {
            "video_id": VID,
            "duration_sec": 20.0,
            "frames": 5,
            "timestamps": [frame["sec"] for frame in frames],
            "coverage_ok": True,
        },
    )
    media = root / "media"
    media.mkdir()
    (media / f"{VID}.mp4").write_bytes(b"SYNTHETIC TEST FIXTURE: not playable video\n")
    write_json(
        root / "transcripts" / f"{VID}.json",
        {"video_id": VID, "no_speech": True, "has_audio": False, "segments": [], "text": ""},
    )
    observation_axes = {
        "hook": "合成コマの冒頭に手順を示す。",
        "visual": "単色画像を使った入力検証用の合成例。",
        "telop": "実投稿の文字は含まない合成例。",
        "price": "価格の表記は無い。",
        "brand": "実在ブランドの表示は無い。",
        "hypothesis": "合成例であり、効果の推定には使わない。",
    }
    anatomy = {
        "video_id": VID,
        "url": raw[VID]["url"],
        "entry": "demo",
        "entry_detail": "合成手順を示す入口の検証用原稿。",
        "creator_type": "架空の一般投稿者",
        "headline": "合成手順の構成を入力検証する",
        "axes": observation_axes,
        "essence": "手順を順番に示す合成例。",
        "story": "冒頭から途中、締めまでを順番に並べる。",
        "cta": "呼びかけ無し。",
        "voice": "発話無し。",
        "observed": "5枚の合成画像を用いた検証用記録。実投稿の観測ではない。",
        "save_reason_on_screen": "手順一覧を想定した合成例。",
        "problem_first": False,
        "product_reveal": "出ない",
        "has_person": False,
        "frames": frames,
        "opened": [f"frames/{VID}/contact.jpg", *(frame["file"] for frame in frames)],
    }
    write_json(root / "anatomy" / f"{VID}.json", anatomy)
    page = {
        key: anatomy[key] for key in ["video_id", "headline", "axes", "essence", "story", "cta"]
    }
    page["creator_type_short"] = "架空一般"
    write_json(root / "anatomy_page" / f"{VID}.json", page)
    evidence_item = {
        "title": "手順を残す",
        "evidence": "合成5コマと変換済みの検証用データを参照。",
        "implication": "実案件では実投稿に基づく原稿を別途用意する。",
    }
    synthesis = {
        "summary": {
            "headline": "合成案件で入力契約を確認する",
            "sub": "実案件の提案や効果評価に使わない検証用原稿",
            "items": [dict(evidence_item, title=title) for title in ["現状", "発見", "次の一歩"]],
        },
        "proposals": {
            "headline": "検証用の合成企画",
            "sub": "企画数の入力契約を検証する",
            "items": [
                {
                    "title": f"合成企画{index + 1}",
                    "body": "手順を示す架空の企画。",
                    "evidence": evidence_item["evidence"],
                }
                for index in range((cfg.get("research") or {}).get("proposal_count", 3))
            ],
        },
        "patterns": [
            {"headline": "手順を示す合成型", "sub": "構成の入力検証用", "items": [evidence_item]}
        ],
        "hooks": [
            {
                "part": 3,
                "video_id": VID,
                "headline": "手順を冒頭に示す合成例",
                "sub": "実コマの代わりに合成コマで検証する",
                "beats": [dict(frames[0], telop="合成手順", speech="発話無し")],
            }
        ],
    }
    write_json(root / "authored" / "synthesis.json", synthesis)
    source_files = ["labels.json", *(axis["file"] for _, axis in axes)]
    fingerprints = {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in source_files
    }
    vocab = kit_path / "tiktok-deck" / "tools" / "vocab" / "general.json"
    fingerprints["_vocab"] = hashlib.sha256(vocab.read_bytes()).hexdigest()
    write_json(root / "first_visit.json", {"inputs_sha256": fingerprints})
    return root
