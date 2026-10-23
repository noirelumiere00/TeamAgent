"""Optionally replay anonymous outputs through the unmodified user_eval checker."""

import json
import os
import runpy
from functools import cache
from pathlib import Path
from typing import Any


@cache
def _evaluator(root: str) -> tuple[dict[str, Any], dict[str, Any]]:
    base = Path(root)
    return runpy.run_path(str(base / "core.py")), {
        row["id"]: row for row in json.loads((base / "scenarios.json").read_text())
    }


def assert_eval_checks(
    scenario_id: str,
    texts: list[str],
    *,
    elapsed: float,
    files: list[str] | None = None,
    slack_url: str | None = None,
) -> None:
    root = os.environ.get("EVAL8_EVALUATOR")
    if not root:
        return
    core, scenarios = _evaluator(root)
    messages = [core["Message"](str(i), text, float(i)) for i, text in enumerate(texts)]
    messages[-1].attachments = [core["Attachment"](name) for name in files or []]
    if slack_url:
        messages[-1].citations = [core["Citation"](slack_url, "U_SOURCE", False)]
    outcomes = [
        core["check"](spec, messages, elapsed)
        for spec in scenarios[scenario_id]["checks"]
        if spec["type"] != "llm_judge"
    ]
    output = os.environ.get("EVAL8_CHECK_OUTPUT")
    if output:
        with Path(output).open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "id": scenario_id,
                        "checks": outcomes,
                        "texts": texts,
                        "files": files or [],
                        "slack_url": slack_url,
                        "elapsed": elapsed,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    assert all(row["passed"] for row in outcomes), outcomes
