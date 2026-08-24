"""カタログ第一弾ツールのルーティング硬化リグレッション（CLAUDE.md §10 E2）。

OC の外側ルーター(Haiku)は name+description だけでツールを選ぶ。ルーティング・シミュ
（tests/routing/README.md・n=51 で 2 シミュ全一致）で潰した混同を、description の
トリガー語と相互排他注記の存在として固定する。description を将来いじって棲み分けが
壊れたら、ここが赤くなって気づける。

corpus 自体の突き合わせ（どのツールが選ばれるか）は LLM 依存で非決定的なため pytest ゲートには
しない（README 手順で手動実行）。ここが決定論的に担えるのは次の 4 つ:

1. description のトリガー語・相互排他注記が在るか（decision-substring の固定）
2. corpus が壊れていないか・R4 の失敗例が消えていないか（形式と凍結）
3. ルーティング指示の**指し先が実在するか**（ダングリング参照。P1-3 (c)）
4. 指し先が**本番タスクで実際に登録されるか**（台帳 enabledBy × factory の env ゲート）
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from teamagent.skills.base import SkillRegistry
from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill
from teamagent.skills.tiktok_acquire.skill import TikTokAcquireSkill, TikTokAcquireStatusSkill
from teamagent.skills.tiktok_comment_mining.skill import TikTokCommentMiningSkill
from teamagent.skills.tiktok_search.skill import TikTokSearchSkill
from teamagent.skills.x_research.skill import (
    XBuzzMeasureSkill,
    XNeedsMiningSkill,
    XVoiceSearchSkill,
)


def test_new_tools_registered() -> None:
    for n in (
        "x_voice_search",
        "x_needs_mining",
        "x_buzz_measure",
        "x_buzz_measure_status",
        "search_surface_check",
        "tiktok_comment_mining",
    ):
        assert n in SkillRegistry.list_all()


def test_voice_vs_needs_boundary_hardened() -> None:
    # 混同の核: 商材名＋『不満/欲求』が x_needs_mining に流出した問題の硬化を固定。
    v = XVoiceSearchSkill.description
    assert "商材名が主語" in v  # 商材名が主語なら（不満収集でも）voice
    assert "x_needs_mining" in v  # テーマ全体は needs へ、の相互排他
    n = XNeedsMiningSkill.description
    assert "業界/テーマ全体" in n  # 商材非特定
    assert "x_voice_search" in n  # 商材名主語は voice へ、の相互排他


def test_buzz_points_to_sync_x_tools() -> None:
    d = XBuzzMeasureSkill.description
    assert "発話量" in d
    assert "x_voice_search" in d and "x_needs_mining" in d  # 今すぐ見る単発は同期系へ


def test_surface_check_excludes_algo_and_voice() -> None:
    d = SearchSurfaceCheckSkill.description
    assert "勢力図" in d and "媒体比較" in d
    assert "video_algorithm" in d  # 中身分析は algorithm
    assert "x_voice_search" in d  # 声集めは voice


def test_comment_mining_excludes_algo_and_surface() -> None:
    d = TikTokCommentMiningSkill.description
    assert "コメント" in d
    assert "video_algorithm" in d  # 映像分析は algorithm
    assert "search_surface_check" in d  # 面の勢力図は surface


def test_tiktok_search_points_to_new_tools() -> None:
    # §10: 被るツール側（既存 tiktok_search）にも相互排他注記を入れる。
    d = TikTokSearchSkill.description
    assert "search_surface_check" in d  # 面の勢力図・媒体比較は surface
    assert "video_algorithm" in d  # 勝ち筋タイムラインは algorithm
    assert "tiktok_acquire" in d  # 本体DL/大量取得の非同期ジョブは acquire（R3敵対で解消）
    assert "今すぐ" in d or "即時" in d  # 同期/即時の性格を明示


def test_tiktok_acquire_and_status_boundaries_are_explicit() -> None:
    acquire = TikTokAcquireSkill.description
    assert "トリガー=" in acquire
    assert "動画本体(mp4)" in acquire
    assert "tiktok_search" in acquire
    assert "video_algorithm" in acquire

    status = TikTokAcquireStatusSkill.description
    assert "tiktok_acquire" in status
    assert "単独の入口にはしない" in status


def _all_registered_skills() -> set[str]:
    """全 skill パッケージを import してレジストリを満たす（corpus の expect 実在確認用）。"""
    import importlib
    import pkgutil

    import teamagent.skills as sk

    for _, name, ispkg in pkgutil.iter_modules(sk.__path__):
        if ispkg:
            try:
                importlib.import_module(f"teamagent.skills.{name}.skill")
            except Exception:
                pass
    return set(SkillRegistry.list_all())


# expect に書ける疑似ラベル。SkillRegistry には実在しない（「どのツールも呼ばず聞き返す」）。
SENTINEL_PREFIX = "__"
ASK_BACK = "__ask_back__"
SENTINEL_EXPECTS = frozenset({ASK_BACK})

# arg_rules に書ける述語（値の捏造を禁止する契約）。
ARG_RULE_ABSENT = "must_be_absent"
ARG_RULE_PRESENT = "must_be_present"
ARG_RULE_EQUAL_PREFIX = "must_equal:"

# R4 ラウンド（顧客名なし・一語入力クラス）で追加した回帰行。**消したら赤**。
# ここが消えると「2026-08-20 の本番QA事故がコーパスから消える」＝再発を検知できなくなる。
R4_REQUIRED_IDS = frozenset(
    {
        "freebusy-01",
        "freebusy-02",
        "freebusy-03",
        "agenda-01",
        "agenda-02",
        "agenda-03",
        "mailnc-01",
        "mailnc-02",
        "mailnc-03",
        "mailnc-04",
        "oauth-02",
        "oauth-03",
        "oauth-04",
        "r4neg-cal-01",
        "r4neg-mail-01",
        "r4neg-mail-02",
        "r4neg-search-01",
        "r4neg-karte-01",
    }
)


def _corpus_rows() -> list[dict[str, Any]]:
    path = Path(__file__).parent.parent / "routing" / "catalog_routing_corpus.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_corpus_is_wellformed() -> None:
    # コーパスが壊れていないこと（id 重複なし・expect が実在ツール・alt_ok も実在）。
    rows = _corpus_rows()
    assert len(rows) >= 45
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "corpus に id 重複がある"
    known = _all_registered_skills()
    for r in rows:
        expect = r["expect"]
        # 先頭 __ は「ツールを呼ばない」ことを表す sentinel（実在チェックの対象外）。
        if expect.startswith(SENTINEL_PREFIX):
            assert expect in SENTINEL_EXPECTS, f"{r['id']}: 未定義の sentinel {expect}"
        else:
            assert expect in known, f"{r['id']}: expect={expect} が未登録"
        for alt in r.get("alt_ok", []):
            assert alt in known, f"{r['id']}: alt_ok={alt} が未登録"


def test_corpus_forbid_and_arg_rules_are_wellformed() -> None:
    """R4 で足した ``forbid`` / ``arg_rules`` が実在の tool 名・実在の引数名を指すこと。

    シミュの採点表がツールの改名・引数の削除に**気づかず素通り**するのを防ぐ。
    ``arg_rules`` のキーは expect/alt_ok のいずれかの入力スキーマに実在する必要があり、
    ``must_equal:`` の値は enum があればその値域に入っていなければならない。
    """
    known = _all_registered_skills()
    checked_rules = 0
    for r in _corpus_rows():
        candidates = [r["expect"], *r.get("alt_ok", [])]
        real_candidates = [c for c in candidates if not c.startswith(SENTINEL_PREFIX)]

        forbid = r.get("forbid", [])
        assert isinstance(forbid, list), f"{r['id']}: forbid は配列"
        for name in forbid:
            assert name in known, f"{r['id']}: forbid={name} が未登録"
            assert name not in candidates, f"{r['id']}: forbid と expect/alt_ok が矛盾（{name}）"

        rules = r.get("arg_rules", {})
        assert isinstance(rules, dict), f"{r['id']}: arg_rules はオブジェクト"
        if not rules:
            continue
        assert real_candidates, f"{r['id']}: arg_rules を書くなら実在ツールを 1 つ挙げること"
        schemas = [SkillRegistry.get(c).input_schema.model_json_schema() for c in real_candidates]
        for field, rule in rules.items():
            owners = [s for s in schemas if field in (s.get("properties") or {})]
            assert owners, f"{r['id']}: arg_rules の {field} が {real_candidates} に存在しない"
            assert rule in (ARG_RULE_ABSENT, ARG_RULE_PRESENT) or rule.startswith(
                ARG_RULE_EQUAL_PREFIX
            ), f"{r['id']}: 未定義の arg_rule {rule!r}"
            if rule.startswith(ARG_RULE_EQUAL_PREFIX):
                want = rule[len(ARG_RULE_EQUAL_PREFIX) :]
                for schema in owners:
                    allowed = schema["properties"][field].get("enum")
                    if allowed is not None:
                        assert want in allowed, (
                            f"{r['id']}: {field}={want!r} は enum 外（{allowed}）"
                        )
            if rule == ARG_RULE_ABSENT:
                for schema in owners:
                    assert field not in (schema.get("required") or []), (
                        f"{r['id']}: {field} は required なので『渡さない』が不可能。"
                        "任意化しない限りこの行は永久に不合格になる"
                    )
            checked_rules += 1
    assert checked_rules >= 8, "arg_rules の検査が空振りしている（vacuous green）"


def test_r4_regression_rows_are_frozen() -> None:
    """本番QA由来の失敗例がコーパスから消えていないこと（R4 の凍結）。"""
    ids = {r["id"] for r in _corpus_rows()}
    missing = sorted(R4_REQUIRED_IDS - ids)
    assert not missing, (
        f"R4（顧客名なし・一語入力クラス）の回帰行が消えている: {missing}。"
        "tests/routing/README.md の R4 節も併せて確認すること"
    )
    # 対照として残すことを決めた既存の正常系（顧客名あり）も消させない。
    for control in ("neg-mail-01", "neg-mailsum-01", "neg-oauth-01"):
        assert control in ids, f"対照行 {control} が消えている"


# ── (P1-3 c) ダングリング参照 = 「呼べないツールを指すルーティング指示」 ────────────

# ツール名ではないが description / SOUL.md に出る snake_case 識別子（出力キー・エラーコード・
# claim キー）。ここに無い未知の識別子が現れたら「実在しないツールを指した」とみなして赤にする。
# 入力/出力スキーマのフィールド名は自動で除外されるので、ここに書くのはそれ以外だけ。
NON_TOOL_IDENTIFIERS = frozenset(
    {
        # skill が返すエラーコード（Output.error の値）
        "not_connected",
        "agenda_failed",  # calendar_freebusy(mode='agenda') の取得失敗＝「予定 0 件」ではない
        "no_hits",
        # mail_followup が顧客名なしで受信箱全体から候補を出したことを示すコード（ツールではない）
        "inbox_triage",
        # mail_draft が「どれを指しているか確定できないので作らなかった」ことを示すコード
        "ambiguous_selection",
        # mail_draft が「選ばれた番号の件が受信箱から消えていた」ことを示すコード
        "vanished_selection",
        "no_attachment",
        "too_large",
        "unsupported_type",
        # 署名済み caller claim / Slack のメタキー
        "caller_claim",
        "user_id",
        "slack_user_id",
        "slack_team_id",
        "thread_id",
        # 出力に出る URL / S3 キー / DB 列
        "app_url",
        "app_client_url",
        "s3_key",
        "cls_doc_type",
        "external_file",
    }
)

# 台帳（effective-tool-scope.json）には載っているが、**authoritative な本番タスクでは
# env フラグが立っておらず登録されない**ツール。ここを指すルーティング指示は「呼べない先」を
# 案内していることになるので、人間が明示裁定した集合として固定する。
# （集合そのものは台帳の enabledBy.kind=="never" と完全一致することを下のテストが検証する）
NOT_WIRED_IN_PRODUCTION = frozenset({"video_approval", "operation_log", "knowledge_search_url"})

_SCOPE_PATH = (
    Path(__file__).resolve().parents[2] / "infra" / "openclaw" / "effective-tool-scope.json"
)
_SOUL_PATH = Path(__file__).resolve().parents[2] / "infra" / "openclaw" / "SOUL.md"
_FACTORY_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "teamagent" / "orchestrator" / "factory.py"
)

# 単語境界つきの snake_case 識別子（``search_surface_check`` の中の ``search`` に誤爆しない）。
_IDENTIFIER_RE = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?![A-Za-z0-9_])")


def _scope_tools() -> list[dict[str, Any]]:
    return list(json.loads(_SCOPE_PATH.read_text(encoding="utf-8"))["tools"])


def _exposed_descriptions() -> dict[str, str]:
    """OC へ露出する 35 本の name→description（真実源は effective-tool-scope.json）。"""
    known = _all_registered_skills()
    out: dict[str, str] = {}
    for tool in _scope_tools():
        name = tool["name"]
        if name in known:
            out[name] = str(getattr(SkillRegistry.get(name), "description", ""))
    return out


def _schema_field_names() -> set[str]:
    """全 Skill の入出力スキーマのフィールド名（＝ツール名ではないと自動で分かる語）。"""
    fields: set[str] = set()
    for name in _all_registered_skills():
        skill = SkillRegistry.get(name)
        for attr in ("input_schema", "output_schema"):
            model = getattr(skill, attr, None)
            if model is None:
                continue
            schema = model.model_json_schema()
            fields |= set((schema.get("properties") or {}).keys())
            for definition in (schema.get("$defs") or {}).values():
                fields |= set((definition.get("properties") or {}).keys())
    return fields


def _factory_env_gates() -> dict[str, frozenset[str]]:
    """build_production_tools の ToolSpec を「それを囲む _envflag 群」へ静的に対応づける。

    重い依存（embedder / boto3 / psycopg）を作らずに *env をどう評価すると何が登録されるか* を
    決定できる。always 登録（初期 specs リスト）は空集合になる。
    """
    tree = ast.parse(_FACTORY_PATH.read_text(encoding="utf-8"), filename=str(_FACTORY_PATH))
    build = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_production_tools"
    )
    class_to_skill = {SkillRegistry.get(n).__name__: n for n in _all_registered_skills()}
    gates: dict[str, frozenset[str]] = {}

    def _flag_of(test: ast.expr) -> str | None:
        if (
            isinstance(test, ast.Call)
            and isinstance(test.func, ast.Name)
            and test.func.id == "_envflag"
            and test.args
            and isinstance(test.args[0], ast.Constant)
            and isinstance(test.args[0].value, str)
        ):
            return test.args[0].value
        return None

    def _walk(nodes: list[ast.stmt], active: frozenset[str]) -> None:
        for node in nodes:
            if isinstance(node, ast.If):
                flag = _flag_of(node.test)
                inner = active | {flag} if flag else active
                _walk(node.body, inner)
                _walk(node.orelse, active)
                continue
            for sub in ast.walk(node):
                if not (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "ToolSpec"
                    and sub.args
                ):
                    continue
                first = sub.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    gates[first.value] = active
                elif (
                    isinstance(first, ast.Attribute)
                    and first.attr == "name"
                    and isinstance(first.value, ast.Name)
                    and first.value.id in class_to_skill
                ):
                    gates[class_to_skill[first.value.id]] = active

    _walk(build.body, frozenset())
    return gates


def _production_callable_tools() -> set[str]:
    """本番の authoritative タスクで実際に登録されるツール（台帳 enabledBy が真実源）。"""
    return {t["name"] for t in _scope_tools() if (t.get("enabledBy") or {}).get("kind") != "never"}


def test_scope_ledger_env_gates_match_the_factory() -> None:
    """台帳の enabledBy と factory の env ゲートが一致すること（スナップショットの裏取り）。

    ここが一致していて初めて、``enabledBy`` を「本番で何が登録されるか」の代用にできる。
    factory 側のフラグ名だけ変えて台帳を直し忘れる（＝台帳が嘘になる）事故を落とす。
    """
    gates = _factory_env_gates()
    assert len(gates) >= 35, f"factory の静的抽出が壊れている（{len(gates)} 件）"
    for tool in _scope_tools():
        name = tool["name"]
        assert name in gates, f"{name} が factory の ToolSpec に見つからない"
        enabled_by = tool.get("enabledBy") or {}
        kind = enabled_by.get("kind")
        if kind == "always":
            assert gates[name] == frozenset(), f"{name} は台帳 always なのに env ゲートがある"
        elif kind == "envAllTrue":
            assert gates[name] == frozenset(enabled_by["names"]), (
                f"{name}: 台帳 {enabled_by['names']} vs factory {sorted(gates[name])}"
            )
        elif kind == "never":
            assert gates[name], f"{name} は未配線のはずが常時登録になっている"
        else:
            raise AssertionError(f"{name}: 未知の enabledBy.kind={kind!r}")


def test_not_wired_allowlist_matches_the_ledger() -> None:
    """「本番では呼べない」集合を人間裁定の固定値として持ち、台帳と一致させる。"""
    never = {t["name"] for t in _scope_tools() if (t.get("enabledBy") or {}).get("kind") == "never"}
    assert never == set(NOT_WIRED_IN_PRODUCTION), (
        "本番未配線ツールが増減した。ルーティング指示がその先を指していないか確認し、"
        f"NOT_WIRED_IN_PRODUCTION を更新すること: ledger={sorted(never)}"
    )


def test_no_dangling_tool_references_in_descriptions_or_soul() -> None:
    """description / SOUL.md が **実在しないツール**を指していないこと。

    ルーティングの指示先が幽霊だと、利用者は「そっちで出来ます」と言われた先が存在しないまま
    放置される。改名・削除・タイプミスを機械的に落とす。
    """
    known = _all_registered_skills()
    allowed = known | _schema_field_names() | NON_TOOL_IDENTIFIERS
    descriptions = _exposed_descriptions()
    assert len(descriptions) >= 30, "露出セットの解決に失敗（テスト自身の前提が壊れている）"

    sources: list[tuple[str, str]] = [
        *((f"description:{name}", text) for name, text in descriptions.items()),
        ("SOUL.md", _SOUL_PATH.read_text(encoding="utf-8")),
    ]
    referenced_tools: set[str] = set()
    unknown: dict[str, list[str]] = {}
    for origin, text in sources:
        for token in set(_IDENTIFIER_RE.findall(text)):
            if token in known:
                referenced_tools.add(token)
            elif "_" in token and token not in allowed:
                unknown.setdefault(token, []).append(origin)

    assert not unknown, (
        "実在しないツール名らしき識別子がルーティング指示に現れている。"
        "改名/削除したなら参照も直すこと（ツール名でないなら NON_TOOL_IDENTIFIERS へ）: "
        + json.dumps({k: sorted(v) for k, v in sorted(unknown.items())}, ensure_ascii=False)
    )
    # 検出器が空振りしていないこと（正規表現が壊れたら vacuous に緑になる）。
    assert len(referenced_tools) >= 20, f"参照検出が薄すぎる（{sorted(referenced_tools)}）"


def test_routing_pointers_target_tools_that_production_can_actually_call() -> None:
    """指し先が本番で登録されないなら、明示裁定（NOT_WIRED_IN_PRODUCTION）が要る。"""
    callable_now = _production_callable_tools()
    descriptions = _exposed_descriptions()
    sources: list[tuple[str, str]] = [
        *((f"description:{name}", text) for name, text in descriptions.items()),
        ("SOUL.md", _SOUL_PATH.read_text(encoding="utf-8")),
    ]
    offenders: dict[str, list[str]] = {}
    for origin, text in sources:
        for token in set(_IDENTIFIER_RE.findall(text)):
            if token in _all_registered_skills() and token not in callable_now:
                offenders.setdefault(token, []).append(origin)

    undeclared = {k: v for k, v in offenders.items() if k not in NOT_WIRED_IN_PRODUCTION}
    assert not undeclared, (
        "本番タスクで登録されないツールをルーティング指示が指している。"
        "env フラグを立てるか参照を外すこと: "
        + json.dumps({k: sorted(v) for k, v in sorted(undeclared.items())}, ensure_ascii=False)
    )
    # 既知の 2 件（video_analysis→video_approval の除外注記 / SOUL の共通注意リストの
    # operation_log）が消えていないことまでは固定しない＝将来 env を立てたら自然に空になる。


# 期待ツールが**本番タスクでは登録されない**既存行（2026-08-20 に本検出器が初めて可視化）。
# これらは「シミュを回しても永久に不合格」になる行なので、期待値として書いた時点の裁定を
# 明示的に持っておく。env フラグを立てるか行を撤去したらこの表も空にすること。
CORPUS_ROWS_EXPECTING_UNWIRED_TOOLS: dict[str, str] = {
    "vapproval-01": "video_approval（USE_VIDEO_APPROVAL 未設定）",
    "vapproval-02": "video_approval（同上）",
    "boundary-04": "video_approval（同上）",
    "neg-ksurl-01": "knowledge_search_url（USE_KNOWLEDGE_SEARCH_URL_TOOL 未設定）",
}


def test_corpus_expectations_are_callable_in_production() -> None:
    """コーパスの期待ツールが本番で呼べること（呼べない先を新たに期待値にしない）。

    既存の 4 行（:data:`CORPUS_ROWS_EXPECTING_UNWIRED_TOOLS`）は本検出器が可視化した
    **既知の負債**。新しく増えたら赤にする。
    """
    callable_now = _production_callable_tools()
    unreachable: dict[str, list[str]] = {}
    for row in _corpus_rows():
        for name in (row["expect"], *row.get("alt_ok", [])):
            if name.startswith(SENTINEL_PREFIX):
                continue
            if name not in callable_now:
                unreachable.setdefault(row["id"], []).append(name)

    assert set(unreachable) == set(CORPUS_ROWS_EXPECTING_UNWIRED_TOOLS), (
        "本番で登録されないツールを期待する corpus 行が増減した（シミュが永久に不合格になる行）。"
        "env フラグを立てる/行を直す/表を更新する のいずれかを人間が裁定すること: "
        + json.dumps({k: sorted(v) for k, v in sorted(unreachable.items())}, ensure_ascii=False)
    )
    # 指し先が NOT_WIRED_IN_PRODUCTION 以外（＝そもそも露出台帳に無い）なら別の壊れ方。
    for names in unreachable.values():
        for name in names:
            assert name in NOT_WIRED_IN_PRODUCTION, f"{name} は露出台帳にも無い"
