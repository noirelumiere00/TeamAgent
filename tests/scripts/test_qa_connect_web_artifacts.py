"""Regression tests for the read-only, aggregate-only connect-web QA gate."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import build_app_html as build
from scripts import qa_connect_web_artifacts as qa


def _valid_woff2() -> bytes:
    encoded = (
        Path(qa.__file__).resolve().parents[1] / "data" / "connect_web_filters" / "inter-var.b64"
    ).read_text(encoding="ascii")
    return base64.b64decode("".join(encoded.split()), validate=True)


def _header_only_woff2() -> bytes:
    raw = bytearray(48)
    raw[:4] = b"wOF2"
    raw[4:8] = b"OTTO"
    raw[8:12] = len(raw).to_bytes(4, "big")
    raw[12:14] = (1).to_bytes(2, "big")
    raw[16:20] = (128).to_bytes(4, "big")
    raw[20:24] = (1).to_bytes(4, "big")
    return bytes(raw)


def _write_sidecars(directory: Path) -> None:
    directory.mkdir()
    (directory / "exclude_stems.json").write_text("[]", encoding="utf-8")
    (directory / "exclude_source_keys.json").write_text(
        json.dumps(["gsheets:SHEETABC:123:99"]), encoding="utf-8"
    )
    (directory / "dedup_drop_map.json").write_text(
        json.dumps({"drop": {}, "keep_canonical": []}), encoding="utf-8"
    )
    (directory / "weird_rename_high.json").write_text("{}", encoding="utf-8")
    (directory / "tag_alias.json").write_text(
        json.dumps({"_note": "test", "industry": {}, "solution": {}}),
        encoding="utf-8",
    )
    (directory / "client_alias.json").write_text(
        json.dumps({"_note": "test", "client": {}}), encoding="utf-8"
    )
    (directory / "inter-var.b64").write_text(
        base64.b64encode(_valid_woff2()).decode("ascii"), encoding="ascii"
    )


def _write_client(vault: Path) -> None:
    (vault / "clients" / "顧客会社.md").write_text(
        """---
client: "顧客会社"
industry: "IT"
deal_phase: "提案"
bant_score: "B"
fb_count: "1"
doc_count: "1"
---

# 顧客会社

## 営業FB時系列（新しい順）

### 2026-07-16 初回提案

- フェーズ: 提案
- ポジ反応: 導入意向あり

## 関連資料
- [[docs/公開資料-a1b2c3d4]]
""",
        encoding="utf-8",
    )


def _write_doc(
    vault: Path,
    stem: str,
    *,
    title: str,
    external_id: str,
    excerpt: str = "公開可能な要約",
    body: str = "公開可能な本文",
) -> None:
    excerpt_line = f"> {excerpt}\n" if excerpt else ""
    (vault / "docs" / f"{stem}.md").write_text(
        f"""---
generated_by: "scripts/export_vault.py"
title: "{title}"
client: "顧客会社"
industry: "IT"
doc_type: "提案書"
solution: "動画広告"
modified_at: "2026-07-16"
source_type: "gsheets"
external_id: "{external_id}"
---

{excerpt_line}
{body}

