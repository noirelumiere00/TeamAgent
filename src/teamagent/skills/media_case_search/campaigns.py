"""Campaign identity and metrics are deterministic; no LLM aggregates counts."""

from __future__ import annotations

import html
import re
import unicodedata
from collections import Counter
from typing import Any

# shortcut: generic tags use this denylist; extend it when another generic tag appears.
_GENERIC = {
    "campaign",
    "cp",
    "キャンペーン",
    "pr",
    "広告",
    "提供",
    "旅行",
    "国内旅行",
    "海外旅行",
    "お出かけ",
    "おすすめ",
    "新商品",
    "イベント",
    "tiktok",
    "fyp",
    "ホテル",
    "宿泊",
    "観光",
    "おすすめスポット",
    "旅行好き",
    "旅行好きな人と繋がりたい",
    "観光スポット",
    "ホテル紹介",
    "ホテルステイ",
    "グルメ",
    "おすすめにのりたい",
    "foryou",
    "foryoupage",
}


def _norm(value: str) -> str:
    return re.sub(r"[\s#＃・_××/／-]+", "", unicodedata.normalize("NFKC", value).casefold())


def _line(value: Any) -> str:
    return html.escape(" ".join(str(value or "").split())).replace("*", "").replace("`", "")[:240]


def matching_item(item: dict[str, Any], filters: dict[str, Any]) -> bool:
    return (
        (not filters.get("categories") or item.get("category") in filters["categories"])
        and (
            not filters.get("campaign_type")
            or filters["campaign_type"] in item.get("campaign_types", [])
        )
        and (not filters.get("geography") or item.get("geography") == filters["geography"])
    )


def _tags(post: dict[str, Any]) -> set[str]:
    return {
        tag
        for item in post.get("items", [])
        for name in item.get("campaign_tags", [])
        if len(tag := _norm(name)) >= 3 and tag not in _GENERIC
    }


def _identity(post: dict[str, Any], common_tags: dict[str, int]) -> str:
    tags = _tags(post)
    explicit = sorted(
        tag for tag in tags if any(s in tag for s in ("キャンペーン", "campaign", "cp"))
    )
    if explicit:
        return "tag:" + explicit[0]
    for item in post["items"]:
        name = _norm(item.get("campaign") or "")
        if name not in _GENERIC and any(s in name for s in ("キャンペーン", "campaign", "cp")):
            brand = _norm(item.get("brand") or "")
            return "tag:" + (name if name.startswith(brand) else brand + name)
    shared = tags & common_tags.keys()
    if shared:
        return "tag:" + min(shared, key=lambda tag: (-common_tags[tag], tag))
    # Missing campaign identity must never merge all posts from one brand.
    item = post["items"][0]
    brand = _norm(item.get("brand") or "")
    return f"post:{post['post_id']}:{brand}:{_norm(item.get('product') or '')}"


