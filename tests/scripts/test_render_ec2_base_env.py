from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.render_ec2_base_env import BaseEnvironmentError, main, render

ROOT = Path(__file__).resolve().parents[2]


def test_renderer_allows_only_nonsecret_values_and_secret_references(tmp_path: Path) -> None:
    base = tmp_path / "base.env"
    override = tmp_path / "override.env"
    base.write_text(
        "APP_ENV=production\n"
        "AWS_REGION=ap-northeast-1\n"
        "SLACK_BOT_TOKEN_SECRET_NAME=teamagent/dev/slack/bot_token\n"
        "RDS_HOST=old.example.internal\n",
        encoding="utf-8",
    )
    override.write_text(
        "RDS_HOST=db.example.internal # reviewed override\n"
        "CONNECT_GOOGLE_CLIENT_ID=123.apps.googleusercontent.com\n",
        encoding="utf-8",
    )

    rendered = render((base, override)).decode()

    assert "RDS_HOST=db.example.internal\n" in rendered
    assert rendered.count("RDS_HOST=") == 1
    assert "SLACK_BOT_TOKEN_SECRET_NAME=teamagent/dev/slack/bot_token\n" in rendered


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SLACK_BOT_TOKEN", "xoxb-real-looking-token"),
        ("DATABASE_URL", "postgresql://user:password@db.internal/teamagent"),
        ("ANTHROPIC_API_KEY", "sk-ant-secret-material"),
        ("UNREVIEWED_FLAG", "true"),
        ("LOG_LEVEL", "$(id)"),
        ("LOG_LEVEL", "aZ9Qm2Lp7Vx4Nc8Rt1Kw6Hy3Df5Bj0Gs"),
        ("SLACK_BOT_TOKEN_SECRET_NAME", "opaque-secret-material"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/unreviewed.json"),
    ],
)
def test_renderer_rejects_secret_like_or_unreviewed_assignments(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    source = tmp_path / "source.env"
    source.write_text(f"{name}={value}\n", encoding="utf-8")

    with pytest.raises(BaseEnvironmentError):
        render((source,))


def test_checked_in_ec2_overrides_are_fully_allowlisted_and_secret_free() -> None:
    rendered = render((ROOT / "infra" / "deploy" / "ec2.overrides.env",)).decode()

    assert "GOOGLE_APPLICATION_CREDENTIALS=/opt/teamagent/secrets/vertex-sa.json" in rendered
    assert "OMP_NUM_THREADS=1" in rendered
    assert "xoxb-" not in rendered
    assert "postgresql://" not in rendered


def test_cli_creates_root_only_environment_without_overwriting(tmp_path: Path) -> None:
    source = tmp_path / "source.env"
    output = tmp_path / "rendered.env"
    source.write_text("APP_ENV=production\n", encoding="utf-8")

    assert main(["--base", str(source), "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == "APP_ENV=production\n"
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert main(["--base", str(source), "--output", str(output)]) == 2
