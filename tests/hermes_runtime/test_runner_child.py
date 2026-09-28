"""JobRunner（使い捨て HOME・子プロセス）と child（ツール・設定・状態の検査）のテスト。

本物の Hermes は使わず、子プロセスは小さな Python スクリプトで代用する。
本番の失敗モード（子の異常終了・時間切れ・結果なし・孫プロセスの居残り・規則外の出力・
秘密の環境変数・黙った API 失敗）を再現する。
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from aico_hermes import child as child_mod
from aico_hermes.config import NEVER
from aico_hermes.runner import JobError, JobRunner, child_env, sweep_stale_workdirs
from aico_hermes.schema import ENTRY_DELIMITER, parse_learn_request

MODEL = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
REGION = "ap-northeast-1"


def _request(**overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "job_id": "job_0123456789",
        "snapshot": {"user": ["返事は結論から3行"], "memory": ["社内FMTを使う"]},
        "utterances": ["花王向けの資料は表でまとめて"],
    }
    payload.update(overrides)
    return parse_learn_request(payload)


def _script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "fake_child.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


def _runner(tmp_path: Path, cmd: list[str], **kwargs: Any) -> JobRunner:
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    base_env = kwargs.pop("base_env", {"PATH": os.environ.get("PATH", "")})
    return JobRunner(
        child_cmd=cmd,
        model=MODEL,
        region=REGION,
        base_env=base_env,
        workdir_root=str(root),
        **kwargs,
    )


def _leftovers(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "work").iterdir())


_OK_CHILD = """
import json, os, pathlib
home = pathlib.Path(os.environ["HERMES_HOME"])
assert pathlib.Path.cwd() == home
assert os.environ["PYTHONSAFEPATH"] == "1"
user = home / "memories" / "USER.md"
entries = [e for e in user.read_text(encoding="utf-8").split("\\n§\\n") if e]
assert entries == ["返事は結論から3行"], entries
data = json.loads((home / "input.json").read_text(encoding="utf-8"))
assert data == {"utterances": ["花王向けの資料は表でまとめて"]}
entries.append("花王向け資料は表形式を好む")
user.write_text("\\n§\\n".join(entries), encoding="utf-8")
(pathlib.Path(os.environ["TMPDIR"]) / "scratch.txt").write_text("x")
(home / ".cache").mkdir()
(home / "result.json").write_text(json.dumps({"ok": True}))
"""


def test_success_returns_updated_entries_and_removes_workdir(tmp_path: Path) -> None:
    result = _runner(tmp_path, _script(tmp_path, _OK_CHILD)).run(_request())
    assert result.job_id == "job_0123456789"
    assert result.user_entries == ("返事は結論から3行", "花王向け資料は表形式を好む")
    assert result.memory_entries == ("社内FMTを使う",)
    assert result.dropped == 0
    assert _leftovers(tmp_path) == []


def test_child_does_not_inherit_server_secrets(tmp_path: Path) -> None:
    body = """
    import json, os, pathlib
    home = pathlib.Path(os.environ["HERMES_HOME"])
    names = ("HERMES_INGRESS_TOKEN", "HERMES_TLS_KEY_FILE", "DATABASE_URL", "ECS_CONTAINER_METADATA_URI_V4")
    leaked = [k for k in names if k in os.environ]
    ok = not leaked and os.environ["HOME"] == str(home) and os.environ["TMPDIR"].startswith(str(home))
    (home / "result.json").write_text(json.dumps({"ok": ok, "code": "leaked"}))
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HERMES_INGRESS_TOKEN": "s" * 40,
        "HERMES_TLS_KEY_FILE": "/secret/key.pem",
        "DATABASE_URL": "postgres://x",
        "ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/x",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/creds",
    }
    result = _runner(tmp_path, _script(tmp_path, body), base_env=env).run(_request())
    assert result.user_entries == ("返事は結論から3行",)


