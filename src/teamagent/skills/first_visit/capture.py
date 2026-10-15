"""Materialize a private saved acquisition without advancing an incomplete case."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from teamagent.skills.first_visit.contract import (
    convert_inputs,
    missing_inputs,
    seal_inputs,
    write_inputs,
)
from teamagent.skills.omiyage_report.metrics import AxisData
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput


def materialize_capture(
    capture_ref: Mapping[str, Any],
    root: Path,
    *,
    reader: Callable[[Mapping[str, Any]], bytes],
    kit_path: Path,
    kit_python: Path | None = None,
    case_details: Mapping[str, Any] | None = None,
    selection: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Verify saved bytes, create new case/raw files and report the unit-1 gate.

    ``reader`` must fetch exactly the bucket/key/version supplied. Unversioned
    capture objects use the storage writer's content-addressed, create-only key;
    SHA-256 and size are checked here in either case. Media artifact references
    remain references: no download or invented transcription is performed.
    """
    ref = dict(capture_ref)
    if (
        any(
            not isinstance(ref.get(k), str) or not ref[k]
            for k in ("bucket", "key", "content_type")
        )
        or not isinstance(ref.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])
        or type(ref.get("size")) is not int
        or ref["size"] < 0
        or (
            ref.get("version_id") is not None
            and (not isinstance(ref["version_id"], str) or ref["version_id"] in {"", "null"})
        )
    ):
        raise ValueError("capture_ref: S3参照・SHA-256・サイズが不正です")
    data = reader(ref)
    if len(data) != ref["size"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
        raise ValueError("capture_ref: 保存データのSHA-256・サイズが一致しません")
    saved = json.loads(data)
    if (
        not isinstance(saved, dict)
        or type(saved.get("schema_version")) is not int
        or saved["schema_version"] != 1
    ):
        raise ValueError("capture: 未対応のschema_versionです")
    request = OmiyageReportSubmitInput.model_validate(saved.get("request"))
    captured_axes = saved.get("axes")
    if not isinstance(captured_axes, list) or any(not isinstance(a, dict) for a in captured_axes):
        raise ValueError("capture: axesはオブジェクト配列が必要です")
    axis_ids = [a.get("axis_id") for a in captured_axes]
    if (
        any(not isinstance(a, str) or not re.fullmatch(r"axis_\d{2,}", a) for a in axis_ids)
        or len(set(axis_ids)) != len(axis_ids)
    ):
        raise ValueError("capture: 軸IDが不正または重複しています")
    for axis in captured_axes:
        if (
            axis.get("role") not in {"general", "brand", "competitor"}
            or any(
                not isinstance(axis.get(k), str) or not axis[k] for k in ("label", "query")
            )
            or type(axis.get("requested")) is not int
            or axis["requested"] < 1
        ):
            raise ValueError("capture: 軸のrole・label・query・requestedが不正です")
    base_axes = [
        AxisData(
            role=a["role"], label=a["label"], query=a["query"], requested=a["requested"], posts=()
        )
        for a in captured_axes
    ]
    # Resolve display labels with the same unit-1 rules before a fallback query changes.
    cfg = convert_inputs(request, base_axes, {"posts": []}, acquisition={})["case.json"]
    names = {item["file"]: item["name"] for item in [*cfg["brands"], *cfg["keywords"]]}
    axes: list[AxisData] = []
    acquisition: dict[str, dict[str, Any]] = {}
    axis_posts: dict[str, Any] = {}
    axis_shortfalls: dict[str, Any] = {}
    manifest_axes = []
    for index, (axis, captured) in enumerate(zip(base_axes, captured_axes, strict=True), 1):
        attempts = captured.get("attempts")
        if not isinstance(attempts, list) or any(not isinstance(a, dict) for a in attempts):
            raise ValueError("capture: attemptsはオブジェクト配列が必要です")
        for attempt in attempts:
            for field in ("posts", "shortfalls"):
                rows = attempt.get(field, [])
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError(f"capture: {field}はオブジェクト配列が必要です")
        selected = attempts[-1] if attempts else {}
        status = selected.get("status", "unknown")
        if status not in {"ok", "observed_zero", "failed", "unknown"}:
            raise ValueError("capture: 取得状態が不正です")
        query = selected.get("query", axis.query)
        if not isinstance(query, str) or not query:
            raise ValueError("capture: 実検索語が必要です")
        axis_id = captured["axis_id"]
        label = names[f"raw/axis_{index:02d}.json"]
        axes.append(
            AxisData(
                axis.role,
                label,
                query,
                axis.requested,
                (),
                failed=status == "failed",
                failure_code=selected.get("error_code") or "",
            )
        )
        meta = {k: v for k, v in selected.items() if k not in {"posts", "raw_response"}}
        meta.update(
            status=status,
            axis_id=axis_id,
            capture_ref=ref,
            label=axis.label,
            attempt_index=len(attempts) - 1 if attempts else None,
        )
        acquisition[f"{axis.role}:{label}"] = meta
        axis_posts[axis_id] = selected.get("posts", [])
        axis_shortfalls[axis_id] = selected.get("shortfalls", [])
        manifest_axes.append(
            {
                **{k: captured[k] for k in ("axis_id", "role", "label", "query", "requested")},
                "selected_attempt": meta["attempt_index"],
                "attempts": [
                    {
                        **{k: v for k, v in attempt.items() if k not in {"posts", "raw_response"}},
                        "post_count": len(attempt.get("posts", [])),
                    }
                    for attempt in attempts
                ],
            }
        )
    inputs = convert_inputs(
        request,
        axes,
        {
            "posts": [row for rows in axis_posts.values() for row in rows],
            "shortfalls": [row for rows in axis_shortfalls.values() for row in rows],
            "axis_posts": axis_posts,
            "axis_shortfalls": axis_shortfalls,
        },
        acquisition=acquisition,
        case_details=case_details,
        selection=selection,
    )
    capture_manifest = {
        "schema_version": 1,
        "capture_ref": ref,
        "capture_id": saved.get("capture_id"),
        "owner_user_id": saved.get("owner_user_id"),
        "request_id": saved.get("request_id"),
        "omiyage_job_id": saved.get("omiyage_job_id"),
        "axes": manifest_axes,
    }
    write_inputs(root, {**inputs, "capture-manifest.json": capture_manifest})
    problems = missing_inputs(root, kit_path=kit_path, kit_python=kit_python)
    return {
        "capture_manifest": capture_manifest,
        "missing_inputs": problems,
        "manifest": (
            None if problems else seal_inputs(root, kit_path=kit_path, kit_python=kit_python)
        ),
    }
