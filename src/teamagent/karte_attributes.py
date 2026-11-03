"""RLS 可視の活動から、静的 Vault と同じカルテ属性を決定論で求める。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from teamagent.client_identity import client_identity_key
from teamagent.client_properties import identity_value_map, resolve_client_industry

# 本番イメージに tzdata が無いので IANA 名ではなく固定オフセットで日本時間を表す。
JST = timezone(timedelta(hours=9))

CLIENT_QUICK_FILTERS = {
    "homework": "宿題あり",
    "high": "温度感 高",
    "positive": "温度感 ポジ優勢",
    "negative": "温度感 ネガ優勢",
    "balanced": "温度感 拮抗",
    "overdue": "フォロー漏れ",
}
SENDER_RE = re.compile(
    r"送信者[ \t　]*[:：][ \t　]*([^\n]*?)"
    r"(?=[ \t　]*(?:タイムスタンプ|連携ステータス)[ \t　]*[:：]|\n|$)"
)
SENDER_TRUNCATED_RE = re.compile(
    r"[ \t　]*(?:タイムス(?:タ(?:ン(?:プ)?)?)?|連携ステ(?:ー(?:タ(?:ス)?)?)?)$"
)
TEMP_NEG_NONE_RE = re.compile(r"^[・\s]*(特になし|特に無し|なし|-)[。、.\s]*$")


def _filters_dir() -> Path:
    """名寄せ辞書の置き場所。

    本番 image は no-editable install で、このファイルは site-packages にあるため
    ``parents[2]`` はリポジトリ直下にならない（2026-11-03 本番 r95 で FileNotFoundError）。
    リポジトリの位置、作業ディレクトリ（image は WORKDIR /app に data/ を焼く）の順に探す。
    """
    candidates = [Path(__file__).resolve().parents[2], Path.cwd()]
    for base in candidates:
        path = base / "data" / "connect_web_filters"
        if (path / "tag_alias.json").is_file():
            return path
    return candidates[-1] / "data" / "connect_web_filters"


@lru_cache(maxsize=1)
def _references() -> tuple[dict[str, Any], dict[str, str]]:
    root = _filters_dir()
    aliases = json.loads((root / "tag_alias.json").read_text())
    industry = json.loads((root / "client_industry.json").read_text())
    return aliases, identity_value_map(industry["industry"])


def normalize_sender(raw: str, aliases: dict[str, str]) -> str:
    value = SENDER_TRUNCATED_RE.sub("", raw.rstrip())
    value = re.sub(r"[0-9][0-9\-‐−–]{5,}$", "", value)
    value = re.split(r"[（(_]", value, maxsplit=1)[0]
    value = value.replace(" ", "").replace("　", "").strip()[:20]
    return aliases.get(value, value)


def sender_from_body(body: str, aliases: dict[str, str]) -> str:
    match = SENDER_RE.search(body)
    return normalize_sender(match[1], aliases) if match else ""


def temperature_tag(timeline: list[dict[str, Any]]) -> str:
    if not timeline:
        return ""
    pos, neg = timeline[0].get("pos", ""), timeline[0].get("neg", "")
    if not neg.strip() or TEMP_NEG_NONE_RE.match(neg):
        return "高"
    if len(pos) >= 2 * len(neg):
        return "ポジ優勢"
    if len(neg) >= 2 * len(pos):
        return "ネガ優勢"
    return "拮抗"


def homework_flag(next_action: str, last: str, today: date) -> bool:
    if not next_action:
        return False
    try:
        last_date = datetime.strptime(last, "%Y-%m-%d").date()
    except ValueError:
        return True
    return (today - last_date).days > 31


def followup_overdue(last: str, today: date) -> bool:
    try:
        last_date = datetime.strptime(last, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (today - last_date).days > 366


def fb_dedup_key(event: dict[str, Any]) -> str:
    raw = unicodedata.normalize("NFKC", event.get("pos", "") + event.get("neg", ""))
    return re.sub(r"\s+", "", raw).lower()[:120]


def dedup_fb_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for event in events:
        key = fb_dedup_key(event)
        if not key:
            out.append(event)
            continue
        index = seen.get(key)
        if index is None:
            seen[key] = len(out)
            out.append(event)
            continue
        keep, drop = (
            (event, out[index])
            if event.get("d") and not out[index].get("d")
            else (out[index], event)
        )
        if not keep.get("by") and drop.get("by"):
            keep = {**keep, "by": drop["by"]}
        out[index] = keep
    return out


def client_attributes(
    client: str,
    events: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    *,
    today: date,
) -> dict[str, Any]:
    """全件のイベント・活動資料から集計。属性用 tl だけ静的版同様に dedup→最新30件。"""
    aliases, industry_master = _references()
    unique = dedup_fb_events(events)
    timeline = sorted(unique, key=lambda event: event.get("d") or "", reverse=True)[:30]
    latest = timeline[0] if timeline else {}
    last_fb = str(latest.get("d") or "")
    last = max([last_fb, *(str(doc.get("modified_at") or "") for doc in documents)])
    per_variant: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        if key := fb_dedup_key(event):
            per_variant[event["variant"]][key] += 1
    totals: Counter[str] = Counter()
    maxima: Counter[str] = Counter()
    for counts in per_variant.values():
        totals.update(counts)
        for key, count in counts.items():
            maxima[key] = max(maxima[key], count)
    fb_count = max(len(unique), len(events) - sum(totals[key] - maxima[key] for key in totals))
    next_full = str(latest.get("next") or "")
    owner_key = client_identity_key(client)
    primary = [
        d["industry"]
        for d in documents
        if d.get("industry") and client_identity_key(d.get("client_name")) == owner_key
    ]
    project = [
        d["industry"]
        for d in documents
        if d.get("industry") and client_identity_key(d.get("cls_project")) == owner_key
    ]
    industry = resolve_client_industry(client, primary, project, industry_master)
    return {
        "owners": sorted({event.get("by", "") for event in timeline} - {""})[:5],
        "temperature": temperature_tag(timeline),
        "next_action": next_full,
        "next_action_short": next_full[:40],
        "last_contact": last,
        "has_homework": homework_flag(next_full[:40], last, today),
        "followup_overdue": followup_overdue(last, today),
        "fb_count": fb_count,
        "doc_count": len({d["id"] for d in documents}),
        "latest_fb_at": last_fb or None,
        "deal_phase": latest.get("ph") or None,
        "bant_score": latest.get("bant") or None,
        "industry": aliases.get("industry", {}).get(industry, industry),
    }


def build_client_summaries(
    records: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> dict[str, dict[str, Any]]:
    today = today or datetime.now(JST).date()
    aliases, _ = _references()
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        metadata = record["metadata"] or {}
        is_fb = metadata.get("is_sales_fb") is True or metadata.get("is_sales_fb") == "true"
        primary = str(metadata.get("client_name") or "").strip()
        project = str(metadata.get("cls_project") or "").strip()
        names = [primary or project] if is_fb else [primary, project]
        identities = {
            client_identity_key(name): str(name).strip()
            for name in names
            if client_identity_key(name)
        }
        for key, name in identities.items():
            group = groups.setdefault(key, {"name": name, "events": [], "documents": []})
            group["name"] = min(group["name"], name)
            if is_fb:
                event = {
                    "d": record.get("modified_at") or "",
                    "variant": str(primary or project),
                    "by": sender_from_body(record.get("content") or "", aliases.get("tans", {})),
                    **{
                        short: str(metadata.get(long) or "").strip()[:cap]
                        for short, long, cap in (
                            ("pos", "positive_reaction", 120),
                            ("neg", "negative_reaction", 120),
                            ("ph", "deal_phase", 80),
                            ("bant", "bant_score", 80),
                        )
                    },
                    "next": str(metadata.get("next_action") or "").strip(),
                }
                group["events"].append(event)
            else:
                group["documents"].append(
                    {
                        "id": record["id"],
                        "modified_at": record.get("modified_at"),
                        "client_name": primary,
                        "cls_project": project,
                        "industry": metadata.get("cls_industry"),
                    }
                )
    result = {}
    for key, group in groups.items():
        attrs = client_attributes(group["name"], group["events"], group["documents"], today=today)
        result[key] = {"name": group["name"], "document_count": attrs["doc_count"], **attrs}
    return result


def matches_client_quick(attributes: dict[str, Any], quick: str) -> bool:
    if quick == "homework":
        return bool(attributes["has_homework"])
    if quick == "overdue":
        return bool(attributes["followup_overdue"])
    return bool(
        attributes["temperature"]
        == {
            "high": "高",
            "positive": "ポジ優勢",
            "negative": "ネガ優勢",
            "balanced": "拮抗",
        }[quick]
    )
