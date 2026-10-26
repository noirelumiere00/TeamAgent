"""teamagent-mcp-core の再現可能なruntime分離契約。"""

from __future__ import annotations

import hashlib
import re
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra/docker/Dockerfile.teamagent-mcp"
DOCKERIGNORE = ROOT / "infra/docker/Dockerfile.teamagent-mcp.dockerignore"
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "uv.lock"
TEXT = DOCKERFILE.read_text(encoding="utf-8")

PYTHON_BUILDER_DIGEST = "2c8414a1ab05655dc3a88e88f4cac4fde9db3342daab7772f1b45f2b937b2fd5"
PYTHON_RUNTIME_DIGEST = "f9552d99b5fcf870d9cd1d285e0086ae90aa2f0758aeff8d1f80dc9346fb52c5"
UV_DIGEST = "9941e2d8e06ff884d328905091eac0a6bc1e40e5ce12e6dd0de4ef4ee26baac4"
PYTHON_BINARY_SHA256 = "775bf454e5816503fe56f81c9b9e0d3f03790694bdce576cbbefd60971fc88ca"
UV_BINARY_SHA256 = "f32f61ced7feb20342032cdac4d0825cebbda61911554f5de5231ec72821812e"
TORCH_WHEEL_SHA256 = "ca021f9eb2f8345c83fa03e3a04587308afb8df71bd472670b3ece00df58621c"
CA_BUNDLE_APK_SHA256 = "4f57ebd7dc21d78ecce228f397d0a4ee46b5cfc777ac5f292821d41a97bfd896"
E5_MODEL_REVISION = "3d7cfbdacd47fdda877c5cd8a79fbcc4f2a574f3"
BAKED_APP_HTML_SHA256 = "716ac25a96516efd6443277c903102d514f3f86729f8706baea41ee48f0ecdeb"
APP_HTML_SHA256 = "fd62e56e51d3cee25c3d3a87085048fae08ce3babc82480411c61df36e467e28"
APP_HTML_VERSION_ID = "D5zzWNc44db5i5lz1DxeRxAkmnf1x8jZ"
APP_HTML_MANIFEST_SHA256 = "1a2834b62a12c60c31ee874820a3a5cfccafb7960193047a17abd426c890568a"
APP_HTML_BUILD_INPUTS_SHA256 = "28ea06cf04cf5d0774ea3e9635cb7dfcfcc2985e49160521cee412431cf38bba"


def _stage(name: str, next_name: str | None = None) -> str:
    start = re.search(rf"^FROM .+ AS {re.escape(name)}$", TEXT, re.MULTILINE)
    assert start is not None, f"missing Docker stage: {name}"
    if next_name is None:
        return TEXT[start.start() :]
    tail = TEXT[start.end() :]
    end = re.search(rf"^FROM .+ AS {re.escape(next_name)}$", tail, re.MULTILINE)
    assert end is not None, f"missing Docker stage after {name}: {next_name}"
    return TEXT[start.start() : start.end() + end.start()]


def test_core_uses_exact_arm64_child_digests_and_binary_hashes() -> None:
    assert f"ARG PYTHON_BUILDER_ARM64_DIGEST=sha256:{PYTHON_BUILDER_DIGEST}" in TEXT
    assert f"ARG PYTHON_RUNTIME_ARM64_DIGEST=sha256:{PYTHON_RUNTIME_DIGEST}" in TEXT
    assert f"ARG UV_ARM64_DIGEST=sha256:{UV_DIGEST}" in TEXT
    assert f"ARG PYTHON_BINARY_SHA256={PYTHON_BINARY_SHA256}" in TEXT
    assert f"ARG UV_BINARY_SHA256={UV_BINARY_SHA256}" in TEXT
    assert "ARG PYTHON_VERSION=3.14.8" in TEXT
    assert "cgr.dev/chainguard/python:latest-dev@${PYTHON_BUILDER_ARM64_DIGEST}" in TEXT
    assert "cgr.dev/chainguard/python:latest@${PYTHON_RUNTIME_ARM64_DIGEST}" in TEXT
    assert "ghcr.io/astral-sh/uv:latest@${UV_ARM64_DIGEST}" in TEXT
    assert TEXT.count("sha256sum -c -") >= 3
    assert TEXT.count('torch_lock="url = \\"$TORCH_ARM64_WHEEL_URL\\"') == 2
    assert TEXT.count('grep -F -c "$torch_lock" uv.lock') == 2
    assert "--no-install-package torch" not in TEXT
    assert "teamagent-core-torch-arm64" not in TEXT


