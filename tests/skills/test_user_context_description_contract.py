"""全 Skill の description が ``_user_context`` の渡し方を**誤解なく**書いていることの契約テスト。

本番実測（2026-09-03）: OpenClaw のセッション記録 166 ファイル・tool call 363 件のうち 83 件が
層1 plugin で block され、そのうち **72 件が ``_user_context must be a plain object``** だった。
モデル（Bedrock Haiku 4.5）が引数を ``{"arguments": {"_user_context": {...}}}`` と**二重に包んで**
いた。当時 15 スキルの description は揃って「呼び出し時は **arguments に** ``_user_context: …``
を必ず含める」と書いており、description は tool 定義としてそのままモデルへ渡るため、この
「arguments に」が「``arguments`` という名前の入れ物を作れ」と読める＝包みを作らせる指示に
なっていた疑いが濃い。

そこでここは 2 つを**全スキル横断**で固定する:
  1. どの description にも「入れ物を作れ」と読める表現（``arguments に`` 等）が 1 つも無い。
  2. ``_user_context`` に触れる description は例外なく共有定数
     :data:`~teamagent.skills._shared.user_context.USER_CONTEXT_RULE` をそのまま使う
     （スキルごとの書き分けを許すと、1 本だけ元へ戻る事故が静かに通る）。

⚠️ ここが赤くなったら「テストを直す」のではなく、**本番のツール呼び出しが二重包みへ戻らないか**
を先に確認すること（block されると利用者には「AI が反応しない」という形で出る）。
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import teamagent.skills as _skills_pkg
from teamagent.skills._shared.user_context import FORBIDDEN_WRAPPER_PHRASES, USER_CONTEXT_RULE
from teamagent.skills.base import SkillRegistry


def _all_descriptions() -> dict[str, str]:
    """登録済み全 Skill の name→description（skill.py を持つ全パッケージを import して収集）。"""
    for mod in pkgutil.iter_modules(_skills_pkg.__path__):
        if not mod.ispkg:
            continue
        try:
            importlib.import_module(f"teamagent.skills.{mod.name}.skill")
        except ModuleNotFoundError:
            continue  # skill.py を持たないパッケージ（vseo 等）
    return {
        name: str(getattr(SkillRegistry.get(name), "description", ""))
        for name in SkillRegistry.list_all()
    }


def test_registry_is_actually_populated() -> None:
    """収集が 0 件でも緑になる（＝何も検査していない）壊れ方を塞ぐ。"""
    descriptions = _all_descriptions()
    assert len(descriptions) >= 30, f"収集できた Skill が少なすぎる: {len(descriptions)}"
    assert all(descriptions.values()), "description が空の Skill がある"


def test_no_description_tells_the_model_to_build_an_arguments_wrapper() -> None:
    """『arguments に …』のような**入れ物を作れ**と読める表現が 1 本も無いこと。"""
    offenders: list[str] = []
    for name, description in _all_descriptions().items():
        for phrase in FORBIDDEN_WRAPPER_PHRASES:
            if phrase in description:
                offenders.append(f"{name}: 『{phrase}』")
    assert not offenders, (
        "description がモデルに arguments ラッパーを作らせる書き方になっている: "
        + " / ".join(offenders)
        + "。『引数のトップレベルに置く』（USER_CONTEXT_RULE）で書くこと"
    )


def test_every_description_mentioning_user_context_uses_the_shared_wording() -> None:
    """``_user_context`` に触れる description は共有定数をそのまま使う（文言の統一）。"""
    using: list[str] = []
    for name, description in _all_descriptions().items():
        if "_user_context" not in description:
            continue
        using.append(name)
        assert USER_CONTEXT_RULE in description, (
            f"{name} の description が USER_CONTEXT_RULE を使わずに _user_context を説明している。"
            "文言はスキルごとに書き分けず、共有定数へ寄せること"
        )
    # 実測 15 本（2026-09-03 時点）。減る＝どこかが独自文言へ戻った可能性がある。
    assert len(using) >= 15, f"_user_context を説明する Skill が {len(using)} 本しかない: {using}"


@pytest.mark.parametrize(
    "phrase", ["トップレベル", "包み直さない", "_user_context", "slack_user_id"]
)
def test_shared_wording_says_top_level_and_forbids_wrapping(phrase: str) -> None:
    """共有文言そのものが「トップレベル」「包み直さない」を明言していること。"""
    assert phrase in USER_CONTEXT_RULE


def test_shared_wording_does_not_contain_the_word_arguments() -> None:
    """共有文言に ``arguments`` の語を出さない（出した瞬間に 15 本すべてへ伝播する）。"""
    assert "arguments" not in USER_CONTEXT_RULE.lower()


def test_soul_md_also_says_top_level_not_arguments() -> None:
    """SOUL.md（OpenClaw の bootstrap）側の同趣旨の 1 行も統一されていること。

    description だけ直しても、毎リクエストの system prompt に「arguments には…」が残っていれば
    モデルは同じ包みを作る。真実源が 2 つある以上、両方を同じテストで縛る。
    """
    from pathlib import Path

    soul = (Path(__file__).resolve().parents[2] / "infra" / "openclaw" / "SOUL.md").read_text(
        encoding="utf-8"
    )
    rule = soul.split("**全 tool call", 1)[1].split("\n", 1)[0]
    assert "arguments" not in rule, (
        f"SOUL.md の _user_context 規約に arguments が残っている: {rule}"
    )
    assert "直下" in rule or "トップレベル" in rule, rule
    assert "包まない" in rule or "包み直さない" in rule, rule
