"""Executable HMAC rollout preflight tests (secret-free fixtures only)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts.preflight_hmac_rotation import (
    main,
    validate_manifest,
    validate_rendered_tasks,
    validate_worker_env,
)

_NOW = 2_000_000_000
_T0 = _NOW
_LEGACY = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:"
    "secret:teamagent/dev/database-url-legacy@" + "a" * 32
)
_MAIL = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:"
    "secret:teamagent/dev/hmac/mail-action-mail00@" + "b" * 32
)
_REPORT = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:"
    "secret:teamagent/dev/hmac/report-link-report@" + "c" * 32
)
_WRONG = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:"
    "secret:teamagent/dev/hmac/wrong-wrong0@" + "d" * 32
)


def _config(
    primary: str,
    previous: str | None,
    t0: int | None,
) -> dict[str, object]:
    return {
        "primary_generation": primary,
        "previous_generation": previous,
        "rotation_started_at": t0,
    }


def _manifest() -> dict[str, object]:
    mail = _config(_MAIL, _LEGACY, _T0)
    report = _config(_REPORT, _LEGACY, _T0)
    return {
        "now": _NOW,
        "legacy_database_generation": _LEGACY,
        "domains": {
            "mail_action": {
                "deployed": _config(_LEGACY, None, None),
                "proposed": mail,
            },
            "report_link": {
                "deployed": _config(_LEGACY, None, None),
                "proposed": report,
            },
        },
        "tasks": {
            "mcp": {
                "mail_action": copy.deepcopy(mail),
                "report_link": copy.deepcopy(report),
            },
            "morning_digest": {"mail_action": copy.deepcopy(mail)},
            "connect_web": {"report_link": copy.deepcopy(report)},
            "worker": {
                "mail_action": copy.deepcopy(mail),
                "report_link": copy.deepcopy(report),
            },
        },
    }


def _reference(generation: str) -> str:
    resource, separator, version_id = generation.rpartition("@")
    assert separator
    return f"{resource}:::{version_id}"


def _rendered_task(*domains: tuple[str, dict[str, object]]) -> dict[str, object]:
    environment: list[dict[str, str]] = []
    secrets: list[dict[str, str]] = []
    prefixes = {"mail_action": "MAIL_ACTION", "report_link": "REPORT_LINK"}
    ttls = {"mail_action": "86400", "report_link": "604800"}
    for domain, config in domains:
        prefix = prefixes[domain]
        primary = str(config["primary_generation"])
        environment.extend(
            [
                {"name": f"{prefix}_HMAC_PRIMARY_GENERATION", "value": primary},
                {"name": f"{prefix}_TTL_S", "value": ttls[domain]},
            ]
        )
        secrets.append({"name": f"{prefix}_HMAC_SECRET", "valueFrom": _reference(primary)})
        previous = config["previous_generation"]
        if previous is not None:
            previous_text = str(previous)
            environment.extend(
                [
                    {
                        "name": f"{prefix}_HMAC_PREVIOUS_GENERATION",
                        "value": previous_text,
                    },
                    {
                        "name": f"{prefix}_HMAC_PREVIOUS_ROTATION_STARTED_AT",
                        "value": str(config["rotation_started_at"]),
                    },
                ]
            )
            secrets.append(
                {
                    "name": f"{prefix}_HMAC_PREVIOUS_SECRET",
                    "valueFrom": _reference(previous_text),
                }
            )
            if previous_text == _LEGACY:
                environment.append(
                    {
                        "name": f"{prefix}_HMAC_PREVIOUS_IS_LEGACY",
                        "value": "1",
                    }
                )
    return {
        "containerDefinitions": [
            {
                "environment": environment,
                "secrets": secrets,
            }
        ]
    }


def _worker_env(manifest: dict[str, object]) -> str:
    worker = manifest["tasks"]["worker"]  # type: ignore[index]
    prefixes = {"mail_action": "MAIL_ACTION", "report_link": "REPORT_LINK"}
    names = {
        "mail_action": "teamagent/dev/hmac/mail-action",
        "report_link": "teamagent/dev/hmac/report-link",
    }
    ttls = {"mail_action": "86400", "report_link": "604800"}
    lines: list[str] = []
    for domain in ("mail_action", "report_link"):
        prefix = prefixes[domain]
        config = worker[domain]  # type: ignore[index]
        primary = str(config["primary_generation"])
        _resource, _separator, primary_version = primary.rpartition("@")
        lines.extend(
            [
                f"export {prefix}_HMAC_SECRET_NAME='{names[domain]}'",
                f"export {prefix}_HMAC_PRIMARY_VERSION_ID='{primary_version}'",
                f"export {prefix}_HMAC_PRIMARY_GENERATION='{primary}'",
            ]
        )
        previous = config["previous_generation"]
        if previous is None:
            previous_name = previous_version = previous_text = t0 = marker = ""
        else:
            previous_text = str(previous)
            _resource, _separator, previous_version = previous_text.rpartition("@")
            previous_name = (
                "teamagent/dev/database-url" if previous_text == _LEGACY else names[domain]
            )
            t0 = str(config["rotation_started_at"])
            marker = "1" if previous_text == _LEGACY else ""
        lines.extend(
            [
                f"export {prefix}_HMAC_PREVIOUS_SECRET_NAME='{previous_name}'",
                f"export {prefix}_HMAC_PREVIOUS_VERSION_ID='{previous_version}'",
                f"export {prefix}_HMAC_PREVIOUS_GENERATION='{previous_text}'",
                f"export {prefix}_HMAC_PREVIOUS_ROTATION_STARTED_AT='{t0}'",
                f"export {prefix}_HMAC_PREVIOUS_IS_LEGACY='{marker}'",
                f"export {prefix}_TTL_S='{ttls[domain]}'",
            ]
        )
    return "\n".join(lines) + "\n"


def test_valid_manifest_covers_every_issuer_and_verifier_task() -> None:
    assert validate_manifest(_manifest()) == {"ok": True, "code": "ok"}


def test_direct_cutover_and_wrong_previous_fail_closed() -> None:
    direct = _manifest()
    direct["domains"]["mail_action"]["proposed"] = _config(_MAIL, None, None)  # type: ignore[index]
    assert validate_manifest(direct) == {
        "ok": False,
        "code": "primary_changed_without_previous",
        "scope": "mail_action",
    }

    wrong = _manifest()
    wrong["domains"]["mail_action"]["proposed"] = _config(_MAIL, _WRONG, _T0)  # type: ignore[index]
    assert validate_manifest(wrong) == {
        "ok": False,
        "code": "previous_generation_mismatch",
        "scope": "mail_action",
    }


def test_mid_window_swap_and_rendered_task_drift_fail_closed() -> None:
    swapped = _manifest()
    mail_domain = swapped["domains"]["mail_action"]  # type: ignore[index]
    mail_domain["deployed"] = _config(_MAIL, _LEGACY, _T0)
    mail_domain["proposed"] = _config(_MAIL, _WRONG, _T0)
    assert validate_manifest(swapped) == {
        "ok": False,
        "code": "previous_generation_changed",
        "scope": "mail_action",
    }

    drifted = _manifest()
    drifted["tasks"]["connect_web"]["report_link"] = _config(  # type: ignore[index]
        _REPORT,
        _WRONG,
        _T0,
    )
    assert validate_manifest(drifted) == {
        "ok": False,
        "code": "task_generation_drift",
        "scope": "connect_web",
    }


def test_database_generation_cannot_be_a_proposed_primary() -> None:
    manifest = _manifest()
    manifest["domains"]["report_link"]["proposed"] = _config(  # type: ignore[index]
        _LEGACY,
        None,
        None,
    )
    assert validate_manifest(manifest) == {
        "ok": False,
        "code": "legacy_primary_forbidden",
        "scope": "report_link",
    }

    another_database_version = _LEGACY.rpartition("@")[0] + "@" + "e" * 32
    manifest = _manifest()
    manifest["domains"]["report_link"]["proposed"] = _config(  # type: ignore[index]
        another_database_version,
        _LEGACY,
        _T0,
    )
    assert validate_manifest(manifest) == {
        "ok": False,
        "code": "legacy_primary_forbidden",
        "scope": "report_link",
    }


def test_mail_and_report_must_use_separate_secret_resources() -> None:
    manifest = _manifest()
    shared_resource_generation = _MAIL.rpartition("@")[0] + "@" + "f" * 32
    manifest["domains"]["report_link"]["proposed"] = _config(  # type: ignore[index]
        shared_resource_generation,
        _LEGACY,
        _T0,
    )
    assert validate_manifest(manifest) == {
        "ok": False,
        "code": "purpose_generation_reuse",
    }


def test_rendered_task_requires_version_pins_and_manifest_parity() -> None:
    manifest = _manifest()
    tasks = manifest["tasks"]  # type: ignore[assignment]
    rendered = {
        "mcp": _rendered_task(
            ("mail_action", tasks["mcp"]["mail_action"]),  # type: ignore[index]
            ("report_link", tasks["mcp"]["report_link"]),  # type: ignore[index]
        ),
        "connect_web": _rendered_task(
            ("report_link", tasks["connect_web"]["report_link"]),  # type: ignore[index]
        ),
    }
    assert validate_rendered_tasks(manifest, rendered) == {"ok": True, "code": "ok"}

    rendered["connect_web"]["containerDefinitions"][0]["secrets"][0]["valueFrom"] = (  # type: ignore[index]
        _REPORT.rpartition("@")[0]
    )
    assert validate_rendered_tasks(manifest, rendered) == {
        "ok": False,
        "code": "rendered_task_drift",
        "scope": "connect_web",
    }


def test_rendered_task_rejects_plaintext_or_unneeded_hmac_entries() -> None:
    manifest = _manifest()
    config = manifest["tasks"]["connect_web"]["report_link"]  # type: ignore[index]

    plaintext = _rendered_task(("report_link", config))
    plaintext["containerDefinitions"][0]["environment"].append(  # type: ignore[index]
        {"name": "REPORT_LINK_HMAC_PREVIOUS_SECRET", "value": "not-a-real-secret"}
    )
    assert validate_rendered_tasks(manifest, {"connect_web": plaintext}) == {
        "ok": False,
        "code": "rendered_task_drift",
        "scope": "connect_web",
    }

    extra_domain = _rendered_task(("report_link", config))
    extra_domain["containerDefinitions"][0]["secrets"].append(  # type: ignore[index]
        {
            "name": "MAIL_ACTION_HMAC_SECRET",
            "valueFrom": _reference(_LEGACY),
        }
    )
    assert validate_rendered_tasks(manifest, {"connect_web": extra_domain}) == {
        "ok": False,
        "code": "rendered_task_drift",
        "scope": "connect_web",
    }


def test_rendered_and_worker_ttl_parsing_is_bounded() -> None:
    manifest = _manifest()
    config = manifest["tasks"]["connect_web"]["report_link"]  # type: ignore[index]
    rendered = _rendered_task(("report_link", config))
    environment = rendered["containerDefinitions"][0]["environment"]  # type: ignore[index]
    next(entry for entry in environment if entry["name"] == "REPORT_LINK_TTL_S")["value"] = (
        "9" * 10_000
    )
    assert validate_rendered_tasks(manifest, {"connect_web": rendered}) == {
        "ok": False,
        "code": "rendered_task_drift",
        "scope": "connect_web",
    }

    worker_env = _worker_env(manifest).replace(
        "MAIL_ACTION_TTL_S='86400'",
        f"MAIL_ACTION_TTL_S='{'9' * 10_000}'",
    )
    assert validate_worker_env(manifest, worker_env) == {
        "ok": False,
        "code": "worker_env_drift",
        "scope": "worker",
    }


def test_rendered_task_requires_legacy_marker_only_for_database_generation() -> None:
    manifest = _manifest()
    config = manifest["tasks"]["connect_web"]["report_link"]  # type: ignore[index]
    missing = _rendered_task(("report_link", config))
    environment = missing["containerDefinitions"][0]["environment"]  # type: ignore[index]
    environment[:] = [
        entry for entry in environment if entry["name"] != "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY"
    ]
    assert validate_rendered_tasks(manifest, {"connect_web": missing}) == {
        "ok": False,
        "code": "rendered_task_drift",
        "scope": "connect_web",
    }

    dedicated = _manifest()
    report = _config(_REPORT, _WRONG, _T0)
    dedicated["domains"]["report_link"] = {  # type: ignore[index]
        "deployed": _config(_REPORT, _WRONG, _T0),
        "proposed": report,
    }
    dedicated["tasks"]["mcp"]["report_link"] = copy.deepcopy(report)  # type: ignore[index]
    dedicated["tasks"]["connect_web"]["report_link"] = copy.deepcopy(report)  # type: ignore[index]
    dedicated["tasks"]["worker"]["report_link"] = copy.deepcopy(report)  # type: ignore[index]
    rendered = _rendered_task(("report_link", report))
    rendered["containerDefinitions"][0]["environment"].append(  # type: ignore[index]
        {"name": "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY", "value": "1"}
    )
    assert validate_rendered_tasks(dedicated, {"connect_web": rendered}) == {
        "ok": False,
        "code": "rendered_task_drift",
        "scope": "connect_web",
    }


def test_worker_env_requires_exact_generations_version_pins_and_legacy_marker() -> None:
    manifest = _manifest()
    worker_env = _worker_env(manifest)
    assert validate_worker_env(manifest, worker_env) == {"ok": True, "code": "ok"}

    assert validate_worker_env(
        manifest,
        worker_env.replace(
            f"REPORT_LINK_HMAC_PRIMARY_VERSION_ID='{'c' * 32}'",
            f"REPORT_LINK_HMAC_PRIMARY_VERSION_ID='{'d' * 32}'",
        ),
    ) == {
        "ok": False,
        "code": "worker_env_drift",
        "scope": "worker",
    }
    assert validate_worker_env(
        manifest,
        worker_env.replace("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY='1'", ""),
    ) == {
        "ok": False,
        "code": "worker_env_drift",
        "scope": "worker",
    }


def test_cli_outputs_only_codes_and_never_generation_identifiers(
    tmp_path: Path,
    capsys: object,
) -> None:
    manifest_path = tmp_path / "hmac-preflight.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")

    assert main(["--manifest", str(manifest_path)]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert json.loads(output) == {"ok": True, "code": "ok"}
    assert _LEGACY not in output
    assert _MAIL not in output
    assert _REPORT not in output
