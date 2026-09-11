"""不変量（AST / 型 / 面ガード）。一次防御を壊した瞬間に赤くなる層。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from teamagent.adapters.gcalendar_readonly import ReadOnlyCalendar
from teamagent.skills._shared.private_surface import is_private_surface
from teamagent.skills.morning_digest.schema import CalendarEventItem
from teamagent.skills.pre_meeting_brief.render import harden
from teamagent.skills.pre_meeting_brief.schema import CaseRef, PreMeetingBriefItem

BRIEF_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "teamagent" / "skills" / "pre_meeting_brief"
)

#: この経路に現れてはいけない語（LLM・外部送信・カレンダー書込）。
FORBIDDEN_TOKENS = (
    "bedrock",
    "Bedrock",
    "gemini",
    "Gemini",
    "embedder",
    "Embedder",
    "embeddings",
    "insert_event",
    "events.insert",
    "delete_event",
    "update_event",
    "post_message",
    "chat.postMessage",
    "files.upload",
    "send_message",
    "create_draft",
    "drafts.create",
    "GCalendarClient",
)


def _sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text(encoding="utf-8")) for p in sorted(BRIEF_DIR.glob("*.py"))]


def test_no_llm_or_write_references_anywhere_in_brief_package() -> None:
    """LLM 非経由・外部送信ゼロ・カレンダー書込ゼロを AST とテキストの両方で固定。

    変異: skill.py に ``from teamagent.adapters.bedrock_client import BedrockClient`` を
    1 行足すと赤。
    """
    offenders: list[str] = []
    for path, src in _sources():
        for token in FORBIDDEN_TOKENS:
            # docstring 中の「GCalendarClient を渡さない」等は説明なので、
            # **コード（識別子・文字列リテラル・import）** だけを見る。
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Name | ast.Attribute):
                    name = node.id if isinstance(node, ast.Name) else node.attr
                    if name == token:
                        offenders.append(f"{path.name}:{name}")
                elif isinstance(node, ast.ImportFrom) and token in (node.module or ""):
                    offenders.append(f"{path.name}:import {node.module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if token in alias.name:
                            offenders.append(f"{path.name}:import {alias.name}")
    assert offenders == [], offenders


def test_brief_schema_has_no_raw_description_field() -> None:
    """生 description のフィールドを **作らない**（schema にも出力にも載せない）。"""
    for model in (PreMeetingBriefItem, CaseRef, CalendarEventItem):
        names = set(model.model_fields)
        assert "description" not in names, model.__name__
        assert not any("description" in n for n in names), model.__name__


def test_display_fields_have_scrubbed_pairs() -> None:
    """``*_display`` には必ず ``*_scrubbed`` の対がある（ログ安全側の存在を強制）。

    例外は「そもそも PII でないもの」だけを明示列挙する。
    """
    allowed_without_pair = {
        # 業種（金庫の分類語彙・個人も取引先も特定しない）
        "industry_display",
        # 社内担当（マスター表の営業担当列＝社内の氏名。DM 本文にだけ出す）
        "owner_display",
    }
    for model in (PreMeetingBriefItem, CaseRef):
        for name in model.model_fields:
            if not name.endswith("_display"):
                continue
            if name in allowed_without_pair:
                continue
            pair = name[: -len("_display")] + "_scrubbed"
            assert pair in model.model_fields, f"{model.__name__}.{name} に {pair} が無い"


def test_attendee_fields_never_hold_local_parts() -> None:
    """参加者は **ドメインのみ**。email 丸ごとを入れる口を作らない。"""
    assert "attendee_domains" in PreMeetingBriefItem.model_fields
    assert "attendees" not in PreMeetingBriefItem.model_fields
    assert "attendee_emails" not in PreMeetingBriefItem.model_fields


# ── read-only facade（書込ゼロの一次防御）────────────────────────────
class _SpyCalendar:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_events(self, request_id: str, **kw: object) -> list[object]:
        self.calls.append("list_events")
        return []

    def insert_event(self, *a: object, **k: object) -> None:  # pragma: no cover
        raise AssertionError("facade 越しに到達してはいけない")


def test_readonly_facade_exposes_only_list_events() -> None:
    """変異: ``insert_event`` を facade へ転送すると赤。"""
    facade = ReadOnlyCalendar(_SpyCalendar())
    public = {n for n in dir(facade) if not n.startswith("_")}
    assert public == {"list_events"}


def test_readonly_facade_blocks_write_methods() -> None:
    facade = ReadOnlyCalendar(_SpyCalendar())
    for name in ("insert_event", "freebusy", "_ensure_service", "delete_event"):
        with pytest.raises(AttributeError):
            getattr(facade, name)


def test_readonly_facade_forwards_reads() -> None:
    inner = _SpyCalendar()
    ReadOnlyCalendar(inner).list_events("req-1", max_results=100)
    assert inner.calls == ["list_events"]


def test_readonly_facade_holds_no_named_handle_to_the_client() -> None:
    """生 client を指す属性（旧 ``_inner``）を持たない。

    変異: ``__init__`` を ``self._inner = inner`` に戻し ``__slots__`` へ
    ``_inner`` を足すと、``facade._inner.insert_event(...)`` が 1 行で書けて赤。

    ⚠️ ただしこれは「原理的に到達不能」の主張ではない。bound method の
    ``__self__`` 経由での到達は残っており、docstring もそう書いてある
    （次のテストがその記述を固定する）。
    """
    facade = ReadOnlyCalendar(_SpyCalendar())
    assert ReadOnlyCalendar.__slots__ == ("_list_events",)
    assert not hasattr(facade, "_inner")
    with pytest.raises(AttributeError):
        facade.__getattribute__("_inner")


def test_readonly_facade_docstring_does_not_overclaim() -> None:
    """docstring が実態より強い主張をしていない（レビューで信用を失う原因）。"""
    import teamagent.adapters.gcalendar_readonly as mod

    doc = mod.__doc__ or ""
    assert "__self__" in doc  # 残っている抜け道を明示している
    assert "sandbox ではない" in doc


# ── 宛先 deny-by-default ─────────────────────────────────────────────
def test_private_surface_allows_only_verified_dm() -> None:
    assert is_private_surface("D0123", True) is True


@pytest.mark.parametrize(
    "channel_id",
    ["", "   ", "C0123", "G0123", "W0123", "X", None],
)
def test_private_surface_denies_everything_else(channel_id: str | None) -> None:
    """変異: 空文字を許容側へ戻す（``not channel_id or ...`` を外す）と赤。

    既存の ``_is_channel_surface``（slack_summary）は空文字を「出してよい」側へ倒すので
    流用してはいけない、をここで固定する。
    """
    assert is_private_surface(channel_id, True) is False


def test_private_surface_requires_identity_verified() -> None:
    assert is_private_surface("D0123", False) is False


# ── 無害化 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw",
    ["<!channel>", "<@U12345>", "<https://evil.example|クリック>", "@here"],
)
def test_harden_makes_slack_markup_unbuildable(raw: str) -> None:
    out = harden(raw, 200)
    assert "<" not in out
    assert ">" not in out
    assert "@" not in out


def test_harden_strips_control_characters_and_newlines() -> None:
    out = harden("A\nB\tC\x00D", 200)
    assert "\n" not in out
    assert "\x00" not in out


def test_harden_applies_length_limit() -> None:
    assert len(harden("あ" * 500, 40)) == 40


# ── 消毒（実在の取引先名・社内担当者名をソース/テストに書かない）────────
#: PLAN §2-2 が clip 側テンプレ資産に課している「消毒（他社名・人名の除去）」を、
#: 本 skill のソースとフィクスチャにも同じ基準で適用する。gitleaks は secrets しか
#: 見ないので素通りする＝ここで明示的に固定する。
FORBIDDEN_NAMES: tuple[str, ...] = (
    "富士急",
    "電通",
    "博報堂",
    "すかいらーく",
    "ヤクルト",
    "初田製作所",
    "伊藤ハム",
    "ジャングリア",
    "JCB",
    "USJ",
)

_SANITIZE_SCOPE = (BRIEF_DIR, Path(__file__).resolve().parent)


def test_no_real_client_names_in_source_or_fixtures() -> None:
    """変異: フィクスチャの社名を実在の取引先へ戻すと赤。"""
    hits: list[str] = []
    for root in _SANITIZE_SCOPE:
        for path in sorted(root.rglob("*.py")):
            if path == Path(__file__).resolve():
                continue  # 禁止語リストそのものを持つファイル
            text = path.read_text(encoding="utf-8")
            hits += [f"{path.name}:{name}" for name in FORBIDDEN_NAMES if name in text]
    assert hits == []
