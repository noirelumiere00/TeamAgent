"""ショート動画データベースの案件単位集計（campaign_aggregate・2026-09-15）。

フェイクは本番の失敗モードを再現する:
- 広告主名が ``#N/A`` の行（自社メディアの通常投稿・実測 1,482 本）が混在する
- 数値が ``1,125`` のような桁区切り文字列で入っている
- 同じ案件の行が離れた位置にある（並び順に依存しない）
変異（それぞれ該当テストが赤になる）:
- ``#N/A`` の除外を外す → 案件数が増える / 集計に混入する
- external_id を行番号ベースに戻す → 並び替えで id が変わる
- pipeline の分岐を外す → 行単位の文書が upsert される
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.ingest.campaign_aggregate import (
    aggregate_campaigns,
    campaign_external_id,
    campaign_metadata,
    campaign_title,
    format_campaign_document,
    is_missing,
    to_number,
)
from teamagent.ingest.loader import GSheetSpec, GSheetsTabSpec, load_ingest_sources

REAL_YAML = Path(__file__).resolve().parents[2] / "data" / "ingest_sources.yaml"
SHEET_ID = "19Y_IRJxr1oPLqNwgpi9nHRkyMqZPuw13ZikN2gD6pWQ"
GID = 386118222

HEADERS = (
    "動画URL",
    "動画ID(抽出)",
    "アカウント名",
    "広告主名",
    "案件名",
    "総再生数",
    "総いいね",
    "総シェア",
    "総コメント",
    "総保存",
    "テキスト",
    "広告名",
    "広告目的",
    "コスト",
    "インプレッション",
    "クリック（誘導先）",
    "CTR（誘導先）",
)


def _row(
    url: str,
    account: str,
    advertiser: str,
    campaign: str,
    plays: str,
    saves: str = "0",
    text: str = "",
    cost: str = "0",
    ctr: str = "",
) -> tuple[str, ...]:
    return (
        url,
        url.rsplit("/", 1)[-1],
        account,
        advertiser,
        campaign,
        plays,
        "10",
        "1",
        "0",
        saves,
        text,
        "未配信" if cost == "0" else "ad_x",
        "-",
        cost,
        "0" if cost == "0" else "4000",
        "0",
        ctr,
    )


ROWS: tuple[tuple[str, ...], ...] = (
    _row(
        "https://t/v/1",
        "acct_a",
        "アース製薬",
        "みんなのシリカ",
        "1,125",
        "12",
        "シリカ水で #朝活 #美容",
    ),
    _row("https://t/v/2", "acct_b", "#N/A", "#N/A", "9,999", "3", "自社メディアの投稿 #美容"),
    _row("https://t/v/3", "acct_a", "サラヤ", "ラカントsシロップ", "726", "0", "ラカント #レシピ"),
    _row(
        "https://t/v/4",
        "acct_c",
        "アース製薬",
        "みんなのシリカ",
        "2,892",
        "40",
        "#美容 #朝活 のルーティン",
        cost="1132",
        ctr="0.1%",
    ),
    _row("https://t/v/5", "acct_a", "", "", "500", "1", "広告主名なし"),
    _row("https://t/v/6", "acct_a", "アース製薬", "みんなのシリカ", "300", "2", "#美容"),
)


# ── 純関数 ─────────────────────────────────────────────────────────────


def test_missing_and_number_parsing() -> None:
    assert is_missing("#N/A") and is_missing("") and is_missing(" #n/a ") and is_missing("-")
    assert not is_missing("アース製薬")
    assert to_number("1,125") == 1125.0
    assert to_number("0.1%") == pytest.approx(0.001)
    assert to_number("#N/A") is None and to_number("abc") is None


def test_rows_without_advertiser_or_campaign_are_not_attached_to_any_campaign() -> None:
    aggregates = aggregate_campaigns(HEADERS, ROWS)
    assert [(a.advertiser, a.campaign) for a in aggregates] == [
        ("アース製薬", "みんなのシリカ"),
        ("サラヤ", "ラカントsシロップ"),
    ]
    earth = aggregates[0]
    # #N/A 行（9,999 再生）と空行（500 再生）は集計に一切入らない。
    assert earth.video_count == 3
    assert earth.total("plays") == 1125 + 2892 + 300
    assert earth.median("plays") == 1125
    assert earth.maximum("plays") == 2892
    assert earth.accounts == ("acct_a", "acct_c")
    assert earth.ad_video_count == 1
    assert earth.top_videos[0].url == "https://t/v/4"
    assert earth.top_hashtags[0] == ("美容", 3)
    assert earth.save_rate_median == pytest.approx(12 / 1125)


def test_external_id_is_stable_across_row_order_and_spacing() -> None:
    a = campaign_external_id(SHEET_ID, GID, "アース製薬", "みんなのシリカ")
    b = campaign_external_id(SHEET_ID, GID, " アース製薬 ", "みんなのシリカ　")
    c = campaign_external_id(SHEET_ID, GID, "サラヤ", "ラカントsシロップ")
    assert a == b and a != c
    assert a.startswith(f"{SHEET_ID}:{GID}:campaign:")
    shuffled = ROWS[::-1]
    ids_original = [
        campaign_external_id(SHEET_ID, GID, x.advertiser, x.campaign)
        for x in aggregate_campaigns(HEADERS, ROWS)
    ]
    ids_shuffled = [
        campaign_external_id(SHEET_ID, GID, x.advertiser, x.campaign)
        for x in aggregate_campaigns(HEADERS, shuffled)
    ]
    assert ids_original == ids_shuffled


def test_document_text_and_metadata_carry_campaign_facts() -> None:
    earth = aggregate_campaigns(HEADERS, ROWS)[0]
    text = format_campaign_document(earth)
    assert text.startswith("施策実績: アース製薬 / みんなのシリカ")
    assert "投稿本数: 3 本" in text and "広告配信あり: 1 本" in text
    assert "中央値 1,125" in text and "最大 2,892" in text
    assert "1. 再生 2,892" in text and "https://t/v/4" in text
    assert "#美容（3 本）" in text
    assert "—" not in text and "**" not in text  # AI 生成感の記号を本文に入れない
    meta = campaign_metadata(earth)
    assert meta["cls_doc_type"] == "施策実績" and meta["cls_project"] == "アース製薬"
    assert meta["video_count"] == "3" and meta["plays_median"] == "1125"
    assert campaign_title(earth) == "施策実績 アース製薬 みんなのシリカ"


def test_required_columns_missing_is_an_error() -> None:
    with pytest.raises(ValueError, match="required columns missing"):
        aggregate_campaigns(("動画URL", "総再生数"), ROWS)


# ── pipeline 経路（extra_metadata.campaign_aggregate で分岐） ───────────


class _FakeEmbedder:
    def embed_passage(self, text: str) -> list[float]:
        return [0.0, 0.0, 0.0]


class _FakeRepository:
    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, Any]] = []

    def get_document_metadata_values(
        self, source_type: str, external_ids: Any, keys: Any
    ) -> dict[str, dict[str, str]]:
        return {}

    def upsert_document_with_chunks(
        self,
        doc: Any,
        chunks: list[Any],
        request_id: str,
        *,
        replace_existing_chunks: bool = True,
    ) -> str:
        self.upsert_calls.append(
            {
                "external_id": doc.external_id,
                "title": doc.title,
                "metadata": dict(doc.metadata),
                "content": chunks[0].content,
            }
        )
        return "fake-doc-id"


def _install_fake_sheets(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    from teamagent.adapters.gsheets_client import TabRows

    fake_client = MagicMock()
    fake_client.get_sheet_metadata.side_effect = RuntimeError("no metadata in test")
    fake_client.get_tab_rows.return_value = TabRows(
        sheet_id=SHEET_ID, tab_name="データベース", headers=HEADERS, rows=ROWS, row_count=len(ROWS)
    )
    monkeypatch.setattr(
        "teamagent.adapters.gsheets_client.GSheetsClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )
    return fake_client


def _spec(*, aggregate: bool) -> GSheetSpec:
    extra = {"topic": "施策実績"}
    if aggregate:
        extra["campaign_aggregate"] = "true"
    return GSheetSpec(
        sheet_id=SHEET_ID,
        sheet_name="ショート動画データベース",
        description="",
        tabs=(GSheetsTabSpec(gid=GID, tab_name="データベース"),),
        extra_metadata=extra,
        sheet_id_env="SHORT_VIDEO_DB_SHEET_ID",
    )


def _run(spec: GSheetSpec, repo: _FakeRepository) -> tuple[int, int]:
    from teamagent.ingest.pipeline import _ingest_gsheet

    return _ingest_gsheet(
        spec,
        embedder=_FakeEmbedder(),
        repository=repo,
        owner_email="s-komata@vectorinc.co.jp",
        dry_run=False,
        request_id="r-campaign",
    )


def test_pipeline_upserts_one_document_per_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sheets(monkeypatch)
    repo = _FakeRepository()
    docs, chunks = _run(_spec(aggregate=True), repo)
    assert (docs, chunks) == (2, 2)
    ids = [c["external_id"] for c in repo.upsert_calls]
    assert all(":campaign:" in i for i in ids)
    assert ids[0] == campaign_external_id(SHEET_ID, GID, "アース製薬", "みんなのシリカ")
    first = repo.upsert_calls[0]
    assert first["title"] == "施策実績 アース製薬 みんなのシリカ"
    assert first["metadata"]["campaign_aggregate"] == "true"
    assert first["metadata"]["cls_project"] == "アース製薬"
    assert first["metadata"]["topic"] == "施策実績"
    assert first["metadata"]["gid"] == str(GID)
    assert "投稿本数: 3 本" in first["content"]


def test_pipeline_without_flag_keeps_row_unit_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """宣言の無い spec は従来どおり 1 行 = 1 document（既存 2 シートの経路は不変）。"""
    _install_fake_sheets(monkeypatch)
    repo = _FakeRepository()
    docs, _ = _run(_spec(aggregate=False), repo)
    assert docs == len(ROWS)
    assert all(":campaign:" not in c["external_id"] for c in repo.upsert_calls)


# ── 実 yaml のエントリ ─────────────────────────────────────────────────


def test_real_yaml_short_video_db_entry_is_skipped_until_env_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHORT_VIDEO_DB_SHEET_ID", raising=False)
    monkeypatch.delenv("SHORT_VIDEO_DB_SHEET_GID", raising=False)
    sources = load_ingest_sources(REAL_YAML, skip_placeholder=True)
    assert not any(s.sheet_id_env == "SHORT_VIDEO_DB_SHEET_ID" for s in sources.gsheets)


def test_real_yaml_short_video_db_entry_resolves_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHORT_VIDEO_DB_SHEET_ID", SHEET_ID)
    monkeypatch.setenv("SHORT_VIDEO_DB_SHEET_GID", str(GID))
    sources = load_ingest_sources(REAL_YAML, skip_placeholder=True)
    match = [s for s in sources.gsheets if s.sheet_id == SHEET_ID]
    assert len(match) == 1
    spec = match[0]
    assert spec.extra_metadata.get("campaign_aggregate") == "true"
    assert [t.gid for t in spec.tabs] == [GID]
    assert spec.tabs[0].tab_name == "データベース"
