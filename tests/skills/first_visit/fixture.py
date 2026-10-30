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


def make_concrete_fixture(root: Path, inputs: Mapping[str, Any], kit_path: Path) -> Path:
    """Add three authored scripts and classified, invented product reviews."""
    make_fixture(root, inputs, kit_path)

    def read(relative: str) -> Any:
        return json.loads((root / relative).read_text(encoding="utf-8"))

    cfg = read("case.json")
    for index, brand in enumerate(cfg["brands"]):
        brand.update(match=brand["name"], review_key=f"fiction_{index}")
    cfg["research"].update(
        review_collect=True, review_spec="review_spec.json", review_sites=["rakuten"]
    )
    write_json(root / "case.json", cfg)
    write_json(
        root / "review_spec.json",
        {
            "product_noun": "合成手順キット",
            "effects": {"clarity": "手順の分かりやすさ"},
            "unsatisfied": {"size": "箱の大きさ"},
            "purchase": {"guide": "説明を見て購入"},
            "scene": {"home": "自宅で使用"},
        },
    )
    reviews = []
    products = []
    for index, brand in enumerate(cfg["brands"]):
        body = "説明を見て購入。自宅で手順が分かりやすかった。箱は大きい。"
        row = {
            "site": "rakuten",
            "review_id": f"invented_review_{index}",
            "product_id": f"invented_product_{index}",
            "product_name": "合成手順キット",
            "title": "合成口コミ",
            "body": body,
            "rating_5": 4,
            "posted_at": "2026-10-14",
            "url": f"https://example.invalid/review/{index}",
        }
        reviews.append(row)
        products.append(
            {
                "site": row["site"],
                "product_id": row["product_id"],
                "product_name": row["product_name"],
                "brand": brand["name"],
                "url": f"https://example.invalid/product/{index}",
                "status": "OK",
                "site_total": 1,
                "fetched": 1,
            }
        )
        label = {
            **row,
            "effects": ["clarity"],
            "satisfied": ["clarity"],
            "unsatisfied": ["size"],
            "purchase": ["guide"],
            "scene": ["home"],
            "quote": body,
            "brand_trust": False,
            "beginner": False,
            "gift_or_pr": False,
        }
        for directory, record in [("labels", label), ("by_brand", row)]:
            path = root / "reviews" / directory / f"{brand['review_key']}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    write_json(root / "reviews/acquire_log.json", {"products": products})
    (root / "reviews/reviews.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in reviews), encoding="utf-8"
    )
    synthesis = read("authored/synthesis.json")
    synthesis["hooks"].append(dict(synthesis["hooks"][0], part=4))
    synthesis["scripts"] = [
        {
            "headline": f"合成台本{index + 1}",
            "sub": "架空商品の手順を見せる",
            "basis": "合成5コマを順に提示する。",
            "specs": [["形式", "縦型"], ["音声", "発話なし"]],
            "rows": [
                ["0〜4秒", "箱を置く", "合成手順", "発話なし", "手順を知らせる"],
                ["4〜8秒", "手順を示す", "順に並べる", "発話なし", "流れを見せる"],
            ],
        }
        for index in range(3)
    ]
    write_json(root / "authored/synthesis.json", synthesis)
    fv = read("first_visit.json")
    fv.update(
        project={
            "cover_title": "合成手順キットの使い方",
            "client": "合成アルファ",
            "client_short": "合成A",
            "query": "手順",
            "footer": "合成案件・動作検証用",
        },
        pages={
            "now": {
                "headline": "合成検索の現状",
                "sub": "架空の入力1本を確認する",
                "cards": [
                    {
                        "rank": 1,
                        "video_id": VID,
                        "cover": f"assets/covers/{VID}.jpg",
                        "url": inputs["selection.json"][0]["url"],
                        "views_label": "120",
                        "caption": "合成手順を並べた例",
                    }
                ],
                "caption": "合成データの表示順",
            },
            "next": {
                "headline": "手順を順に見せる",
                "sub": "架空商品の検証用提案",
                "items": [{"title": "合成手順", "body": "箱から手順を順に示す。"}],
            },
        },
        appendix={"premise": [["用途", "架空の会社と商品による動作検証。"]]},
    )
    write_json(root / "first_visit.json", fv)
    return root
