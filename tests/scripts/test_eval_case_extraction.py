"""scripts/eval_case_extraction.py: client 名の正規化一致で対応付け、tag 一致率を出す。

変異テスト（赤くなることを確認済み）:
- ``clients_match`` を常に True にする → test_unmatched_truth_is_reported が赤
- ``evaluate`` で使用済み case_id の再利用を許す → test_each_extracted_used_once が赤
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "eval_case_extraction", _ROOT / "scripts" / "eval_case_extraction.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["eval_case_extraction"] = _mod
_spec.loader.exec_module(_mod)


def _truth(
    id: str, client: str, sector: str, purpose: list[str], traits: list[str]
) -> dict[str, Any]:
    return {
        "id": id,
        "client": client,
        "tags": {
            "sector": sector,
            "purpose": purpose,
            "product_state": "新商品",
            "channel": [],
            "traits": traits,
        },
    }


def _extracted(
    case_id: str, client: str, sector: str, purpose: list[str], traits: list[str]
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "client_internal": client,
        "sector": sector,
        "purpose": purpose,
        "traits": traits,
    }


def test_clients_match_uses_company_head_token_and_normalization() -> None:
    assert _mod.clients_match("カネカ Q10グミ", "株式会社カネカ")
    assert _mod.clients_match("カネカ Q10グミ", "カネカ様")
    assert not _mod.clients_match("カネカ Q10グミ", "花王")
    assert not _mod.clients_match("A B", "A")  # 1 文字は照合しない


def test_set_f1() -> None:
    assert _mod.set_f1(frozenset(), frozenset()) == 1.0
    assert _mod.set_f1(frozenset({"a"}), frozenset()) == 0.0
    assert _mod.set_f1(frozenset({"a", "b"}), frozenset({"a"})) == 2 * 1.0 * 0.5 / 1.5


def test_evaluate_scores_sector_purpose_traits() -> None:
    truth = _mod.load_truth(
        {
            "cases": [
                _truth(
                    "kaneka",
                    "カネカ Q10グミ",
                    "飲料食品",
                    ["認知拡大", "指名検索・VSEO"],
                    ["検証型"],
                ),
                _truth("kao", "花王 アタック", "日用品美容", ["売上・POS"], []),
            ]
        }
    )
    extracted = _mod.load_extracted(
        [
            _extracted("d1#1", "カネカ", "飲料食品", ["認知拡大"], ["検証型"]),
            _extracted("d2#1", "花王", "その他", ["売上・POS"], ["セール連動"]),
        ]
    )
    report = _mod.evaluate(truth, extracted)
    assert report.matched == 2
    assert report.sector_accuracy == 0.5  # その他 は不一致
    assert report.purpose_f1 == (2 * 1.0 * 0.5 / 1.5 + 1.0) / 2
    assert report.traits_f1 == 0.5
    assert report.pairs == [("kaneka", "d1#1"), ("kao", "d2#1")]
    text = _mod.render_report(report)
    assert "sector accuracy: 0.50" in text and "purpose F1:" in text
    assert report.as_dict()["coverage"] == 1.0


def test_best_of_multiple_extractions_is_chosen() -> None:
    truth = _mod.load_truth(
        {"cases": [_truth("kaneka", "カネカ Q10グミ", "飲料食品", ["認知拡大"], [])]}
    )
    extracted = _mod.load_extracted(
        [
            _extracted("d1#1", "カネカ", "その他", ["採用"], []),
            _extracted("d2#1", "カネカ", "飲料食品", ["認知拡大"], []),
        ]
    )
    report = _mod.evaluate(truth, extracted)
    assert report.pairs == [("kaneka", "d2#1")]
    assert report.sector_accuracy == 1.0


def test_unmatched_truth_is_reported() -> None:
    truth = _mod.load_truth(
        {"cases": [_truth("kao", "花王 アタック", "日用品美容", ["売上・POS"], [])]}
    )
    extracted = _mod.load_extracted([_extracted("d1#1", "カネカ", "飲料食品", ["認知拡大"], [])])
    report = _mod.evaluate(truth, extracted)
    assert report.matched == 0
    assert report.unmatched_truth == ["kao"]
    assert report.sector_accuracy == 0.0


def test_each_extracted_used_once() -> None:
    truth = _mod.load_truth(
        {
            "cases": [
                _truth("k1", "カネカ Q10グミ", "飲料食品", ["認知拡大"], []),
                _truth("k2", "カネカ 別商材", "飲料食品", ["認知拡大"], []),
            ]
        }
    )
    extracted = _mod.load_extracted([_extracted("d1#1", "カネカ", "飲料食品", ["認知拡大"], [])])
    report = _mod.evaluate(truth, extracted)
    assert report.matched == 1
    assert report.unmatched_truth == ["k2"]


def test_main_reads_files(tmp_path: Path, capsys: Any) -> None:
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(
        json.dumps(
            {"cases": [_truth("k", "カネカ Q10グミ", "飲料食品", ["認知拡大"], [])]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    extracted_path = tmp_path / "extracted.json"
    extracted_path.write_text(
        json.dumps([_extracted("d#1", "カネカ", "飲料食品", ["認知拡大"], [])], ensure_ascii=False),
        encoding="utf-8",
    )
    assert (
        _mod.main(["--truth", str(truth_path), "--extracted", str(extracted_path), "--json"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["sector_accuracy"] == 1.0 and payload["matched"] == 1
