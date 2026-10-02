"""scripts/backfill_classification.py（未分類の文書の分類し直し）のテスト。

固定すること:
- 対象 SQL は cls_* を 1 つも持たず、印（cls_backfill_at）も無い文書だけ
- Bedrock が失敗した文書は書かない（rules-only の結果で「分類済み」に見せかけない）
- 3 件続けて失敗したら止める（deny が残っているのに全件を流さない）
- 空の分類でも印だけは書く（再課金しない）。cls_* に空文字は書かない
- UPDATE は既存 metadata にマージし、「まだ未分類」を条件にする

本物の DocClassifier（ルール＋Bedrock 呼び出し＋失敗を握ってルールだけ返す）を使い、
Bedrock だけをフェイクにする＝本番の失敗の仕方（例外を握られて戻り値からは分からない）を再現する。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.ingest.classify import DocClassifier

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "backfill_classification", _ROOT / "scripts" / "backfill_classification.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["backfill_classification"] = _mod
_spec.loader.exec_module(_mod)

CLS_JSON = '{"project": "ローソン", "industry": "小売", "doc_type": "その他", "phase": "受注", "solution": "動画広告"}'


class _AccessDeniedError(Exception):
    pass


class _FakeBedrock:
    """script の順に応答する。"deny" は本番の AccessDenied と同じく例外を投げる。"""

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)
        self.calls = 0

    def converse(self, **_: Any) -> ConverseResponse:
        self.calls += 1
        step = self._script.pop(0) if self._script else "ok"
        if step == "deny":
            raise _AccessDeniedError("explicit deny AiLa-CostCap-DenyBedrock")
        text = CLS_JSON if step == "ok" else "{}"
        return ConverseResponse(
            text=text,
            usage=TokenUsage(
                input_tokens=1,
                output_tokens=1,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                cost_usd=0.0,
            ),
            model_id="m",
            latency_ms=1,
            stop_reason="end_turn",
        )


def _targets(n: int) -> list[tuple[str, str, str]]:
    return [
        (f"doc-{i}", f"#proj-01案件決定-同行依頼 17900000{i}", "ADK九州 ローソン 受注")
        for i in range(n)
    ]


def _run(script: list[str], n: int) -> tuple[Any, dict[str, dict[str, str]]]:
    fake = _FakeBedrock(script)
    bedrock = _mod._CountingBedrock(fake)
    written: dict[str, dict[str, str]] = {}
    out = _mod.run(
        _targets(n),
        classifier=DocClassifier(bedrock),
        bedrock=bedrock,
        write=lambda doc_id, patch: written.__setitem__(doc_id, patch),
        today="2026-10-02",
        progress=lambda _m: None,
    )
    return out, written


def test_classifies_and_marks() -> None:
    out, written = _run(["ok", "ok"], 2)
    assert (out.classified, out.failed, out.aborted) == (2, 0, False)
    assert written["doc-0"]["cls_phase"] == "受注"
    assert written["doc-0"]["cls_solution"] == "動画広告"
    assert written["doc-0"]["cls_backfill_at"] == "2026-10-02"


def test_bedrock_failure_is_not_written() -> None:
    """DocClassifier は例外を握るので戻り値では分からない。包みで観測して書かない。"""
    out, written = _run(["ok", "deny", "ok"], 3)
    assert set(written) == {"doc-0", "doc-2"}
    assert (out.classified, out.failed, out.aborted) == (2, 1, False)


def test_aborts_after_three_consecutive_failures() -> None:
    out, written = _run(["deny", "deny", "deny", "ok"], 4)
    assert out.aborted is True
    assert written == {}  # deny が残っているのに rules-only で全件を「分類済み」にしない


def test_empty_classification_writes_only_the_marker() -> None:
    out, written = _run(["empty"], 1)
    assert out.empty == 1
    patch = written["doc-0"]
    assert patch["cls_backfill_at"] == "2026-10-02"
    assert all(
        v for v in patch.values()
    )  # 空文字の cls_* は書かない（#508 の自動フィルタで落ちる）


def test_target_sql_selects_only_unclassified_and_unmarked() -> None:
    sql, params = _mod.target_sql(since="2026-09-25", limit=10)
    for key in ("cls_project", "cls_industry", "cls_doc_type", "cls_phase", "cls_solution"):
        assert f"(d.metadata->>'{key}') IS NULL" in sql
    assert "(d.metadata->>'cls_backfill_at') IS NULL" in sql
    assert "d.ingested_at >= (%s::date AT TIME ZONE 'Asia/Tokyo')" in sql
    assert params == [4000, "2026-09-25", 10]


def test_update_merges_and_guards_still_unclassified() -> None:
    sql = _mod.UPDATE_SQL
    assert "COALESCE(metadata, '{}'::jsonb) || %s::jsonb" in sql
    assert "(metadata->>'cls_phase') IS NULL" in sql
    assert "d.metadata" not in sql


@pytest.mark.parametrize("n", [0, 150])
def test_estimate_is_monotonic(n: int) -> None:
    assert _mod.estimate_cost_usd(n) >= 0
