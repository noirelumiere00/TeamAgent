"""Live attributes match the actual static generator, without editing that generator."""

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
from tests.scripts.test_build_app_html import _mod, _payload, _run, _write_doc
from tests.scripts.test_build_app_html import sidecars as sidecars  # reuse the static build fixture

TODAY = date(2026, 11, 2)


@pytest.fixture
def aliases(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    root = Path(__file__).resolve().parents[2] / "data/connect_web_filters"
    data = json.loads((root / "tag_alias.json").read_text())
    monkeypatch.setattr(_mod, "TAG_ALIAS", data)
    return data["tans"]


@pytest.mark.parametrize(
    "name",
    [
        "沖井",
        "小倉　岳之（ogura）",
        "佐藤杏香(Sato",
        "川上壮汰_KawakamiSota",
        "清水達哉タイムスタ",
        "徳野和香タ",
        "架空 姓名090-1234-5678",
        "担当連携ステー",
        "a" * 30,
        "",
    ],
)
def test_sender_normalization_matches_static(name: str, aliases: dict[str, str]) -> None:
    assert normalize_sender(name, aliases) == _mod._norm_sender(name)
    body = "長い本文" * 150 + "\n送信者：" + name + " タイムスタンプ: 2026/11/02 10:00"
    match = _mod.FB_SENDER_RE.search(body)
    assert sender_from_body(body, aliases) == _mod._norm_sender(match[1])


@pytest.mark.parametrize(
    ("pos", "neg"),
    [
        ("", ""),
        ("", "特になし"),
        ("abc", "・特に無し。"),
        ("", "なし"),
        ("x", "-"),
        ("abcdef", "abc"),
        ("abc", "abcdef"),
        ("abc", "abcd"),
        ("", "断り"),
    ],
)
def test_temperature_matches_static(pos: str, neg: str) -> None:
    events = [{"pos": pos, "neg": neg}]
    assert temperature_tag(events) == _mod.temperature_tag(events)
    assert temperature_tag([]) == _mod.temperature_tag([])


@pytest.mark.parametrize("days", [0, 31, 32, 366, 367, -1])
def test_homework_and_followup_boundaries_match_static(days: int) -> None:
    last = (TODAY - timedelta(days=days)).isoformat()
    for action in ("", "次に連絡"):
        assert homework_flag(action, last, TODAY) == _mod.homework_flag(action, last, TODAY)
    assert followup_overdue(last, TODAY) == (_mod._age_bucket(last, TODAY) == "1年以上前")


@pytest.mark.parametrize("last", ["", "not-a-date", "2026-02-30"])
def test_unknown_last_contact_matches_static(last: str) -> None:
    assert homework_flag("連絡", last, TODAY) == _mod.homework_flag("連絡", last, TODAY)
    assert not followup_overdue(last, TODAY)


def test_dedup_names_and_cap_match_static(aliases: dict[str, str]) -> None:
    events = [
        {"d": "2026-01-01", "pos": "", "neg": "", "by": f"担当{i}", "variant": "顧客"}
        for i in range(8)
    ]
    events += [
        dict(events[0], pos="同じ反応", by=""),
        dict(events[0], pos="同じ反応", by="補完担当"),
    ]
    assert dedup_fb_events(events) == _mod.dedup_fb_events(events)
    result = client_attributes("顧客", events, [], today=TODAY)
    tl = _mod._sort_fb_events(_mod.dedup_fb_events(events))
    assert result["owners"] == _mod.tans_of(tl) and len(result["owners"]) == 5
    assert result["temperature"] == _mod.temperature_tag(tl)
    assert result["next_action_short"] == _mod.next_action(tl)


def test_record_fields_use_120_character_reactions_and_full_sender(aliases: dict[str, str]) -> None:
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
    assert result["temperature"] == _mod.temperature_tag([{"pos": "好" * 120, "neg": "難" * 80}])
    assert result["temperature"] == "拮抗"  # not the uncapped positive majority
    assert result["owners"] == ["沖井智哉"]
    assert result["next_action_short"] == _mod.next_action([{"next": "次" * 120}])
    assert result["next_action"] == "次" * 200


def test_attributes_match_generated_payload_activity_counts_and_dates(
    sidecars: Path,
    tmp_path: Path,
) -> None:
    vault = tmp_path / "comparison-vault"
    (vault / "clients").mkdir(parents=True)
    (vault / "docs").mkdir()
    name = "比較顧客"
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
    body = f"---\nclient: {name}\nfb_count: 2\ndoc_count: 99\n---\n\n# {name}\n\n## 営業FB時系列\n"
    for i, event in enumerate(events):
        body += f"\n### {event['d']} 営業FB row {i + 1}\n"
        body += "\n".join(
            f"- {label}: {event[key]}"
            for label, key in (
                ("フェーズ", "ph"),
                ("BANT", "bant"),
                ("ポジ反応", "pos"),
                ("ネガ反応", "neg"),
                ("次アクション", "next"),
            )
        )
        body += f"\n> 送信者: {event['by']} タイムスタンプ: 2026/02/02 10:00\n"
    body += "\n## 関連資料\n- [[docs/A]]\n- [[docs/B]]\n- [[docs/C]]\n"
    (vault / "clients" / f"{name}.md").write_text(body)
    variant = "株式会社 " + name + " 様"
    (vault / "clients" / f"{variant}.md").write_text(body.replace(name, variant))
    _write_doc(vault, "A", client=name, modified_at="2026-01-01")
    _write_doc(vault, "B", client="別社", project=name, modified_at="2026-03-01")
    _write_doc(
        vault, "C", client="別社", title=name + "だけがタイトルにある資料", modified_at="2026-12-01"
    )
    out = tmp_path / "comparison.html"
    assert _run(vault, out) == 0
    [static] = _payload(out.read_text())["clients"]
    documents = [{"id": "A", "modified_at": "2026-01-01"}, {"id": "B", "modified_at": "2026-03-01"}]
    attrs = client_attributes(
        name,
        events + [{**event, "variant": variant} for event in events],
        documents,
        today=date.today(),
    )
    assert (attrs["fb_count"], attrs["doc_count"], attrs["last_contact"]) == (
        static["fb"],
        static["doc"],
        static["last"],
    )
    assert attrs["fb_count"] == 2 and attrs["doc_count"] == 2
    assert attrs["next_action_short"] == static["nx"] and attrs["owners"] == static["tans"]
    assert attrs["temperature"] == static["temp"]
    assert attrs["has_homework"] == bool(static["hw"])


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
