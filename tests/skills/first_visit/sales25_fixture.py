"""Anonymous synthetic photos and six query populations; no acquisition or real customers."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from PIL import Image

from teamagent.skills.first_visit import sales25_contract as contract

OWNER = "owner-fixture-a"


def inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    queries = [
        {"query_id": f"q{n}", "role": role, "group": group, "query": group + " 合成検索"}
        for n, (role, group) in enumerate(
            [
                ("self", "ブランドA"),
                ("competitor", "ブランドB"),
                ("competitor", "ブランドC"),
                ("market_keyword", "中立語A"),
                ("market_keyword", "中立語B"),
                ("market_keyword", "中立語C"),
            ]
        )
    ]
    case = {
        "edition": "sales25",
        "category": "合成カテゴリー",
        "owner_id": OWNER,
        "synthetic_only": True,
        "queries": queries,
    }
    raw, decisions = {}, {}
    for n, query in enumerate(queries):
        posts = []
        for rank in range(1, 101):
            pid = str(9000000000 + n * 1000 + rank)
            posts.append(
                {
                    "id": pid,
                    "url": f"https://www.tiktok.com/@synthetic/photo/{pid}",
                    "rank_display": rank,
                    "desc": "合成写真。実投稿ではありません。",
                    "author": {"uniqueId": "synthetic", "followerCount": None},
                    "stats": {
                        "playCount": 1000,
                        "diggCount": 101 - rank,
                        "commentCount": 0,
                        "shareCount": 0,
                        "collectCount": 101 - rank,
                    },
                    "missingFields": ["author.followerCount"],
                    "isAd": rank == 1,
                    "hashtags": ["PR"] if rank == 1 else [],
                    "mediaType": "photo",
                    "photoCount": 3,
                }
            )
        raw[query["query_id"]] = {
            "owner_id": OWNER,
            "query": query["query"],
            "videos": posts,
            "status": "ok",
            "fetched_at": "2026-10-23T12:00:00+09:00",
            "sessions": 1,
            "order_basis": "search_display_order",
            "stop_reason": "capped",
            "diagnostics": {},
            "unfiltered": True,
            "missingness_preserved": True,
        }
        decisions[query["query_id"]] = [
            {"id": p["id"], "eligible": True, "reason": "合成の関連投稿"} for p in posts
        ]
    return case, raw, decisions


def prepare(root: Path) -> None:
    case, raw, decisions = inputs()
    contract.write_inputs(root, contract.convert_inputs(case, raw, decisions, owner_id=OWNER))
    image_dir = root / "images"
    image_dir.mkdir()
    Image.new("RGB", (72, 128), (40, 100, 180)).save(image_dir / "portrait.png")
    Image.new("RGB", (160, 80), (180, 90, 40)).save(image_dir / "landscape.jpeg")
    transparent = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    transparent.paste((20, 180, 40, 128), (20, 20, 80, 80))
    transparent.save(image_dir / "transparent.png")
    opened = ["images/portrait.png", "images/landscape.jpeg", "images/transparent.png"]
    fields = contract.read_json(contract.BUNDLE / "assets/fields.json")["fields"]
    texts = {}
    labels = {
        "self_brand": "ブランドA",
        "competitor_a": "ブランドB",
        "competitor_b": "ブランドC",
        "self_query": case["queries"][0]["query"],
        "competitor_a_query": case["queries"][1]["query"],
        "competitor_b_query": case["queries"][2]["query"],
        "date": "2026-10-23",
        "cover_subtitle": "合成入力・実際の調査結果ではありません",
    }
    labels.update({f"neutral_query_{n}": case["queries"][n + 2]["query"] for n in range(1, 4)})
    for field in fields:
        if field["kind"] == "text":
            key = field["key"]
            texts[key] = labels.get(key, "100" if key.startswith("n") else "合成見本")
    plan = copy.deepcopy(contract.read_json(contract.BUNDLE / "assets/template-plan.json"))
    plan.pop("template_only")
    tokens = {f["token"]: texts[f["key"]] for f in fields if f["kind"] == "text"}
    for slide in plan["slides"]:
        for key in ("group", "query"):
            if key in slide:
                slide[key] = tokens.get(slide[key], slide[key])
    images = {
        f["key"]: {"path": opened[i % 3]} for i, f in enumerate(fields) if f["kind"] == "image"
    }
    reviews: dict[str, Any] = {"version": 1, "posts": {}, "slides": []}
    entries = []
    for n, query in enumerate(case["queries"]):
        posts = raw[query["query_id"]]["videos"]
        ids = [p["id"] for p in posts]
        start = 2 + n * 4 if n < 3 else 14 + (n - 3) * 2
        for slide in plan["slides"][start : start + (4 if n < 3 else 2)]:
            slide.update(group=query["group"], query=query["query"])
        plan["slides"][start]["post_ids"] = ids[:10]
        if n >= 3:
            continue
        rankings = {role: ids[:10] for role in contract.TOP}
        entries.append(
            {
                "query": query["query"],
                "group": query["group"],
                "source_path": f"selection/source_{query['query_id']}.json",
                "rankings": rankings,
            }
        )
        for pid in ids[:10]:
            reviews["posts"][pid] = {
                "media_type": "photo",
                "url": next(p["url"] for p in posts if p["id"] == pid),
                "identity_verified": True,
                "identity_basis": "合成入力内のID対応（実投稿の確認ではない）",
                "source": {
                    "path": opened[0],
                    "sha256": hashlib.sha256((root / opened[0]).read_bytes()).hexdigest(),
                },
                "photo_count": 3,
                "visual": {"status": "reviewed", "opened_files": opened},
                "text": {"status": "reviewed", "summary": "合成写真の図形"},
                "audio": {
                    "status": "not_applicable",
                    "summary": "合成写真のみ",
                    "reason": "オフラインの画像処理試験に限定",
                },
                "evidence": {
                    key: {
                        "file": opened[min(i, 2)],
                        "photo_index": min(i + 1, 3),
                        "observation": "合成写真に図形がある。",
                    }
                    for i, key in enumerate(("opening", "development", "product", "ending"))
                },
            }
        for offset, _role in enumerate(contract.TOP, 1):
            page = start + offset + 1
            plan["slides"][page - 1]["post_ids"] = ids[:5]
            field = next(
                f
                for f in fields
                if f["part"] == f"ppt/slides/slide{page}.xml" and f["key"].startswith("t")
            )
            text = "合成写真に図形がある。順序を試す。"
            texts[field["key"]] = text
            reviews["slides"].append(
                {
                    "page": page,
                    "elements": [
                        {
                            "text": text,
                            "observation": "合成写真に図形がある。",
                            "interpretation": "識別を助ける仮説。",
                            "proposal": "順序を試す。",
                            "refs": [{"post_id": ids[9], "evidence": "product"}],
                        }
                    ],
                }
            )
            for index, pid in enumerate(ids[:5], 1):
                images[f"p{page:02d}_image_{index:02d}"]["url"] = reviews["posts"][pid]["url"]
    plan["analysis_selection"] = {"top_n": 10, "target_count_per_query": 100, "queries": entries}
    plan["pr_examples"] = []
    for n, page in enumerate((21, 22, 23)):
        query = case["queries"][n]
        post = raw[query["query_id"]]["videos"][0]
        plan["slides"][page - 1]["post_ids"] = [post["id"]]
        long_fields = [
            f
            for f in fields
            if f["part"] == f"ppt/slides/slide{page}.xml"
            and f["kind"] == "text"
            and f["bbox"]["height"] >= 160
        ]
        example = {
            "page": page,
            "post_id": post["id"],
            "query_id": query["query_id"],
            "pr_basis": "合成isAd=true",
        }
        for kind, field, text in zip(
            ("observation", "proposal"),
            long_fields,
            ("合成写真に図形がある。", "ブランドAで順序を試す。"),
            strict=True,
        ):
            example[kind + "_key"], example[kind] = field["key"], text
            texts[field["key"]] = text
        plan["pr_examples"].append(example)
        for index in range(1, 4):
            images[f"p{page}_image_{index:02d}"] = {"path": opened[index - 1], "url": post["url"]}
    data = {
        "texts": texts,
        "images": images,
        "links": {"self_brand": "https://example.invalid/synthetic"},
        "notes": {
            str(page): "合成入力のみ。実取得・視聴・原音確認の証明ではありません。"
            for page in range(1, 26)
        },
    }
    for relative, value in {
        "deck-plan.json": plan,
        "deck-input.json": data,
        "top5-analysis.json": reviews,
    }.items():
        (root / relative).write_bytes(contract.json_bytes(value))
