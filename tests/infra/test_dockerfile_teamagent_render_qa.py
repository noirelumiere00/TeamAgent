"""Local render QA must stay isolated from the released core and secret contexts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_render_qa_image_hardening_and_locked_network_installs():
    dockerfile = (ROOT / "infra/docker/Dockerfile.teamagent-render-qa").read_text()
    assert "public.ecr.aws/docker/library/alpine:3.24${ALPINE_DIGEST}" in dockerfile
    assert "ARG ALPINE_DIGEST" in dockerfile and "TODO" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "apk add --no-cache $(grep" in dockerfile
    assert "render-qa-apk.lock" in dockerfile
    for line in dockerfile.splitlines():
        if "uv sync" in line:
            assert "--frozen" in line and "--only-group render-qa-runtime" in line
    lock = (ROOT / "infra/docker/render-qa-apk.lock").read_text()
    assert all("=" in line for line in lock.splitlines() if line and not line.startswith("#"))
    assert "libreoffice-impress=" in lock


def test_core_does_not_import_render_qa_or_install_libreoffice():
    core = (ROOT / "infra/docker/Dockerfile.teamagent-mcp").read_text()
    assert "libreoffice" not in core.lower()
    for folder in ("adapters", "skills", "runtime", "mcp_gateway"):
        for source in (ROOT / "src/teamagent" / folder).rglob("*.py"):
            assert "teamagent.media.render_qa" not in source.read_text()


def test_font_builder_has_no_secrets_or_proprietary_font_paths():
    import subprocess

    dockerfile = (ROOT / "infra/docker/Dockerfile.teamagent-render-qa").read_text()
    assert "secret" not in dockerfile
    assert "RUN .venv/bin/python /opt/build_palt_font.py" in dockerfile
    assert "--out /palt-fonts" in dockerfile
    assert "COPY --from=font-builder /palt-fonts" in dockerfile
    assert "--metrics" not in dockerfile
    assert "render-qa-font-build" in dockerfile
    ignore = (ROOT / "infra/docker/Dockerfile.teamagent-render-qa.dockerignore").read_text()
    assert ".env*" in ignore and "secrets/" in ignore
    assert "**/metrics.json" not in ignore
    assert "**/metrics.json" not in (ROOT / ".gitignore").read_text()
    assert not (ROOT / "scripts/fonts" / ("extract_font_" + "metrics.py")).exists()
    files = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    # 秘密情報・git 内部を読み取らず、追跡対象と未追跡のソースを固定文字列で検査。
    files = [
        f
        for f in files
        if f
        and (ROOT / f).is_file()
        and not any(
            part.startswith(".env") or part in {"secrets", ".aws", ".ssh", ".git"}
            for part in Path(f).parts
        )
        and not f.endswith(".pem")
    ]
    result = subprocess.run(
        [
            "rg",
            "-n",
            "-F",
            "-e",
            "Microsoft " + "PowerPoint.app",
            "-e",
            "Yu" + "Goth",
            "--",
            *files,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_cli_network_and_readonly_flags():
    from scripts.deck_render_qa import docker_command

    command = docker_command(
        Path("/tmp/deck.pptx"), Path("/tmp/out"), "proposal_builder_v1", "qa:local"
    )
    for option, value in (
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--memory", "4g"),
        ("--pids-limit", "256"),
        ("--pull", "never"),
    ):
        assert command[command.index(option) + 1] == value
    assert "--read-only" in command
    assert "/tmp:rw,nosuid,nodev,size=4g,mode=1777" in command


def test_lo_profile_disables_macros_and_link_updates():
    profile = (ROOT / "infra/docker/render-qa/lo-profile/registrymodifications.xcu").read_text()
    assert 'oor:name="MacroSecurityLevel"' in profile and "<value>3</value>" in profile
    assert 'oor:name="DisableMacrosExecution"' in profile
    assert 'oor:name="Link"' in profile and "<value>2</value>" in profile
