"""Executable EC2 HMAC loader tests with a local fake AWS CLI."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_MAIL_PRIMARY = "dedicated-mail-primary-" + "m" * 32
_LEGACY_DATABASE_URL = (
    "postgresql://teamagent:legacy-password@db.internal:5432/teamagent?sslmode=require"
)
_PRIMARY_VERSION = "b" * 32
_PREVIOUS_VERSION = "a" * 32
_SLACK_BOT = "xoxb-sensitive-bot-fragment-123456789"
_SLACK_APP = "xapp-sensitive-app-fragment-123456789"
_GOOGLE_CLIENT = "sensitive-client-id.apps.googleusercontent.com"
_GOOGLE_REFRESH = "1//0SensitiveRefreshFragment123456789"


def _fake_aws(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "aws.log"
    aws_path = fake_bin / "aws"
    aws_path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
secret_id=""
version_id=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --secret-id) secret_id="$2"; shift 2 ;;
    --version-id) version_id="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '%s|%s\\n' "$secret_id" "$version_id" >>"$FAKE_AWS_LOG"
case "$secret_id" in
  teamagent/dev/db_password) printf '%s\\n' 'fake-db-password' ;;
  teamagent/dev/slack/bot_token) printf '%s\\n' "$FAKE_SLACK_BOT" ;;
  teamagent/dev/slack/app_token) printf '%s\\n' "$FAKE_SLACK_APP" ;;
  teamagent/dev/google_oauth) printf '%s\\n' "$FAKE_GOOGLE_JSON" ;;
  teamagent/dev/hmac/mail-action) printf '%s\\n' "$FAKE_MAIL_PRIMARY" ;;
  teamagent/dev/database-url) printf '%s\\n' "$FAKE_LEGACY_DATABASE_URL" ;;
  *) exit 9 ;;
esac
""",
        encoding="utf-8",
    )
    aws_path.chmod(0o755)
    return fake_bin, log_path


def _base_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    fake_bin, log_path = _fake_aws(tmp_path)
    env = {
        name: value
        for name, value in os.environ.items()
        if "_HMAC_" not in name
        and name
        not in {
            "TEAMAGENT_HMAC_REQUIRED_DOMAINS",
            "SENTRY_DSN_SECRET_NAME",
            "OPS_SLACK_WEBHOOK_SECRET_NAME",
            "OAUTH_STATE_SECRET_NAME",
            "CONNECT_GOOGLE_CLIENT_SECRET_NAME",
            "GOOGLE_OAUTH_SECRET_NAME",
            "VERTEX_SA_SECRET_NAME",
        }
    }
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_AWS_LOG": str(log_path),
            "FAKE_MAIL_PRIMARY": _MAIL_PRIMARY,
            "FAKE_LEGACY_DATABASE_URL": _LEGACY_DATABASE_URL,
            "FAKE_SLACK_BOT": _SLACK_BOT,
            "FAKE_SLACK_APP": _SLACK_APP,
            "FAKE_GOOGLE_JSON": (
                '{"client_id":"'
                + _GOOGLE_CLIENT
                + '","client_secret":"sensitive-client-secret",'
                + '"refresh_token":"'
                + _GOOGLE_REFRESH
                + '"}'
            ),
            "DB_PASSWORD_SECRET_NAME": "teamagent/dev/db_password",
            "SLACK_BOT_TOKEN_SECRET_NAME": "teamagent/dev/slack/bot_token",
            "SLACK_APP_TOKEN_SECRET_NAME": "teamagent/dev/slack/app_token",
            "RDS_HOST": "db.internal",
        }
    )
    return env, log_path


def _mail_migration_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    env, log_path = _base_environment(tmp_path)
    env.update(
        {
            "MAIL_ACTION_HMAC_SECRET_NAME": "teamagent/dev/hmac/mail-action",
            "MAIL_ACTION_HMAC_PRIMARY_VERSION_ID": _PRIMARY_VERSION,
            "MAIL_ACTION_HMAC_PRIMARY_GENERATION": (
                "arn:aws:secretsmanager:ap-northeast-1:123456789012:"
                f"secret:teamagent/dev/hmac/mail-action-mail00@{_PRIMARY_VERSION}"
            ),
            "MAIL_ACTION_HMAC_PREVIOUS_SECRET_NAME": "teamagent/dev/database-url",
            "MAIL_ACTION_HMAC_PREVIOUS_VERSION_ID": _PREVIOUS_VERSION,
            "MAIL_ACTION_HMAC_PREVIOUS_GENERATION": (
                "arn:aws:secretsmanager:ap-northeast-1:123456789012:"
                f"secret:teamagent/dev/database-url-legacy@{_PREVIOUS_VERSION}"
            ),
            "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT": "2000000000",
            "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY": "1",
        }
    )
    return env, log_path


