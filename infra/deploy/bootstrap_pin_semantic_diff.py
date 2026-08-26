#!/usr/bin/env python3
"""Bootstrap Pin 専用の semantic no-op 判定。

通常の HMAC rollout 制約（worker_verified / canary anchor / morning DISABLED /
rollback_image != candidate）は **この経路へ流用しない**。代わりに
「差分は VersionId の固定だけ」という一点を security boundary にする。

許可する差分は 1 種類だけ:

    valueFrom:  <SECRET_ARN>  →  <SECRET_ARN>:::<現在の AWSCURRENT VersionId>

ARN 部分は byte 一致でなければならず、VersionId は呼出側が live の
Secrets Manager から解決した AWSCURRENT でなければならない。
それ以外の差分が **1 byte でも** あれば RED。

image digest / container definitions / CPU / memory / network / consumer topology /
non-HMAC env / secret material / secret ARN / service・rule target がすべて
一致していることを、除外リストではなく **全体の構造比較**で確かめる
（新しいフィールドが将来増えても既定で検査対象に入る = fail-closed）。
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "BootstrapPinError",
    "PIN_SEPARATOR",
    "assert_semantic_no_op_pin",
    "split_pinned_reference",
]

PIN_SEPARATOR = ":::"


class BootstrapPinError(Exception):
    """Bootstrap Pin の semantic no-op 契約に反したときに送出する。"""

    def __init__(self, code: str, *, scope: str = "") -> None:
        super().__init__(code if not scope else f"{code} ({scope})")
        self.code = code
        self.scope = scope


def split_pinned_reference(reference: str) -> tuple[str, str | None]:
    """`ARN:::VersionId` を (ARN, VersionId) に割る。pin されていなければ (ARN, None)。"""
    if type(reference) is not str:
        raise BootstrapPinError("reference_not_a_string")
    resource, separator, version = reference.rpartition(PIN_SEPARATOR)
    if not separator:
        return reference, None
    if not resource or not version:
        raise BootstrapPinError("reference_malformed")
    return resource, version


def _secrets_by_name(container: dict[str, Any], *, scope: str) -> dict[str, str]:
    entries = container.get("secrets")
    if entries is None:
        return {}
    if type(entries) is not list:
        raise BootstrapPinError("secrets_not_a_list", scope=scope)
    mapping: dict[str, str] = {}
    for entry in entries:
        if type(entry) is not dict or set(entry) != {"name", "valueFrom"}:
            raise BootstrapPinError("secret_entry_shape_invalid", scope=scope)
        name = entry["name"]
        if type(name) is not str or name in mapping:
            raise BootstrapPinError("secret_entry_name_invalid", scope=scope)
        value = entry["valueFrom"]
        if type(value) is not str:
            raise BootstrapPinError("secret_entry_value_invalid", scope=scope)
        mapping[name] = value
    return mapping


def _without_secret_values(definition: Any) -> Any:
    """secrets の valueFrom だけを取り除いた構造を返す（それ以外は素通し）。

    除外は valueFrom の **値** のみ。name も順序も他フィールドも残すので、
    「HMAC 以外が動いていない」ことは丸ごとの構造比較で担保される。
    """
    if type(definition) is list:
        return [_without_secret_values(item) for item in definition]
    if type(definition) is not dict:
        return definition
    result: dict[str, Any] = {}
    for key, value in definition.items():
        if key == "secrets" and type(value) is list:
            result[key] = [
                {k: (None if k == "valueFrom" else v) for k, v in entry.items()}
                if type(entry) is dict
                else entry
                for entry in value
            ]
        else:
            result[key] = _without_secret_values(value)
    return result


def _canonical(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def assert_semantic_no_op_pin(
    *,
    live: dict[str, Any],
    candidate: dict[str, Any],
    resolved_versions: dict[str, str],
    pinnable_secret_names: frozenset[str],
    scope: str = "",
) -> dict[str, str]:
    """live → candidate の差分が「VersionId の固定だけ」であることを証明する。

    live / candidate は同一 workload の container definition（dict）。
    resolved_versions は **呼出側が live の Secrets Manager から解決した**
    `secret ARN -> AWSCURRENT VersionId`。
    pinnable_secret_names は pin してよい環境変数名（HMAC の secret 名）。

    成功すると {secret 名: 適用した VersionId} を返す。差分が 1 つでも
    許可範囲外なら BootstrapPinError を送出する。
    """
    if type(live) is not dict or type(candidate) is not dict:
        raise BootstrapPinError("definition_not_a_mapping", scope=scope)

    # 1) secrets の valueFrom 以外は完全一致でなければならない。
    #    除外リスト方式ではなく「valueFrom だけ抜いた全体」を比べるので、
    #    未知フィールドが増えても既定で検査対象に入る。
    if _canonical(_without_secret_values(live)) != _canonical(_without_secret_values(candidate)):
        raise BootstrapPinError("non_pin_difference_detected", scope=scope)

    live_secrets = _secrets_by_name(live, scope=scope)
    candidate_secrets = _secrets_by_name(candidate, scope=scope)

    # 2) secret の集合そのものが変わってはいけない（追加も削除も RED）。
    if set(live_secrets) != set(candidate_secrets):
        raise BootstrapPinError("secret_set_changed", scope=scope)

    applied: dict[str, str] = {}
    for name, live_value in live_secrets.items():
        candidate_value = candidate_secrets[name]
        if candidate_value == live_value:
            continue

        # 3) 変化してよいのは pin 対象の HMAC secret だけ。
        if name not in pinnable_secret_names:
            raise BootstrapPinError("non_hmac_secret_changed", scope=f"{scope}:{name}")

        live_arn, live_pin = split_pinned_reference(live_value)
        candidate_arn, candidate_pin = split_pinned_reference(candidate_value)

        # 4) live 側は未 pin であること（既に pin 済みなら「固定」ではない）。
        if live_pin is not None:
            raise BootstrapPinError("live_already_pinned", scope=f"{scope}:{name}")
        # 5) candidate は pin 済みであること。
        if candidate_pin is None:
            raise BootstrapPinError("candidate_not_pinned", scope=f"{scope}:{name}")
        # 6) ARN は byte 一致。secret そのものの差し替えは許さない。
        if candidate_arn != live_arn:
            raise BootstrapPinError("secret_arn_changed", scope=f"{scope}:{name}")
        # 7) VersionId は live から解決した AWSCURRENT でなければならない。
        expected = resolved_versions.get(live_arn)
        if not expected:
            raise BootstrapPinError("version_not_resolved", scope=f"{scope}:{name}")
        if candidate_pin != expected:
            raise BootstrapPinError("version_not_awscurrent", scope=f"{scope}:{name}")

        applied[name] = candidate_pin

    # 8) 何も pin していない candidate は Bootstrap Pin ではない。
    if not applied:
        raise BootstrapPinError("no_pin_applied", scope=scope)

    return applied
