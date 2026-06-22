"""LLM 出力から JSON を頑健に取り出すユーティリティ。

max_tokens 打ち切りで配列/オブジェクトの末尾が壊れても、完結している部分だけを
救済して返す。morning_digest の triage / ingest の自動分類など、Bedrock の JSON 応答を
パースする箇所で共有する（脆い単一 ``json.loads`` の置き換え）。

注意: フラットな JSON オブジェクト（ネストした ``{}`` を含まない）を想定する。
分類・要約のような 1 階層オブジェクトが対象。
"""

from __future__ import annotations

import json
import re
from typing import Any

# ネストを含まない完結した {...} を 1 個ずつ拾う（救済フォールバック用）。
_FLAT_OBJ_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def salvage_json_array(text: str) -> list[dict[str, Any]]:
    """テキストから JSON オブジェクト配列を頑健に取り出す。

    1. 最初の ``[ ... ]`` を ``json.loads``。成功すれば dict 要素のみ返す。
    2. 失敗（打ち切り等で配列が壊れている）時は、完結している ``{...}`` だけを
       個別に拾って返す（末尾の不完全オブジェクトは捨てる）。

    どちらも取れなければ空リスト。
    """
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except json.JSONDecodeError:
            pass  # 救済へフォールバック
    out: list[dict[str, Any]] = []
    for om in _FLAT_OBJ_RE.finditer(text):
        try:
            obj = json.loads(om.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def salvage_json_object(text: str) -> dict[str, Any] | None:
    """テキストから JSON オブジェクトを 1 個頑健に取り出す（取れなければ None）。

    1. 最初の ``{ ... }`` を ``json.loads``。成功すれば返す。
    2. 失敗時は完結した ``{...}`` のうち最初の 1 個を返す。
    """
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass  # 救済へフォールバック
    for om in _FLAT_OBJ_RE.finditer(text):
        try:
            obj = json.loads(om.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None
