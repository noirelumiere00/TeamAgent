"""Live attributes preserve golden values captured from the static generator."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from teamagent.karte_attributes import (
    build_client_summaries,
    client_attributes,
    dedup_fb_events,
    followup_overdue,
    homework_flag,
    normalize_sender,
    sender_from_body,
    temperature_tag,
)

TODAY = date(2026, 11, 2)


@pytest.fixture
def aliases() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2] / "data/connect_web_filters"
    data = json.loads((root / "tag_alias.json").read_text())
    return data["tans"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("沖井", "沖井智哉"),
        ("小倉\u3000岳之（ogura）", "小倉岳之"),
        ("佐藤杏香(Sato", "佐藤杏香"),
        ("川上壮汰_KawakamiSota", "川上壮汰"),
        ("清水達哉タイムスタ", "清水達哉"),
        ("徳野和香タ", "徳野和香"),
        ("架空 姓名090-1234-5678", "架空姓名"),
        ("担当連携ステー", "担当"),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "aaaaaaaaaaaaaaaaaaaa"),
        ("", ""),
    ],
)
def test_sender_normalization(name: str, expected: str, aliases: dict[str, str]) -> None:
    assert normalize_sender(name, aliases) == expected
    body = "長い本文" * 150 + "\n送信者：" + name + " タイムスタンプ: 2026/11/02 10:00"
    assert sender_from_body(body, aliases) == expected


@pytest.mark.parametrize(
    ("pos", "neg", "expected"),
    [
        ("", "", "高"),
        ("", "特になし", "高"),
        ("abc", "・特に無し。", "高"),
        ("", "なし", "高"),
        ("x", "-", "高"),
        ("abcdef", "abc", "ポジ優勢"),
        ("abc", "abcdef", "ネガ優勢"),
        ("abc", "abcd", "拮抗"),
        ("", "断り", "ネガ優勢"),
    ],
)
def test_temperature(pos: str, neg: str, expected: str) -> None:
    events = [{"pos": pos, "neg": neg}]
    assert temperature_tag(events) == expected
    assert temperature_tag([]) == ""


@pytest.mark.parametrize(
    ("days", "has_homework", "overdue"),
    [
        (0, False, False),
        (31, False, False),
        (32, True, False),
        (366, True, False),
        (367, True, True),
        (-1, False, False),
    ],
)
def test_homework_and_followup_boundaries(days: int, has_homework: bool, overdue: bool) -> None:
    last = (TODAY - timedelta(days=days)).isoformat()
    assert homework_flag("", last, TODAY) is False
    assert homework_flag("次に連絡", last, TODAY) is has_homework
    assert followup_overdue(last, TODAY) is overdue


@pytest.mark.parametrize("last", ["", "not-a-date", "2026-02-30"])
def test_unknown_last_contact(last: str) -> None:
    assert homework_flag("連絡", last, TODAY) is True
    assert not followup_overdue(last, TODAY)


def test_dedup_names_and_cap() -> None:
    events = [
        {"d": "2026-01-01", "pos": "", "neg": "", "by": f"担当{i}", "variant": "顧客"}
        for i in range(8)
    ]
    events += [
        dict(events[0], pos="同じ反応", by=""),
        dict(events[0], pos="同じ反応", by="補完担当"),
    ]
    assert dedup_fb_events(events) == [
        {"by": "担当0", "d": "2026-01-01", "neg": "", "pos": "", "variant": "顧客"},
        {"by": "担当1", "d": "2026-01-01", "neg": "", "pos": "", "variant": "顧客"},
        {"by": "担当2", "d": "2026-01-01", "neg": "", "pos": "", "variant": "顧客"},
        {"by": "担当3", "d": "2026-01-01", "neg": "", "pos": "", "variant": "顧客"},
        {"by": "担当4", "d": "2026-01-01", "neg": "", "pos": "", "variant": "顧客"},
        {"by": "担当5", "d": "2026-01-01", "neg": "", "pos": "", "variant": "顧客"},
        {"by": "担当6", "d": "2026-01-01", "neg": "", "pos": "", "variant": "顧客"},
        {"by": "担当7", "d": "2026-01-01", "neg": "", "pos": "", "variant": "顧客"},
        {"by": "補完担当", "d": "2026-01-01", "neg": "", "pos": "同じ反応", "variant": "顧客"},
    ]
    result = client_attributes("顧客", events, [], today=TODAY)
    assert result["owners"] == ["担当0", "担当1", "担当2", "担当3", "担当4"]
    assert result["temperature"] == "高"
    assert result["next_action_short"] == ""


def test_record_fields_use_120_character_reactions_and_full_sender() -> None:
    rows = [
        {
            "id": "fb1",
            "metadata": {
                "client_name": "顧客",
                "is_sales_fb": "true",
                "positive_reaction": "好" * 300,
                "negative_reaction": "難" * 80,
                "next_action": "次" * 200,
            },
            "modified_at": "2026-01-01",
            "content": "本文" * 300 + "\n送信者: 沖井 タイムスタンプ: 2026/1/1 12:00",
        }
    ]
    result = build_client_summaries(rows, today=TODAY)["顧客"]
    assert result["temperature"] == "拮抗"  # not the uncapped positive majority
    assert result["owners"] == ["沖井智哉"]
    assert result["next_action_short"] == "次" * 40
    assert result["next_action"] == "次" * 200


def test_attributes_activity_counts_and_dates() -> None:
    name = "比較顧客"
    variant = "株式会社 " + name + " 様"
    events = [
        {
            "d": "2026-02-02",
            "src": "フォーム",
            "by": "担当B",
            "ph": "提案",
            "bant": "B",
            "menu": "",
            "pos": "好感",
            "neg": "特になし",
            "next": "次" * 55,
            "variant": name,
        },
        {
            "d": "2026-01-01",
            "src": "フォーム",
            "by": "担当A",
            "ph": "",
            "bant": "",
            "menu": "",
            "pos": "別の反応",
            "neg": "",
            "next": "",
            "variant": name,
        },
    ]
    documents = [{"id": "A", "modified_at": "2026-01-01"}, {"id": "B", "modified_at": "2026-03-01"}]
    attrs = client_attributes(
        name,
        events + [{**event, "variant": variant} for event in events],
        documents,
        today=TODAY,
    )
    assert (attrs["fb_count"], attrs["doc_count"], attrs["last_contact"]) == (
        2,
        2,
        "2026-03-01",
    )
    assert attrs["next_action_short"] == "次" * 40
    assert attrs["owners"] == ["担当A", "担当B"]
    assert attrs["temperature"] == "高"
    assert attrs["has_homework"] is True


def test_variant_fb_totals_and_activity_identity() -> None:
    rows: list[dict[str, Any]] = []
    for variant in ("顧客", "株式会社 顧客 様"):
        for i in range(2):
            rows.append(
                {
                    "id": f"{variant}{i}",
                    "metadata": {
                        "client_name": variant,
                        "is_sales_fb": "true",
                        "positive_reaction": "同じ反応",
                    },
                    "modified_at": "2026-01-01",
                    "content": "",
                }
            )
    rows += [
        {
            "id": "doc",
            "metadata": {"client_name": "別社", "cls_project": "顧客"},
            "modified_at": "2026-03-01",
        },
        {
            "id": "title-only",
            "metadata": {"client_name": "別社"},
            "title": "顧客",
            "modified_at": "2099-01-01",
        },
    ]
    attrs = build_client_summaries(rows, today=TODAY)["顧客"]
    assert (
        attrs["fb_count"] == 2
    )  # 4 records - 2 cross-variant copies; within-variant multiplicity remains
    assert attrs["doc_count"] == 1 and attrs["last_contact"] == "2026-03-01"
    assert attrs["temperature"] == "高"


@pytest.mark.parametrize(
    ("flag", "is_fb"),
    [(True, True), ("true", True), (False, False), ("false", False), (None, False), (1, False)],
)
def test_ingest_json_boolean_and_legacy_string_fb_flags(flag: Any, is_fb: bool) -> None:
    attrs = build_client_summaries(
        [
            {
                "id": "id",
                "metadata": {"client_name": "顧客", "is_sales_fb": flag},
                "modified_at": "2026-01-01",
                "content": "",
            }
        ],
        today=TODAY,
    )["顧客"]
    assert attrs["fb_count"] == int(is_fb) and attrs["doc_count"] == int(not is_fb)
