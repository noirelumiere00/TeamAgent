#!/usr/bin/env python3
"""Remove signed, out-of-scope secret-bearing extractors from vendored yt-dlp."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

REMOVED_EXTRACTORS = {
    "adultswim": "69f8279b21e2e697f277a2ffb6c2e8f42e2e7a520ab5441c699d1e674254be90",
    "aenetworks": "2c16a7a383d41edb7c5b5278971425729b213ace213e0761a44ae360fe5a2e2c",
    "blackboardcollaborate": ("c16d92311be21faf5a23cec365c942e1acdda5ac4afa14406d4b772f79ffa607"),
    "cloudflarestream": ("261f2f26747d181c2fee01a5f065b12d02b055828c99dbdd8261b67193c29a81"),
    "espn": "c5fd3174b057e8471c9df4cd674c9a69658870a70ddcae35a29dc3c7a5486f77",
    "go": "f6dd8e584fd33a3412854fc0288513e21e8079071e77bd97031d2a1635b1c917",
    "nbc": "830344cb2e2e7c05eb0ea1d07f60118f2c672a99f35036109e0ca36c6d3b3fc1",
    "shahid": "f82c1f065f6aa3dd5ce8ee3491d4c49f245d1e7ba921b8cc0cc9c8658a634fbd",
    "tbs": "4008e7a3576cde5ce3bfc453ac2286816ab10ac3abc5f43823a59396730259b7",
    "vice": "d43235315b016e81161ba1a4132c3d84358c905482307c325837c6ec9ec58246",
}
ALLOWLISTED_EXTRACTORS = {
    "youtube": "extractor/youtube/__init__.py",
    "tiktok": "extractor/tiktok.py",
    "instagram": "extractor/instagram.py",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: expected one exact match, got {text.count(old)}")
    return text.replace(old, new)


def source_tree_digest(root: Path) -> str:
    checksum = hashlib.sha256()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item.suffix != ".pyc" and "__pycache__" not in item.parts
    ):
        checksum.update(path.relative_to(root).as_posix().encode("utf-8"))
        checksum.update(b"\0")
        checksum.update(path.read_bytes())
        checksum.update(b"\0")
    return checksum.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--expected-shahid-sha256", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    extractor = args.package_root / "extractor"
    lazy = extractor / "lazy_extractors.py"
    registry = extractor / "_extractors.py"
    expected = args.expected_shahid_sha256
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("expected Shahid hash must be lowercase SHA-256")
    if expected != REMOVED_EXTRACTORS["shahid"]:
        raise RuntimeError("Docker Shahid hash and sanitizer contract disagree")
    for name, expected_hash in REMOVED_EXTRACTORS.items():
        source = extractor / f"{name}.py"
        if not source.is_file() or digest(source) != expected_hash:
            raise RuntimeError(f"yt-dlp {name} source hash does not match the signed contract")

    registry_text = replace_once(
        registry.read_text(encoding="utf-8"),
        "from .shahid import (\n    ShahidIE,\n    ShahidShowIE,\n)\n",
        "",
        label="_extractors Shahid import",
    )
    registry.write_text(registry_text, encoding="utf-8")

    lazy_text = lazy.read_text(encoding="utf-8")
    lazy_text, count = re.subn(
        r"\nclass ShahidBaseIE\(AWSIE\):.*?(?=\nclass SharePointIE\()",
        "\n",
        lazy_text,
        count=1,
        flags=re.DOTALL,
    )
    if count != 1:
        raise RuntimeError("lazy extractor Shahid class block did not match exactly once")
    for name in ("ShahidIE", "ShahidShowIE"):
        lazy_text = replace_once(
            lazy_text,
            f"'{name}': {name}, ",
            "",
            label=f"lazy extractor lookup {name}",
        )
    lazy.write_text(lazy_text, encoding="utf-8")

    removed_compiled: dict[str, str] = {}
    for name in REMOVED_EXTRACTORS:
        (extractor / f"{name}.py").unlink()
        for compiled in extractor.rglob(f"{name}*.pyc"):
            relative = compiled.relative_to(args.package_root).as_posix()
            removed_compiled[relative] = digest(compiled)
            compiled.unlink()
        if (extractor / f"{name}.py").exists() or any(extractor.rglob(f"{name}*.pyc")):
            raise RuntimeError(f"source or compiled {name} extractor remains")
    for name, relative_path in ALLOWLISTED_EXTRACTORS.items():
        if not (args.package_root / relative_path).is_file():
            raise RuntimeError(f"allowlisted {name} extractor was removed")

    manifest = {
        "schema_version": 1,
        "action": "remove-out-of-scope-secret-bearing-extractors",
        "allowlisted_extractors": sorted(ALLOWLISTED_EXTRACTORS),
        "removed": {
            f"yt_dlp/extractor/{name}.py": expected_hash
            for name, expected_hash in REMOVED_EXTRACTORS.items()
        },
        "removed_compiled": removed_compiled,
        "removed_extractor_set_sha256": hashlib.sha256(
            json.dumps(
                REMOVED_EXTRACTORS,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "modified": {
            "yt_dlp/extractor/_extractors.py": digest(registry),
            "yt_dlp/extractor/lazy_extractors.py": digest(lazy),
        },
        "sanitized_source_tree_sha256": source_tree_digest(args.package_root),
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
