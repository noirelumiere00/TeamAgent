"""OC tool scope / SkillRegistry / MCP exposure の静的な集合契約。

``build_production_tools()`` は SearchSkill の重い依存と boto3 client を構築するため、全 env
flag を ON にした dry-run は CI では安全に実行できない。代わりに ToolSpec 登録箇所と
``@register`` クラスを AST で抽出し、scope 記載・明示 dark 許容リストとの完全一致を固定する。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = ROOT / "src/teamagent/skills"
FACTORY = ROOT / "src/teamagent/orchestrator/factory.py"
MCP_SERVER = ROOT / "src/teamagent/mcp_gateway/server.py"
SCOPE = ROOT / "infra/openclaw/effective-tool-scope.json"

# SkillRegistry に存在するが effective scope には載せない、と人間が明示裁定した集合。
# 新規 skill を分類せず factory の env flag だけ ON にする変更は、この固定集合との差分で落ちる。
DARK_SKILL_ALLOWLIST = frozenset(
    {
        "chitchat",
        "recommend",
        "mail_constraints",
        "workspace_search",
        "proposal_deck",
        "proposal_campaign",
        "proposal_builder",
        # お土産資料 便1（2026-08-24 実装）。USE_OMIYAGE_REPORT_TOOLS は未点灯のため
        # scope/OC toolFilter へは未掲載。点灯時に scope 台帳 + include へ移す（人間ゲート）。
        "omiyage_report_submit",
        "omiyage_report_status",
    }
)

# chitchat は Socket Mode 専用。同期proposal_builderはPython互換用で、MCPはsubmit/statusだけ。
NON_FACTORY_SKILL_ALLOWLIST = frozenset({"chitchat", "proposal_builder"})

# ToolSpec を経由せず server.py が直接 MCP に追加できる dark tool。
MCP_ONLY_DARK_ALLOWLIST = {"run_agent": "USE_AGENT_ORCHESTRATOR"}

# Drive→Slack の実ファイル配信共通部品（_shared/drive_slack_delivery.py）。
# これを使う skill は「読むだけ」ではなく**利用者の Slack にファイルを投下する**。
DELIVERY_MODULE = "teamagent.skills._shared.drive_slack_delivery"
DELIVERY_SYMBOLS = frozenset({"deliver_files", "upload_all", "prepare_drive_files"})
# 実ファイル配信を行うと人間が裁定した skill。ここに載る skill は台帳 effect でも
# 配信を申告する（下の契約テストが機械照合する）。新しく配信を始めた skill は
# この集合との差分で必ず赤くなる＝申告漏れのままマージできない。
FILE_DELIVERY_SKILLS = frozenset({"clientkarte", "knowledge_deliver"})
DELIVERY_EFFECT_MARKER = "slack-file-delivery"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _registered_skills() -> dict[str, str]:
    """skills package 全体の @register class を name -> class-name で返す。"""
    registered: dict[str, str] = {}
    for path in sorted(SKILLS_ROOT.rglob("*.py")):
        tree = _parse(path)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(
                isinstance(decorator, ast.Name) and decorator.id == "register"
                for decorator in node.decorator_list
            ):
                continue
            name_value: ast.expr | None = None
            for statement in node.body:
                if (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    and statement.target.id == "name"
                ):
                    name_value = statement.value
                    break
                if isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "name"
                    for target in statement.targets
                ):
                    name_value = statement.value
                    break
            assert isinstance(name_value, ast.Constant) and isinstance(name_value.value, str), (
                f"{path}:{node.lineno} @register class の name は静的文字列で宣言すること"
            )
            skill_name = name_value.value
            assert skill_name not in registered, f"SkillRegistry name 重複: {skill_name}"
            registered[skill_name] = node.name
    return registered


def _skill_names_by_module() -> dict[Path, set[str]]:
    """skills package の各 .py が定義する @register skill 名を返す。"""
    by_module: dict[Path, set[str]] = {}
    for path in sorted(SKILLS_ROOT.rglob("*.py")):
        tree = _parse(path)
        names = set()
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(
                isinstance(decorator, ast.Name) and decorator.id == "register"
                for decorator in node.decorator_list
            ):
                continue
            for statement in node.body:
                target_is_name = (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    and statement.target.id == "name"
                ) or (
                    isinstance(statement, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "name"
                        for target in statement.targets
                    )
                )
                if not target_is_name:
                    continue
                value = statement.value  # type: ignore[union-attr]
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    names.add(value.value)
                break
        if names:
            by_module[path] = names
    return by_module


def _file_delivering_skill_names() -> set[str]:
    """Drive→Slack 配信の共通部品を import している module の skill 名を集める。"""
    by_module = _skill_names_by_module()
    delivering: set[str] = set()
    for path, names in by_module.items():
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != DELIVERY_MODULE:
                continue
            if any(alias.name in DELIVERY_SYMBOLS for alias in node.names):
                delivering |= names
                break
    return delivering


def _tool_spec_name(call: ast.Call, class_to_skill: dict[str, str]) -> str:
    assert isinstance(call.func, ast.Name) and call.func.id == "ToolSpec"
    assert call.args, f"{FACTORY}:{call.lineno} ToolSpec の name 引数が無い"
    expression = call.args[0]
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value
    assert (
        isinstance(expression, ast.Attribute)
        and expression.attr == "name"
        and isinstance(expression.value, ast.Name)
    ), f"{FACTORY}:{call.lineno} ToolSpec name を静的解決できない"
    class_name = expression.value.id
    assert class_name in class_to_skill, (
        f"{FACTORY}:{call.lineno} {class_name}.name は @register class に実在しない"
    )
    return class_to_skill[class_name]


def _factory_tool_names(registered: dict[str, str]) -> set[str]:
    """build_production_tools の ``specs`` 構築を静的に全数抽出する。"""
    tree = _parse(FACTORY)
    build = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_production_tools"
    )
    class_to_skill = {class_name: name for name, class_name in registered.items()}

    initial_assignments = [
        node
        for node in build.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "specs" for target in node.targets)
    ]
    assert len(initial_assignments) == 1, "specs の初期化は静的リスト1箇所に保つこと"
    initial = initial_assignments[0]
    assert isinstance(initial.value, ast.List), "specs の初期値は ToolSpec の静的リストにすること"

    # specs の別名化・後段再代入・helper 引き渡し・slice/attribute 書込を許すと、
    # 下の ToolSpec 抽出を迂回して未分類 tool を返せてしまう。Load/Store の使い道を
    # 「初期化・直接 append・return specs」だけへ閉じ、静的完全抽出を構造契約にする。
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(build):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    specs_stores = [
        node
        for node in ast.walk(build)
        if isinstance(node, ast.Name)
        and node.id == "specs"
        and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    initial_targets = [
        target
        for target in initial.targets
        if isinstance(target, ast.Name) and target.id == "specs"
    ]
    assert specs_stores == initial_targets, (
        "specs は静的リスト初期化後に再代入・削除・slice/attribute 書込しないこと"
    )

    for node in ast.walk(build):
        if not (
            isinstance(node, ast.Name) and node.id == "specs" and isinstance(node.ctx, ast.Load)
        ):
            continue
        parent = parents.get(node)
        append_call = (
            isinstance(parent, ast.Attribute)
            and parent.value is node
            and parent.attr == "append"
            and isinstance((grandparent := parents.get(parent)), ast.Call)
            and grandparent.func is parent
        )
        direct_return = isinstance(parent, ast.Return) and parent.value is node
        assert append_call or direct_return, (
            f"{FACTORY}:{node.lineno} specs は直接 append と return 以外へ渡さないこと"
        )

    returns = [node for node in ast.walk(build) if isinstance(node, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0].value, ast.Name), (
        "build_production_tools は末尾の return specs 1箇所だけにすること"
    )
    assert returns[0].value.id == "specs", "build_production_tools は return specs にすること"

    calls: list[ast.Call] = []
    for element in initial.value.elts:
        assert (
            isinstance(element, ast.Call)
            and isinstance(element.func, ast.Name)
            and element.func.id == "ToolSpec"
        ), f"{FACTORY}:{element.lineno} specs 初期値は ToolSpec(...) のみ許可"
        calls.append(element)

    for node in ast.walk(build):
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            assert node.target.id != "specs", "specs += ... は静的抽出不能なので禁止"
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "specs"
        ):
            continue
        assert node.func.attr == "append", (
            f"{FACTORY}:{node.lineno} specs.{node.func.attr} は静的抽出不能。append を使うこと"
        )
        assert len(node.args) == 1 and not node.keywords, (
            f"{FACTORY}:{node.lineno} specs.append は ToolSpec 1個だけを受け取ること"
        )
        value = node.args[0]
        assert (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "ToolSpec"
        ), f"{FACTORY}:{node.lineno} specs.append の引数は ToolSpec(...) にすること"
        calls.append(value)

    all_tool_spec_calls = [
        node
        for node in ast.walk(build)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ToolSpec"
    ]
    assert len(calls) == len(all_tool_spec_calls), (
        "build_production_tools の ToolSpec は specs 初期値または specs.append に直接置くこと"
    )

    names = [_tool_spec_name(call, class_to_skill) for call in calls]
    assert len(names) == len(set(names)), f"factory ToolSpec name 重複: {names}"
    return set(names)


def _mcp_only_tool_names() -> set[str]:
    """server.py の ``Tool(name=...)`` から ToolSpec 由来でない名前を抽出する。"""
    tree = _parse(MCP_SERVER)
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    special: set[str] = set()
    for call in (
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Tool"
    ):
        name_kw = next((kw.value for kw in call.keywords if kw.arg == "name"), None)
        assert name_kw is not None, f"{MCP_SERVER}:{call.lineno} Tool(name=...) が必要"

        ancestor: ast.AST | None = call
        while ancestor is not None and not isinstance(ancestor, ast.FunctionDef):
            ancestor = parents.get(ancestor)
        function_name = ancestor.name if isinstance(ancestor, ast.FunctionDef) else ""

        # list_tool_defs の s.name だけが factory ToolSpec 由来の動的な名前。
        if (
            function_name == "list_tool_defs"
            and isinstance(name_kw, ast.Attribute)
            and isinstance(name_kw.value, ast.Name)
            and name_kw.value.id == "s"
            and name_kw.attr == "name"
        ):
            continue
        if isinstance(name_kw, ast.Constant) and isinstance(name_kw.value, str):
            special.add(name_kw.value)
            continue
        if isinstance(name_kw, ast.Name) and name_kw.id in constants:
            special.add(constants[name_kw.id])
            continue
        raise AssertionError(f"{MCP_SERVER}:{call.lineno} MCP tool name を静的解決できない")
    return special


def _assert_mcp_tool_builder_is_static() -> None:
    """MCP tool 定義の合流点を ToolSpec 群 + run_agent 1本へ閉じる。"""

    tree = _parse(MCP_SERVER)
    list_specs = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "list_tool_defs"
    )
    spec_returns = [node for node in list_specs.body if isinstance(node, ast.Return)]
    assert len(spec_returns) == 1 and isinstance(spec_returns[0].value, ast.ListComp)
    comprehension = spec_returns[0].value
    assert (
        isinstance(comprehension.elt, ast.Call)
        and isinstance(comprehension.elt.func, ast.Name)
        and comprehension.elt.func.id == "Tool"
        and len(comprehension.generators) == 1
        and isinstance(comprehension.generators[0].target, ast.Name)
        and comprehension.generators[0].target.id == "s"
        and isinstance(comprehension.generators[0].iter, ast.Name)
        and comprehension.generators[0].iter.id == "specs"
        and not comprehension.generators[0].ifs
        and comprehension.generators[0].is_async == 0
    ), "list_tool_defs は specs の1対1 Tool 内包表記だけに保つこと"
    executable_spec_statements = [
        node
        for node in list_specs.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    assert executable_spec_statements == spec_returns

    list_all = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "list_all_tool_defs"
    )
    assignments = [
        node
        for node in list_all.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "defs" for target in node.targets)
    ]
    assert len(assignments) == 1, "MCP defs の初期化は list_tool_defs(specs) 1箇所に保つこと"
    initial = assignments[0]
    assert (
        isinstance(initial.value, ast.Call)
        and isinstance(initial.value.func, ast.Name)
        and initial.value.func.id == "list_tool_defs"
        and len(initial.value.args) == 1
        and isinstance(initial.value.args[0], ast.Name)
        and initial.value.args[0].id == "specs"
        and not initial.value.keywords
    ), "MCP defs は factory ToolSpec の写像だけで初期化すること"

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(list_all):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    stores = [
        node
        for node in ast.walk(list_all)
        if isinstance(node, ast.Name)
        and node.id == "defs"
        and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    initial_targets = [
        target for target in initial.targets if isinstance(target, ast.Name) and target.id == "defs"
    ]
    assert stores == initial_targets, "MCP defs の再代入・別名化可能な書込は禁止"

    appended_helpers: list[str] = []
    append_statements: list[ast.Expr] = []
    for node in ast.walk(list_all):
        if not (
            isinstance(node, ast.Name) and node.id == "defs" and isinstance(node.ctx, ast.Load)
        ):
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Return) and parent.value is node:
            continue
        call = parents.get(parent) if isinstance(parent, ast.Attribute) else None
        assert (
            isinstance(parent, ast.Attribute)
            and parent.value is node
            and parent.attr == "append"
            and isinstance(call, ast.Call)
            and call.func is parent
            and len(call.args) == 1
            and not call.keywords
            and isinstance(call.args[0], ast.Call)
            and isinstance(call.args[0].func, ast.Name)
            and not call.args[0].args
            and not call.args[0].keywords
        ), f"{MCP_SERVER}:{node.lineno} defs は明示 helper の直接 append 以外で変更しないこと"
        appended_helpers.append(call.args[0].func.id)
        statement = parents.get(call)
        assert isinstance(statement, ast.Expr)
        append_statements.append(statement)

    assert appended_helpers == ["_run_agent_tool_def"]
    assert len(append_statements) == 1
    orchestrator_gate = parents.get(append_statements[0])
    assert (
        isinstance(orchestrator_gate, ast.If)
        and isinstance(orchestrator_gate.test, ast.Name)
        and orchestrator_gate.test.id == "enable_orchestrator"
        and orchestrator_gate.body == append_statements
        and not orchestrator_gate.orelse
    ), "run_agent は厳密に if enable_orchestrator 配下だけで追加すること"
    returns = [node for node in ast.walk(list_all) if isinstance(node, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0].value, ast.Name)
    assert returns[0].value.id == "defs"
    executable_list_all = [
        node
        for node in list_all.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    assert executable_list_all == [initial, orchestrator_gate, returns[0]]

    build_server = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_server"
    )
    list_callbacks = [
        node
        for node in build_server.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_list"
    ]
    assert len(list_callbacks) == 1
    list_tools_registrations = [
        node
        for node in ast.walk(build_server)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "list_tools"
    ]
    assert len(list_tools_registrations) == 1, "server.list_tools callback は1箇所だけにすること"
    callback = list_callbacks[0]
    assert (
        len(callback.decorator_list) == 1
        and callback.decorator_list[0] is list_tools_registrations[0]
        and isinstance(callback.decorator_list[0].func, ast.Attribute)
        and isinstance(callback.decorator_list[0].func.value, ast.Name)
        and callback.decorator_list[0].func.value.id == "server"
        and callback.decorator_list[0].func.attr == "list_tools"
        and not callback.decorator_list[0].args
        and not callback.decorator_list[0].keywords
    ), "唯一の server.list_tools registration は _list 自身の decorator に固定すること"
    callback_returns = [node for node in callback.body if isinstance(node, ast.Return)]
    assert len(callback_returns) == 1
    executable_callback = [
        node
        for node in callback.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    assert executable_callback == callback_returns, (
        "_list callback は静的 return 以外を実行しないこと"
    )
    returned = callback_returns[0].value
    assert (
        isinstance(returned, ast.Call)
        and isinstance(returned.func, ast.Name)
        and returned.func.id == "list_all_tool_defs"
        and len(returned.args) == 1
        and isinstance(returned.args[0], ast.Name)
        and returned.args[0].id == "specs"
        and len(returned.keywords) == 1
        and returned.keywords[0].arg == "enable_orchestrator"
        and isinstance(returned.keywords[0].value, ast.Name)
        and returned.keywords[0].value.id == "enable_orchestrator"
    ), "server.list_tools callback は静的な list_all_tool_defs 合流点だけを返すこと"

    orchestrator_assignments = [
        node
        for node in build_server.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "enable_orchestrator"
            for target in node.targets
        )
    ]
    assert len(orchestrator_assignments) == 1
    orchestrator_assignment = orchestrator_assignments[0]
    orchestrator_targets = [
        target
        for target in orchestrator_assignment.targets
        if isinstance(target, ast.Name) and target.id == "enable_orchestrator"
    ]
    orchestrator_stores = [
        node
        for node in ast.walk(build_server)
        if isinstance(node, ast.Name)
        and node.id == "enable_orchestrator"
        and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    assert len(orchestrator_targets) == 1 and orchestrator_stores == orchestrator_targets, (
        "enable_orchestrator の後段・nested・AugAssignによるgate拡幅は禁止"
    )
    orchestrator_value = orchestrator_assignment.value
    assert (
        isinstance(orchestrator_value, ast.Call)
        and isinstance(orchestrator_value.func, ast.Name)
        and orchestrator_value.func.id == "_envflag"
        and len(orchestrator_value.args) == 1
        and isinstance(orchestrator_value.args[0], ast.Constant)
        and orchestrator_value.args[0].value == "USE_AGENT_ORCHESTRATOR"
        and not orchestrator_value.keywords
    ), "enable_orchestrator は USE_AGENT_ORCHESTRATOR 単独のenv gateに固定すること"


def _scope_names() -> set[str]:
    scope = json.loads(SCOPE.read_text(encoding="utf-8"))
    return {tool["name"] for tool in scope["tools"]}


def test_scope_registry_and_factory_have_an_exact_classification() -> None:
    """scope幽霊・未分類dark・factory追加時の副作用分類漏れを同時に拒否する。"""
    registered = _registered_skills()
    registry_names = set(registered)
    scope_names = _scope_names()
    factory_names = _factory_tool_names(registered)
    scope = json.loads(SCOPE.read_text(encoding="utf-8"))

    # 不変量(1): scope に書かれた name は必ず SkillRegistry の @register class に実在する。
    assert scope_names <= registry_names
    assert all(
        isinstance(tool.get("effect"), str) and tool["effect"].strip() for tool in scope["tools"]
    ), "scope 登録には空でない副作用分類 effect が必須"

    # 素朴な registry⊆scope ではなく、台帳外は固定 allowlist と完全一致させる。
    assert registry_names - scope_names == DARK_SKILL_ALLOWLIST

    # 不変量(2): factory は scope 全件 + MCP化されるdark skillを過不足なく登録可能。
    assert factory_names == scope_names | (DARK_SKILL_ALLOWLIST - NON_FACTORY_SKILL_ALLOWLIST)
    assert registry_names == factory_names | NON_FACTORY_SKILL_ALLOWLIST


def test_server_only_mcp_tools_are_explicitly_dark_allowlisted() -> None:
    """ToolSpec 外から MCP 露出可能な tool が増えたら明示裁定なしでは赤にする。"""
    _assert_mcp_tool_builder_is_static()
    assert _mcp_only_tool_names() == set(MCP_ONLY_DARK_ALLOWLIST)
    # server.py の定義変換は外部I/Oを行わないため、空specに対する実出力も固定する。
    from teamagent.mcp_gateway.server import list_all_tool_defs

    assert list_all_tool_defs([], enable_orchestrator=False) == []
    assert [tool.name for tool in list_all_tool_defs([], enable_orchestrator=True)] == ["run_agent"]
    server_source = MCP_SERVER.read_text(encoding="utf-8")
    for tool_name, env_gate in MCP_ONLY_DARK_ALLOWLIST.items():
        assert f'"{tool_name}"' in server_source
        assert f'_envflag("{env_gate}")' in server_source


def test_file_delivering_skills_declare_the_side_effect_in_the_scope_ledger() -> None:
    """「読むだけ」の申告のまま実ファイル配信を始めた skill を機械的に落とす。

    effect が非空かどうかだけでは、clientkarte が Drive バイナリ DL + Slack file upload を
    始めても台帳が ``company-data-read-analysis`` のまま通ってしまう（実際に起きた）。
    共通部品の import という**実装側の事実**と台帳の申告を突き合わせる。
    """
    detected = _file_delivering_skill_names()
    # 検出器そのものが空振り（import 経路の改名等）したら vacuous に緑にしない。
    assert detected, f"{DELIVERY_MODULE} の配信部品を使う skill が 1 つも検出できていない"
    assert detected == FILE_DELIVERY_SKILLS, (
        "実ファイル配信を始めた/やめた skill がある。"
        "FILE_DELIVERY_SKILLS と effective-tool-scope.json の effect を人間が裁定して更新すること"
    )

    scope = json.loads(SCOPE.read_text(encoding="utf-8"))
    effect_by_name = {tool["name"]: tool["effect"] for tool in scope["tools"]}
    for name in sorted(FILE_DELIVERY_SKILLS):
        assert name in effect_by_name, f"{name} が effective-tool-scope.json に無い"
        assert DELIVERY_EFFECT_MARKER in effect_by_name[name], (
            f"{name} は Slack に実ファイルを投下するのに "
            f"effect='{effect_by_name[name]}' が配信を申告していない"
        )