def test_core_runtime_receipt_is_verified_in_builder_and_bound_in_final() -> None:
    builder = _stage("builder", "final")
    final = _stage("final")
    receipt_args = (
        "RUNTIME_CONTRACT_SHA256",
        "RUNTIME_RECEIPT_B64",
        "RUNTIME_RECEIPT_SHA256",
    )
    for name in receipt_args:
        assert len(re.findall(rf"^ARG {name}$", builder, re.MULTILINE)) == 1
        assert len(re.findall(rf"^ARG {name}$", final, re.MULTILINE)) == 1
        assert not re.search(rf"^ARG {name}=", TEXT, re.MULTILINE)

    assert (
        "COPY infra/codebuild/teamagent_runtime_contract.json /tmp/teamagent_runtime_contract.json"
    ) in builder
    for proof in (
        "pathlib.Path('/tmp/teamagent_runtime_contract.json').read_bytes()",
        "hashlib.sha256(raw).hexdigest() == os.environ['RUNTIME_CONTRACT_SHA256']",
        "base64.b64decode(encoded, validate=True)",
        "base64.b64encode(receipt).decode('ascii') == encoded",
        "hashlib.sha256(receipt).hexdigest() == os.environ['RUNTIME_RECEIPT_SHA256']",
        "json.dumps(expected_receipt, sort_keys=True, separators=(',', ':'))",
        "parsed_receipt['subject'] == 'core'",
        "parsed_receipt['values'] == expected_values",
        "os.environ[entry['build_arg']] == entry['value']",
    ):
        assert proof in builder

    expected_labels = {
        "io.teamagent.build.runtime-contract-sha256": "RUNTIME_CONTRACT_SHA256",
        "io.teamagent.build.runtime-receipt": "RUNTIME_RECEIPT_B64",
        "io.teamagent.build.runtime-receipt-sha256": "RUNTIME_RECEIPT_SHA256",
    }
    for label, name in expected_labels.items():
        assert f'{label}="${name}"' in final


def test_core_contains_e5_mcp_db_aws_but_no_media_or_js_runtime() -> None:
    builder = _stage("builder", "final")
    assert "--extra mcp --extra embeddings" in builder
    for required in ("anthropic", "boto3", "psycopg", "sentence_transformers"):
        assert required in builder
    assert "teamagent.media.contracts" in builder
    for blocked in ("playwright", "yt_dlp", "weasyprint", "pptx", "claude_agent_sdk"):
        assert f"'{blocked}'" in builder
    for binary in ("node", "bun", "npm", "npx", "chromium", "ffmpeg", "yt-dlp"):
        assert f"-name {binary}" in builder
    assert "claude_agent_sdk/_bundled/claude" in builder
    assert "COPY tools/" not in TEXT
    assert "apt-get" not in TEXT
    assert "apk add" not in TEXT