def test_child_env_keeps_only_allowlisted_variables(tmp_path: Path) -> None:
    env = child_env(
        {
            "PATH": "/bin",
            "HERMES_INGRESS_TOKEN": "t",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2",
            "FOO": "1",
        },
        home=tmp_path,
        model=MODEL,
        region=REGION,
    )
    assert "HERMES_INGRESS_TOKEN" not in env and "FOO" not in env
    assert env["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"] == "/v2"
    assert env["HERMES_HOME"] == str(tmp_path) and env["AWS_REGION"] == REGION
    assert env["PYTHONSAFEPATH"] == "1"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("import sys; sys.exit(1)", "child_exit_1"),
        ("pass", "no_result"),
        (
            "import json, os, pathlib; "
            "pathlib.Path(os.environ['HERMES_HOME'], 'result.json').write_text('{')",
            "bad_result",
        ),
        (
            "import json, os, pathlib; pathlib.Path(os.environ['HERMES_HOME'], 'result.json')"
            ".write_text(json.dumps({'ok': False, 'code': 'unexpected_tools'}))",
            "unexpected_tools",
        ),
        (
            "import json, os, pathlib; pathlib.Path(os.environ['HERMES_HOME'], 'result.json')"
            ".write_text(json.dumps({'ok': False, 'code': '田中 さん'}))",
            "child_failed",
        ),
        (
            "import json, os, pathlib; pathlib.Path(os.environ['HERMES_HOME'], 'result.json')"
            ".write_text(json.dumps({'ok': False, 'code': '田中さん'}))",
            "child_failed",
        ),
    ],
)
def test_child_failures_map_to_codes_and_clean_up(tmp_path: Path, body: str, code: str) -> None:
    with pytest.raises(JobError) as exc:
        _runner(tmp_path, _script(tmp_path, body)).run(_request())
    assert exc.value.code == code
    assert _leftovers(tmp_path) == []


def test_timeout_kills_child_quickly_and_cleans_up(tmp_path: Path) -> None:
    body = """
    import os, pathlib, time
    pathlib.Path(os.environ["HERMES_HOME"], "started").write_text("1")
    time.sleep(30)
    """
    started = time.monotonic()
    with pytest.raises(JobError) as exc:
        _runner(tmp_path, _script(tmp_path, body), timeout_s=1.0).run(_request())
    assert exc.value.code == "timeout"
    assert time.monotonic() - started < 10
    assert _leftovers(tmp_path) == []


def test_grandchild_is_killed_after_child_exits(tmp_path: Path) -> None:
    # 子は正常終了するが、同じプロセスグループの孫が作業場の外（tmp_path）へ後から書こうとする
    marker = tmp_path / "late_write.txt"
    body = f"""
    import json, os, pathlib, subprocess, sys
    home = pathlib.Path(os.environ["HERMES_HOME"])
    code = "import time, pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('x')"
    subprocess.Popen([sys.executable, "-c", code])
    (home / "result.json").write_text(json.dumps({{"ok": True}}))
    """
    _runner(tmp_path, _script(tmp_path, body)).run(_request())
    time.sleep(2.5)
    assert not marker.exists()
    assert _leftovers(tmp_path) == []


def test_invalid_output_entries_are_dropped(tmp_path: Path) -> None:
    body = """
    import json, os, pathlib
    home = pathlib.Path(os.environ["HERMES_HOME"])
    entries = ["返事は結論から3行", "区切り§入り", "x" * 5000, "資料は表形式を好む"]
    (home / "memories" / "USER.md").write_text("\\n§\\n".join(entries), encoding="utf-8")
    (home / "result.json").write_text(json.dumps({"ok": True}))
    """
    result = _runner(tmp_path, _script(tmp_path, body)).run(_request())
    assert result.user_entries == ("返事は結論から3行", "資料は表形式を好む")
    assert result.dropped == 2
    assert _leftovers(tmp_path) == []


def test_sweep_removes_stale_workdirs(tmp_path: Path) -> None:
    stale = tmp_path / "hermes-job-abc123"
    stale.mkdir()
    (stale / "input.json").write_text('{"utterances": ["x"]}')
    other = tmp_path / "keep-me"
    other.mkdir()
    assert sweep_stale_workdirs(str(tmp_path)) == 1
    assert not stale.exists() and other.exists()


def test_runner_refuses_empty_command() -> None:
    with pytest.raises(ValueError):
        JobRunner(child_cmd=[], model=MODEL, region=REGION)


def test_entry_delimiter_matches_hermes() -> None:
    # Hermes v2026.9.21 tools/memory_tool_store.py の ENTRY_DELIMITER と同じであること
    assert ENTRY_DELIMITER == "\n§\n"


