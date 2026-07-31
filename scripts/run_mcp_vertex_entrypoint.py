#!/usr/bin/env python3
"""Prepare optional Vertex ADC credentials and exec the MCP server.

The core image intentionally has no shell.  ECS therefore invokes this module
with the image's pinned interpreter instead of composing ``sh`` with a Python
ENTRYPOINT.  Secret bytes are written only to the task-scoped ``/tmp`` volume,
with owner-only permissions, and are removed when the task volume is destroyed.
"""

from __future__ import annotations

import os
from pathlib import Path

_PYTHON = "/app/.venv/bin/python"
_SERVER = "/app/scripts/run_mcp_http_server.py"
_ADC_PATH = Path("/tmp/teamagent/state/vertex_sa.json")  # nosec B108


def main() -> None:
    environment = dict(os.environ)
    vertex_json = environment.pop("VERTEX_SA_JSON", "")
    if vertex_json:
        _ADC_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(_ADC_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, vertex_json.encode("utf-8"))
        finally:
            os.close(descriptor)
        environment["GOOGLE_APPLICATION_CREDENTIALS"] = str(_ADC_PATH)
    proposal_builder_enabled = all(
        environment.get(name, "false").strip().lower() in ("1", "true", "yes")
        for name in (
            "USE_PROPOSAL_BUILDER_TOOLS",
            "USE_PROPOSAL_BUILDER_SYNC_RUNTIME_VERIFIED",
        )
    )
    if proposal_builder_enabled:
        from teamagent.adapters.proposal_assets import provision_proposal_builder_assets

        assets = provision_proposal_builder_assets(environment)
        environment["PROPOSAL_BUILDER_TEMPLATE_PATH"] = str(assets.template_path)
        environment["PROPOSAL_BUILDER_ACCOUNT_DB_PATH"] = str(assets.account_db_path)
        for name in tuple(environment):
            if name.startswith(
                (
                    "PROPOSAL_BUILDER_TEMPLATE_S3_",
                    "PROPOSAL_BUILDER_ACCOUNT_S3_",
                )
            ) or name == "PROPOSAL_BUILDER_ASSETS_KMS_KEY_ARN":
                environment.pop(name, None)
    os.execve(_PYTHON, [_PYTHON, _SERVER], environment)


if __name__ == "__main__":
    main()