def test_core_model_torch_and_app_html_are_content_addressed() -> None:
    assert f"ARG E5_MODEL_REVISION={E5_MODEL_REVISION}" in TEXT
    assert f"ARG TORCH_ARM64_WHEEL_SHA256={TORCH_WHEEL_SHA256}" in TEXT
    assert "torch-2.13.0%2Bcpu-cp314-cp314-manylinux_2_28_aarch64.whl" in TEXT
    assert "revision=os.environ['TEAMAGENT_E5_MODEL_REVISION']" in TEXT
    assert TEXT.count("local_files_only=True") >= 2
    assert "HF_HUB_OFFLINE=1" in TEXT
    assert "TRANSFORMERS_OFFLINE=1" in TEXT
    for name, value in {
        "BAKED_APP_HTML_SHA256": BAKED_APP_HTML_SHA256,
        "APP_HTML_SHA256": APP_HTML_SHA256,
        "APP_HTML_VERSION_ID": APP_HTML_VERSION_ID,
        "APP_HTML_MANIFEST_SHA256": APP_HTML_MANIFEST_SHA256,
        "APP_HTML_BUILD_INPUTS_SHA256": APP_HTML_BUILD_INPUTS_SHA256,
    }.items():
        assert f"ARG {name}={value}" in TEXT
    assert "ARG APP_HTML_SOURCE=s3" in TEXT
    assert "/app/src/teamagent/connect_web/static/app.html" in TEXT
    assert 'io.teamagent.contract.baked-app-html-sha256="$BAKED_APP_HTML_SHA256"' in TEXT
    assert 'io.teamagent.contract.app-html-source="$APP_HTML_SOURCE"' in TEXT
    assert 'io.teamagent.contract.app-html-sha256="$APP_HTML_SHA256"' in TEXT
    assert 'io.teamagent.contract.app-html-version-id="$APP_HTML_VERSION_ID"' in TEXT
    assert 'io.teamagent.contract.app-html-manifest-sha256="$APP_HTML_MANIFEST_SHA256"' in TEXT
    assert (
        'io.teamagent.contract.app-html-build-inputs-sha256="$APP_HTML_BUILD_INPUTS_SHA256"' in TEXT
    )
    assert 'org.opencontainers.image.revision="$GIT_COMMIT"' in TEXT


def test_core_ca_bundle_uses_the_official_fixed_apk_without_changing_python() -> None:
    builder = _stage("builder", "final")
    assert f"ARG CA_CERTIFICATES_BUNDLE_APK_SHA256={CA_BUNDLE_APK_SHA256}" in TEXT
    assert "ARG CA_CERTIFICATES_BUNDLE_VERSION=20260909-r3" in TEXT
    assert "COPY infra/docker/vendor/ca-certificates-bundle-20260909-r3.apk " in builder
    assert "COPY infra/docker/core-ca-bundle.lock.json " in builder
    assert "COPY infra/docker/replace_core_ca_bundle.py " in builder
    assert '--apk-sha256 "$CA_CERTIFICATES_BUNDLE_APK_SHA256"' in builder
    assert '--version "$CA_CERTIFICATES_BUNDLE_VERSION"' in builder


def test_core_brotli_apks_are_pinned_glibc_arm64_libraries() -> None:
    builder = _stage("builder", "final")
    final = _stage("final")
    assert "ARG BROTLI_PACKAGE_VERSION=1.2.1-r0" in TEXT
    assert "apk --root /tmp/core-ca/root --no-network --no-scripts --allow-untrusted add" in builder
    for part, digest in {
        "common": "1801ba6f752b201c5d52fadd98a3698a672a5695ad18d2321169b9e1c9fabe3e",
        "dec": "a5a25fb6a8238af95af1c21f039ecb4cb1a32fadd040ac276d73532ffd72fb1c",
        "enc": "d15c3aaa157a5fa281768fe5e69ae3da9357d3e58668ca47aa7bf2c3442d09b8",
    }.items():
        relative = f"infra/docker/vendor/libbrotli{part}1-1.2.1-r0.apk"
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest
        assert f"ARG BROTLI_{part.upper()}_APK_SHA256={digest}" in TEXT
        assert f"!{relative}" in DOCKERIGNORE.read_text()
        assert f"/tmp/core-brotli/libbrotli{part}1-${{BROTLI_PACKAGE_VERSION}}.apk" in builder
        with tarfile.open(ROOT / relative) as archive:
            metadata = archive.extractfile(".PKGINFO").read().decode()
            assert f"pkgname = libbrotli{part}1\n" in metadata
            assert "pkgver = 1.2.1-r0\n" in metadata
            assert "arch = aarch64\n" in metadata
            library = archive.extractfile(f"usr/lib/libbrotli{part}.so.1.2.1").read()
            assert library[:5] == b"\x7fELF\x02"
            assert int.from_bytes(library[18:20], "little") == 183
            assert b"libc.so.6\x00" in library
    assert "BrotliDecoderVersion() == 0x1002001" in final
    assert "BrotliEncoderVersion() == 0x1002001" in final
    assert "libbrotli'+part+'.so.1.2.0').unlink()" in final
    assert "libbrotli'+part+'1-1.2.0-r5.spdx.json').unlink()" in final


