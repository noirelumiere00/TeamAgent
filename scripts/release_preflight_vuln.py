#!/usr/bin/env python3
"""Public-registry-only release vulnerability preflight (0=clear, 1=blocked, 2=unknown)."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Reuse the release gate's strict policy loader, including expiry and duplicate checks.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infra.codebuild.verify_ecr_scan import (
    FindingKey,
    load_exceptions,
)

DB_REPOSITORY = "ghcr.io/aquasecurity/trivy-db:2"
DEFAULT_APKWATCH = Path.home() / "Documents/Claude/Artifacts/aico-inputs-20260911/apkwatch"
SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}


@dataclass(frozen=True)
class Blocker:
    gate: str
    item: str
    fix: str


def evaluate_report(
    report: Mapping[str, Any],
    gate: str,
    expected_type: str,
    *,
    policy: Path | None = None,
) -> list[Blocker]:
    """Evaluate scanner output; an empty/malformed scan must never turn green."""
    results = report.get("Results")
    if (
        report.get("SchemaVersion") != 2
        or not isinstance(results, list)
        or any(not isinstance(r, dict) for r in results)
    ):
        raise ValueError(f"{gate}: incomplete Trivy JSON")
    relevant = [r for r in results if r.get("Type") == expected_type]
    if not relevant or not any(r.get("Packages") for r in relevant):
        raise ValueError(f"{gate}: no {expected_type} package inventory detected")
    exceptions = (
        load_exceptions(policy, today=datetime.now(UTC).date()) if policy is not None else {}
    )
    observed: set[FindingKey] = set()
    blockers: set[Blocker] = set()
    for result in relevant:
        vulnerabilities = result.get("Vulnerabilities")
        if vulnerabilities is None:
            vulnerabilities = []
        if not isinstance(vulnerabilities, list) or any(
            not isinstance(v, dict) for v in vulnerabilities
        ):
            raise ValueError(f"{gate}: malformed vulnerability list")
        for vuln in vulnerabilities:
            values = [
                vuln.get(k) for k in ("VulnerabilityID", "Severity", "PkgName", "InstalledVersion")
            ]
            if any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError(f"{gate}: malformed vulnerability")
            key = FindingKey(*values)
            if key.severity not in SEVERITIES:
                raise ValueError(f"{gate}: unsupported severity {key.severity}")
            observed.add(key)
            threshold = {"MEDIUM", "HIGH", "CRITICAL"} if policy else {"HIGH", "CRITICAL"}
            if key.severity not in threshold or key in exceptions:
                continue
            fixed = vuln.get("FixedVersion", "")
            if fixed is None:
                fixed = ""
            if not isinstance(fixed, str):
                raise ValueError(f"{gate}: invalid FixedVersion")
            # CI fs uses --ignore-unfixed; the OS/ECR prediction must retain unfixed findings.
            if gate == "uv.lock" and not fixed.strip():
                continue
            fix = f"修正版候補: {fixed}" if fixed.strip() else "修正版未公開（上流対応を確認）"
            blockers.add(Blocker(gate, key.display(), fix))
    for key in exceptions.keys() - observed:
        blockers.add(Blocker(gate, f"stale: {key.display()}", "例外を再確認して削除"))
    return sorted(blockers, key=lambda b: (b.gate, b.item, b.fix))


def run(command: Sequence[str], *, env: dict[str, str], cwd: Path, log: Path) -> str:
    """Keep complete diagnostics on disk, including timeouts and nonzero exits."""
    with log.open("w") as output:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"{command[0]}: {exc}; log={log}") from exc
    if completed.returncode:
        lines = log.read_text().splitlines()
        detail = lines[-1] if lines else "no output"
        raise RuntimeError(f"{command[0]} exit={completed.returncode}: {detail}; log={log}")
    return log.read_text()


def scan(
    target: Path,
    mode: str,
    name: str,
    *,
    env: dict[str, str],
    work: Path,
    ignorefile: Path | None = None,
) -> dict[str, Any]:
    command = [
        "trivy",
        mode,
        "--config",
        str(work / "trivy.yaml"),
        "--cache-dir",
        env["TRIVY_CACHE_DIR"],
        "--db-repository",
        DB_REPOSITORY,
        "--skip-db-update",
        "--skip-java-db-update",
        "--skip-check-update",
        "--skip-vex-repo-update",
        "--skip-version-check",
        "--offline-scan",
        "--scanners",
        "vuln",
        "--pkg-types",
        "os" if mode == "rootfs" else "library",
        "--list-all-pkgs",
        "--format",
        "json",
        "--exit-code",
        "0",
        "--ignorefile",
        str(ignorefile or work / "empty-ignore"),
        "--output",
        str(work / f"{name}.json"),
        str(target),
    ]
    run(command, env=env, cwd=work, log=work / f"{name}.log")
    payload: dict[str, Any] = json.loads((work / f"{name}.json").read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"{name}: invalid Trivy report")
    if mode == "fs":
        expected = {str(p.relative_to(target)) for p in target.rglob("package-lock.json")}
        if (target / "uv.lock").is_file():
            expected.add("uv.lock")
        results = payload.get("Results")
        if not isinstance(results, list) or any(
            not isinstance(r, dict) or not isinstance(r.get("Target"), str) for r in results
        ):
            raise ValueError(f"{name}: invalid lock scan results")
        actual = {r["Target"] for r in results}
        if missing := expected - actual:
            raise ValueError(f"{name}: missing lock inventories: {sorted(missing)}")
    return payload


def core_dockerfile(source: str) -> str:
    """Run the actual offline APK replacement block, without app/model/AWS stages."""
    header = source.split("\nFROM ", 1)[0]
    runtime = re.search(r"^FROM (cgr\.dev/chainguard/python:[^\s]+) AS runtime-base$", source, re.M)
    builder = re.search(r"^FROM (cgr\.dev/chainguard/python:[^\s]+) AS builder$", source, re.M)
    start = "COPY --from=runtime-base / /tmp/core-ca/root/"
    end = "\nENV UV_COMPILE_BYTECODE="
    if not runtime or not builder or source.count(start) != 1 or source.count(end) != 1:
        raise ValueError("core Dockerfile structure changed; update preflight extraction")
    block = source.split(start, 1)[1].split(end, 1)[0]
    args = re.findall(r"^ARG (\w+)=", header, re.M)
    return (
        header
        + f"\nFROM {runtime[1]} AS runtime-base\nFROM {builder[1]} AS builder\nUSER 0\n"
        + "".join(f"ARG {arg}\n" for arg in args)
        + start
        + block
        + "\nFROM scratch\nCOPY --from=builder /tmp/core-ca/root/ /\n"
    )


def prepare_core(repo: Path, work: Path, env: dict[str, str]) -> Path:
    context = work / "core-context"
    docker = context / "infra/docker"
    docker.mkdir(parents=True)
    source = (repo / "infra/docker/Dockerfile.teamagent-mcp").read_text()
    recipe = core_dockerfile(source)
    (context / "Dockerfile").write_text(recipe)
    # Copy only the explicit inputs, never the repository's .env or credentials.
    for line in recipe.splitlines():
        if line.startswith("COPY infra/docker/"):
            for relative in line.split()[1:-1]:
                target = context / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(repo / relative, target)
    rootfs = work / "core-rootfs"
    run(
        [
            "docker",
            "buildx",
            "build",
            "--platform",
            "linux/arm64",
            "--progress",
            "plain",
            "--output",
            f"type=local,dest={rootfs}",
            str(context),
        ],
        env=env,
        cwd=work,
        log=work / "core-build.log",
    )
    return rootfs


class PublicRegistryRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        check_registry_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_registry_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme, parsed.hostname, parsed.port) != (
        "https",
        "registry.npmjs.org",
        None,
    ) or parsed.username is not None:
        raise ValueError(f"non-public npm registry URL: {url}")


def fetch_registry(url: str) -> bytes:
    check_registry_url(url)
    ca = os.environ.get("SSL_CERT_FILE")
    default_ca = Path.home() / ".hermes/ca_bundle.pem"
    if ca is None and default_ca.is_file():
        ca = str(default_ca)
    opener = urllib.request.build_opener(
        PublicRegistryRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca)),
    )
    with opener.open(url, timeout=120) as response:
        return bytes(response.read())


def tar_member(raw: bytes, name: str) -> bytes:
    # Only read named metadata; never extract untrusted paths or execute npm scripts.
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        matches = [m for m in archive.getmembers() if m.name == f"package/{name}"]
        if len(matches) != 1 or not matches[0].isfile():
            raise ValueError(f"npm tarball missing/duplicate metadata: {name}")
        member = archive.extractfile(matches[0])
        assert member is not None
        return member.read()


def npm_patches(source: str) -> dict[str, tuple[str, str]]:
    specs = re.findall(r"^\s+npm pack (\S+) --ignore-scripts --pack-destination", source, re.M)
    patches: dict[str, tuple[str, str]] = {}
    for spec in specs:
        name, version = spec.rsplit("@", 1)
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.-]+)?", version):
            raise ValueError(f"npm pack must pin an exact version: {spec}")
        archive = f"{name.lstrip('@').replace('/', '-')}-{version}.tgz"
        if archive in patches:
            raise ValueError(f"duplicate npm pack: {spec}")
        patches[archive] = (name, version)
    if not patches:
        raise ValueError("no npm pack replacements detected")
    return patches


def patch_plugin_lock(
    lock: dict[str, Any],
    plugin_id: str,
    source: str,
    patches: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    packages = lock["packages"]
    if lock.get("lockfileVersion") not in (2, 3) or not packages:
        raise ValueError("unsupported/empty plugin shrinkwrap")
    destinations = re.findall(
        r'tar -xzf "\$patch_dir/([^"/]+)" --strip-components=1 -C (\S+);',
        source,
    )
    for archive, destination in destinations:
        if archive not in patches:
            raise ValueError(f"unrecognized patch archive {archive}")
        prefix = f"/opt/teamagent/plugins/{plugin_id}/"
        if not destination.startswith(prefix):
            continue
        relative = destination.removeprefix(prefix)
        # rm -rf of a replaced package also removes its private dependency tree.
        for path in list(packages):
            if path.startswith(relative + "/"):
                del packages[path]
        packages[relative] = patches[archive]
    return lock


def prepare_openclaw(repo: Path, work: Path) -> Path:
    source = (repo / "infra/docker/Dockerfile.openclaw").read_text()
    patches: dict[str, dict[str, Any]] = {}
    target = work / "openclaw"
    target.mkdir()
    for archive, (name, version) in npm_patches(source).items():
        url = f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='')}/{version}"
        metadata = json.loads(fetch_registry(url))
        raw = fetch_registry(metadata["dist"]["tarball"])
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode()
        if integrity != metadata["dist"]["integrity"]:
            raise ValueError(f"npm integrity mismatch: {name}@{version}")
        package = json.loads(tar_member(raw, "package.json"))
        if (package["name"], package["version"]) != (name, version):
            raise ValueError(f"npm identity mismatch: {name}@{version}")
        patches[archive] = {
            k: package[k]
            for k in ("name", "version", "dependencies", "optionalDependencies")
            if k in package
        }
    # A lock inventory scans the exact npm-pack versions, without resolving version ranges.
    inventory = {f"node_modules/{p['name']}": p for p in patches.values()}
    (target / "patches").mkdir()
    (target / "patches/package-lock.json").write_text(
        json.dumps(
            {
                "name": "release-preflight-patches",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "release-preflight-patches",
                        "version": "1.0.0",
                        "dependencies": {p["name"]: p["version"] for p in patches.values()},
                    },
                    **inventory,
                },
            }
        )
    )
    plugins = json.loads((repo / "infra/openclaw/plugins-lock.json").read_text())["plugins"]
    if not plugins:
        raise ValueError("empty plugin registry")
    for plugin in plugins:
        if not re.fullmatch(r"[a-z0-9-]+", plugin["id"]):
            raise ValueError("invalid plugin id")
        if plugin["tarball"] not in source or f"sha256:{plugin['sha256']}" not in source:
            raise ValueError(f"plugin registry/Dockerfile mismatch: {plugin['id']}")
        raw = fetch_registry(plugin["tarball"])
        if hashlib.sha256(raw).hexdigest() != plugin["sha256"]:
            raise ValueError(f"plugin archive checksum mismatch: {plugin['id']}")
        shrinkwrap = tar_member(raw, "npm-shrinkwrap.json")
        if hashlib.sha256(shrinkwrap).hexdigest() != plugin["shrinkwrapSha256"]:
            raise ValueError(f"plugin shrinkwrap checksum mismatch: {plugin['id']}")
        lock = patch_plugin_lock(json.loads(shrinkwrap), plugin["id"], source, patches)
        directory = target / plugin["id"]
        directory.mkdir()
        (directory / "package-lock.json").write_text(json.dumps(lock))
    return target


def check_media(repo: Path, apkwatch: Path, work: Path, env: dict[str, str]) -> list[Blocker]:
    """Adapt prefire to the worktree and public indices; exclude its ECR shadow build."""
    directory = work / "apkwatch"
    directory.mkdir()
    source = (apkwatch / "prefire_apk_check.sh").read_text()
    for old, new in (
        (' --ref "$REF"', ""),
        ("if docker info >/dev/null 2>&1; then", "if false; then"),
        (
            "WARN: Docker が起動していないので apk 段の実ビルドは省略（索引での確認は OK）。確定させたいなら open -a Docker のあと撃ち直す",
            "WARN: 公開索引のみを確認。ECR を使う shadow build は実行しない",
        ),
    ):
        if source.count(old) != 1:
            raise ValueError("prefire_apk_check.sh changed; review public-only adapter")
        source = source.replace(old, new)
    script = directory / "prefire_apk_check.sh"
    script.write_text(source)
    shutil.copyfile(apkwatch / "check_media_apk_pins.py", directory / "check_media_apk_pins.py")
    for path in apkwatch.glob("base-installed-*.lock"):
        shutil.copyfile(path, directory / path.name)
    log = work / "media.log"
    try:
        output = run(["bash", str(script), str(repo)], env=env, cwd=work, log=log)
    except RuntimeError:
        output = log.read_text()
        if "DRIFT:" not in output:
            raise
    return [
        Blocker(
            "media apk",
            line.strip().removeprefix("- "),
            "apkwatch/apply_apklock.sh で pin/lock を再導出（上流版は左記）",
        )
        for line in output.splitlines()
        if line.startswith("  - ")
    ]


def print_table(rows: list[Blocker]) -> None:
    print("| 門 | 便が止まる見込みの項目 | 直し方の候補 |")
    print("| --- | --- | --- |")
    for row in rows:
        cells = [s.replace("|", "\\|").replace("\n", " ") for s in (row.gate, row.item, row.fix)]
        print("| " + " | ".join(cells) + " |")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apkwatch", type=Path, default=DEFAULT_APKWATCH)
    parser.add_argument("--output-dir", type=Path, help="保存先（省略時は /tmp に新規作成）")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    work = (
        args.output_dir.resolve()
        if args.output_dir
        else Path(tempfile.mkdtemp(prefix="release-preflight-"))
    )
    work.mkdir(parents=True, exist_ok=True)
    home = work / "home"
    home.mkdir(exist_ok=True)
    docker_config = home / ".docker"
    docker_config.mkdir(exist_ok=True)
    # A nonempty anonymous auth map prevents Docker's automatic Keychain discovery.
    (docker_config / "config.json").write_text(
        json.dumps(
            {
                "auths": {"https://index.docker.io/v1/": {}},
                "cliPluginsExtraDirs": [
                    str(Path.home() / ".docker/cli-plugins"),
                    "/Applications/Docker.app/Contents/Resources/cli-plugins",
                ],
            }
        )
    )
    (work / "trivy.yaml").write_text("{}\n")
    (work / "empty-ignore").write_text("")
    cache = repo / ".cache/release-preflight/trivy"
    cache.mkdir(parents=True, exist_ok=True)
    # Do not inherit AWS credentials, npm auth, Trivy overrides, or user config.
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "SSL_CERT_FILE") if key in os.environ}
    ca = Path.home() / ".hermes/ca_bundle.pem"
    if "SSL_CERT_FILE" not in env and ca.is_file():
        env["SSL_CERT_FILE"] = str(ca)
    env.update(
        {
            "HOME": str(home),
            "DOCKER_CONFIG": str(home / ".docker"),
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "TRIVY_CACHE_DIR": str(cache),
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )
    blockers: list[Blocker] = []
    unknown: list[Blocker] = []
    try:
        run(
            [
                "trivy",
                "image",
                "--config",
                str(work / "trivy.yaml"),
                "--db-repository",
                DB_REPOSITORY,
                "--download-db-only",
                "--skip-version-check",
            ],
            env=env,
            cwd=work,
            log=work / "db.log",
        )
        db_ready = True
    except RuntimeError as exc:
        unknown.append(Blocker("Trivy DB", str(exc), "GHCR/TLS 接続を直して再実行"))
        db_ready = False
    checks = ["core OS", "uv.lock", "OpenClaw", "media apk"]
    for gate in checks:
        print(f"確認中: {gate}（証跡: {work}）", file=sys.stderr, flush=True)
        try:
            if gate == "media apk":
                blockers.extend(check_media(repo, args.apkwatch.resolve(), work, env))
            elif not db_ready:
                unknown.append(Blocker(gate, "DB 更新失敗のため未検査", "DB 接続復旧後に再実行"))
            elif gate == "core OS":
                rootfs = prepare_core(repo, work, env)
                blockers.extend(
                    evaluate_report(
                        scan(rootfs, "rootfs", "core", env=env, work=work),
                        gate,
                        "wolfi",
                        policy=repo / "infra/codebuild/ecr_scan_exceptions_core.json",
                    )
                )
            elif gate == "uv.lock":
                target = work / "python"
                target.mkdir()
                shutil.copyfile(repo / "uv.lock", target / "uv.lock")
                blockers.extend(
                    evaluate_report(
                        scan(
                            target,
                            "fs",
                            "uv",
                            env=env,
                            work=work,
                            ignorefile=repo / ".trivyignore.yaml",
                        ),
                        gate,
                        "uv",
                    )
                )
            else:
                target = prepare_openclaw(repo, work)
                blockers.extend(
                    evaluate_report(
                        scan(target, "fs", "openclaw", env=env, work=work),
                        gate,
                        "npm",
                    )
                )
        except (OSError, ValueError, RuntimeError, KeyError, tarfile.TarError) as exc:
            unknown.append(Blocker(gate, f"確認不能: {exc}", "証跡ログ/入力を確認して再実行"))
    print_table(blockers + unknown)
    print(f"停止見込み {len(blockers)} 件 / 確認不能 {len(unknown)} 件 / 証跡: {work}")
    return 2 if unknown else int(bool(blockers))


if __name__ == "__main__":
    raise SystemExit(main())
