"""scripts/backfill_doc_kind.py の純関数（plan_updates）テスト。

契約:
- タイトルの決定論ルール（ingest.classify._kind_from_title の再利用）だけで判定し、
  Bedrock / 本文は一切使わない
- 立てるべきフラグのうち **まだ "true" が付いていないものだけ** patch に載せる（冪等）
- どのフラグも立たない行は返さない（UPDATE 0 件＝既存 metadata 不変）
- フラグの削除 / false 上書きはしない（追記のみ・印なし文書は検索で常に残る後方互換）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "backfill_doc_kind", _ROOT / "scripts" / "backfill_doc_kind.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["backfill_doc_kind"] = _mod
_spec.loader.exec_module(_mod)

plan_updates = _mod.plan_updates


def test_plan_marks_template_and_recurring_titles() -> None:
    rows = [
        ("id-1", "提案書テンプレート", None, None),
        ("id-2", "2025年上期売上報告", None, None),
        ("id-3", "出光興産様向けSNS運用提案書", None, None),
    ]
    updates = plan_updates(rows)
    assert [(u[0], u[2]) for u in updates] == [
        ("id-1", {"cls_is_template": "true"}),
        ("id-2", {"cls_is_recurring": "true"}),
    ]


def test_plan_both_flags_for_recurring_template() -> None:
    updates = plan_updates([("id-1", "月次報告テンプレート", None, None)])
    assert updates == [
        ("id-1", "月次報告テンプレート", {"cls_is_template": "true", "cls_is_recurring": "true"})
    ]


def test_plan_idempotent_skips_already_flagged() -> None:
    rows = [
        ("id-1", "提案書テンプレート", "true", None),  # 既に template 付与済 → skip
        ("id-2", "月次報告テンプレート", "true", None),  # recurring だけ追記
    ]
    updates = plan_updates(rows)
    assert updates == [("id-2", "月次報告テンプレート", {"cls_is_recurring": "true"})]


def test_plan_never_unsets_existing_flags() -> None:
    # ルール非該当タイトルでも既存 "true" を消さない（patch を作らない＝追記のみ）。
    updates = plan_updates([("id-1", "ふつうの提案書", "true", "true")])
    assert updates == []


def test_plan_empty_title_and_none_title_safe() -> None:
    assert plan_updates([("id-1", "", None, None), ("id-2", None, None, None)]) == []


def test_plan_short_english_fmt_not_matched() -> None:
    # 短い英語 FMT / format は誤爆するため対象外（新提案書FMT は正規資料）。
    assert plan_updates([("id-1", "新提案書FMT", None, None)]) == []