def aggregate_cases(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    posts_by_id = {post["post_id"]: post for post in hits}
    tag_counts: Counter[str] = Counter()
    tag_accounts: dict[str, set[str]] = {}
    for post in posts_by_id.values():
        tags = _tags(post)
        tag_counts.update(tags)
        for tag in tags:
            tag_accounts.setdefault(tag, set()).add(post["account_handle"])
    common_tags = {tag: count for tag, count in tag_counts.items() if len(tag_accounts[tag]) >= 2}
    groups: dict[str, dict[str, Any]] = {}
    for post in posts_by_id.values():
        if not post.get("items"):
            continue
        key = _identity(post, common_tags)
        group = groups.setdefault(key, {"posts": {}, "items": []})
        group["posts"][post["post_id"]] = post
        for item in post["items"]:
            if item not in group["items"]:
                group["items"].append(item)
    result = []
    for group in groups.values():
        posts = list(group["posts"].values())
        plays = [p for p in posts if type(p.get("metrics", {}).get("playCount")) is int]
        group.update(
            post_count=len(posts),
            account_count=len({p["account_handle"] for p in posts}),
            score=max(float(p.get("score") or 0) for p in posts),
            play_count=sum(p["metrics"]["playCount"] for p in plays),
            play_missing=len(posts) - len(plays),
            save_count=sum(
                p.get("metrics", {}).get("collectCount", 0)
                for p in posts
                if type(p.get("metrics", {}).get("collectCount")) is int
            ),
            save_missing=sum(
                type(p.get("metrics", {}).get("collectCount")) is not int for p in posts
            ),
            top=max(plays, key=lambda p: (p["metrics"]["playCount"], p["post_id"]))
            if plays
            else None,
        )
        result.append(group)
    return sorted(
        result, key=lambda g: (g["score"], g["play_count"], g["post_count"]), reverse=True
    )


def _period(group: dict[str, Any]) -> str:
    dates = sorted({p["posted_on"][:7] for p in group["posts"].values() if p.get("posted_on")})
    if not dates:
        return "投稿日不明"
    first, last = dates[0], dates[-1]
    year, month = first.split("-")
    if first == last:
        return f"{year}年{int(month)}月"
    if first[:4] == last[:4]:
        return f"{year}年{int(month)}〜{int(last[5:])}月"
    return f"{year}年{int(month)}月〜{last[:4]}年{int(last[5:])}月"


def _title(group: dict[str, Any]) -> str:
    def known(value: str | None) -> str:
        return value if value and "未特定" not in value and "不明" not in value else ""

    items = group["items"]
    brand = next((b for item in items if (b := known(item.get("brand")))), "")
    campaign = next(
        (c for item in items if (c := known(item.get("campaign"))) and _norm(c) not in _GENERIC),
        "",
    )
    tag = next(
        (
            tag
            for item in items
            for tag in item.get("campaign_tags", [])
            if known(tag) and len(_norm(tag)) >= 3 and _norm(tag) not in _GENERIC
        ),
        "",
    )
    name = ((campaign or tag) if brand else (tag or campaign)) or "施策紹介"
    if _norm(name) == "tiktokgo":
        name = "TikTok GO の" + (
            "宿泊紹介" if any(i.get("category") in {"旅行", "宿泊"} for i in items) else "施策紹介"
        )
    else:
        name = name.lstrip("#＃")
    if _norm(brand) == "tiktokgo":
        brand = "TikTok GO"
    return _line(name if _norm(name).startswith(_norm(brand)) else brand + " " + name)


def _details(group: dict[str, Any]) -> str:
    field = "facility" if any(i.get("facility") for i in group["items"]) else "product"
    names = Counter(
        name
        for post in group["posts"].values()
        for name in {
            item[field] for item in post["items"] if item in group["items"] and item.get(field)
        }
    )
    if not names:
        return ""
    unit = "施設" if field == "facility" else "商品"
    return (
        "（"
        + "、".join(_line(name) for name, _ in names.most_common(3))
        + (f" など{len(names)}{unit}" if len(names) > 3 else "を紹介")
        + "）"
    )


def _metric(group: dict[str, Any], name: str, label: str) -> str:
    missing = group[f"{name}_missing"]
    if missing == group["post_count"]:
        return f"{label}未取得"
    return f"{group[f'{name}_count']:,}{label}" + (f"（{missing}本未取得）" if missing else "")


def _pr(group: dict[str, Any]) -> str:
    posts = list(group["posts"].values())
    labels = []
    if any(p.get("has_hash_pr") for p in posts):
        labels.append("#PR 表記あり")
    if any(p.get("isSponsored") is True for p in posts):
        labels.append("提供表示あり")
    if not labels and any(p.get("pr_status") == "disclosed" for p in posts):
        labels.append("PR 表記あり")
    unknown = sum(p.get("pr_status") != "disclosed" for p in posts)
    if unknown:
        labels.append("PR 不明（参考）" + (f" {unknown}本" if unknown < len(posts) else ""))
    return "／".join(labels) or "PR 不明（参考）"


def render_hits(hits: list[dict[str, Any]], filters: dict[str, Any], limit: int = 3) -> str:
    groups = aggregate_cases(hits)
    primary = [
        g
        for g in groups
        if g["score"] >= 0.25 and any(matching_item(item, filters) for item in g["items"])
    ][:limit]
    cards = []
    for group in primary:
        top = group["top"]
        biggest = (
            f"。最大は{top['metrics']['playCount']:,}再生（@{_line(top['account_handle'])}）"
            if top
            else "。最大投稿は再生数未取得のため不明"
        )
        link = (
            f"\n〔いちばん伸びた投稿〕{top['post_url']}"
            if top
            else f"\n〔参考投稿〕{next(iter(group['posts'].values()))['post_url']}"
        )
        cards.append(
            f"**{_title(group)}（{_period(group)}）**\n"
            f"- {group['account_count']} アカウントで {group['post_count']} 本"
            f"{_details(group)}。{_pr(group)}\n"
            f"- 合計 {_metric(group, 'play', '再生')}・保存 {_metric(group, 'save', '件')}"
            f"{biggest}{link}"
        )
    intro = (
        "国内旅行のキャンペーン事例は、"
        if filters.get("geography") == "domestic"
        and filters.get("campaign_type") == "campaign"
        and "旅行" in filters.get("categories", [])
        else "自社メディアの事例は、"
    )
    message = (
        intro + f"この {len(cards)} 件です。\n\n" + "\n\n".join(cards)
        if cards
        else ("収録済みの投稿では、条件に合う案件はまだ確認できません。")
    )
    related_filters = {k: v for k, v in filters.items() if k != "campaign_type"}
    if "旅行" in filters.get("categories", []):
        related_filters["categories"] = [*filters["categories"], "娯楽"]
    related = next(
        (
            g
            for g in groups
            if g not in primary
            and g["score"] >= 0.25
            and not any(matching_item(i, filters) for i in g["items"])
            and any(matching_item(i, related_filters) for i in g["items"])
        ),
        None,
    )
    if related:
        message += (
            f"\n\n関連では、{_title(related)}（{_period(related)}・"
            f"{related['account_count']} アカウント・計 {_metric(related, 'play', '再生')}）"
            f"もあります。{_pr(related)}"
        )
    return message