def test_core_runtime_is_uid_10001_read_only_ready_and_python_health_checked() -> None:
    final = _stage("final")
    assert "USER 10001:10001" in final
    assert "COPY --from=builder /runtime-etc/passwd /etc/passwd" in final
    assert "teamagent:x:10001:10001:" in TEXT
    assert "USER=teamagent" in final
    assert "LOGNAME=teamagent" in final
    assert 'VOLUME ["/tmp"]' in final
    assert "HOME=/tmp/teamagent/home" in final
    assert "TMPDIR=/tmp/teamagent/tmp" in final
    assert "XDG_CACHE_HOME=/tmp/teamagent/cache" in final
    assert "VIDEO_APPROVAL_STATE_PATH=/tmp/teamagent/state/video_approval_processed.json" in final
    assert "TEAMAGENT_RUNTIME_KIND=core" in final
    assert "ENTRYPOINT []" in final
    assert 'CMD ["/app/.venv/bin/python", "/app/scripts/run_mcp_http_server.py"]' in final
    assert "urllib.request.urlopen('http://127.0.0.1:8787/healthz'" in final
    assert "curl" not in final


def test_core_build_ca_is_secret_mounted_and_package_provenance_is_retained() -> None:
    builder = _stage("builder", "final")
    assert "--mount=type=secret,id=teamagent_ca,required=false" in builder
    assert "COPY /run/secrets" not in TEXT
    assert "uv pip freeze" in builder
    assert "python-builder-arm64.digest" in builder
    assert "python-runtime-arm64.digest" in builder
    assert "uv-arm64.digest" in builder


def test_core_dockerignore_is_deny_by_default_and_excludes_sensitive_state() -> None:
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    assert text.startswith("**\n")
    for allowed in (
        "!pyproject.toml",
        "!uv.lock",
        "!src/**",
        "!scripts/run_mcp_http_server.py",
        "!scripts/run_mcp_vertex_entrypoint.py",
        "!scripts/run_canary_health.py",
        "!scripts/run_ingest_fargate.py",
        "!scripts/ingest_sources.py",
        "!scripts/run_morning_digest_fargate.py",
        "!infra/codebuild/teamagent_runtime_contract.json",
        "!infra/docker/replace_core_ca_bundle.py",
        "!infra/docker/core-ca-bundle.lock.json",
        "!infra/docker/vendor/ca-certificates-bundle-20260909-r3.apk",
    ):
        assert allowed in text
    assert "!scripts/**" not in text
    for blocked in (
        "**/.env",
        "**/*.pem",
        "**/*.key",
        "**/*.tfstate",
        "**/__pycache__/",
        "**/node_modules/",
        "**/*.sqlite",
    ):
        assert blocked in text


def test_core_lock_has_no_bundled_agent_or_media_runtime_dependencies() -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    lock = LOCK.read_text(encoding="utf-8")
    base = tomllib.loads(pyproject)["project"]["dependencies"]
    for dependency in ("claude-agent-sdk", "yt-dlp", "playwright", "weasyprint", "python-pptx"):
        assert not any(item.startswith(dependency) for item in base)
    assert 'name = "claude-agent-sdk"' not in lock
    assert '"playwright==1.60.0"' in pyproject
    assert '"yt-dlp==2026.8.19"' in pyproject
