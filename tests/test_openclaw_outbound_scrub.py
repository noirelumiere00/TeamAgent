"""送信前の本文洗浄: 署名 URL・内部名・裸 URL の全角区切りを実物の Node plugin で検証する。"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNIT_PROBE = ROOT / "tests/scripts/openclaw_outbound_scrub_probe.mjs"
CALLER_PROBE = ROOT / "tests/scripts/openclaw_caller_identity_probe.mjs"
OMITTED_LINK = "（社内リンクは省略しました）"


def _run_unit_probe(texts: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        ["node", str(UNIT_PROBE)],
        cwd=ROOT,
        input=json.dumps({"texts": texts}),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return cast(dict[str, Any], json.loads(completed.stdout))


def _scrub(text: str) -> dict[str, Any]:
    return cast(dict[str, Any], _run_unit_probe([text])["results"][0])


def _counts(*, signed: int = 0, tools: int = 0, separators: int = 0) -> dict[str, int]:
    return {"signedUrls": signed, "toolNames": tools, "urlSeparators": separators}


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("parameter", ["X-Amz-Signature", "X-Amz-Credential"])
@pytest.mark.parametrize("form", ["bare", "slack", "markdown"])
def test_signed_url_is_removed_in_every_rendered_form(
    scheme: str, parameter: str, form: str
) -> None:
    url = f"{scheme}://internal.example.test/report.pptx?download=1&{parameter}=probe-secret&x=2"
    rendered = {"bare": url, "slack": f"<{url}|資料を開く>", "markdown": f"[資料を開く]({url})"}
    result = _scrub(f"資料: {rendered[form]} 続きの説明です。")
    assert result == {
        "text": f"資料: {OMITTED_LINK} 続きの説明です。",
        "counts": _counts(signed=1),
    }
    assert "probe-secret" not in result["text"] and "資料を開く" not in result["text"]


def test_multiple_signed_urls_count_matches_replacements_and_keeps_the_following_sentence() -> None:
    first = "https://internal.example.test/a?X-Amz-Signature=first"
    second = "https://internal.example.test/b?X-Amz-Credential=second"
    result = _scrub(f"（{first}）本文。[二つ目]({second}) 続き")
    assert result == {
        "text": f"（{OMITTED_LINK}）本文。{OMITTED_LINK} 続き",
        "counts": _counts(signed=2),
    }


@pytest.mark.parametrize("form", ["bare", "slack", "markdown"])
@pytest.mark.parametrize(
    "url",
    [
        "https://internal.example.test/report(v2).pptx?X-Amz-Signature=probe-secret",
        "https://internal.example.test/report.pptx?filename=(v2)&X-Amz-Credential=probe-secret",
        "https://internal.example.test/report(a(b(c))).pptx?X-Amz-Signature=probe-secret",
    ],
)
def test_signed_url_with_balanced_parentheses_is_removed_in_every_form(url: str, form: str) -> None:
    rendered = {"bare": url, "slack": f"<{url}|資料を開く>", "markdown": f"[資料を開く]({url})"}
    assert _scrub(f"資料: {rendered[form]} 続きの説明です。") == {
        "text": f"資料: {OMITTED_LINK} 続きの説明です。",
        "counts": _counts(signed=1),
    }


def test_signed_markdown_link_with_title_is_removed_with_its_label() -> None:
    text = (
        "[資料を開く](https://internal.example.test/report(v2).pptx?"
        'X-Amz-Signature=probe-secret "お土産資料") 続き'
    )
    assert _scrub(text) == {"text": f"{OMITTED_LINK} 続き", "counts": _counts(signed=1)}


@pytest.mark.parametrize(
    "url",
    [
        "https://internal.example.test/report.pptx?X-Amz-Signature=probe-secret",
        "https://internal.example.test/report(v2).pptx?X-Amz-Credential=probe-secret",
    ],
)
def test_ascii_parentheses_around_signed_bare_url_are_preserved(url: str) -> None:
    assert _scrub(f"資料 ({url}) 続き") == {
        "text": f"資料 ({OMITTED_LINK}) 続き",
        "counts": _counts(signed=1),
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://internal.example.test/o'hare.pptx?X-Amz-Signature=probe-secret",
        "https://internal.example.test/report.pptx?filename=o'hare&X-Amz-Credential=probe-secret",
    ],
)
def test_signed_bare_url_with_apostrophe_is_removed(url: str) -> None:
    assert _scrub(f"資料 {url} 続き") == {
        "text": f"資料 {OMITTED_LINK} 続き",
        "counts": _counts(signed=1),
    }


def test_single_quotes_around_signed_bare_url_are_preserved() -> None:
    text = "資料 'https://internal.example.test/report.pptx?X-Amz-Signature=probe' 続き"
    assert _scrub(text) == {
        "text": f"資料 '{OMITTED_LINK}' 続き",
        "counts": _counts(signed=1),
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("`omiyage_report_submit` を呼んで、", ""),
        ("omiyage_report_submit を呼んで、確認します。", "確認します。"),
        ("`attachment_assist` で確認・加工できます。", "確認・加工できます。"),
        ("attachment_assistで確認できます。", "確認できます。"),
        ("teamagent__future_tool を呼んで、確認します。", "確認します。"),
        ("`teamagent__future_tool` で確認します。", "確認します。"),
    ],
)
def test_tool_names_and_their_local_call_phrases_are_removed(text: str, expected: str) -> None:
    assert _scrub(text) == {"text": expected, "counts": _counts(tools=1)}


def test_only_whole_internal_names_are_removed() -> None:
    text = "pre_attachment_assist_suffix attachment_assistance myteamagent__future_tool"
    assert _scrub(text) == {"text": text, "counts": _counts()}


@pytest.mark.parametrize(
    ("text", "expected", "tools"),
    [
        ("`attachment_assist`、`omiyage_report_submit`で確認します。", "確認します。", 2),
        ("`attachment_assist` では確認できません。", "確認できません。", 1),
    ],
)
def test_tool_lists_and_compound_particles_are_removed_naturally(
    text: str, expected: str, tools: int
) -> None:
    assert _scrub(text) == {"text": expected, "counts": _counts(tools=tools)}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "<https://example.test/doc|attachment_assist>",
            "<https://example.test/doc|リンク>",
        ),
        (
            "[omiyage_report_submit](https://example.test/doc)",
            "[リンク](https://example.test/doc)",
        ),
    ],
)
def test_internal_tool_name_in_explicit_link_label_is_removed_without_breaking_link(
    text: str, expected: str
) -> None:
    assert _scrub(text) == {"text": expected, "counts": _counts(tools=1)}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "<https://example.test/doc|https://s3.example.test/report?X-Amz-Signature=probe>",
            f"<https://example.test/doc|{OMITTED_LINK}>",
        ),
        (
            "[https://s3.example.test/report?X-Amz-Credential=probe](https://example.test/doc)",
            f"[{OMITTED_LINK}](https://example.test/doc)",
        ),
        (
            '[資料](https://example.test/doc "https://s3.example.test/report?X-Amz-Signature=probe")',
            f'[資料](https://example.test/doc "{OMITTED_LINK}")',
        ),
    ],
)
def test_signed_url_in_unsigned_link_label_or_title_is_scrubbed(text: str, expected: str) -> None:
    assert _scrub(text) == {"text": expected, "counts": _counts(signed=1)}


def test_every_registered_tool_name_is_scrubbed_bare_and_in_code_spans() -> None:
    """コードスパンでは全部消す。素の文では _ を含む名前だけ消す（search 等は普通の語と同じ綴り）。"""
    names = _run_unit_probe([])["toolNames"]
    texts = [text for name in names for text in (name, f"`{name}`")]
    report = _run_unit_probe(texts)
    assert names and len(names) == len(set(names))
    for text, result in zip(texts, report["results"], strict=True):
        if text.startswith("`") or "_" in text:
            assert result == {"text": "", "counts": _counts(tools=1)}, text
        else:
            assert result == {"text": text, "counts": _counts()}, text


@pytest.mark.parametrize("symbol", list("）」』、。！？"))
def test_bare_url_is_cut_before_full_width_punctuation(symbol: str) -> None:
    url = "http://aicheck.newstv.co.jp/app"
    text = f"（{url}{symbol}への貼り付けで不明です。"
    assert _scrub(text) == {
        "text": f"（{url} {symbol}への貼り付けで不明です。",
        "counts": _counts(separators=1),
    }


def test_parentheses_inside_bare_url_are_preserved_before_full_width_punctuation() -> None:
    text = "https://example.test/path(foo)）説明です。"
    assert _scrub(text) == {
        "text": "https://example.test/path(foo) ）説明です。",
        "counts": _counts(separators=1),
    }


def test_apostrophe_inside_bare_url_is_preserved_before_full_width_punctuation() -> None:
    assert _scrub("https://example.test/o'hare）") == {
        "text": "https://example.test/o'hare ）",
        "counts": _counts(separators=1),
    }


@pytest.mark.parametrize(
    "text",
    [
        "こんにちは。資料を確認します。",
        "<@U0123456789> <https://example.test/search|検索ページ>をご覧ください。",
        "[検索ページ](https://example.test/search)をご覧ください。",
        "<https://example.test/app|資料）はこちら>。",
        "[資料）はこちら](https://example.test/app)。",
        "https://example.test/search を確認します。",
        "https://example.test/app ）の説明です。",
        "https://example.test/app%EF%BC%89 の説明です。",
        '[資料](https://example.test/path(a(b)) "title")',
        "(https://example.test/path) の説明です。",
    ],
)
def test_ordinary_text_and_explicit_links_mentions_are_preserved(text: str) -> None:
    assert _scrub(text) == {"text": text, "counts": _counts()}


def test_scrub_is_idempotent() -> None:
    text = (
        "`attachment_assist` で確認します。 "
        "<https://internal.example.test/a?X-Amz-Signature=probe|資料> "
        "（http://aicheck.newstv.co.jp/app）への貼り付け"
    )
    first = _scrub(text)
    assert first["counts"] == _counts(signed=1, tools=1, separators=1)
    assert _scrub(first["text"]) == {"text": first["text"], "counts": _counts()}


def _skill_tool_names() -> set[str]:
    """Skill を import せず ClassVar[str] の name 定義を取得する（外部接続なし）。"""
    names = set()
    for path in (ROOT / "src/teamagent/skills").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "name"
                and ast.unparse(node.annotation) == "ClassVar[str]"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                names.add(node.value.value)
    return names


def test_tool_dictionary_tracks_source_names_and_soul_fixed_dictionary() -> None:
    actual = set(_run_unit_probe([])["toolNames"])
    soul = (ROOT / "infra/openclaw/SOUL.md").read_text(encoding="utf-8")
    forbidden_line = next(line for line in soul.splitlines() if "**禁止語の固定辞書**" in line)
    tool_dictionary = forbidden_line.split("など**ツール名", 1)[0]
    fixed_names = set(re.findall(r"`([a-z]+(?:_[a-z]+)*)`", tool_dictionary))
    required = (
        _skill_tool_names()
        | fixed_names
        | {
            "run_agent",
            "answer_feedback_record",
            "personal_memory_observe",
            "personal_memory_context",
            "personal_memory_command",
        }
    )
    assert required <= actual, f"洗浄辞書へ追加が必要: {sorted(required - actual)}"


def test_reply_payload_sending_scrubs_text_only_and_logs_counts_without_body() -> None:
    """既存 probe の本番形状の event/ctx をそのまま通して結合を確かめる。"""
    completed = subprocess.run(
        ["node", str(CALLER_PROBE)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    scenario = json.loads(completed.stdout)["outbound_scrub"]
    original = scenario["input"]
    result = scenario["result"]
    assert result["payload"] == {
        **original,
        "text": (
            f"確認します。資料: {OMITTED_LINK} "
            "（http://aicheck.newstv.co.jp/app ）への貼り付け。 "
            "<https://example.test/search|検索> <@U0123456789>"
        ),
    }
    assert scenario["inputUnchanged"] is True
    assert scenario["blocksSameReference"] is True
    assert scenario["unchangedResult"] is None
    assert scenario["unchangedLogs"] == []
    # 既存 makePlugin の logger は warn のみ。info の洗浄ログは console 側で必ず捕捉する。
    assert len(scenario["console"]) == 1
    expected = "outbound scrubbed signed_urls=1 tool_names=1 url_separators=1"
    assert scenario["console"][0]["text"].endswith(expected)
    for line in scenario["logs"] + [entry["text"] for entry in scenario["console"]]:
        assert "probe-secret" not in line and "確認します" not in line and "http" not in line


@pytest.mark.parametrize(
    "text",
    [
        "Google search で調べました",
        "search結果です",
        "チャット（chitchat）の雑談",
        "おすすめ（recommend）を 3 件",
        "clientkarte という単語",
    ],
)
def test_plain_words_spelled_like_tool_names_are_kept(text: str) -> None:
    """_ を含まない名前（search 等）は、コードスパンの外では普通の語として残す（2026-10-07）。"""
    assert _scrub(text) == {"text": text, "counts": _counts()}


def test_plain_word_tool_name_in_code_span_is_still_removed() -> None:
    result = _scrub("`search` で探しました")
    assert "search" not in result["text"]
    assert result["counts"] == _counts(tools=1)
