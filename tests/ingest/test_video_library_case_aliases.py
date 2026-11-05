"""Execute catalog/evaluation SELECTs with SQLite fixtures; PostgreSQL view/RLS checks are separate."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.ingest import video_library_case_evaluation as evaluation
from teamagent.ingest.video_library_case_models import DEFAULT_MODEL, Prediction
from teamagent.ingest.video_library_case_repository import CaseRepository


@pytest.fixture
def catalog() -> Iterator[Any]:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = lambda cursor, row: dict(
            zip([c[0] for c in cursor.description], row, strict=True)
        )
        conn.executescript("""
            CREATE TABLE vl_campaigns(advertiser TEXT, campaign TEXT);
            CREATE TABLE vl_case_aliases(from_advertiser TEXT, from_campaign TEXT,
                to_advertiser TEXT, to_campaign TEXT);
            CREATE TABLE vl_videos(platform TEXT,video_id TEXT,advertiser TEXT,campaign TEXT,is_pr BOOLEAN);
            CREATE TABLE vl_videos_effective(platform TEXT,video_id TEXT,advertiser TEXT,campaign TEXT,
                caption TEXT,origin TEXT);
            INSERT INTO vl_campaigns VALUES ('TikTok','20260717'),('ラフォーレ','セール'),
                ('トップバリュ','昭和レトロ'),('AEON','昭和レトロ');
            INSERT INTO vl_case_aliases VALUES ('TikTok','20260717','ラフォーレ','セール'),
                ('トップバリュ','昭和レトロ','AEON',NULL);
            INSERT INTO vl_videos VALUES ('tiktok','1','TikTok','20260717',true),
                ('tiktok','2','トップバリュ','昭和レトロ',true),('tiktok','3',NULL,NULL,true);
            INSERT INTO vl_videos_effective VALUES ('tiktok','1','ラフォーレ','セール','ラフォーレ セール','own'),
                ('tiktok','2','AEON','昭和レトロ','昭和レトロ','own'),
                ('tiktok','3','ラフォーレ','セール','an inferred label is not truth','own'),
                ('tiktok','4','ラフォーレ','セール','external PR','external');
            INSERT INTO vl_videos VALUES ('tiktok','4','TikTok','20260717',true);
        """)
        yield conn


def test_candidates_are_canonical_deduplicated_and_null_campaign_keeps_original(
    catalog: Any,
) -> None:
    cases = CaseRepository.cases(catalog)
    assert len(cases) == 2
    assert set((c.advertiser, c.campaign) for c in cases) == {
        ("ラフォーレ", "セール"),
        ("AEON", "昭和レトロ"),
    }


def test_evaluation_truth_and_known_advertiser_are_canonical_but_inferred_rows_excluded(
    catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = CaseRepository.evaluation_rows(catalog)
    assert [(r["video_id"], r["advertiser"], r["campaign"]) for r in rows] == [
        ("1", "ラフォーレ", "セール"),
        ("2", "AEON", "昭和レトロ"),
    ]
    cases = CaseRepository.cases(catalog)
    engine = MagicMock()
    engine.client.model_id = DEFAULT_MODEL
    engine.prompt = "prompt"
    engine.cost_usd = engine.reserved_usd = 0.0
    engine.predict.side_effect = [
        Prediction(
            advertiser=r["advertiser"],
            campaign=r["campaign"],
            match_type="existing",
            confidence=0.95,
            evidence=[],
        )
        for r in rows
    ]
    calibrate = MagicMock(return_value=None)
    monkeypatch.setattr(evaluation, "calibrate", calibrate)
    evaluation.evaluate(rows, cases, engine, seed=42)
    trials = calibrate.call_args.args[0]
    assert {(t.truth.advertiser, t.truth.campaign) for t in trials} == {
        ("ラフォーレ", "セール"),
        ("AEON", "昭和レトロ"),
    }
    first = engine.predict.call_args_list[0].args
    assert first[0].advertiser == "ラフォーレ" and first[1] == cases
    assert engine.predict.call_args_list[1].args[0].advertiser is None


def test_inference_routes_external_pr_even_with_campaign_but_skips_non_pr_and_human(
    catalog: Any,
) -> None:
    catalog.executescript("""
        ALTER TABLE vl_videos ADD COLUMN caption TEXT DEFAULT 'MOFT #PR';
        ALTER TABLE vl_videos_effective ADD COLUMN is_pr BOOLEAN DEFAULT true;
        CREATE TABLE vl_video_campaign_links(platform TEXT,video_id TEXT,source TEXT,
            prompt_version TEXT,input_hash TEXT,decided_at TEXT);
    """)
    pg = MagicMock()
    pg.connection.return_value.__enter__.return_value = catalog
    repo = CaseRepository(pg)
    rows = repo.load()[1]
    assert {r["video_id"]: r["effective_origin"] for r in rows} == {"3": "own", "4": "external"}
    catalog.execute("UPDATE vl_videos_effective SET is_pr=false WHERE video_id='4'")
    assert [r["video_id"] for r in repo.load()[1]] == ["3"]
    catalog.execute("UPDATE vl_videos_effective SET is_pr=true WHERE video_id='4'")
    catalog.execute(
        "INSERT INTO vl_video_campaign_links VALUES ('tiktok','4','human','external-v1',NULL,NULL)"
    )
    assert [r["video_id"] for r in repo.load()[1]] == ["3"]
    catalog.execute("UPDATE vl_video_campaign_links SET prompt_version='v1'")
    assert {r["video_id"] for r in repo.load()[1]} == {"3", "4"}
