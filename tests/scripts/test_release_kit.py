"""Release kit の停止条件・移植時の設定を、PATH 上の偽 aws で検証する。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "scripts/aws/release/kit"
PROJECTS = (
    "teamagent-dev-approval-publisher",
    "teamagent-dev-mcp-source-publisher",
    "teamagent-dev-image-builder",
    "teamagent-dev-image-attestor",
    "teamagent-dev-image-promoter",
)
SERVICES = ("teamagent-dev-mcp", "teamagent-dev-openclaw", "teamagent-dev-connect-web")


@pytest.fixture
def fake_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "fake bin"
    bin_dir.mkdir()
    aws = bin_dir / "aws"
    aws.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['AWS_CALLS'], 'a') as f:
    f.write(json.dumps([args, os.environ.get('AWS_PROFILE'), os.environ.get('AWS_CA_BUNDLE')]) + '\\n')
if '--profile' in args:
    i = args.index('--profile'); profile = args[i+1]; del args[i:i+2]
    assert profile == 'test-mfa'
else:
    profile = None
    assert os.environ['AWS_PROFILE'] == 'test-read'
service, op = args[:2]
def value(flag): return args[args.index(flag)+1]
scenario = os.environ.get('SCENARIO', '')
if scenario == 'aws-error': sys.exit(19)
if (service, op) == ('codebuild', 'list-builds-for-project'):
    project = value('--project-name')
    count = 101 if scenario == 'old-build' else 1
    print(json.dumps({'ids': [project + ':' + str(i) for i in range(count)]}))
elif (service, op) == ('codebuild', 'batch-get-builds'):
    ids = args[args.index('--ids')+1:args.index('--output')]
    builds = [{'id': i, 'buildStatus': 'IN_PROGRESS' if (i == scenario + ':0' or scenario == 'old-build' and i.endswith(':100')) else 'SUCCEEDED', 'initiator': 'operator/test'} for i in ids]
    print(json.dumps({'builds': builds}))
elif (service, op) == ('ecs', 'describe-services'):
    if '--query' not in args:
        services = args[args.index('--services')+1:args.index('--output')]
        result = [{'serviceName': s, 'deployments': [{'id': 'primary', 'status': 'PRIMARY'}] + ([{'id': 'old-deployment', 'status': 'ACTIVE'}] if scenario == s else [])} for s in services]
        if scenario == 'missing-service': result.pop()
        print(json.dumps({'services': result}))
    elif 'networkConfiguration' in value('--query'):
        print('{}')
    else:
        print('arn:aws:ecs:task-definition/mcp:1')
elif (service, op) == ('ecs', 'describe-task-definition'):
    if 'containerDefinitions[0].image' in value('--query'):
        print('registry/teamagent-mcp@sha256:' + 'a' * 64)
    elif scenario == 'manifest':
        manifest = json.loads(Path(os.environ['KIT_PATH'], 'consumer-manifest-template.json').read_text())
        c = next(c for c in manifest['consumers'] if c['ecs_family'] == value('--task-definition'))
        containers = c['before']['task_definition']['container_definitions']
        print(json.dumps({'containerDefinitions': [{'name': cd['name'], 'environment': [{'name': e['name'], 'value': 'runtime-' + e['name']} for e in cd.get('environment', [])]} for cd in containers]}))
    else:
        print(json.dumps({'family': 'mcp', 'containerDefinitions': [{'name': 'teamagent-mcp', 'image': 'registry/teamagent-mcp@sha256:' + 'a'*64, 'environment': [], 'logConfiguration': {'options': {}}}]}))
elif (service, op) == ('ecs', 'register-task-definition'):
    assert profile == 'test-mfa'
    td = json.loads(Path(value('--cli-input-json').removeprefix('file://')).read_text())
    Path(os.environ['TASK_DIGEST']).write_text(td['containerDefinitions'][0]['image'].split('@')[1])
    print('arn:aws:ecs:task-definition/answer-gate:1')
elif (service, op) == ('ecs', 'deregister-task-definition'):
    assert profile == 'test-mfa'
    print('{}')
elif (service, op) == ('ecs', 'run-task'):
    assert profile == 'test-mfa'
    print('arn:aws:ecs:task/answer-gate')
elif (service, op) == ('ecs', 'wait'):
    pass
elif (service, op) == ('ecs', 'describe-tasks'):
    print('0')
elif (service, op) == ('logs', 'filter-log-events'):
    live = Path(os.environ['TASK_DIGEST']).read_text() == 'sha256:' + 'a'*64
    if scenario == 'live-incomplete' and live:
        print('network failure')
    else:
        status = 'FAIL' if scenario == 'regression' and not live else 'PASS'
        print('ANSWER_GATE case1 ' + status)
        print('ANSWER_GATE_EXIT 0')
else:
    sys.exit('Unexpected AWS operation: ' + repr(args))
"""
    )
    aws.chmod(0o755)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho REACHED_MERGED_PR_CHECK >&2\nexit 42\n")
    gh.chmod(0o755)
    return {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "AWS_CALLS": str(tmp_path / "aws-calls.jsonl"),
        "TASK_DIGEST": str(tmp_path / "task-digest"),
        "RELEASE_WORK_DIR": str(tmp_path / "release work's"),
        "AWS_PROFILE": "test-read",
        "RELEASE_PROFILE": "test-mfa",
        "AWS_CA_BUNDLE": "test-ca",
    }


