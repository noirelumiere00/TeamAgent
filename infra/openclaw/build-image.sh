#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
DOCKERFILE="$REPO_ROOT/infra/docker/Dockerfile.openclaw"
LOCK_FILE="$REPO_ROOT/infra/openclaw/plugins-lock.json"

usage() {
  echo "usage: $0 --image <repository:git-COMMIT12> [--push] [--manifest <path>]" >&2
}

fail() {
  echo "[openclaw-build] FATAL: $*" >&2
  exit 1
}

IMAGE_REF=""
PUSH=0
MANIFEST_PATH=/tmp/openclaw-build-manifest.json
while (($#)); do
  case "$1" in
    --image)
      (($# >= 2)) || { usage; exit 2; }
      IMAGE_REF=$2
      shift 2
      ;;
    --push)
      PUSH=1
      shift
      ;;
    --manifest)
      (($# >= 2)) || { usage; exit 2; }
      MANIFEST_PATH=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      fail "unknown argument: $1"
      ;;
  esac
done

[[ -n "$IMAGE_REF" ]] || { usage; exit 2; }
[[ "$IMAGE_REF" != *@* ]] || fail "image must be a tag, not a digest"
last_component=${IMAGE_REF##*/}
[[ "$last_component" == *:* ]] || fail "image tag is required"
image_tag=${last_component##*:}
image_repository=${IMAGE_REF%:*}
[[ "$image_tag" != latest ]] || fail "mutable latest tag is forbidden"

for command in docker jq sha256sum trivy; do
  command -v "$command" >/dev/null || fail "required command not found: $command"
done
docker buildx version >/dev/null 2>&1 || fail "docker buildx is unavailable"
jq -e '.schemaVersion == 1' "$LOCK_FILE" >/dev/null || fail "invalid plugins lock"
EXPECTED_TRIVY_VERSION=$(jq -r '.tooling.trivy.version' "$LOCK_FILE")
TRIVY_VERSION=$(trivy --version --format json | jq -er '.Version')
[[ "$TRIVY_VERSION" == "$EXPECTED_TRIVY_VERSION" ]] || \
  fail "Trivy $EXPECTED_TRIVY_VERSION is required (found $TRIVY_VERSION)"

SOURCE_COMMIT=${SOURCE_COMMIT:-}
SOURCE_BRANCH=${SOURCE_BRANCH:-}
SOURCE_ARCHIVE_SHA256=${SOURCE_ARCHIVE_SHA256:-}
SOURCE_ARTIFACT_VERSION=${SOURCE_ARTIFACT_VERSION:-}
if git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  [[ -z "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all)" ]] || \
    fail "refusing to build a dirty or untracked source tree"
  actual_commit=$(git -C "$REPO_ROOT" rev-parse HEAD)
  [[ -z "$SOURCE_COMMIT" || "$SOURCE_COMMIT" == "$actual_commit" ]] || \
    fail "SOURCE_COMMIT does not match checked-out HEAD"
  SOURCE_COMMIT=$actual_commit
  if [[ -z "$SOURCE_BRANCH" ]]; then
    SOURCE_BRANCH=$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD) || \
      fail "SOURCE_BRANCH is required for detached HEAD"
  fi
  if [[ -z "$SOURCE_ARCHIVE_SHA256" ]]; then
    SOURCE_ARCHIVE_SHA256=$(git -C "$REPO_ROOT" archive --format=tar HEAD | sha256sum | cut -d' ' -f1)
  fi
  SOURCE_ARTIFACT_VERSION=${SOURCE_ARTIFACT_VERSION:-git-$actual_commit}
fi

[[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || fail "SOURCE_COMMIT must be a full lowercase Git SHA"
[[ "$SOURCE_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || fail "SOURCE_BRANCH contains unsafe characters"
[[ "$SOURCE_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]] || \
  fail "SOURCE_ARCHIVE_SHA256 must be a lowercase SHA-256"
[[ -n "$SOURCE_ARTIFACT_VERSION" && ${#SOURCE_ARTIFACT_VERSION} -le 1024 ]] || \
  fail "SOURCE_ARTIFACT_VERSION is required and must not exceed 1024 characters"
[[ "$SOURCE_ARTIFACT_VERSION" =~ ^[A-Za-z0-9._+/=-]+$ ]] || \
  fail "SOURCE_ARTIFACT_VERSION contains unsafe characters"
expected_tag="git-${SOURCE_COMMIT:0:12}"
[[ "$image_tag" == "$expected_tag" ]] || fail "image tag must be $expected_tag"

OPENCLAW_VERSION=$(jq -r '.openclaw.version' "$LOCK_FILE")
OPENCLAW_ARM64_DIGEST=$(jq -r '.openclaw.linuxArm64Digest' "$LOCK_FILE")
DISTROLESS_ARM64_DIGEST=$(jq -r '.runtime.linuxArm64Digest' "$LOCK_FILE")
DOCKERFILE_FRONTEND_DIGEST=$(jq -r '.tooling.dockerfileFrontend.digest' "$LOCK_FILE")
SBOM_GENERATOR=$(jq -r '.tooling.sbomGenerator | .image + "@" + .linuxArm64Digest' "$LOCK_FILE")
PLUGINS_LOCK_SHA256=$(sha256sum "$LOCK_FILE" | cut -d' ' -f1)
DOCKERFILE_SHA256=$(sha256sum "$DOCKERFILE" | cut -d' ' -f1)
for pin in "$OPENCLAW_VERSION" "$OPENCLAW_ARM64_DIGEST" "$DISTROLESS_ARM64_DIGEST" "$DOCKERFILE_FRONTEND_DIGEST"; do
  grep -F -- "$pin" "$DOCKERFILE" >/dev/null || fail "Dockerfile does not contain lock pin: $pin"
done

build=(docker buildx build --platform linux/arm64 --pull -f "$DOCKERFILE"
  --build-arg "OPENCLAW_VERSION=$OPENCLAW_VERSION"
  --build-arg "OPENCLAW_ARM64_DIGEST=$OPENCLAW_ARM64_DIGEST"
  --build-arg "DISTROLESS_ARM64_DIGEST=$DISTROLESS_ARM64_DIGEST"
  --build-arg "GIT_COMMIT=$SOURCE_COMMIT"
  --build-arg "GIT_BRANCH=$SOURCE_BRANCH"
  --build-arg "SOURCE_ARCHIVE_SHA256=$SOURCE_ARCHIVE_SHA256"
  --build-arg "SOURCE_ARTIFACT_VERSION=$SOURCE_ARTIFACT_VERSION"
  --build-arg "PLUGINS_LOCK_SHA256=$PLUGINS_LOCK_SHA256"
  -t "$IMAGE_REF")

tmp_dir=$(mktemp -d /tmp/openclaw-build.XXXXXX)
gateway_container=""
cleanup() {
  if [[ -n "$gateway_container" ]]; then docker rm -f "$gateway_container" >/dev/null 2>&1 || true; fi
  rm -rf "$tmp_dir"
}
trap cleanup EXIT
TRIVY_CACHE_DIR=${TRIVY_CACHE_DIR:-$tmp_dir/trivy-cache}
mkdir -p "$TRIVY_CACHE_DIR"
export TRIVY_DB_REPOSITORY=${TRIVY_DB_REPOSITORY:-public.ecr.aws/aquasecurity/trivy-db:2,mirror.gcr.io/aquasec/trivy-db:2,ghcr.io/aquasecurity/trivy-db:2}

INDEX_DIGEST=""
if ((PUSH)); then
  "${build[@]}" --metadata-file "$tmp_dir/build-metadata.json" \
    --provenance=mode=max --sbom="generator=$SBOM_GENERATOR" --push "$REPO_ROOT"
  INDEX_DIGEST=$(docker buildx imagetools inspect "$IMAGE_REF" | awk '$1 == "Digest:" {print $2; exit}')
  [[ "$INDEX_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "could not resolve pushed OCI index digest"
  raw_index=$(docker buildx imagetools inspect "$IMAGE_REF" --raw)
  ARM64_DIGEST=$(jq -er '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "arm64")] | if length == 1 then .[0].digest else error("expected exactly one linux/arm64 child") end' <<<"$raw_index")
  RUNTIME_REF="$image_repository@$ARM64_DIGEST"
  docker pull "$RUNTIME_REF" >/dev/null
else
  "${build[@]}" --metadata-file "$tmp_dir/build-metadata.json" \
    --provenance=false --sbom=false --load "$REPO_ROOT"
  # For a single-platform build this is the actual OCI image manifest digest,
  # not Docker's config/image ID. The scan below runs against this arm64 image.
  ARM64_DIGEST=$(jq -er '."containerimage.digest"' "$tmp_dir/build-metadata.json")
  [[ "$ARM64_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "could not resolve local arm64 OCI child digest"
  RUNTIME_REF=$IMAGE_REF
fi

inspect_json=$(docker image inspect "$RUNTIME_REF")
jq -e \
  --arg commit "$SOURCE_COMMIT" \
  --arg branch "$SOURCE_BRANCH" \
  --arg archive "$SOURCE_ARCHIVE_SHA256" \
  --arg artifactVersion "$SOURCE_ARTIFACT_VERSION" \
  --arg lock "$PLUGINS_LOCK_SHA256" \
  '.[0].Architecture == "arm64" and
   .[0].Os == "linux" and
   .[0].Config.User == "65532:65532" and
   .[0].Config.Volumes["/tmp"] == {} and
   ([.[0].Config.Env[] | select(test("^(SLACK_BOT_TOKEN|SLACK_APP_TOKEN|OPENCLAW_GATEWAY_TOKEN|TEAMAGENT_MCP_BEARER)="))] | length) == 0 and
   .[0].Config.Labels["org.opencontainers.image.revision"] == $commit and
   .[0].Config.Labels["io.teamagent.source.branch"] == $branch and
   .[0].Config.Labels["io.teamagent.source.archive.sha256"] == $archive and
   .[0].Config.Labels["io.teamagent.source.artifact.version"] == $artifactVersion and
   .[0].Config.Labels["io.teamagent.openclaw.plugins-lock.sha256"] == $lock and
   .[0].Config.Labels["io.teamagent.runtime.readonly-rootfs-required"] == "true"' \
  <<<"$inspect_json" >/dev/null || fail "runtime image metadata contract failed"

runtime_probe=$(docker run --rm --entrypoint /nodejs/bin/node "$RUNTIME_REF" -e '
const fs=require("fs");
const forbidden=["/bin/sh","/bin/bash","/usr/bin/npm","/usr/local/bin/npm","/usr/local/lib/node_modules/npm","/app/node_modules/npm","/app/node_modules/corepack","/app/node_modules/.bin/npm","/usr/bin/curl","/usr/bin/git","/usr/bin/python3"].filter(fs.existsSync);
console.log(JSON.stringify({node:process.version,uid:process.getuid(),gid:process.getgid(),forbidden}));
process.exit(forbidden.length===0&&process.getuid()===65532?0:1);') || fail "distroless/nonroot contract failed"

run_args=(--rm --network none --read-only --cap-drop ALL --tmpfs /tmp:rw,noexec,nosuid,size=512m
  -e SLACK_BOT_TOKEN=xoxb-offline-smoke
  -e SLACK_APP_TOKEN=xapp-offline-smoke
  -e OPENCLAW_GATEWAY_TOKEN=offline-gateway-smoke
  -e TEAMAGENT_MCP_BEARER=offline-mcp-smoke
  -e 'SLACK_DM_ALLOWLIST=*'
  -e AWS_EC2_METADATA_DISABLED=true)

docker run "${run_args[@]}" "$RUNTIME_REF" /app/openclaw.mjs config validate --json \
  >"$tmp_dir/config.json"
jq -e '.valid == true and (.warnings | length) == 0' "$tmp_dir/config.json" >/dev/null || \
  fail "OpenClaw config validation failed"

docker run "${run_args[@]}" "$RUNTIME_REF" /app/openclaw.mjs plugins list --json \
  >"$tmp_dir/plugins.json"
jq -e --arg version "$OPENCLAW_VERSION" '
  ([.plugins[] | select(.id == "slack" and .version == $version and .status == "loaded" and (.channelIds | index("slack")) != null)] | length) == 1 and
  ([.plugins[] | select(.id == "amazon-bedrock" and .version == $version and .status == "loaded" and (.providerIds | index("amazon-bedrock")) != null)] | length) == 1 and
  ([.plugins[] | select(.id == "browser" and .status == "loaded")] | length) == 0' \
  "$tmp_dir/plugins.json" >/dev/null || fail "Slack/Bedrock plugin compatibility smoke failed"

gateway_args=(-d --network none --read-only --cap-drop ALL --tmpfs /tmp:rw,noexec,nosuid,size=512m
  -e SLACK_BOT_TOKEN=xoxb-offline-smoke
  -e SLACK_APP_TOKEN=xapp-offline-smoke
  -e OPENCLAW_GATEWAY_TOKEN=offline-gateway-smoke
  -e TEAMAGENT_MCP_BEARER=offline-mcp-smoke
  -e 'SLACK_DM_ALLOWLIST=*'
  -e AWS_EC2_METADATA_DISABLED=true
  -e OPENCLAW_SKIP_CHANNELS=1)
gateway_container=$(docker run "${gateway_args[@]}" "$RUNTIME_REF")
gateway_ok=0
for _ in $(seq 1 45); do
  if docker exec "$gateway_container" node -e "fetch('http://127.0.0.1:18789/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" >/dev/null 2>&1; then
    gateway_ok=1
    break
  fi
  [[ $(docker inspect -f '{{.State.Running}}' "$gateway_container" 2>/dev/null || true) == true ]] || break
  sleep 1
done
docker logs "$gateway_container" >"$tmp_dir/gateway.log" 2>&1 || true
((gateway_ok)) || { tail -120 "$tmp_dir/gateway.log" >&2; fail "offline gateway health smoke failed"; }
if grep -E 'spawn npm|Config observe anomaly|auto-enabled plugins|browser configured' "$tmp_dir/gateway.log" >/dev/null || \
   grep -F -e xoxb-offline-smoke -e xapp-offline-smoke -e offline-gateway-smoke -e offline-mcp-smoke "$tmp_dir/gateway.log" >/dev/null; then
  tail -120 "$tmp_dir/gateway.log" >&2
  fail "gateway attempted package repair or enabled the browser plugin"
fi
docker rm -f "$gateway_container" >/dev/null
gateway_container=""

trivy --cache-dir "$TRIVY_CACHE_DIR" image --quiet --scanners vuln --severity CRITICAL,HIGH --format json \
  --output "$tmp_dir/vulnerabilities.json" "$RUNTIME_REF"
vulnerability_count=$(jq '[.Results[]?.Vulnerabilities[]?] | length' "$tmp_dir/vulnerabilities.json")
if ((vulnerability_count)); then
  jq -r '.Results[]? as $r | $r.Vulnerabilities[]? | "\(.Severity) \(.VulnerabilityID) \(.PkgName) \($r.Target)"' "$tmp_dir/vulnerabilities.json" >&2
  fail "Critical/High vulnerability gate failed ($vulnerability_count findings)"
fi

trivy --cache-dir "$TRIVY_CACHE_DIR" image --quiet --scanners secret --format json --output "$tmp_dir/secrets.json" "$RUNTIME_REF"
secret_count=$(jq '[.Results[]?.Secrets[]?] | length' "$tmp_dir/secrets.json")
if ((secret_count)); then
  jq -r '.Results[]? as $r | $r.Secrets[]? | "\(.RuleID) \($r.Target)"' "$tmp_dir/secrets.json" >&2
  fail "embedded secret gate failed ($secret_count findings)"
fi

mkdir -p "$(dirname -- "$MANIFEST_PATH")"
jq -n \
  --arg createdAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg image "$IMAGE_REF" \
  --arg runtimeRef "$RUNTIME_REF" \
  --arg indexDigest "$INDEX_DIGEST" \
  --arg arm64Digest "$ARM64_DIGEST" \
  --arg sourceCommit "$SOURCE_COMMIT" \
  --arg sourceBranch "$SOURCE_BRANCH" \
  --arg sourceArchiveSha256 "$SOURCE_ARCHIVE_SHA256" \
  --arg sourceArtifactVersion "$SOURCE_ARTIFACT_VERSION" \
  --arg dockerfileSha256 "$DOCKERFILE_SHA256" \
  --arg pluginsLockSha256 "$PLUGINS_LOCK_SHA256" \
  --arg openclawVersion "$OPENCLAW_VERSION" \
  --arg trivyVersion "$TRIVY_VERSION" \
  --arg sbomGenerator "$SBOM_GENERATOR" \
  --argjson runtime "$runtime_probe" \
  '{schemaVersion:1,createdAt:$createdAt,image:$image,runtimeRef:$runtimeRef,indexDigest:$indexDigest,arm64Digest:$arm64Digest,source:{commit:$sourceCommit,branch:$sourceBranch,archiveSha256:$sourceArchiveSha256,artifactVersion:$sourceArtifactVersion,dockerfileSha256:$dockerfileSha256,pluginsLockSha256:$pluginsLockSha256},runtime:{platform:"linux/arm64",openclawVersion:$openclawVersion,node:$runtime.node,uid:$runtime.uid,gid:$runtime.gid,readOnlySmoke:true,offlineGatewaySmoke:true},scan:{trivyVersion:$trivyVersion,sbomGenerator:$sbomGenerator,critical:0,high:0,secrets:0}}' \
  >"$MANIFEST_PATH"

echo "[openclaw-build] PASS image=$RUNTIME_REF manifest=$MANIFEST_PATH"
