#!/usr/bin/env python3
"""Report whether confirmation-button tokens can be issued, and why not.

Morning-digest draft buttons, the digest acknowledgement button, and calendar-registration buttons
all refuse to render unless the mail-action HMAC keyring loads. Every loader in
``teamagent.hmac_keyring`` fails closed by returning ``None``, so a misconfigured key is
indistinguishable at runtime from a deliberately disabled feature. This command closes that gap.

It prints one JSON object and nothing else. No key material, environment variable name, or
environment value is emitted -- only coarse reason codes -- so it is safe to run as a one-off ECS
task against production and to paste the output into a ticket.

Exit codes:
  0  buttons can be issued
  2  buttons cannot be issued (see ``mail_action.primary`` for the reason)

Usage (production, read-only, no deploy):
  aws ecs run-task --cluster teamagent-dev --task-definition teamagent-dev-mcp ... \
    --overrides '{"containerOverrides":[{"name":"mcp","command":[
      "python","scripts/check_mail_action_button_key.py"]}]}'
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_APPLICATION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_APPLICATION_ROOT / "src"))

from teamagent.hmac_keyring import (  # noqa: E402
    PRIMARY_OK,
    diagnose_mail_action_primary,
    diagnose_report_link_primary,
    load_mail_action_hmac_keyring,
    load_mail_action_token_ttl_s,
    load_report_link_hmac_keyring,
)


def _describe_mail_action() -> dict[str, object]:
    primary = diagnose_mail_action_primary()
    try:
        keyring_loaded = load_mail_action_hmac_keyring() is not None
    except Exception:
        keyring_loaded = False
    try:
        ttl_configured = load_mail_action_token_ttl_s() is not None
    except Exception:
        ttl_configured = False
    return {
        "primary": primary,
        "keyring_loaded": keyring_loaded,
        "ttl_configured": ttl_configured,
        # A primary that passes while the keyring still refuses points at the rotation metadata or
        # the durable-state contract rather than at the key itself.
        "blocked_after_primary": primary == PRIMARY_OK and not keyring_loaded,
    }


def _describe_report_link() -> dict[str, object]:
    try:
        keyring_loaded = load_report_link_hmac_keyring() is not None
    except Exception:
        keyring_loaded = False
    return {
        "primary": diagnose_report_link_primary(),
        "keyring_loaded": keyring_loaded,
    }


def main() -> int:
    mail_action = _describe_mail_action()
    can_issue = bool(mail_action["keyring_loaded"] and mail_action["ttl_configured"])
    result = {
        "can_issue_confirmation_buttons": can_issue,
        "mail_action": mail_action,
        "report_link": _describe_report_link(),
    }
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0 if can_issue else 2


if __name__ == "__main__":
    raise SystemExit(main())