- 出典: [gsheets](https://docs.google.com/spreadsheets/d/SHEETABC/edit#gid=123)

[[clients/顧客会社]]
""",
        encoding="utf-8",
    )


def _write_manifest(vault: Path) -> str:
    files = {
        f"{kind}/{path.name}": hashlib.sha256(path.read_bytes()).hexdigest()
        for kind in ("clients", "docs")
        for path in (vault / kind).glob("*.md")
    }
    payload = {
        "version": 1,
        "generator": "scripts/export_vault.py",
        "complete_export": True,
        "active_files": sorted(files),
        "files": files,
    }
    path = vault / ".export-vault-manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, str]:
    vault = tmp_path / "vault"
    (vault / "clients").mkdir(parents=True)
    (vault / "docs").mkdir()
    sidecars = tmp_path / "sidecars"
    _write_sidecars(sidecars)
    _write_client(vault)
    _write_doc(
        vault,
        "公開資料-a1b2c3d4",
        title="公開資料",
        external_id="SHEETABC:123:45",
    )
    _write_doc(
        vault,
        "除外資料-e5f6a7b8",
        title="除外資料",
        external_id="SHEETABC:123:99",
        body="既知の除外対象",
    )
    manifest_sha = _write_manifest(vault)
    html = tmp_path / "app.html"
    monkeypatch.setattr(build, "SIDECAR_DIR", sidecars)
    assert build.main(["--vault", str(vault), "--out", str(html)]) == 0
    return vault, html, sidecars, manifest_sha


def _config(artifacts: tuple[Path, Path, Path, str]) -> qa.QAConfig:
    vault, html, sidecars, manifest_sha = artifacts
    return qa.QAConfig(
        vault=vault,
        html=html,
        sidecar_dir=sidecars,
        expected_manifest_sha256=manifest_sha,
    )


def _snapshot(paths: list[Path]) -> dict[Path, tuple[str, int]]:
    return {
        path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in paths
    }


def _rewrite_html_payload(html: Path, mutate: Callable[[dict[str, object]], None]) -> None:
    text = html.read_text(encoding="utf-8")
    match = re.search(r"<script>\s*const DATA=", text)
    assert match is not None
    payload, consumed = json.JSONDecoder().raw_decode(text[match.end() :])
    assert isinstance(payload, dict)
    mutate(payload)
    encoded = json.dumps(payload, ensure_ascii=False)
    encoded = (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    rewritten = text[: match.end()] + encoded + text[match.end() + consumed :]
    html.write_text(rewritten, encoding="utf-8")
    stats_path = build._stats_path(html)
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    stats["bytes"] = len(html.read_bytes())
    stats_path.write_text(json.dumps(stats), encoding="utf-8")


def _assert_aggregate_only(value: object) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _assert_aggregate_only(item)
        return
    assert isinstance(value, (bool, int, str))
    if isinstance(value, str):
        assert re_full_sha256(value)


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def test_happy_path_is_read_only_and_stdout_is_aggregate_only(
    artifacts: tuple[Path, Path, Path, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault, html, sidecars, manifest_sha = artifacts
    capsys.readouterr()  # discard build output; QA itself must emit one safe JSON line
    inputs = [
        vault / ".export-vault-manifest.json",
        *sorted((vault / "clients").glob("*.md")),
        *sorted((vault / "docs").glob("*.md")),
        *sorted(sidecars.iterdir()),
        html,
        build._stats_path(html),
    ]
    before = _snapshot(inputs)

    rc = qa.main(
        [
            "--vault",
            str(vault),
            "--html",
            str(html),
            "--sidecar-dir",
            str(sidecars),
            "--expected-manifest-sha256",
            manifest_sha,
        ]
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert "顧客会社" not in output
    assert "公開資料" not in output
    assert "SHEETABC" not in output
    assert ".md" not in output
    result = json.loads(output)
    assert result["ok"] is True
    assert result["manifest"]["unchanged"] is True
    assert result["manifest"]["sha256"] == manifest_sha
    assert result["gsheets"]["count"] == 2
    assert result["gsheets"]["junk_excluded_count"] == 1
    assert result["html"]["doc_count"] == 1
    assert result["html"]["internal_source_exposure_count"] == 0
    assert result["html"]["manifest_bound"] is True
    assert result["html"]["build_inputs_bound"] is True
    assert result["html"]["data_bound"] is True
    assert result["html"]["font_bound"] is True
    _assert_aggregate_only(result)
    assert _snapshot(inputs) == before


def test_prebuild_manifest_hash_and_active_file_hash_are_enforced(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    config = _config(artifacts)
    wrong_expected = qa.QAConfig(
        vault=config.vault,
        html=config.html,
        sidecar_dir=config.sidecar_dir,
        expected_manifest_sha256="f" * 64,
    )
    result = qa.run_qa(wrong_expected)
    assert result["ok"] is False
    assert result["violations"] == {"manifest_changed_since_snapshot": 1}

    note = config.vault / "docs" / "公開資料-a1b2c3d4.md"
    note.write_text(note.read_text(encoding="utf-8") + "\n改変", encoding="utf-8")
    result = qa.run_qa(config)
    assert result["ok"] is False
    violations = result["violations"]
    assert isinstance(violations, dict)
    assert violations["manifest_active_hash_mismatch"] == 1


def test_manifest_contract_rejects_incomplete_and_duplicate_active_entries(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    path = vault / ".export-vault-manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = 2
    payload["generator"] = "unexpected"
    payload["complete_export"] = False
    payload["active_files"].append(payload["active_files"][0])
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    violations = result["violations"]
    assert isinstance(violations, dict)
    assert violations["manifest_version_invalid"] == 1
    assert violations["manifest_generator_invalid"] == 1
    assert violations["manifest_incomplete"] == 1
    assert violations["manifest_active_duplicate"] == 1


def test_sidecar_types_duplicates_and_woff2_are_enforced(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    (sidecars / "exclude_stems.json").write_text('["x","x"]', encoding="utf-8")
    (sidecars / "dedup_drop_map.json").write_text(
        '{"drop":{},"drop":{},"keep_canonical":[]}', encoding="utf-8"
    )
    (sidecars / "weird_rename_high.json").write_text("[]", encoding="utf-8")
    (sidecars / "inter-var.b64").write_text("bm90LWEtZm9udA==", encoding="ascii")

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    violations = result["violations"]
    assert isinstance(violations, dict)
    assert violations["sidecar_duplicate_key"] == 1
    assert violations["sidecar_duplicate_value"] == 1
    assert violations["sidecar_type_invalid"] >= 1
    assert violations["font_invalid"] == 1


def test_gsheets_quality_failures_are_counts_only(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    _write_doc(
        vault,
        "不明資料-row-50-c0ffee00",
        title="資料 row 50",
        external_id="SHEETABC:123:50",
        body="固有本文A",
    )
    _write_doc(
        vault,
        "重複資料1-11111111",
        title="同じ表示",
        external_id="SHEETABC:123:51",
        excerpt="同じ要約",
        body="同じ本文",
    )
    _write_doc(
        vault,
        "重複資料2-22222222",
        title="同じ表示",
        external_id="SHEETABC:123:52",
        excerpt="同じ要約",
        body="同じ本文",
    )
    _write_doc(
        vault,
        "欠損資料-33333333",
        title="",
        external_id="",
        excerpt="",
        body="",
    )
    _write_doc(
        vault,
        "不正ID資料-44444444",
        title="通常資料",
        external_id="bad-id",
        body="固有本文B",
    )
    _write_doc(
        vault,
        "未除外junk-55555555",
        title="test",
        external_id="SHEETABC:123:55",
        body="固有本文C",
    )
    _write_manifest(vault)

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    sheets = result["gsheets"]
    assert isinstance(sheets, dict)
    assert sheets["missing_id_count"] == 1
    assert sheets["malformed_id_count"] == 1
    assert sheets["empty_title_count"] == 1
    assert sheets["empty_excerpt_count"] == 1
    assert sheets["duplicate_fingerprint_count"] == 1
    assert sheets["junk_unexcluded_count"] == 1
    assert sheets["rename_missing_count"] == 1
    serialized = json.dumps(result, ensure_ascii=False)
    assert "不明資料" not in serialized
    assert "SHEETABC" not in serialized
    assert ".md" not in serialized


def test_rename_sidecar_resolves_ambiguous_gsheets_title(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    stem = "不明資料-row-50-c0ffee00"
    _write_doc(
        vault,
        stem,
        title="資料 row 50",
        external_id="SHEETABC:123:50",
        body="固有本文",
    )
    (sidecars / "weird_rename_high.json").write_text(
        json.dumps({stem: "意味のある表示名"}, ensure_ascii=False), encoding="utf-8"
    )
    _write_manifest(vault)
    assert build.main(["--vault", str(vault), "--out", str(html)]) == 0

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    sheets = result["gsheets"]
    sidecar_result = result["sidecars"]
    assert isinstance(sheets, dict)
    assert isinstance(sidecar_result, dict)
    assert sheets["ambiguous_title_count"] == 1
    assert sheets["rename_applied_count"] == 1
    assert sheets["rename_missing_count"] == 0
    assert sidecar_result["rename_applied_count"] == 1


def test_html_data_stats_footer_bytes_and_source_exposure_are_enforced(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    original = html.read_text(encoding="utf-8")
    html.write_text(
        original.replace("・資料1", "・資料9") + "<!-- gsheets:SHEETABC:123:45 -->",
        encoding="utf-8",
    )
    stats_path = build._stats_path(html)
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    stats["docs"] = 9
    stats_path.write_text(json.dumps(stats), encoding="utf-8")

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    violations = result["violations"]
    assert isinstance(violations, dict)
    assert violations["html_internal_source_value_exposed"] >= 1
    assert violations["stats_doc_count_mismatch"] == 1
    assert violations["stats_byte_count_mismatch"] == 1
    assert violations["html_footer_doc_count_mismatch"] == 1


def test_expected_html_hash_is_enforced(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    config = _config(artifacts)
    result = qa.run_qa(
        qa.QAConfig(
            vault=config.vault,
            html=config.html,
            sidecar_dir=config.sidecar_dir,
            expected_html_sha256="e" * 64,
        )
    )
    assert result["ok"] is False
    assert result["violations"] == {"html_hash_mismatch": 1}


def test_manifest_matches_nfc_paths_to_normalized_physical_names(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    _write_doc(
        vault,
        "ガイド資料-66666666",
        title="追加資料",
        external_id="SHEETABC:123:66",
        body="固有本文D",
    )
    _write_manifest(vault)
    manifest_path = vault / ".export-vault-manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["active_files"] = [
        unicodedata.normalize("NFC", item) for item in payload["active_files"]
    ]
    payload["files"] = {
        unicodedata.normalize("NFC", key): value for key, value in payload["files"].items()
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["manifest"]["ok"] is True
    assert not any(key.startswith("manifest_") for key in cast_dict(result["violations"]))


def cast_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def test_cli_parse_error_never_echoes_untrusted_argument(
    artifacts: tuple[Path, Path, Path, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    del artifacts
    capsys.readouterr()

    assert qa.main(["--unknown", "DO-NOT-ECHO-SENSITIVE-VALUE"]) == 1

    output = capsys.readouterr().out
    assert "DO-NOT-ECHO-SENSITIVE-VALUE" not in output
    result = json.loads(output)
    assert result["violations"] == {"cli_argument_error": 1}


def test_current_manifest_is_cryptographically_bound_to_html_and_stats(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    note = vault / "docs" / "公開資料-a1b2c3d4.md"
    note.write_text(note.read_text(encoding="utf-8") + "\n新しい本文", encoding="utf-8")
    current_manifest_sha = _write_manifest(vault)

    result = qa.run_qa(
        qa.QAConfig(
            vault=vault,
            html=html,
            sidecar_dir=sidecars,
            expected_manifest_sha256=current_manifest_sha,
        )
    )

    assert result["ok"] is False
    assert result["manifest"]["ok"] is True
    assert result["html"]["manifest_bound"] is False
    violations = cast_dict(result["violations"])
    assert violations["html_manifest_hash_mismatch"] == 1
    assert violations["html_data_stats_manifest_hash_mismatch"] == 1
    assert violations["stats_manifest_hash_mismatch"] == 1


def test_data_requires_runtime_collections_and_nonempty_clients_docs(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts

    def remove_runtime_collections(payload: dict[str, object]) -> None:
        payload.pop("graph")
        payload.pop("links")
        payload.pop("colors")

    _rewrite_html_payload(html, remove_runtime_collections)
    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))
    violations = cast_dict(result["violations"])
    assert result["ok"] is False
    assert violations["html_data_required_key_missing"] == 3
    assert violations["html_graph_schema_invalid"] == 1
    assert violations["html_links_schema_invalid"] == 1
    assert violations["html_colors_schema_invalid"] == 1

    assert build.main(["--vault", str(vault), "--out", str(html)]) == 0

    def empty_primary_collections(payload: dict[str, object]) -> None:
        payload["clients"] = []
        payload["docs"] = []
        stats = payload["stats"]
        assert isinstance(stats, dict)
        stats["clients"] = 0
        stats["docs"] = 0

    _rewrite_html_payload(html, empty_primary_collections)
    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))
    violations = cast_dict(result["violations"])
    assert violations["html_clients_empty"] == 1
    assert violations["html_docs_empty"] == 1


def test_manifest_validates_every_file_entry_and_portable_collision(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    manifest = vault / ".export-vault-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["files"]["../../sensitive.md"] = "0" * 64
    payload["files"]["docs/inactive.md"] = 123
    payload["files"]["docs/Report.md"] = "a" * 64
    payload["files"]["docs/report.md"] = "b" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    violations = cast_dict(result["violations"])
    assert violations["manifest_file_path_invalid"] == 1
    assert violations["manifest_file_hash_invalid"] == 1
    assert violations["manifest_file_portable_collision"] == 1


def test_portable_rename_lookup_matches_actual_builder_payload(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    nfc_stem = "Café-row-50-77777777"
    nfd_stem = unicodedata.normalize("NFD", nfc_stem)
    _write_doc(
        vault,
        nfc_stem,
        title="資料 row 50",
        external_id="SHEETABC:123:77",
        body="固有本文E",
    )
    source = vault / "docs" / f"{nfc_stem}.md"
    physical = vault / "docs" / f"{nfd_stem}.md"
    if source.name != physical.name:
        source.rename(physical)
    (sidecars / "weird_rename_high.json").write_text(
        json.dumps({nfc_stem: "意味のある表示名"}, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_manifest(vault)
    assert build.main(["--vault", str(vault), "--out", str(html)]) == 0

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is True
    assert result["gsheets"]["rename_applied_count"] == 1
    assert result["html"]["rename_applied_count"] == 1


def test_data_and_footer_are_checked_only_at_structural_positions(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    _write_doc(
        vault,
        "構造文字列資料-88888888",
        title="const DATA= を説明する資料",
        external_id="SHEETABC:123:88",
        body="更新: 2026-07-16 JST・取引先999・資料999",
    )
    _write_manifest(vault)
    assert build.main(["--vault", str(vault), "--out", str(html)]) == 0

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))
    assert result["ok"] is True
    assert result["html"]["footer_count"] == 1

    text = html.read_text(encoding="utf-8")
    stats_path = build._stats_path(html)
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    actual_stamp = f"<span>更新: {stats['built_at'][:10]} JST・取引先1・資料2</span>"
    assert actual_stamp in text
    html.write_text(text.replace(actual_stamp, "<span>removed</span>"), encoding="utf-8")
    stats["bytes"] = len(html.read_bytes())
    stats_path.write_text(json.dumps(stats), encoding="utf-8")

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))
    violations = cast_dict(result["violations"])
    assert result["ok"] is False
    assert violations["html_status_function_invalid"] == 1
    assert violations["html_footer_invalid"] == 1


def test_qa_runs_without_importing_builder_or_leaking_traceback(
    artifacts: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    vault, html, sidecars, _ = artifacts
    isolated = tmp_path / "isolated" / "scripts"
    isolated.mkdir(parents=True)
    source = Path(qa.__file__)
    copied = isolated / source.name
    shutil.copy2(source, copied)

    completed = subprocess.run(
        [
            sys.executable,
            str(copied),
            "--vault",
            str(vault),
            "--html",
            str(html),
            "--sidecar-dir",
            str(sidecars),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result["ok"] is True
    assert "Traceback" not in completed.stdout
    assert str(vault) not in completed.stdout


def test_optional_alias_symlink_is_an_explicit_safe_failure(
    artifacts: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    vault, html, sidecars, _ = artifacts
    alias = sidecars / "tag_alias.json"
    target = tmp_path / "DO-NOT-LEAK-ALIAS-CONTENT.json"
    target.write_text('{"industry":{"secret":"value"}}', encoding="utf-8")
    alias.unlink()
    alias.symlink_to(target)

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    assert cast_dict(result["violations"])["sidecar_symlink"] == 1
    serialized = json.dumps(result)
    assert "DO-NOT-LEAK" not in serialized
    assert "secret" not in serialized


def test_concurrent_artifact_change_is_detected_by_end_snapshot(
    artifacts: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, html, sidecars, _ = artifacts
    original_read_bytes = Path.read_bytes
    changed = False

    def racing_read_bytes(path: Path) -> bytes:
        nonlocal changed
        raw = original_read_bytes(path)
        if path == html and not changed:
            path.write_bytes(raw + b" ")
            changed = True
        return raw

    monkeypatch.setattr(Path, "read_bytes", racing_read_bytes)

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert changed is True
    assert result["ok"] is False
    assert cast_dict(result["violations"])["artifact_changed_during_qa"] == 1


def test_sidecar_bundle_rejects_html_built_from_stale_aliases(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    (sidecars / "tag_alias.json").write_text(
        json.dumps(
            {"_note": "changed", "industry": {"IT": "Technology"}, "solution": {}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    assert result["html"]["build_inputs_bound"] is False
    violations = cast_dict(result["violations"])
    assert violations["html_build_inputs_hash_mismatch"] == 1
    assert violations["html_data_stats_build_inputs_hash_mismatch"] == 1
    assert violations["stats_build_inputs_hash_mismatch"] == 1


def test_font_bundle_rejects_a_stale_embedded_font_even_when_both_headers_pass(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    changed_font = bytearray(_valid_woff2())
    changed_font[24] = 1
    (sidecars / "inter-var.b64").write_text(
        base64.b64encode(changed_font).decode("ascii"),
        encoding="ascii",
    )

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    assert result["sidecars"]["font_valid"] is True
    assert result["html"]["font_bound"] is False
    violations = cast_dict(result["violations"])
    assert violations["html_font_hash_mismatch"] == 1
    assert violations["html_build_inputs_hash_mismatch"] == 1
    assert violations["html_data_stats_build_inputs_hash_mismatch"] == 1
    assert violations["stats_build_inputs_hash_mismatch"] == 1


def test_woff2_header_without_a_parseable_font_is_rejected(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    (sidecars / "inter-var.b64").write_text(
        base64.b64encode(_header_only_woff2()).decode("ascii"),
        encoding="ascii",
    )

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    assert result["sidecars"]["font_valid"] is False
    assert result["html"]["font_bound"] is False
    assert cast_dict(result["violations"])["font_invalid"] == 1


def test_raw_data_hash_and_nonempty_fields_reject_payload_tampering(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts

    def erase_required_content(payload: dict[str, object]) -> None:
        clients = payload["clients"]
        docs = payload["docs"]
        assert isinstance(clients, list) and isinstance(clients[0], dict)
        assert isinstance(docs, list) and isinstance(docs[0], dict)
        clients[0]["name"] = ""
        clients[0]["md"] = ""
        docs[0]["md"] = ""

    _rewrite_html_payload(html, erase_required_content)
    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    assert result["html"]["data_bound"] is False
    violations = cast_dict(result["violations"])
    assert violations["html_data_item_schema_invalid"] == 2
    assert violations["stats_data_hash_mismatch"] == 1


def test_client_timeline_gate_rejects_fb_count_without_payload_events(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts

    def erase_timeline(payload: dict[str, object]) -> None:
        clients = payload["clients"]
        assert isinstance(clients, list) and isinstance(clients[0], dict)
        clients[0]["fb"] = 1
        clients[0]["tl"] = []

    _rewrite_html_payload(html, erase_timeline)
    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    timelines = cast_dict(result["client_timelines"])
    assert timelines["payload_missing_count"] == 1
    assert cast_dict(result["violations"])["html_client_timeline_missing"] == 1


def test_client_timeline_gate_rejects_source_heading_count_mismatch(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts
    client = vault / "clients" / "顧客会社.md"
    client.write_text(
        client.read_text(encoding="utf-8").replace('fb_count: "1"', 'fb_count: "2"'),
        encoding="utf-8",
    )
    _write_manifest(vault)

    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    timelines = cast_dict(result["client_timelines"])
    assert timelines["source_count_mismatch"] == 1
    assert cast_dict(result["violations"])["client_timeline_count_mismatch"] == 1


def test_activity_dates_and_explicit_client_links_are_enforced(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts

    def corrupt_activity_contract(payload: dict[str, object]) -> None:
        clients = payload["clients"]
        docs = payload["docs"]
        assert isinstance(clients, list) and isinstance(docs, list)
        client = clients[0]
        doc = docs[0]
        assert isinstance(client, dict) and isinstance(doc, dict)
        stem = doc["stem"]
        assert isinstance(stem, str)
        doc["modified"] = ""
        doc["_primary_owner_key"] = "must-not-be-public"
        client["ds"] = [stem, stem, "missing-doc"]
        client["doc"] = 1
        client["tl"] = [{"d": ""}]
        client["last"] = "2099-01-01"
        payload["links"] = []

    _rewrite_html_payload(html, corrupt_activity_contract)
    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))
    violations = cast_dict(result["violations"])

    assert result["ok"] is False
    assert violations["html_internal_source_key_exposed"] == 1
    assert violations["html_doc_date_missing"] == 1
    assert violations["html_fb_date_missing"] == 1
    assert violations["html_client_activity_duplicate"] == 1
    assert violations["html_client_activity_doc_missing"] == 1
    assert violations["html_client_activity_not_explicit"] == 3
    assert violations["html_client_doc_count_mismatch"] == 1
    assert violations["html_client_last_contact_mismatch"] == 1


def test_graph_link_endpoint_bounds_reject_runtime_breakage(
    artifacts: tuple[Path, Path, Path, str],
) -> None:
    vault, html, sidecars, _ = artifacts

    def break_graph_endpoint(payload: dict[str, object]) -> None:
        graph = payload["graph"]
        assert isinstance(graph, dict)
        links = graph["links"]
        assert isinstance(links, list) and links
        links[0] = [999999, 999999]

    _rewrite_html_payload(html, break_graph_endpoint)
    result = qa.run_qa(qa.QAConfig(vault=vault, html=html, sidecar_dir=sidecars))

    assert result["ok"] is False
    assert result["html"]["data_bound"] is False
    violations = cast_dict(result["violations"])
    assert violations["html_graph_schema_invalid"] == 1
    assert violations["stats_data_hash_mismatch"] == 1