class _FakeAgent:
    def __init__(self, tools: set[str], *, fail: bool = False, outcome: Any = None) -> None:
        self.valid_tool_names = tools
        self.fail = fail
        self.outcome = (
            {"completed": True, "failed": False, "final_response": "完了"}
            if outcome is None
            else outcome
        )
        self.messages: list[str] = []
        self._memory_store = object()
        self._memory_nudge_interval = NEVER
        self._skill_nudge_interval = NEVER

    def run_conversation(self, *, user_message: str) -> Any:
        self.messages.append(user_message)
        if self.fail:
            raise RuntimeError("田中さんの電話 090-1234-5678")
        return self.outcome


def _child_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "input.json").write_text(json.dumps({"utterances": ["資料は表で"]}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("AICO_HERMES_MODEL", MODEL)
    return home


def _run_child(home: Path, agent: Any, problems: list[str] | None = None) -> int:
    return child_mod.run(
        home, agent_factory=lambda model: agent, config_checker=lambda: list(problems or [])
    )


def _result(home: Path) -> dict[str, Any]:
    return json.loads((home / "result.json").read_text(encoding="utf-8"))


def test_child_runs_agent_with_memory_tool_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _child_home(tmp_path, monkeypatch)
    agent = _FakeAgent({"memory"})
    assert _run_child(home, agent) == 0
    assert _result(home) == {"ok": True}
    assert len(agent.messages) == 1 and "<utterances>" in agent.messages[0]


@pytest.mark.parametrize(
    "tools", [{"memory", "terminal"}, {"memory", "session_search"}, set(), {"todo_list"}]
)
def test_child_stops_before_model_call_when_other_tools_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: set[str]
) -> None:
    home = _child_home(tmp_path, monkeypatch)
    agent = _FakeAgent(tools)
    assert _run_child(home, agent) == 3
    assert _result(home) == {"ok": False, "code": "unexpected_tools"}
    assert agent.messages == []


def test_child_stops_when_effective_config_is_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _child_home(tmp_path, monkeypatch)
    agent = _FakeAgent({"memory"})
    assert _run_child(home, agent, problems=["memory.write_approval"]) == 6
    assert _result(home) == {"ok": False, "code": "config_lint"}
    assert agent.messages == []


@pytest.mark.parametrize(
    ("attr", "value"),
    [
        ("_memory_store", None),
        ("_memory_nudge_interval", 10),
        ("_skill_nudge_interval", 10),
        ("_memory_nudge_interval", None),
    ],
)
def test_child_stops_when_agent_state_is_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attr: str, value: Any
) -> None:
    home = _child_home(tmp_path, monkeypatch)
    agent = _FakeAgent({"memory"})
    setattr(agent, attr, value)
    assert _run_child(home, agent) == 7
    assert _result(home) == {"ok": False, "code": "bad_agent_state"}
    assert agent.messages == []


def test_child_hides_agent_exception_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _child_home(tmp_path, monkeypatch)
    agent = _FakeAgent({"memory"}, fail=True)
    assert _run_child(home, agent) == 4
    raw = (home / "result.json").read_text(encoding="utf-8")
    assert "田中" not in raw and json.loads(raw) == {"ok": False, "code": "agent_error"}


@pytest.mark.parametrize(
    "outcome",
    [
        # Hermes は Bedrock が失敗しても例外を投げず、エラー文を最終応答にして返す（09-25 実測）
        {
            "completed": False,
            "failed": True,
            "final_response": "AWS Bedrock didn't answer after 3 attempts",
        },
        {"completed": True, "failed": True},
        {"completed": True, "interrupted": True},
        {"completed": True, "partial": True},
        {"completed": True, "error": "compression timeout"},
        {"completed": True, "failure_reason": "budget"},
        {"final_response": "完了"},
        "完了",
        None,
    ],
)
def test_child_treats_silent_agent_failure_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: Any
) -> None:
    home = _child_home(tmp_path, monkeypatch)
    agent = _FakeAgent({"memory"})
    agent.outcome = outcome
    assert _run_child(home, agent) == 5
    assert _result(home) == {"ok": False, "code": "agent_failed"}


def test_child_rejects_wrong_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _child_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert _run_child(home, _FakeAgent({"memory"})) == 2
    assert _result(home)["code"] == "bad_env"


def test_child_rejects_bad_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _child_home(tmp_path, monkeypatch)
    (home / "input.json").write_text(json.dumps({"utterances": [1]}), encoding="utf-8")
    assert _run_child(home, _FakeAgent({"memory"})) == 2
    assert _result(home)["code"] == "bad_input"
