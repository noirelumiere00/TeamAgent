#!/usr/bin/env python3
"""Read-only, PII-safe QA gate for connect-web Vault/build artifacts.

The command emits exactly one JSON object.  Values are counts, booleans, or
SHA-256 digests; document titles, paths, filenames, and source identifiers are
never emitted, including on failure.

Typical production flow::

    python scripts/qa_connect_web_artifacts.py --vault ~/AiLaVault \
      --html /path/to/app.html

Capture ``manifest.sha256`` before the build, then prove the build did not
change its input manifest::

    python scripts/qa_connect_web_artifacts.py ... \
      --expected-manifest-sha256 <pre-build-sha256>

The command never writes to the Vault, HTML, stats, sidecars, database, AWS, or
GitHub.  Exit status is 0 only when every invariant passes.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import NoReturn, cast

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_VAULT = Path.home() / "AiLaVault"
_DEFAULT_HTML = (
    Path.home() / "Documents" / "Claude" / "Artifacts" / "connect-web-obsidian-preview.html"
)
_DEFAULT_SIDECAR_DIR = _REPO_ROOT / "data" / "connect_web_filters"
_EXPORT_MANIFEST_NAME = ".export-vault-manifest.json"
_EXPORT_VAULT_GENERATOR = "scripts/export_vault.py"
_ZERO_SHA256 = "0" * 64
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SHEET_ID_RE = re.compile(r"[^:\s]+:\d+:\d+")
_AMBIGUOUS_SHEET_TITLE_RE = re.compile(
    r"(?:^|[\s_\-])(?:row|行)[\s_\-]*\d+(?:$|[\s_\-])",
    re.IGNORECASE,
)
_JUNK_TITLE_RE = re.compile(
    r"(?:test|dummy|sample|untitled|none|null|n/?a|テスト|無題)[\s_\-]*",
    re.IGNORECASE,
)
_FRONT_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_FRONT_LINE_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*):\s*"?(.*?)"?\s*$')
_DATA_ASSIGNMENT_RE = re.compile(r"<script>\s*const DATA=")
_FOOTER_RE = re.compile(r"更新: (\d{4}-\d{2}-\d{2}) JST・取引先(\d+)・資料(\d+)")
_STATUS_FUNCTION_RE = re.compile(r"function updateStatus\(bl,chars\)\{([^\n]*)\}")
_INTERNAL_PAYLOAD_KEYS = frozenset({"external_id", "source_type", "source_key"})
_REQUIRED_JSON_SIDECARS = (
    "exclude_stems.json",
    "exclude_source_keys.json",
    "dedup_drop_map.json",
    "weird_rename_high.json",
)
_OPTIONAL_JSON_SIDECARS = ("tag_alias.json", "client_alias.json")
_FONT_SIDECAR = "inter-var.b64"
_ALL_SIDECAR_INPUTS = tuple(
    sorted((*_REQUIRED_JSON_SIDECARS, *_OPTIONAL_JSON_SIDECARS, _FONT_SIDECAR))
)
_MANAGED_DIRS = frozenset({"clients", "docs"})
_GENERATED_BY = "scripts/export_vault.py"
_CHUNK_RE = re.compile(r"_\d{1,2}$")
_REQUIRED_DATA_KEYS = frozenset(
    {
        "manifest_sha256",
        "build_inputs_sha256",
        "clients",
        "docs",
        "reports",
        "links",
        "graph",
        "colors",
        "stats",
    }
)
_EMBEDDED_FONT_RE = re.compile(
    r'@font-face\{font-family:"InterVar";font-style:normal;font-weight:100 900;'
    r"font-display:swap;src:url\(data:font/woff2;base64,([A-Za-z0-9+/=]+)\) "
    r'format\("woff2"\)\}'
)
_FB_SECTION_RE = re.compile(
    r"^##\s+営業FB時系列（新しい順）\s*$([\s\S]*?)(?=^##\s|\Z)", re.MULTILINE
)
_FB_HEADING_RE = re.compile(r"^###\s+(?:-{2,}.*|\d{4}-\d{2}-\d{2}(?=[T\s]|$).*)$", re.MULTILINE)
_FB_EVENT_KEYS = frozenset({"d", "src", "by", "ph", "bant", "menu", "pos", "neg", "next"})


@dataclass(frozen=True)
class QAConfig:
    """Inputs for one read-only QA pass."""

    vault: Path
    html: Path
    sidecar_dir: Path = _DEFAULT_SIDECAR_DIR
    expected_manifest_sha256: str | None = None
    expected_html_sha256: str | None = None


@dataclass(frozen=True)
class ActiveNote:
    """An active manifest note.  Fields are intentionally never serialized."""

    kind: str
    path: Path
    stem: str
    text: str
    frontmatter: dict[str, str]


@dataclass
class ManifestState:
    sha256: str = _ZERO_SHA256
    notes: list[ActiveNote] = field(default_factory=list)
    file_count: int = 0
    active_count: int = 0
    client_count: int = 0
    doc_count: int = 0


@dataclass
class SidecarState:
    exclude_stems: set[str] = field(default_factory=set)
    exclude_source_keys: set[str] = field(default_factory=set)
    dedup_drop: dict[str, str] = field(default_factory=dict)
    dedup_keep: set[str] = field(default_factory=set)
    rename: dict[str, str] = field(default_factory=dict)
    tag_alias: dict[str, dict[str, str]] = field(default_factory=dict)
    client_alias: dict[str, str] = field(default_factory=dict)
    font_valid: bool = False
    font_bytes: int = 0
    font_sha256: str = _ZERO_SHA256
    build_inputs_sha256: str = _ZERO_SHA256
    json_duplicate_keys: int = 0
    list_duplicate_values: int = 0


@dataclass
class ContentState:
    expected_doc_stems: set[str] = field(default_factory=set)
    expected_doc_titles: dict[str, str] = field(default_factory=dict)
    expected_renamed_stems: set[str] = field(default_factory=set)
    expected_gsheets_renamed_stems: set[str] = field(default_factory=set)
    sensitive_source_tokens: set[str] = field(default_factory=set)
    gsheets_count: int = 0
    missing_id_count: int = 0
    malformed_id_count: int = 0
    duplicate_id_count: int = 0
    empty_title_count: int = 0
    empty_excerpt_count: int = 0
    duplicate_fingerprint_count: int = 0
    empty_fingerprint_count: int = 0
    junk_excluded_count: int = 0
    junk_unexcluded_count: int = 0
    ambiguous_title_count: int = 0
    rename_applied_count: int = 0
    gsheets_rename_applied_count: int = 0
    rename_missing_count: int = 0
    stem_exclusion_applied_count: int = 0
    source_exclusion_applied_count: int = 0
    dedup_applied_count: int = 0
    chunk_fold_applied_count: int = 0
    invoice_rule_applied_count: int = 0
    alias_applied_count: int = 0
    client_declared_fb_count: int = 0
    client_timeline_heading_count: int = 0
    client_timeline_count_mismatch: int = 0
    client_timeline_section_missing: int = 0
    client_fb_count_invalid: int = 0


@dataclass
class HtmlState:
    sha256: str = _ZERO_SHA256
    byte_count: int = 0
    client_count: int = 0
    doc_count: int = 0
    report_count: int = 0
    stats_client_count: int = 0
    stats_doc_count: int = 0
    stats_byte_count: int = 0
    footer_count: int = 0
    internal_source_exposure_count: int = 0
    manifest_bound: bool = False
    build_inputs_bound: bool = False
    data_bound: bool = False
    font_bound: bool = False
    payload_rename_applied_count: int = 0
    client_fb_count: int = 0
    timeline_event_count: int = 0
    client_timeline_missing_count: int = 0
    client_timeline_schema_invalid_count: int = 0


@dataclass
class SnapshotTracker:
    """Track one byte snapshot per input and prove it did not change mid-QA."""

    file_states: dict[Path, str] = field(default_factory=dict)
    directory_states: dict[Path, str] = field(default_factory=dict)
    end_hashes: dict[Path, str] = field(default_factory=dict)

    @staticmethod
    def _capture_file(path: Path) -> tuple[str, bytes | None]:
        try:
            if path.is_symlink():
                return "symlink", None
            if not path.is_file():
                return "missing", None
            raw = path.read_bytes()
        except OSError:
            return "error", None
        return f"file:{hashlib.sha256(raw).hexdigest()}", raw

    @staticmethod
    def _directory_state(entries: list[Path]) -> str:
        rows: list[tuple[str, bool, bool]] = []
        for path in entries:
            try:
                rows.append((path.name, path.is_symlink(), path.is_file()))
            except OSError:
                rows.append((path.name, False, False))
        encoded = json.dumps(sorted(rows), ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def read(
        self,
        path: Path,
        violations: Counter[str],
        *,
        required: bool,
        missing_kind: str,
        unsafe_kind: str,
        read_kind: str,
    ) -> bytes | None:
        state, raw = self._capture_file(path)
        previous = self.file_states.setdefault(path, state)
        if previous != state:
            _add(violations, "artifact_changed_during_qa")
        if state == "symlink":
            _add(violations, unsafe_kind)
            return None
        if state == "missing":
            if required:
                _add(violations, missing_kind)
            return None
        if state == "error" or raw is None:
            _add(violations, read_kind)
            return None
        return raw

    def track_directory(
        self,
        path: Path,
        entries: list[Path],
    ) -> None:
        self.directory_states[path] = self._directory_state(entries)

    def verify(self, violations: Counter[str]) -> None:
        changed = 0
        for path, expected_state in self.file_states.items():
            actual_state, _ = self._capture_file(path)
            if actual_state != expected_state:
                changed += 1
            if actual_state.startswith("file:"):
                self.end_hashes[path] = actual_state.removeprefix("file:")
        for path, expected_state in self.directory_states.items():
            try:
                entries = list(path.iterdir())
                actual_state = self._directory_state(entries)
            except OSError:
                actual_state = "error"
            if actual_state != expected_state:
                changed += 1
        _add(violations, "artifact_changed_during_qa", changed)

    def end_hash(self, path: Path) -> str:
        return self.end_hashes.get(path, _ZERO_SHA256)

    def input_marker(self, path: Path) -> str:
        state = self.file_states.get(path, "missing")
        if state.startswith("file:"):
            return f"sha256:{state.removeprefix('file:')}"
        return state


def _add(violations: Counter[str], kind: str, count: int = 1) -> None:
    if count > 0:
        violations[kind] += count


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _portable_key(value: str) -> str:
    return _nfc(value).casefold()


def _frontmatter(text: str) -> dict[str, str]:
    match = _FRONT_RE.search(text)
    if match is None:
        return {}
    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        item = _FRONT_LINE_RE.match(line)
        if item is not None:
            result[item.group(1)] = item.group(2)
    return result


def _body(text: str) -> str:
    return re.sub(r"^---\n.*?\n---\n?", "", text, count=1, flags=re.DOTALL)


def _excerpt(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("> "):
            return line[2:].strip()
    return ""


def _source_key(frontmatter: dict[str, str]) -> str:
    source_type = frontmatter.get("source_type", "").strip().lower()
    external_id = frontmatter.get("external_id", "").strip()
    if not source_type or not external_id:
        return ""
    return f"{source_type}:{external_id}"


def _normalized_exclusion_stem(stem: str) -> str:
    return re.sub(r"[\s_]+", "", stem).lower()


def _normalized_lookup(values: set[str]) -> set[str]:
    return {_nfc(value) for value in values}


def _portable_map(values: dict[str, str]) -> dict[str, str]:
    return {_portable_key(key): value for key, value in values.items()}


def _build_inputs_bundle_sha256(
    manifest_sha256: str,
    sidecar_dir: Path,
    snapshots: SnapshotTracker,
) -> str:
    digest = hashlib.sha256(b"connect-web-build-inputs-v1\0")
    digest.update(b"manifest\0sha256:")
    digest.update(manifest_sha256.encode("ascii"))
    digest.update(b"\0")
    for name in _ALL_SIDECAR_INPUTS:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(snapshots.input_marker(sidecar_dir / name).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _load_json(
    path: Path,
    violations: Counter[str],
    snapshots: SnapshotTracker,
    *,
    required: bool,
) -> tuple[object | None, int]:
    raw = snapshots.read(
        path,
        violations,
        required=required,
        missing_kind="sidecar_missing",
        unsafe_kind="sidecar_symlink",
        read_kind="sidecar_read_error",
    )
    if raw is None:
        return None, 0

    duplicate_keys = 0

    def object_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate_keys
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                duplicate_keys += 1
            result[key] = value
        return result

    try:
        value = cast(
            object,
            json.loads(raw.decode("utf-8"), object_pairs_hook=object_hook),
        )
    except (UnicodeError, json.JSONDecodeError):
        _add(violations, "sidecar_json_invalid")
        return None, duplicate_keys
    if duplicate_keys:
        _add(violations, "sidecar_duplicate_key", duplicate_keys)
    return value, duplicate_keys


def _string_list(
    value: object,
    violations: Counter[str],
) -> tuple[set[str], int]:
    if not isinstance(value, list):
        _add(violations, "sidecar_type_invalid")
        return set(), 0
    strings: list[str] = []
    invalid = 0
    for item in value:
        if not isinstance(item, str) or not item.strip():
            invalid += 1
        else:
            strings.append(item)
    _add(violations, "sidecar_value_invalid", invalid)
    duplicate_count = len(strings) - len(set(strings))
    _add(violations, "sidecar_duplicate_value", duplicate_count)
    normalized_duplicate_count = len({_nfc(item) for item in strings})
    normalized_duplicate_count = len(set(strings)) - normalized_duplicate_count
    _add(violations, "sidecar_normalized_key_collision", normalized_duplicate_count)
    return set(strings), duplicate_count


def _string_map(value: object, violations: Counter[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        _add(violations, "sidecar_type_invalid")
        return {}
    result: dict[str, str] = {}
    invalid = 0
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(item, str)
            or not item.strip()
        ):
            invalid += 1
            continue
        result[key] = item
    _add(violations, "sidecar_value_invalid", invalid)
    normalized_key_count = len({_nfc(key) for key in result})
    _add(
        violations,
        "sidecar_normalized_key_collision",
        len(result) - normalized_key_count,
    )
    return result


def _validate_source_keys(values: set[str], violations: Counter[str]) -> None:
    invalid = sum(
        1
        for value in values
        if re.fullmatch(r"[a-z][a-z0-9_-]*:.+", value) is None
        or any(char.isspace() for char in value)
    )
    _add(violations, "sidecar_source_key_invalid", invalid)


def _validate_font(
    path: Path,
    state: SidecarState,
    violations: Counter[str],
    snapshots: SnapshotTracker,
) -> None:
    encoded_bytes = snapshots.read(
        path,
        violations,
        required=True,
        missing_kind="sidecar_missing",
        unsafe_kind="sidecar_symlink",
        read_kind="sidecar_read_error",
    )
    if encoded_bytes is None:
        return
    try:
        encoded = "".join(encoded_bytes.decode("ascii").split())
        raw = base64.b64decode(encoded, validate=True)
    except (UnicodeError, ValueError, binascii.Error):
        _add(violations, "font_invalid")
        return

    state.font_bytes = len(raw)
    state.font_sha256 = hashlib.sha256(raw).hexdigest()
    header_valid = (
        len(raw) >= 48
        and raw[:4] == b"wOF2"
        and int.from_bytes(raw[8:12], "big") == len(raw)
        and int.from_bytes(raw[12:14], "big") > 0
        and int.from_bytes(raw[16:20], "big") > 0
        and 0 < int.from_bytes(raw[20:24], "big") <= len(raw)
    )
    if not header_valid:
        _add(violations, "font_invalid")
        return

    try:
        from fontTools.ttLib import TTFont  # type: ignore[import-untyped]
    except ImportError:
        _add(violations, "font_parser_unavailable")
        return

    font: TTFont | None = None
    try:
        font = TTFont(
            BytesIO(raw),
            lazy=False,
            recalcBBoxes=False,
            recalcTimestamp=False,
        )
        font.ensureDecompiled(recurse=True)
        required_tables = {"head", "hhea", "maxp", "hmtx", "cmap", "name"}
        valid = (
            font.flavor == "woff2"
            and required_tables.issubset(font.keys())
            and bool(font.getGlyphOrder())
            and bool(font["cmap"].getBestCmap())
        )
    except Exception:
        valid = False
    finally:
        if font is not None:
            font.close()
    state.font_valid = valid
    if not valid:
        _add(violations, "font_invalid")


def _load_sidecars(
    directory: Path,
    manifest_sha256: str,
    violations: Counter[str],
    snapshots: SnapshotTracker,
) -> SidecarState:
    state = SidecarState()
    loaded: dict[str, object | None] = {}
    for name in _REQUIRED_JSON_SIDECARS:
        loaded[name], duplicates = _load_json(
            directory / name, violations, snapshots, required=True
        )
        state.json_duplicate_keys += duplicates
    for name in _OPTIONAL_JSON_SIDECARS:
        loaded[name], duplicates = _load_json(
            directory / name, violations, snapshots, required=False
        )
        state.json_duplicate_keys += duplicates

    state.exclude_stems, duplicates = _string_list(loaded["exclude_stems.json"], violations)
    state.list_duplicate_values += duplicates
    state.exclude_source_keys, duplicates = _string_list(
        loaded["exclude_source_keys.json"], violations
    )
    state.list_duplicate_values += duplicates
    _validate_source_keys(state.exclude_source_keys, violations)

    dedup = loaded["dedup_drop_map.json"]
    if isinstance(dedup, dict):
        state.dedup_drop = _string_map(dedup.get("drop"), violations)
        state.dedup_keep, duplicates = _string_list(dedup.get("keep_canonical"), violations)
        state.list_duplicate_values += duplicates
        unknown = set(dedup) - {"drop", "keep_canonical"}
        _add(violations, "sidecar_schema_invalid", len(unknown))
        _add(
            violations,
            "sidecar_dedup_self_reference",
            sum(key == target for key, target in state.dedup_drop.items()),
        )
        keep_nfc = _normalized_lookup(state.dedup_keep)
        _add(
            violations,
            "sidecar_dedup_target_missing",
            sum(_nfc(target) not in keep_nfc for target in state.dedup_drop.values()),
        )
    else:
        _add(violations, "sidecar_type_invalid")

    state.rename = _string_map(loaded["weird_rename_high.json"], violations)
    _add(
        violations,
        "sidecar_portable_key_collision",
        len(state.rename) - len({_portable_key(key) for key in state.rename}),
    )
    _add(
        violations,
        "sidecar_rename_noop",
        sum(key == target for key, target in state.rename.items()),
    )

    tag_alias = loaded.get("tag_alias.json")
    if tag_alias is not None:
        if isinstance(tag_alias, dict):
            unknown = set(tag_alias) - {"_note", "industry", "solution"}
            _add(violations, "sidecar_schema_invalid", len(unknown))
            for key in ("industry", "solution"):
                state.tag_alias[key] = _string_map(tag_alias.get(key), violations)
            note = tag_alias.get("_note", "")
            if not isinstance(note, str):
                _add(violations, "sidecar_type_invalid")
        else:
            _add(violations, "sidecar_type_invalid")

    client_alias = loaded.get("client_alias.json")
    if client_alias is not None:
        if isinstance(client_alias, dict):
            unknown = set(client_alias) - {"_note", "client"}
            _add(violations, "sidecar_schema_invalid", len(unknown))
            state.client_alias = _string_map(client_alias.get("client"), violations)
            note = client_alias.get("_note", "")
            if not isinstance(note, str):
                _add(violations, "sidecar_type_invalid")
        else:
            _add(violations, "sidecar_type_invalid")

    _validate_font(directory / _FONT_SIDECAR, state, violations, snapshots)
    state.build_inputs_sha256 = _build_inputs_bundle_sha256(manifest_sha256, directory, snapshots)
    return state


def _physical_note_index(
    vault: Path,
    violations: Counter[str],
    snapshots: SnapshotTracker,
) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = defaultdict(list)
    resolved_vault = vault.resolve()
    for kind in sorted(_MANAGED_DIRS):
        directory = vault / kind
        if directory.is_symlink() or not directory.is_dir():
            _add(violations, "vault_structure_invalid")
            continue
        expected_parent = resolved_vault / kind
        try:
            entries = list(directory.iterdir())
        except OSError:
            _add(violations, "vault_read_error")
            continue
        snapshots.track_directory(directory, entries)
        for path in entries:
            if path.suffix != ".md":
                continue
            if path.is_symlink() or not path.is_file():
                _add(violations, "vault_note_unsafe")
                continue
            try:
                if path.resolve().parent != expected_parent:
                    _add(violations, "vault_note_unsafe")
                    continue
            except OSError:
                _add(violations, "vault_note_unsafe")
                continue
            rel = f"{kind}/{path.name}"
            index[_portable_key(rel)].append(path)
    _add(
        violations,
        "vault_portable_path_collision",
        sum(len(paths) - 1 for paths in index.values() if len(paths) > 1),
    )
    return index


def _manifest_rel_valid(value: str) -> bool:
    if "\\" in value or value != value.strip():
        return False
    parts = value.split("/")
    return (
        len(parts) == 2
        and parts[0] in _MANAGED_DIRS
        and bool(parts[1])
        and parts[1] != ".md"
        and parts[1].endswith(".md")
        and parts[1] not in {".", ".."}
    )


def _load_manifest(
    config: QAConfig,
    violations: Counter[str],
    snapshots: SnapshotTracker,
) -> ManifestState:
    state = ManifestState()
    manifest_path = config.vault / _EXPORT_MANIFEST_NAME
    try:
        safe_manifest = manifest_path.resolve().parent == config.vault.resolve()
    except OSError:
        safe_manifest = False
    if not safe_manifest:
        _add(violations, "manifest_unsafe")
        return state
    manifest_bytes = snapshots.read(
        manifest_path,
        violations,
        required=True,
        missing_kind="manifest_missing",
        unsafe_kind="manifest_unsafe",
        read_kind="manifest_read_error",
    )
    if manifest_bytes is None:
        return state
    state.sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if config.expected_manifest_sha256 is not None:
        expected = config.expected_manifest_sha256.lower()
        if _SHA256_RE.fullmatch(expected) is None:
            _add(violations, "expected_manifest_hash_invalid")
        elif state.sha256 != expected:
            _add(violations, "manifest_changed_since_snapshot")

    duplicate_keys = 0

    def object_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate_keys
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                duplicate_keys += 1
            result[key] = value
        return result

    try:
        payload = cast(
            object,
            json.loads(
                manifest_bytes.decode("utf-8"),
                object_pairs_hook=object_hook,
            ),
        )
    except (UnicodeError, json.JSONDecodeError):
        _add(violations, "manifest_json_invalid")
        return state
    _add(violations, "manifest_duplicate_key", duplicate_keys)
    if not isinstance(payload, dict):
        _add(violations, "manifest_schema_invalid")
        return state

    version = payload.get("version")
    if not _is_int(version) or version != 1:
        _add(violations, "manifest_version_invalid")
    if payload.get("generator") != _EXPORT_VAULT_GENERATOR:
        _add(violations, "manifest_generator_invalid")
    if payload.get("complete_export") is not True:
        _add(violations, "manifest_incomplete")
    files = payload.get("files")
    active_raw = payload.get("active_files")
    if not isinstance(files, dict) or not isinstance(active_raw, list):
        _add(violations, "manifest_schema_invalid")
        return state

    state.file_count = len(files)
    manifest_hashes: dict[str, str] = {}
    manifest_paths_seen: set[str] = set()
    for rel_value, hash_value in files.items():
        if not isinstance(rel_value, str) or not _manifest_rel_valid(rel_value):
            _add(violations, "manifest_file_path_invalid")
            continue
        portable = _portable_key(rel_value)
        if portable in manifest_paths_seen:
            _add(violations, "manifest_file_portable_collision")
            continue
        manifest_paths_seen.add(portable)
        if not isinstance(hash_value, str) or _SHA256_RE.fullmatch(hash_value) is None:
            _add(violations, "manifest_file_hash_invalid")
            continue
        manifest_hashes[portable] = hash_value

    active_strings = [value for value in active_raw if isinstance(value, str)]
    _add(violations, "manifest_active_type_invalid", len(active_raw) - len(active_strings))
    if not active_strings:
        _add(violations, "manifest_active_empty")
    exact_duplicates = len(active_strings) - len(set(active_strings))
    portable_duplicates = len(set(active_strings)) - len(
        {_portable_key(value) for value in active_strings}
    )
    _add(violations, "manifest_active_duplicate", exact_duplicates)
    _add(violations, "manifest_active_portable_collision", portable_duplicates)

    physical = _physical_note_index(config.vault, violations, snapshots)
    active_by_portable: dict[str, str] = {}
    for rel in active_strings:
        if not _manifest_rel_valid(rel):
            _add(violations, "manifest_active_path_invalid")
            continue
        portable = _portable_key(rel)
        if portable in active_by_portable:
            continue
        active_by_portable[portable] = rel
        expected_hash = manifest_hashes.get(portable)
        if expected_hash is None:
            _add(violations, "manifest_active_hash_invalid")
            continue
        candidates = physical.get(portable, [])
        if not candidates:
            _add(violations, "manifest_active_file_missing")
            continue
        if len(candidates) != 1:
            _add(violations, "manifest_active_file_ambiguous")
            continue
        path = candidates[0]
        note_bytes = snapshots.read(
            path,
            violations,
            required=True,
            missing_kind="manifest_active_file_missing",
            unsafe_kind="manifest_active_file_unsafe",
            read_kind="manifest_active_file_unreadable",
        )
        if note_bytes is None:
            continue
        actual_hash = hashlib.sha256(note_bytes).hexdigest()
        if actual_hash != expected_hash:
            _add(violations, "manifest_active_hash_mismatch")
            continue
        try:
            text = note_bytes.decode("utf-8", errors="replace")
        except UnicodeError:
            _add(violations, "manifest_active_file_encoding_invalid")
            continue
        kind = rel.split("/", 1)[0]
        state.notes.append(
            ActiveNote(
                kind=kind,
                path=path,
                stem=path.stem,
                text=text,
                frontmatter=_frontmatter(text),
            )
        )

    state.active_count = len(active_strings)
    state.client_count = sum(note.kind == "clients" for note in state.notes)
    state.doc_count = sum(note.kind == "docs" for note in state.notes)
    return state


def _is_stem_excluded(stem: str, sidecars: SidecarState) -> bool:
    normalized = {_normalized_exclusion_stem(value) for value in sidecars.exclude_stems}
    return stem in sidecars.exclude_stems or _normalized_exclusion_stem(stem) in normalized


def _is_invoice(stem: str) -> bool:
    return "請求" in _normalized_exclusion_stem(stem)


def _chunk_key(stem: str) -> str:
    key = stem
    while _CHUNK_RE.search(key):
        key = _CHUNK_RE.sub("", key)
    return key


def _chunk_drop_stems(
    notes: list[ActiveNote],
    sidecars: SidecarState,
) -> set[str]:
    groups: dict[str, list[str]] = defaultdict(list)
    source_excludes = sidecars.exclude_source_keys
    for note in notes:
        if note.kind != "docs":
            continue
        if (
            _is_stem_excluded(note.stem, sidecars)
            or _is_invoice(note.stem)
            or _source_key(note.frontmatter) in source_excludes
        ):
            continue
        if note.frontmatter.get("generated_by") == _GENERATED_BY:
            continue
        groups[_chunk_key(note.stem)].append(note.stem)
    dropped: set[str] = set()
    for key, stems in groups.items():
        if len(stems) <= 1:
            continue
        representative = key if key in stems else min(stems, key=lambda item: (len(item), item))
        dropped.update(stem for stem in stems if stem != representative)
    return dropped


def _fingerprint(text: str) -> str:
    body = _body(text)
    cleaned_lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- 出典:") or re.fullmatch(r"\[\[[^\]]+\]\]", stripped):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cleaned)
    cleaned = unicodedata.normalize("NFKC", cleaned).lower()
    cleaned = re.sub(r"\s+", "", cleaned)
    if not cleaned:
        return ""
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def _is_heuristic_junk(title: str) -> bool:
    normalized = unicodedata.normalize("NFKC", title).strip()
    return _JUNK_TITLE_RE.fullmatch(normalized) is not None


def _is_ambiguous_sheet_title(title: str) -> bool:
    normalized = unicodedata.normalize("NFKC", title).strip()
    return _AMBIGUOUS_SHEET_TITLE_RE.search(normalized) is not None


def _analyze_content(
    manifest: ManifestState,
    sidecars: SidecarState,
    violations: Counter[str],
) -> ContentState:
    state = ContentState()
    dedup_drop = set(sidecars.dedup_drop)
    rename_portable = _portable_map(sidecars.rename)
    chunk_drop = _chunk_drop_stems(manifest.notes, sidecars)
    source_excludes = sidecars.exclude_source_keys
    sheet_ids: list[str] = []
    fingerprints: list[str] = []

    tag_industry = sidecars.tag_alias.get("industry", {})
    tag_solution = sidecars.tag_alias.get("solution", {})
    for note in manifest.notes:
        fm = note.frontmatter
        if fm.get("industry", "") in tag_industry:
            state.alias_applied_count += 1
        if fm.get("solution", "") in tag_solution:
            state.alias_applied_count += 1
        if fm.get("client", "") in sidecars.client_alias:
            state.alias_applied_count += 1
        if note.kind == "clients":
            raw_fb_count = fm.get("fb_count", "").strip()
            if re.fullmatch(r"\d+", raw_fb_count) is None:
                state.client_fb_count_invalid += 1
                declared_fb_count: int | None = None
            else:
                declared_fb_count = int(raw_fb_count)
                state.client_declared_fb_count += declared_fb_count

            section = _FB_SECTION_RE.search(_body(note.text))
            if section is None:
                state.client_timeline_section_missing += 1
                heading_count = 0
            else:
                heading_count = len(_FB_HEADING_RE.findall(section.group(1)))
            state.client_timeline_heading_count += heading_count
            if declared_fb_count is not None and declared_fb_count != heading_count:
                state.client_timeline_count_mismatch += 1
        if note.kind != "docs":
            continue

        stem_portable = _portable_key(note.stem)
        source_key = _source_key(fm)
        stem_excluded = _is_stem_excluded(note.stem, sidecars)
        source_excluded = bool(source_key) and source_key in source_excludes
        dedup_excluded = note.stem in dedup_drop
        chunk_excluded = note.stem in chunk_drop
        invoice_excluded = _is_invoice(note.stem)
        renamed = stem_portable in rename_portable

        state.stem_exclusion_applied_count += int(stem_excluded)
        state.source_exclusion_applied_count += int(source_excluded)
        state.dedup_applied_count += int(dedup_excluded)
        state.chunk_fold_applied_count += int(chunk_excluded)
        state.invoice_rule_applied_count += int(invoice_excluded)
        state.rename_applied_count += int(renamed)

        excluded = (
            stem_excluded or source_excluded or dedup_excluded or chunk_excluded or invoice_excluded
        )
        if not excluded:
            state.expected_doc_stems.add(stem_portable)
            state.expected_doc_titles[stem_portable] = rename_portable.get(stem_portable) or fm.get(
                "title", note.stem
            )
            if renamed:
                state.expected_renamed_stems.add(stem_portable)

        external_id = fm.get("external_id", "").strip()
        source_type = fm.get("source_type", "").strip().lower()
        if source_key:
            state.sensitive_source_tokens.add(source_key)
        if source_type == "gsheets" and external_id:
            state.sensitive_source_tokens.add(external_id)
        if source_type != "gsheets":
            continue

        state.gsheets_count += 1
        sheet_ids.append(external_id)
        if not external_id:
            state.missing_id_count += 1
        elif _SHEET_ID_RE.fullmatch(external_id) is None:
            state.malformed_id_count += 1

        title = fm.get("title", "").strip()
        excerpt = _excerpt(note.text)
        if not title:
            state.empty_title_count += 1
        if not excerpt:
            state.empty_excerpt_count += 1

        known_junk = source_excluded or stem_excluded
        heuristic_junk = _is_heuristic_junk(title)
        if known_junk:
            state.junk_excluded_count += 1
        if heuristic_junk and not excluded:
            state.junk_unexcluded_count += 1

        ambiguous = _is_ambiguous_sheet_title(title)
        state.ambiguous_title_count += int(ambiguous)
        if ambiguous and not renamed and not excluded:
            state.rename_missing_count += 1
        if renamed and not excluded:
            state.expected_gsheets_renamed_stems.add(stem_portable)

        if not excluded:
            fingerprint = _fingerprint(note.text)
            if fingerprint:
                fingerprints.append(fingerprint)
            else:
                state.empty_fingerprint_count += 1

    nonempty_ids = [item for item in sheet_ids if item]
    state.duplicate_id_count = sum(
        count - 1 for count in Counter(nonempty_ids).values() if count > 1
    )
    state.duplicate_fingerprint_count = sum(
        count - 1 for count in Counter(fingerprints).values() if count > 1
    )

    _add(violations, "gsheets_id_missing", state.missing_id_count)
    _add(violations, "gsheets_id_malformed", state.malformed_id_count)
    _add(violations, "gsheets_id_duplicate", state.duplicate_id_count)
    _add(violations, "gsheets_title_empty", state.empty_title_count)
    _add(violations, "gsheets_excerpt_empty", state.empty_excerpt_count)
    _add(violations, "gsheets_fingerprint_empty", state.empty_fingerprint_count)
    _add(violations, "gsheets_fingerprint_duplicate", state.duplicate_fingerprint_count)
    _add(violations, "gsheets_junk_unexcluded", state.junk_unexcluded_count)
    _add(violations, "gsheets_rename_missing", state.rename_missing_count)
    _add(violations, "client_fb_count_invalid", state.client_fb_count_invalid)
    _add(
        violations,
        "client_timeline_section_missing",
        state.client_timeline_section_missing,
    )
    _add(
        violations,
        "client_timeline_count_mismatch",
        state.client_timeline_count_mismatch,
    )
    return state


def _extract_data(
    html: str,
    violations: Counter[str],
) -> tuple[dict[str, object] | None, str, str]:
    assignments = list(_DATA_ASSIGNMENT_RE.finditer(html))
    if len(assignments) != 1:
        _add(violations, "html_data_assignment_invalid", abs(len(assignments) - 1) or 1)
        return None, "", ""
    offset = assignments[0].end()
    try:
        payload, consumed = json.JSONDecoder().raw_decode(html[offset:])
    except json.JSONDecodeError:
        _add(violations, "html_data_json_invalid")
        return None, "", ""
    raw_data = html[offset : offset + consumed]
    remainder = html[offset + consumed :]
    if re.match(r"^;\s*const \$=", remainder) is None:
        _add(violations, "html_data_position_invalid")
    if not isinstance(payload, dict):
        _add(violations, "html_data_schema_invalid")
        return None, remainder, raw_data
    return cast(dict[str, object], payload), remainder, raw_data


def _list_of_objects(
    value: object,
    violations: Counter[str],
) -> list[dict[str, object]]:
    if not isinstance(value, list):
        _add(violations, "html_data_schema_invalid")
        return []
    result: list[dict[str, object]] = []
    invalid = 0
    for item in value:
        if isinstance(item, dict):
            result.append(cast(dict[str, object], item))
        else:
            invalid += 1
    _add(violations, "html_data_schema_invalid", invalid)
    return result


def _internal_key_count(value: object) -> int:
    if isinstance(value, dict):
        return sum(key in _INTERNAL_PAYLOAD_KEYS for key in value) + sum(
            _internal_key_count(item) for item in value.values()
        )
    if isinstance(value, list):
        return sum(_internal_key_count(item) for item in value)
    return 0


def _stats_path(html: Path) -> Path:
    return Path(str(html) + ".stats.json")


def _read_stats(
    path: Path,
    violations: Counter[str],
    snapshots: SnapshotTracker,
) -> dict[str, object] | None:
    raw = snapshots.read(
        path,
        violations,
        required=True,
        missing_kind="stats_missing",
        unsafe_kind="stats_unsafe",
        read_kind="stats_read_error",
    )
    if raw is None:
        return None
    try:
        value = cast(object, json.loads(raw.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError):
        _add(violations, "stats_json_invalid")
        return None
    if not isinstance(value, dict):
        _add(violations, "stats_schema_invalid")
        return None
    return cast(dict[str, object], value)


def _analyze_html(
    config: QAConfig,
    manifest: ManifestState,
    sidecars: SidecarState,
    content: ContentState,
    violations: Counter[str],
    snapshots: SnapshotTracker,
) -> HtmlState:
    state = HtmlState()
    raw = snapshots.read(
        config.html,
        violations,
        required=True,
        missing_kind="html_missing",
        unsafe_kind="html_unsafe",
        read_kind="html_read_error",
    )
    if raw is None:
        return state
    state.byte_count = len(raw)
    state.sha256 = hashlib.sha256(raw).hexdigest()
    if config.expected_html_sha256 is not None:
        expected = config.expected_html_sha256.lower()
        if _SHA256_RE.fullmatch(expected) is None:
            _add(violations, "expected_html_hash_invalid")
        elif state.sha256 != expected:
            _add(violations, "html_hash_mismatch")
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError:
        _add(violations, "html_encoding_invalid")
        return state

    embedded_fonts = _EMBEDDED_FONT_RE.findall(html)
    if len(embedded_fonts) != 1:
        _add(
            violations,
            "html_font_embedding_invalid",
            abs(len(embedded_fonts) - 1) or 1,
        )
    else:
        try:
            embedded_font = base64.b64decode(embedded_fonts[0], validate=True)
        except (ValueError, binascii.Error):
            _add(violations, "html_font_embedding_invalid")
        else:
            embedded_font_matches = (
                hashlib.sha256(embedded_font).hexdigest() == sidecars.font_sha256
            )
            if not embedded_font_matches:
                _add(violations, "html_font_hash_mismatch")
            state.font_bound = sidecars.font_valid and embedded_font_matches

    payload, script_tail, raw_data = _extract_data(html, violations)
    data_sha256 = hashlib.sha256(raw_data.encode("utf-8")).hexdigest() if raw_data else _ZERO_SHA256
    payload_stats: dict[str, object] | None = None
    data_manifest_matches = False
    data_build_inputs_matches = False
    payload_stats_manifest_matches = False
    payload_stats_build_inputs_matches = False
    if payload is not None:
        _add(
            violations,
            "html_data_required_key_missing",
            len(_REQUIRED_DATA_KEYS - set(payload)),
        )
        clients = _list_of_objects(payload.get("clients"), violations)
        docs = _list_of_objects(payload.get("docs"), violations)
        reports = _list_of_objects(payload.get("reports"), violations)
        state.client_count = len(clients)
        state.doc_count = len(docs)
        state.report_count = len(reports)
        if not clients:
            _add(violations, "html_clients_empty")
        if not docs:
            _add(violations, "html_docs_empty")

        client_item_errors = sum(
            not all(
                isinstance(client.get(key), str) and bool(cast(str, client[key]).strip())
                for key in ("stem", "name", "md")
            )
            for client in clients
        )
        doc_item_errors = sum(
            not all(
                isinstance(doc.get(key), str) and bool(cast(str, doc[key]).strip())
                for key in ("stem", "title", "md")
            )
            for doc in docs
        )
        _add(
            violations,
            "html_data_item_schema_invalid",
            client_item_errors + doc_item_errors,
        )

        for client in clients:
            fb_count = client.get("fb")
            timeline = client.get("tl")
            schema_invalid = False
            if not _is_int(fb_count) or cast(int, fb_count) < 0:
                schema_invalid = True
            else:
                state.client_fb_count += cast(int, fb_count)
            if not isinstance(timeline, list) or len(timeline) > 30:
                schema_invalid = True
                timeline_events: list[object] = []
            else:
                timeline_events = timeline
                state.timeline_event_count += len(timeline_events)
                for event in timeline_events:
                    if (
                        not isinstance(event, dict)
                        or not _FB_EVENT_KEYS.issubset(event)
                        or not all(isinstance(event.get(key), str) for key in _FB_EVENT_KEYS)
                    ):
                        schema_invalid = True
            if _is_int(fb_count) and cast(int, fb_count) > 0 and not timeline_events:
                state.client_timeline_missing_count += 1
            if schema_invalid:
                state.client_timeline_schema_invalid_count += 1
        _add(
            violations,
            "html_client_timeline_missing",
            state.client_timeline_missing_count,
        )
        _add(
            violations,
            "html_client_timeline_schema_invalid",
            state.client_timeline_schema_invalid_count,
        )

        links = payload.get("links")
        if not isinstance(links, list):
            _add(violations, "html_links_schema_invalid")
        else:
            _add(
                violations,
                "html_links_schema_invalid",
                sum(
                    not isinstance(link, list)
                    or len(link) != 3
                    or not all(isinstance(item, str) for item in link)
                    for link in links
                ),
            )

        colors = payload.get("colors")
        if not isinstance(colors, dict) or not colors:
            _add(violations, "html_colors_schema_invalid")
        elif not all(
            isinstance(key, str) and isinstance(value, str) for key, value in colors.items()
        ):
            _add(violations, "html_colors_schema_invalid")

        graph = payload.get("graph")
        if not isinstance(graph, dict):
            _add(violations, "html_graph_schema_invalid")
        else:
            nodes = graph.get("nodes")
            graph_links = graph.get("links")
            graph_invalid = False
            if not isinstance(nodes, list) or not nodes:
                graph_invalid = True
                node_count = 0
            else:
                node_count = len(nodes)
                node_ids: set[str] = set()
                for node in nodes:
                    if not isinstance(node, dict):
                        graph_invalid = True
                        continue
                    node_id = node.get("id")
                    label = node.get("label")
                    node_type = node.get("type")
                    industry = node.get("industry")
                    phase = node.get("phase")
                    degree = node.get("deg")
                    if (
                        not isinstance(node_id, str)
                        or not node_id.strip()
                        or node_id in node_ids
                        or not isinstance(label, str)
                        or not label.strip()
                        or node_type not in {"client", "doc", "tag"}
                        or not isinstance(industry, str)
                        or not isinstance(phase, str)
                        or not _is_int(degree)
                        or cast(int, degree) < 0
                    ):
                        graph_invalid = True
                    else:
                        node_ids.add(node_id)
                    if node_type == "client" and (
                        not _is_int(node.get("fb"))
                        or cast(int, node["fb"]) < 0
                        or not _is_int(node.get("doc"))
                        or cast(int, node["doc"]) < 0
                    ):
                        graph_invalid = True
            degrees = [0] * node_count
            if not isinstance(graph_links, list):
                graph_invalid = True
            else:
                for link in graph_links:
                    if (
                        not isinstance(link, list)
                        or len(link) != 2
                        or not all(_is_int(item) for item in link)
                    ):
                        graph_invalid = True
                        continue
                    source, target = cast(list[int], link)
                    if not (0 <= source < node_count and 0 <= target < node_count):
                        graph_invalid = True
                        continue
                    degrees[source] += 1
                    degrees[target] += 1
            if isinstance(nodes, list) and len(nodes) == node_count:
                for index, node in enumerate(nodes):
                    if (
                        isinstance(node, dict)
                        and _is_int(node.get("deg"))
                        and node["deg"] != degrees[index]
                    ):
                        graph_invalid = True
            if graph_invalid:
                _add(violations, "html_graph_schema_invalid")

        data_manifest = payload.get("manifest_sha256")
        if not isinstance(data_manifest, str) or _SHA256_RE.fullmatch(data_manifest) is None:
            _add(violations, "html_manifest_hash_invalid")
        elif data_manifest != manifest.sha256:
            _add(violations, "html_manifest_hash_mismatch")
        else:
            data_manifest_matches = True

        data_build_inputs = payload.get("build_inputs_sha256")
        if (
            not isinstance(data_build_inputs, str)
            or _SHA256_RE.fullmatch(data_build_inputs) is None
        ):
            _add(violations, "html_build_inputs_hash_invalid")
        elif data_build_inputs != sidecars.build_inputs_sha256:
            _add(violations, "html_build_inputs_hash_mismatch")
        else:
            data_build_inputs_matches = True

        stats_raw = payload.get("stats")
        if isinstance(stats_raw, dict):
            payload_stats = cast(dict[str, object], stats_raw)
        else:
            _add(violations, "html_data_stats_invalid")

        payload_doc_titles: dict[str, str] = {}
        invalid_doc_stems = 0
        duplicate_doc_stems = 0
        for doc in docs:
            stem = doc.get("stem")
            title = doc.get("title")
            if not isinstance(stem, str) or not isinstance(title, str):
                invalid_doc_stems += 1
                continue
            key = _portable_key(stem)
            if key in payload_doc_titles:
                duplicate_doc_stems += 1
                continue
            payload_doc_titles[key] = title
        payload_doc_stems = set(payload_doc_titles)
        _add(violations, "html_doc_stem_invalid", invalid_doc_stems)
        _add(violations, "html_doc_stem_duplicate", duplicate_doc_stems)
        _add(
            violations,
            "html_doc_set_missing",
            len(content.expected_doc_stems - payload_doc_stems),
        )
        _add(
            violations,
            "html_doc_set_extra",
            len(payload_doc_stems - content.expected_doc_stems),
        )
        title_mismatches = sum(
            payload_doc_titles.get(stem) != expected_title
            for stem, expected_title in content.expected_doc_titles.items()
            if stem in payload_doc_titles
        )
        _add(violations, "html_doc_title_mismatch", title_mismatches)
        actual_renamed = {
            stem
            for stem in content.expected_renamed_stems
            if payload_doc_titles.get(stem) == content.expected_doc_titles.get(stem)
        }
        state.payload_rename_applied_count = len(actual_renamed)
        content.gsheets_rename_applied_count = len(
            actual_renamed & content.expected_gsheets_renamed_stems
        )
        if reports:
            _add(violations, "html_report_not_allowed", len(reports))
        internal_keys = _internal_key_count(payload)
        _add(violations, "html_internal_source_key_exposed", internal_keys)
        state.internal_source_exposure_count += internal_keys

    leaked_tokens = sum(token in html for token in content.sensitive_source_tokens if token)
    _add(violations, "html_internal_source_value_exposed", leaked_tokens)
    state.internal_source_exposure_count += leaked_tokens

    stats = _read_stats(_stats_path(config.html), violations, snapshots)
    stats_manifest_matches = False
    stats_build_inputs_matches = False
    stats_data_matches = False
    if stats is not None:
        stats_clients = stats.get("clients")
        stats_docs = stats.get("docs")
        stats_bytes = stats.get("bytes")
        if _is_int(stats_clients):
            state.stats_client_count = cast(int, stats_clients)
        else:
            _add(violations, "stats_schema_invalid")
        if _is_int(stats_docs):
            state.stats_doc_count = cast(int, stats_docs)
        else:
            _add(violations, "stats_schema_invalid")
        if _is_int(stats_bytes):
            state.stats_byte_count = cast(int, stats_bytes)
        else:
            _add(violations, "stats_schema_invalid")
        if state.stats_client_count != state.client_count:
            _add(violations, "stats_client_count_mismatch")
        if state.stats_doc_count != state.doc_count:
            _add(violations, "stats_doc_count_mismatch")
        if state.stats_byte_count != state.byte_count:
            _add(violations, "stats_byte_count_mismatch")
        stats_manifest = stats.get("manifest_sha256")
        if not isinstance(stats_manifest, str) or _SHA256_RE.fullmatch(stats_manifest) is None:
            _add(violations, "stats_manifest_hash_invalid")
        elif stats_manifest != manifest.sha256:
            _add(violations, "stats_manifest_hash_mismatch")
        else:
            stats_manifest_matches = True

        stats_build_inputs = stats.get("build_inputs_sha256")
        if (
            not isinstance(stats_build_inputs, str)
            or _SHA256_RE.fullmatch(stats_build_inputs) is None
        ):
            _add(violations, "stats_build_inputs_hash_invalid")
        elif stats_build_inputs != sidecars.build_inputs_sha256:
            _add(violations, "stats_build_inputs_hash_mismatch")
        else:
            stats_build_inputs_matches = True

        stats_data = stats.get("data_sha256")
        if not isinstance(stats_data, str) or _SHA256_RE.fullmatch(stats_data) is None:
            _add(violations, "stats_data_hash_invalid")
        elif stats_data != data_sha256:
            _add(violations, "stats_data_hash_mismatch")
        else:
            stats_data_matches = data_sha256 != _ZERO_SHA256

    if payload_stats is not None:
        for key, expected_count in (
            ("clients", state.client_count),
            ("docs", state.doc_count),
            ("reports", state.report_count),
        ):
            value = payload_stats.get(key)
            if not _is_int(value) or value != expected_count:
                _add(violations, "html_data_stats_mismatch")
        payload_stats_manifest = payload_stats.get("manifest_sha256")
        if (
            not isinstance(payload_stats_manifest, str)
            or _SHA256_RE.fullmatch(payload_stats_manifest) is None
        ):
            _add(violations, "html_data_stats_manifest_hash_invalid")
        elif payload_stats_manifest != manifest.sha256:
            _add(violations, "html_data_stats_manifest_hash_mismatch")
        else:
            payload_stats_manifest_matches = True

        payload_stats_build_inputs = payload_stats.get("build_inputs_sha256")
        if (
            not isinstance(payload_stats_build_inputs, str)
            or _SHA256_RE.fullmatch(payload_stats_build_inputs) is None
        ):
            _add(violations, "html_data_stats_build_inputs_hash_invalid")
        elif payload_stats_build_inputs != sidecars.build_inputs_sha256:
            _add(violations, "html_data_stats_build_inputs_hash_mismatch")
        else:
            payload_stats_build_inputs_matches = True

    state.manifest_bound = (
        manifest.sha256 != _ZERO_SHA256
        and data_manifest_matches
        and payload_stats_manifest_matches
        and stats_manifest_matches
    )
    state.build_inputs_bound = (
        manifest.sha256 != _ZERO_SHA256
        and sidecars.build_inputs_sha256 != _ZERO_SHA256
        and data_build_inputs_matches
        and payload_stats_build_inputs_matches
        and stats_build_inputs_matches
    )
    state.data_bound = stats_data_matches

    status_functions = _STATUS_FUNCTION_RE.findall(script_tail)
    if len(status_functions) != 1:
        _add(violations, "html_status_function_invalid", abs(len(status_functions) - 1) or 1)
        footer_matches: list[tuple[str, str, str]] = []
    else:
        status_body = status_functions[0]
        structurally_valid = (
            '$("#statusbar").innerHTML=' in status_body
            and "DATA.stats.clients" in status_body
            and "DATA.stats.docs" in status_body
        )
        footer_matches = _FOOTER_RE.findall(status_body)
        structurally_valid = structurally_valid and len(footer_matches) == 1
        if len(footer_matches) == 1:
            stamp = _FOOTER_RE.search(status_body)
            assert stamp is not None
            structurally_valid = (
                structurally_valid
                and re.search(
                    rf"<span>{re.escape(stamp.group(0))}</span>'\s*;\s*$",
                    status_body,
                )
                is not None
            )
        if not structurally_valid:
            _add(violations, "html_status_function_invalid")

    state.footer_count = len(footer_matches)
    if len(footer_matches) != 1:
        _add(violations, "html_footer_invalid", abs(len(footer_matches) - 1) or 1)
    else:
        footer_date, footer_clients_raw, footer_docs_raw = footer_matches[0]
        if int(footer_clients_raw) != state.client_count:
            _add(violations, "html_footer_client_count_mismatch")
        if int(footer_docs_raw) != state.doc_count:
            _add(violations, "html_footer_doc_count_mismatch")
        if stats is not None:
            built_at = stats.get("built_at")
            try:
                built_date = datetime.fromisoformat(cast(str, built_at)).date().isoformat()
            except (TypeError, ValueError):
                _add(violations, "stats_built_at_invalid")
            else:
                if built_date != footer_date:
                    _add(violations, "html_footer_date_mismatch")
    return state


def _safe_result(
    manifest: ManifestState,
    manifest_end_sha: str,
    manifest_unchanged: bool,
    sidecars: SidecarState,
    content: ContentState,
    html: HtmlState,
    violations: Counter[str],
) -> dict[str, object]:
    clean_violations = {key: count for key, count in sorted(violations.items()) if count > 0}
    return {
        "schema_version": 1,
        "ok": not clean_violations,
        "manifest": {
            "ok": not any(key.startswith("manifest_") for key in clean_violations),
            "sha256": manifest.sha256,
            "end_sha256": manifest_end_sha,
            "unchanged": manifest_unchanged,
            "file_count": manifest.file_count,
            "active_count": manifest.active_count,
            "active_client_count": manifest.client_count,
            "active_doc_count": manifest.doc_count,
        },
        "sidecars": {
            "ok": not any(key.startswith(("sidecar_", "font_")) for key in clean_violations),
            "json_duplicate_key_count": sidecars.json_duplicate_keys,
            "list_duplicate_value_count": sidecars.list_duplicate_values,
            "stem_rule_count": len(sidecars.exclude_stems),
            "source_rule_count": len(sidecars.exclude_source_keys),
            "dedup_rule_count": len(sidecars.dedup_drop),
            "rename_rule_count": len(sidecars.rename),
            "font_valid": sidecars.font_valid,
            "font_bytes": sidecars.font_bytes,
            "font_sha256": sidecars.font_sha256,
            "build_inputs_sha256": sidecars.build_inputs_sha256,
            "stem_applied_count": content.stem_exclusion_applied_count,
            "source_applied_count": content.source_exclusion_applied_count,
            "dedup_applied_count": content.dedup_applied_count,
            "chunk_fold_applied_count": content.chunk_fold_applied_count,
            "invoice_rule_applied_count": content.invoice_rule_applied_count,
            "rename_applied_count": content.rename_applied_count,
            "alias_applied_count": content.alias_applied_count,
        },
        "gsheets": {
            "ok": not any(key.startswith("gsheets_") for key in clean_violations),
            "count": content.gsheets_count,
            "missing_id_count": content.missing_id_count,
            "malformed_id_count": content.malformed_id_count,
            "duplicate_id_count": content.duplicate_id_count,
            "empty_title_count": content.empty_title_count,
            "empty_excerpt_count": content.empty_excerpt_count,
            "empty_fingerprint_count": content.empty_fingerprint_count,
            "duplicate_fingerprint_count": content.duplicate_fingerprint_count,
            "junk_excluded_count": content.junk_excluded_count,
            "junk_unexcluded_count": content.junk_unexcluded_count,
            "ambiguous_title_count": content.ambiguous_title_count,
            "rename_applied_count": content.gsheets_rename_applied_count,
            "rename_missing_count": content.rename_missing_count,
        },
        "client_timelines": {
            "ok": not any(
                key.startswith(("client_", "html_client_timeline_")) for key in clean_violations
            ),
            "declared_fb_count": content.client_declared_fb_count,
            "source_heading_count": content.client_timeline_heading_count,
            "source_count_mismatch": content.client_timeline_count_mismatch,
            "source_section_missing": content.client_timeline_section_missing,
            "source_fb_count_invalid": content.client_fb_count_invalid,
            "payload_fb_count": html.client_fb_count,
            "payload_event_count": html.timeline_event_count,
            "payload_missing_count": html.client_timeline_missing_count,
            "payload_schema_invalid_count": html.client_timeline_schema_invalid_count,
        },
        "html": {
            "ok": not any(key.startswith(("html_", "stats_")) for key in clean_violations),
            "sha256": html.sha256,
            "bytes": html.byte_count,
            "client_count": html.client_count,
            "doc_count": html.doc_count,
            "report_count": html.report_count,
            "stats_client_count": html.stats_client_count,
            "stats_doc_count": html.stats_doc_count,
            "stats_byte_count": html.stats_byte_count,
            "footer_count": html.footer_count,
            "manifest_bound": html.manifest_bound,
            "build_inputs_bound": html.build_inputs_bound,
            "data_bound": html.data_bound,
            "font_bound": html.font_bound,
            "rename_applied_count": html.payload_rename_applied_count,
            "internal_source_exposure_count": html.internal_source_exposure_count,
        },
        "violations": clean_violations,
    }


def run_qa(config: QAConfig) -> dict[str, object]:
    """Run a read-only QA pass and return a PII-safe aggregate result."""
    violations: Counter[str] = Counter()
    snapshots = SnapshotTracker()
    manifest = _load_manifest(config, violations, snapshots)
    sidecars = _load_sidecars(config.sidecar_dir, manifest.sha256, violations, snapshots)
    content = _analyze_content(manifest, sidecars, violations)
    html = _analyze_html(config, manifest, sidecars, content, violations, snapshots)
    snapshots.verify(violations)
    manifest_end_sha = snapshots.end_hash(config.vault / _EXPORT_MANIFEST_NAME)
    unchanged = manifest.sha256 != _ZERO_SHA256 and manifest_end_sha == manifest.sha256
    if manifest.sha256 != _ZERO_SHA256 and not unchanged:
        _add(violations, "manifest_changed_during_qa")
    return _safe_result(
        manifest,
        manifest_end_sha,
        unchanged,
        sidecars,
        content,
        html,
        violations,
    )


class _CLIArgumentError(Exception):
    """A deliberately detail-free CLI parse failure."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _CLIArgumentError


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = _SafeArgumentParser(
        description="Read-only connect-web artifact QA with aggregate-only JSON output"
    )
    parser.add_argument("--vault", default=str(_DEFAULT_VAULT))
    parser.add_argument("--html", default=str(_DEFAULT_HTML))
    parser.add_argument("--sidecar-dir", default=str(_DEFAULT_SIDECAR_DIR))
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-html-sha256")
    return parser.parse_args(argv)


def _failure_result(kind: str) -> dict[str, object]:
    """Return a fixed safe shape without serializing exception details."""
    zero_manifest = ManifestState()
    zero_sidecars = SidecarState()
    zero_content = ContentState()
    zero_html = HtmlState()
    violations: Counter[str] = Counter({kind: 1})
    return _safe_result(
        zero_manifest,
        _ZERO_SHA256,
        False,
        zero_sidecars,
        zero_content,
        zero_html,
        violations,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        result = run_qa(
            QAConfig(
                vault=Path(args.vault).expanduser(),
                html=Path(args.html).expanduser(),
                sidecar_dir=Path(args.sidecar_dir).expanduser(),
                expected_manifest_sha256=args.expected_manifest_sha256,
                expected_html_sha256=args.expected_html_sha256,
            )
        )
    except _CLIArgumentError:
        result = _failure_result("cli_argument_error")
    except Exception:  # exception details must never leak PII
        result = _failure_result("internal_error")
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