def _run_loader(
    env: dict[str, str],
    assertion: str = ":",
    *,
    domains: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f"source scripts/load_secrets.sh {domains} || exit $?; {assertion}",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_loader_fetches_exact_versions_and_never_logs_hmac_values(tmp_path: Path) -> None:
    env, log_path = _mail_migration_environment(tmp_path)
    result = _run_loader(
        env,
        (
            '[[ "$MAIL_ACTION_HMAC_SECRET" == "$FAKE_MAIL_PRIMARY" ]] && '
            '[[ "$MAIL_ACTION_HMAC_PREVIOUS_SECRET" == "$FAKE_LEGACY_DATABASE_URL" ]] && '
            '[[ "$MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY" == "1" ]] && '
            '[[ -z "${REPORT_LINK_HMAC_SECRET+x}" ]]'
        ),
        domains="MAIL_ACTION",
    )

    assert result.returncode == 0, result.stderr
    calls = log_path.read_text(encoding="utf-8")
    assert f"teamagent/dev/hmac/mail-action|{_PRIMARY_VERSION}" in calls
    assert f"teamagent/dev/database-url|{_PREVIOUS_VERSION}" in calls
    assert _MAIL_PRIMARY not in result.stdout + result.stderr
    assert _LEGACY_DATABASE_URL not in result.stdout + result.stderr
    assert _SLACK_BOT not in result.stdout + result.stderr
    assert _SLACK_APP not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "0"),
        ("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", ""),
        (
            "MAIL_ACTION_HMAC_PRIMARY_GENERATION",
            "arn:aws:secretsmanager:ap-northeast-1:123456789012:"
            "secret:teamagent/dev/hmac/mail-action-mail00@" + "c" * 32,
        ),
        ("TEAMAGENT_HMAC_REQUIRED_DOMAINS", "MAIL_ACTION,UNKNOWN"),
    ],
)
def test_loader_fails_closed_on_marker_generation_or_domain_drift(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    env, _log_path = _mail_migration_environment(tmp_path)
    env[name] = value
    result = _run_loader(env, domains="MAIL_ACTION")
    assert result.returncode != 0


def test_shared_loader_skips_hmac_for_non_token_ingest_process(tmp_path: Path) -> None:
    env, log_path = _base_environment(tmp_path)
    result = _run_loader(env, '[[ -z "${MAIL_ACTION_HMAC_SECRET+x}" ]]')

    assert result.returncode == 0, result.stderr
    calls = log_path.read_text(encoding="utf-8")
    assert "hmac/" not in calls
    assert "database-url" not in calls


@pytest.mark.parametrize(
    "name",
    [
        "TEAMAGENT_HMAC_REQUIRED_DOMAINS",
        "MAIL_ACTION_HMAC_SECRET",
        "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
        "REPORT_LINK_HMAC_SECRET",
        "REPORT_LINK_HMAC_PREVIOUS_SECRET",
    ],
)
def test_loader_rejects_and_unsets_inherited_hmac_runtime_values(
    tmp_path: Path,
    name: str,
) -> None:
    env, _log_path = _mail_migration_environment(tmp_path)
    env[name] = "inherited-runtime-value"
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "source scripts/load_secrets.sh MAIL_ACTION; rc=$?; "
                f'[[ "$rc" -ne 0 && -z "${{{name}+x}}" ]]'
            ),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "inherited-runtime-value" not in result.stdout + result.stderr


def test_loader_logs_only_boolean_slack_and_google_status(tmp_path: Path) -> None:
    env, _log_path = _base_environment(tmp_path)
    env["GOOGLE_OAUTH_SECRET_NAME"] = "teamagent/dev/google_oauth"
    result = _run_loader(env)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "bot=true" in combined
    assert "configured=true" in combined
    for secret_fragment in (
        _SLACK_BOT,
        _SLACK_APP,
        _SLACK_BOT[:8],
        _SLACK_APP[:8],
        _GOOGLE_CLIENT[:20],
        _GOOGLE_REFRESH,
        _GOOGLE_REFRESH[:8],
    ):
        assert secret_fragment not in combined
