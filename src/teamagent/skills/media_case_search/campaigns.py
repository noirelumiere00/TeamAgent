"""Campaign identity and metrics are deterministic; no LLM aggregates counts."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any


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


def _identities(item: dict[str, Any], post_id: str) -> list[str]:
    brand = _norm(item.get("brand") or "")
    # Generic labels are not identities, even when the model calls them a campaign.
    generic = {
        "campaign",
        "キャンペーン",
        "pr",
        "旅行",
        "国内旅行",
        "おすすめ",
        "新商品",
        "イベント",
        "tiktok",
        "tiktokgo",
        "fyp",
        "ホテル",
        "観光",
        "おすすめスポット",
    }
    names = [item.get("campaign") or "", *item.get("campaign_tags", [])]
    identities = []
    for name in names:
        normalized = _norm(name)
        if brand and normalized.startswith(brand):
            normalized = normalized[len(brand) :]
        if brand and len(normalized) >= 3 and normalized not in generic:
            identities.append(brand + ":" + normalized)
    # Missing campaign identity must never merge all posts from one brand.
    return identities or [f"post:{post_id}:{brand}:{_norm(item.get('product') or '')}"]


def aggregate_cases(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parents: dict[str, str] = {}

    def root(key: str) -> str:
        parents.setdefault(key, key)
        path = []
        while parents[key] != key:
            path.append(key)
            key = parents[key]
        for visited in path:
            parents[visited] = key
        return key

    entries = []
    for hit in hits:
        for item in hit.get("items", []):
            keys = _identities(item, hit["post_id"])
            for key in keys[1:]:
                parents[root(key)] = root(keys[0])
            entries.append((keys[0], hit, item))
    groups: dict[str, dict[str, Any]] = {}
    for key, post, item in entries:
        group = groups.setdefault(root(key), {"posts": {}, "items": []})
        group["posts"][post["post_id"]] = post
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
    first = group["items"][0]
    name = first.get("campaign") or next(iter(first.get("campaign_tags") or []), "")
    if not name:
        name = first.get("product") or "施策名未特定"
    brand = first.get("brand") or "ブランド未特定"
    return _line(name if _norm(name).startswith(_norm(brand)) else brand + " " + name)


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
        facilities = sorted({x["facility"] for x in group["items"] if x.get("facility")})
        detail = (
            (
                "。"
                + "、".join(_line(f) for f in facilities[:2])
                + (f" など{len(facilities)}施設" if len(facilities) > 2 else "を紹介")
            )
            if facilities
            else ""
        )
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
            f"{detail}。{_pr(group)}\n"
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
    message += (
        "\n施設名が分かれば、その言葉でも事例を探せます。"
        if cards
        else "\nブランド名や施策名でも探せます。"
    )
    dates = sorted(
        {str(p.get("metrics_observed_at") or "")[:10] for p in hits if p.get("metrics_observed_at")}
    )
    if dates:
        message += (
            f"\n※ 数値は {dates[0]}"
            + (f"〜{dates[-1]}" if dates[0] != dates[-1] else "")
            + " の取得時点。"
        )
    return message
