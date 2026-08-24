"""ツール入力スキーマの「捏造ハザード」台帳（P1-3 (b)）。

## 何を捕まえるのか

外側ルーター(Haiku)は **required なフィールドを必ず埋める**。埋める材料が依頼文しか無い以上、
利用者が値を言っていない依頼では**依頼文の断片が詰められる**。値空間に制約（enum / pattern /
format）が無いとサーバ側でも弾けず、そのまま「0 件」という*もっともらしい嘘*になる。
これが 2026-08-20 の実測事故（``client_name="今日のメール"`` → scanned=0）の構造的な原因。

## 不変量

registry の全 Skill の入力スキーマを走査し、**required かつ自由文字列**（enum / const /
pattern / format / $ref / anyOf のいずれも無い ``type: "string"``）のフィールドを全部数え上げ、
下の 4 つの台帳と**完全一致**させる。増えても減っても赤になるので、

* 新しい捏造ハザードを黙って追加できない（人間が台帳へ分類を書く必要がある）
* 直したのに台帳に残り続ける（＝嘘の負債）も起きない

``min_length``/``max_length`` は**制約とみなさない**。長さが合っていても「今日のメール」は通る。

## 台帳の分類

* :data:`ENTITY_NAME_DEBT` — 実在エンティティ名。**P0-2 と同じ失敗クラス**。期限つき負債。
* :data:`SEARCH_TERM_DEBT` — 検索語。捏造しても「空振り」で済むが誤誘導はしうる。
* :data:`OPAQUE_TOKEN_OK` — job_id / 署名トークン。捏造すると**大きな音で失敗する**（黙って
  0 件にならない）ので、自由文字列のままでよいと裁定したもの。
* :data:`USER_VERBATIM_OK` — 「利用者の言葉そのもの」が正解の自由文（検索クエリ・ブリーフ）。
  依頼文の断片を渡すのが**正しい挙動**なので捏造ハザードではない。
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from typing import Any

import teamagent.skills as _skills_pkg
from teamagent.skills.base import SkillRegistry
from teamagent.skills.calendar_freebusy.schema import CalendarFreeBusyInput
from teamagent.skills.mail_draft.schema import MailDraftInput
from teamagent.skills.mail_followup.schema import MailFollowupInput
from teamagent.skills.mail_reply.schema import MailReplyInput
from teamagent.skills.mail_summary.schema import MailSummaryInput

# 値空間を機械的に狭めるキー。1 つでもあれば「自由文字列」ではない。
_CONSTRAINT_KEYS = ("enum", "const", "pattern", "format", "$ref", "anyOf", "allOf", "oneOf")

# 実在エンティティ名を指すフィールド名の形（P0-2 の失敗クラスの目印）。
_ENTITY_NAME_FIELDS = frozenset(
    {"client_name", "customer_name", "company_name", "account_name", "product_name"}
)

Key = tuple[str, str]


# ── 台帳 ───────────────────────────────────────────────────────────────────

# 🔴 期限つき負債（2026-09-30 までに「任意化 + client_name_guard」か enum 化）。
# ここに載っている tool は、顧客名を言わない依頼で今も依頼文の断片を詰められる。
# clientkarte / mail_to_internal_context は **本番で OC に露出済み**。
ENTITY_NAME_DEBT: dict[Key, str] = {
    ("clientkarte", "client_name"): "本番露出。P0-2 と同型。任意化 + guard へ移す（〜2026-09-30）",
    (
        "mail_to_internal_context",
        "client_name",
    ): "本番露出。client_name_guard 適用済（断片では受信箱を叩かない）。任意化は未（〜2026-09-30）",
    ("mail_constraints", "client_name"): "dark（USE_MAIL_TOOLS）。露出前に任意化（〜2026-09-30）",
    ("x_voice_search", "product_name"): "商材名。空振りで済むが表題に嘘が載る（〜2026-09-30）",
    (
        "proposal_deck",
        "product_name",
    ): "dark（USE_PROPOSAL_DECK_TOOLS）。露出前に見直し（〜2026-09-30）",
}

# 検索語。捏造されても「0 件 / 的外れ」で済み、顧客名ほどの誤解は生まない。
SEARCH_TERM_DEBT: dict[Key, str] = {
    ("x_needs_mining", "theme"): "対象領域。空なら聞き返す設計が望ましい",
    ("x_buzz_measure", "keyword"): "発話量の検索語。start/end_date は pattern 済み",
}

# 捏造すると大きな音で失敗する（存在しない job/token は即エラー）。自由文字列のままでよい。
OPAQUE_TOKEN_OK: dict[Key, str] = {
    ("schedule_propose", "schedule_token"): "HMAC 署名トークン。同上",
    ("proposal_builder_status", "job_id"): "存在しない job_id は not found",
    ("tiktok_acquire_status", "job_id"): "同上",
    ("x_buzz_measure_status", "job_id"): "同上",
}

# 「利用者の言葉そのもの」が正解の自由文。依頼文を渡すのが正しい挙動＝ハザードではない。
USER_VERBATIM_OK: dict[Key, str] = {
    ("search", "query"): "自然文クエリ",
    ("knowledge_deliver", "query"): "探したい資料の自然文",
    ("proposal_draft", "brief"): "案件概要（自然文）",
    ("recommend", "brief"): "案件概要（自然文）",
    ("proposal_review", "proposal_text"): "レビュー対象の本文",
    ("tiktok_search", "query"): "検索語",
    ("video_algorithm", "query"): "検索KW",
    ("web_research", "query"): "外部検索クエリ（顧客名を入れない旨は description で禁止済み）",
    ("chitchat", "message"): "Socket Mode 専用。発話そのもの",
    ("proposal_deck", "goal"): "提案の目的（自然文）",
    ("proposal_deck", "target_persona"): "ターゲット像（自然文）",
    ("workspace_search", "query"): "検索語",
    ("video_analysis", "url"): "動画 URL。pattern が無いのは短縮URL許容のため（要検討）",
    ("workspace_search", "service"): (
        "🔴 schema.py:12 に WORKSPACE_SERVICES=('calendar','people') があるのに Literal 化"
        "されていない。dark（USE_WORKSPACE_TOOLS）だが露出前に Literal へ（〜2026-09-30）"
    ),
}

_ALL_LEDGERS: tuple[dict[Key, str], ...] = (
    ENTITY_NAME_DEBT,
    SEARCH_TERM_DEBT,
    OPAQUE_TOKEN_OK,
    USER_VERBATIM_OK,
)


# ── 走査 ───────────────────────────────────────────────────────────────────


def _import_all_skills() -> None:
    for mod in pkgutil.iter_modules(_skills_pkg.__path__):
        if not mod.ispkg:
            continue
        try:
            importlib.import_module(f"teamagent.skills.{mod.name}.skill")
        except ModuleNotFoundError:
            continue  # skill.py を持たないパッケージ（_shared / vseo 等）


def _is_free_string(prop: dict[str, Any]) -> bool:
    if prop.get("type") != "string":
        return False
    return not any(key in prop for key in _CONSTRAINT_KEYS)


def _free_string_required_fields() -> dict[Key, dict[str, Any]]:
    """全 Skill の入力スキーマから「required かつ自由文字列」を全数抽出する。"""
    _import_all_skills()
    found: dict[Key, dict[str, Any]] = {}
    for name in SkillRegistry.list_all():
        schema = SkillRegistry.get(name).input_schema.model_json_schema()
        properties = schema.get("properties") or {}
        for field in schema.get("required") or []:
            prop = properties.get(field) or {}
            if _is_free_string(prop):
                found[(name, field)] = prop
    return found


def test_every_free_text_required_field_is_declared_in_the_ledger() -> None:
    """自由文字列 required の集合と台帳を完全一致させる（増減どちらでも赤）。"""
    found = _free_string_required_fields()
    # 検出器が空振り（import 失敗・スキーマ生成の仕様変更）したら vacuous に緑にしない。
    assert len(found) >= 20, f"検出器が壊れている疑い（検出 {len(found)} 件）"

    declared: dict[Key, str] = {}
    for ledger in _ALL_LEDGERS:
        for key, reason in ledger.items():
            assert key not in declared, f"台帳に重複エントリ: {key}"
            declared[key] = reason

    undeclared = sorted(set(found) - set(declared))
    stale = sorted(set(declared) - set(found))
    assert not undeclared, (
        "required な自由文字列フィールドが台帳に無い。ルーターは必ず値を埋めるので、"
        "利用者が言っていない依頼では依頼文の断片が詰められる。"
        "任意化(+guard)・enum/pattern 化・または台帳へ分類を追加すること: " + str(undeclared)
    )
    assert not stale, f"台帳に実体の無いエントリが残っている（直したら消すこと）: {stale}"


def test_entity_name_fields_are_exactly_the_tracked_debt() -> None:
    """顧客名の形をした required 自由文字列は、必ず期限つき負債として可視化されていること。"""
    found = _free_string_required_fields()
    detected = {key for key in found if key[1] in _ENTITY_NAME_FIELDS}
    assert detected == set(ENTITY_NAME_DEBT), (
        "顧客名フィールドの検出結果と ENTITY_NAME_DEBT がずれている。"
        f"detected={sorted(detected)} ledger={sorted(ENTITY_NAME_DEBT)}"
    )
    for reason in ENTITY_NAME_DEBT.values():
        assert "2026-" in reason, "期限つき負債には期限を書くこと"


def test_mail_tools_no_longer_require_a_customer_name() -> None:
    """P0-2 の修正が required へ戻る退行を止める（**この失敗クラスの本丸**）。"""
    for model in (MailSummaryInput, MailFollowupInput, MailReplyInput):
        schema = model.model_json_schema()
        assert "client_name" not in (schema.get("required") or []), (
            f"{model.__name__}.client_name が required に戻っている。"
            "ルーターは値を捏造するしかなくなり『今日のメール』事故が再発する"
        )
        prop = schema["properties"]["client_name"]
        assert prop.get("default") == "", "顧客不明で空のまま呼べる既定値が要る"
        assert prop.get("minLength") is None, "min_length=1 は空呼び出しを塞ぐので付けないこと"
    # 直った 2 本は負債台帳から消えていること／同型の未修理が残っていることの両方を固定する。
    assert ("mail_summary", "client_name") not in ENTITY_NAME_DEBT
    assert ("mail_followup", "client_name") not in ENTITY_NAME_DEBT
    assert ("mail_reply", "client_name") not in ENTITY_NAME_DEBT
    assert ("clientkarte", "client_name") in ENTITY_NAME_DEBT


def test_mail_draft_can_be_called_without_inventing_a_token() -> None:
    """一覧から選ばれた件の下書きは **署名トークン無し**でも作れる（selection 経路）。

    draft_token を required のまま残すと、ルーターは押していないボタンの value を捏造する
    しかなくなる。任意化したので負債台帳（OPAQUE_TOKEN_OK）からも外れていること。
    """
    schema = MailDraftInput.model_json_schema()
    assert not (schema.get("required") or []), "mail_draft は全項目省略可（入口が 2 つあるため）"
    assert schema["properties"]["draft_token"]["default"] == ""
    assert schema["properties"]["selection"]["default"] == ""
    assert ("mail_draft", "draft_token") not in OPAQUE_TOKEN_OK


def test_calendar_freebusy_router_knobs_are_enumerated_not_free_text() -> None:
    """P1-2 の mode / relative_day が自由文字列に緩まないこと（日付捏造の再発防止）。"""
    schema = CalendarFreeBusyInput.model_json_schema()
    assert not (schema.get("required") or []), "calendar_freebusy は全項目省略可のまま保つこと"
    assert schema["properties"]["mode"]["enum"] == ["free", "agenda"]
    assert schema["properties"]["mode"]["default"] == "free"
    assert schema["properties"]["relative_day"]["enum"] == ["", "today", "tomorrow"]
    assert schema["properties"]["relative_day"]["default"] == ""
    # date は「LLM に計算させない」ので空許容 pattern。自由文字列に戻したら赤。
    assert schema["properties"]["date"]["pattern"] == r"^(\d{4}-\d{2}-\d{2})?$"


def test_all_tool_schemas_are_json_serializable() -> None:
    """MCP の tool 定義として実際に JSON 化できること（走査自体の健全性）。"""
    _import_all_skills()
    names = SkillRegistry.list_all()
    assert len(names) >= 35, f"registry が薄すぎる（{len(names)} 件）"
    for name in names:
        json.dumps(SkillRegistry.get(name).input_schema.model_json_schema(), ensure_ascii=False)