def deploy(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            str(KIT / "deploy_mcp_gate.sh"),
            "123",
            "--",
            "r123",
            "test",
            "src/file.py",
            "marker",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("busy", (*PROJECTS, *SERVICES, "old-build"))
def test_release_kit_stops_before_pr_or_writes(fake_env: dict[str, str], busy: str) -> None:
    result = deploy({**fake_env, "SCENARIO": busy})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STOP:" in result.stdout
    assert "REACHED_MERGED_PR_CHECK" not in result.stderr
    if busy in PROJECTS or busy == "old-build":
        assert "initiator=operator/test" in result.stdout
    else:
        assert busy in result.stdout
    calls = [
        json.loads(line)[0][:2] for line in Path(fake_env["AWS_CALLS"]).read_text().splitlines()
    ]
    assert all(
        op in ("list-builds-for-project", "batch-get-builds", "describe-services")
        for _, op in calls
    )


def test_release_kit_idle_proceeds_to_pr_check(fake_env: dict[str, str]) -> None:
    result = deploy(fake_env)
    assert "進行中の mcp 便・サービス差し替えなし" in result.stdout
    assert "REACHED_MERGED_PR_CHECK" in result.stderr
    assert result.returncode != 0  # fake gh が止める。実際の便は起動しない。


@pytest.mark.parametrize("scenario", ("aws-error", "missing-service"))
def test_release_kit_unreadable_state_stops(fake_env: dict[str, str], scenario: str) -> None:
    result = deploy({**fake_env, "SCENARIO": scenario})
    assert result.returncode == 1
    assert "同時実行の確認に失敗" in result.stderr
    assert "REACHED_MERGED_PR_CHECK" not in result.stderr


@pytest.mark.parametrize("scenario", ("", "regression", "live-incomplete"))
def test_release_kit_answer_gate_profiles_and_comparison(
    fake_env: dict[str, str], scenario: str
) -> None:
    result = subprocess.run(
        ["/bin/bash", str(KIT / "answer_gate_compare.sh"), "sha256:" + "b" * 64],
        env={**fake_env, "SCENARIO": scenario},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == (0 if scenario == "" else 1), result.stdout + result.stderr
    if scenario == "":
        assert "関門 通過" in result.stdout
    else:
        assert "FATAL" in result.stdout
    calls = [json.loads(line) for line in Path(fake_env["AWS_CALLS"]).read_text().splitlines()]
    assert all(profile == "test-read" and ca == "test-ca" for _, profile, ca in calls)
    assert any(args[:3] == ["--profile", "test-mfa", "ecs"] for args, _, _ in calls)


@pytest.mark.parametrize(
    "path", sorted(KIT.rglob("*.sh")), ids=lambda p: p.relative_to(KIT).as_posix()
)
def test_release_kit_bash32_syntax(path: Path) -> None:
    subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, check=True, timeout=10)


def test_release_kit_template_and_personal_literals() -> None:
    template = json.loads((KIT / "releases/template.json").read_text())
    assert template["name"] == template["description"] == ""
    assert template["OLD_CORE"].startswith("sha256:")
    assert template["checks"]
    for path in KIT.rglob("*"):
        if path.is_file():
            body = path.read_text()
            assert all(term not in body for term in ("/Users/", "vectorinc.co.jp", ".hermes")), path
    assert not (KIT / "mfa_refresh.sh").exists()


def test_release_kit_session_patch_quotes_paths_and_profiles(tmp_path: Path) -> None:
    body = (KIT / "fire_and_authorize.sh").read_text()
    patcher = body.split("\"$RELEASE_PROFILE\" <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    launch = tmp_path / "launch's directory"
    profile = "operator's mfa"
    patched = tmp_path / "patched.sh"
    subprocess.run(
        [
            sys.executable,
            "-",
            str(ROOT / "scripts/aws/release_mcp.sh"),
            str(patched),
            str(launch),
            profile,
        ],
        input=patcher,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    subprocess.run(["/bin/bash", "-n", str(patched)], capture_output=True, check=True, timeout=10)
    text = patched.read_text()
    for key, expected in (("REPO_ROOT", str(launch)), ("RELEASE_PROFILE", profile)):
        assignment = next(line for line in text.splitlines() if line.startswith(key + "="))
        result = subprocess.run(
            ["/bin/bash", "-c", assignment + f'; printf "%s" "${key}"'],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.stdout == expected


def test_release_kit_consumer_manifest_restores_runtime_env(
    fake_env: dict[str, str], tmp_path: Path
) -> None:
    body = (KIT / "fire_and_authorize.sh").read_text()
    generator = body.split("\"$KIT_DIR\" <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    core, media = "sha256:" + "b" * 64, "sha256:" + "c" * 64
    receipt = {
        "approval_evidence": {
            "payload": {"key": "payload", "version_id": "v1", "sha256": "d" * 64},
            "signature": {"key": "signature", "version_id": "v1", "sha256": "e" * 64},
        },
        "subjects": [{"name": "core", "digest": core}, {"name": "media", "digest": media}],
    }
    (tmp_path / "receipt.json").write_text(json.dumps(receipt))
    subprocess.run(
        [sys.executable, "-", str(tmp_path), "sha256:" + "a" * 64, str(KIT)],
        input=generator,
        env={**fake_env, "SCENARIO": "manifest", "KIT_PATH": str(KIT)},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = (tmp_path / "consumer-manifest.json").read_text()
    assert "__LIVE_ENV__" not in output
    assert "runtime-INGEST_OWNER_EMAIL" in output
    assert output.count(core) == 2
    manifest = json.loads(output)
    oc = next(c for c in manifest["consumers"] if c["consumer_id"] == "openclaw")
    assert oc["before"] == oc["after"]
    assert core in (tmp_path / "args.env").read_text()
