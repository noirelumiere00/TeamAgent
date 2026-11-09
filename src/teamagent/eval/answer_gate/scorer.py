"""Deterministic scoring of captured visible answers; never calls a model."""

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlsplit

from teamagent.skills._shared.slack_mrkdwn import _OUTBOUND_RULES

URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>|)\]\"]+")
ASK = re.compile(
    r"[?？]|教えて(?:ください|いただ)|指定して|どの(?:会社|ブランド|業種)|"
    r"もう少し|見つかりません|見つから|ありません|該当なし|確認できません"
)
INTERNAL = re.compile(
    r"teamagent__|(?<![a-z0-9_])(?:"
    + "|".join(
        map(
            re.escape,
            [
                *_OUTBOUND_RULES["tool_names"],
                *_OUTBOUND_RULES["internal_terms"],
                "direct_search_answer",
            ],
        )
    )
    + r")(?![a-z0-9_])|\[S:|chunk_id|(?:gdrive|slack|s3)://|\b(?:ref-|item-)[\w-]+",
    re.IGNORECASE,
)


def visible_items(answer, refs):
    """Parse existing one-line lists and multiline cards, binding title to its own URL.

    Known list/card formats only; add a parser when a new skill format ships.
    """
    items = []
    # A new titled line starts an item; a separate links footer cannot repair a lost item.
    answer = re.split(r"(?m)^(?:📁|🏆|💬|📎|探した範囲)", answer, maxsplit=1)[0]
    lines = answer.splitlines()
    for i, line in enumerate(lines):
        matches = [r for r in refs if r["title"] in line]
        if not matches or line.lstrip().startswith(("📎", "# 出典")):
            continue
        end = i + 1
        while (
            end < len(lines)
            and lines[end].strip()
            and not any(r["title"] in lines[end] for r in refs)
        ):
            end += 1
        block = "\n".join(lines[i:end])
        for ref in matches:
            items.append((ref, ref["url"] in URL.findall(block)))
    return items


def score(case, fixture, record):
    """Return every requested check, plus mandatory completion/safety checks with reasons."""
    answer = record.get("answer", "")
    refs = [r for r in fixture["refs"] if r["ref"] in case["eligible_refs"]]
    items = visible_items(answer, refs)
    keys = [r[case["item_key"]] for r, linked in items if linked]
    target = (
        min(case["requested_count"], len({r[case["item_key"]] for r in refs}))
        if case.get("requested_count") is not None
        else None
    )
    checks = {}

    def result(name, ok, reason):
        checks[name] = {"pass": bool(ok), "reason": reason}

    for check in case["checks"]:
        if check == "count":
            available = len({r[case["item_key"]] for r in refs})
            shortage = available < case["requested_count"]
            stated = bool(
                re.search(
                    rf"(?:見つかったのは|該当するのは|確認できたのは)\s*{available}\s*件(?:です|のみ|しか)",
                    unicodedata.normalize("NFKC", answer),
                )
            )
            ok = len(keys) == len(set(keys)) == target and (not shortage or stated)
            result(
                check,
                ok,
                f"expected={target}, displayed={len(keys)}, unique={len(set(keys))}, "
                f"shortage_stated={stated}",
            )
        elif check == "links":
            urls = URL.findall(answer)
            allowed = {r["url"] for r in refs}
            safe = all(
                url in allowed
                and urlsplit(url).scheme == "https"
                and not urlsplit(url).query
                and not re.search(r"…|\.\.\.", url)
                for url in urls
            )
            ok = safe and all(linked for _, linked in items) and len(items) == target
            result(check, ok, f"items={len(items)}, expected={target}, allowed_urls={safe}")
        elif check == "no_ask":
            match = ASK.search(answer) if refs and not case.get("allow_ask", False) else None
            result(check, match is None, f"forbidden_phrase={match[0] if match else None}")
        elif check == "no_internal_terms":
            visible = answer
            for ref in refs:
                visible = visible.replace(ref["url"], "")
            match = INTERNAL.search(visible)
            result(check, match is None, f"internal_term={match[0] if match else None}")
        elif check == "length":
            result(
                check,
                0 < len(answer) <= case["max_chars"],
                f"chars={len(answer)}, limit={case['max_chars']}",
            )
        elif check == "numeric":
            values = [Decimal(v) for v in case["operands"]]
            expected = (
                (values[0] * values[1] + values[2]).quantize(
                    Decimal(case["rounding"]), rounding=ROUND_HALF_UP
                )
                if case["operation"] == "multiply_add"
                else None
            )
            matches = re.findall(
                re.escape(case["result_label"])
                + r"\s*[:：]\s*([\d,]+(?:\.\d+)?)\s*"
                + re.escape(case["unit"]),
                unicodedata.normalize("NFKC", answer),
            )
            actual = Decimal(matches[0].replace(",", "")) if len(matches) == 1 else None
            result(
                check,
                expected == Decimal(case["expected"]) == actual,
                f"expected={expected}, actual={actual}",
            )
        elif check == "calendar":
            missing = [s for s in case["expected_text"] if s not in answer]
            result(check, not missing, f"missing={missing}")
        elif check == "headings":
            sections = re.findall(r"^##\s+([^\n]+)\n([^#]+)", answer, re.MULTILINE)
            result(
                check,
                [h.strip() for h, _ in sections] == case["headings"]
                and all(b.strip() for _, b in sections),
                f"headings={[h for h, _ in sections]}",
            )
        else:
            result(check, False, f"unknown check: {check}")
    calls = record.get("tool_calls", [])
    tools = [c["name"] for c in calls]
    expected_tools = case["expected_tools"]
    lanes = record.get("lanes", [])
    successful = {lane["name"] for lane in lanes if lane["status"] == "ok"}
    result(
        "tools",
        tools == expected_tools and not set(tools).intersection(case["forbid"]),
        f"expected={expected_tools}, actual={tools}",
    )
    result(
        "lanes",
        set(case["required_lanes"]) <= successful and all(lane["status"] == "ok" for lane in lanes),
        f"required={case['required_lanes']}, successful={sorted(successful)}",
    )
    effects = record.get("side_effects")
    result(
        "safety",
        effects == [] and record.get("external_sent") is False,
        f"side_effects={effects}, external_sent={record.get('external_sent')}",
    )
    result(
        "completion",
        record.get("completed") is True
        and not record.get("error")
        and 0 <= record.get("elapsed_s", float("inf")) <= case["time_limit_s"],
        f"completed={record.get('completed')}, error={record.get('error')}, "
        f"elapsed_s={record.get('elapsed_s')}",
    )
    return {
        "id": case["id"],
        "critical": case["critical"],
        "pass": all(c["pass"] for c in checks.values()),
        "checks": checks,
    }
