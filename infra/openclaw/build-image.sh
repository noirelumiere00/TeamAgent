#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
DOCKERFILE="$REPO_ROOT/infra/docker/Dockerfile.openclaw"
LOCK_FILE="$REPO_ROOT/infra/openclaw/plugins-lock.json"

usage() {
  echo "usage: $0 --image <repository:git-COMMIT12> [--push] [--manifest <path>] [--evidence-dir <path>]" >&2
}

fail() {
  echo "[openclaw-build] FATAL: $*" >&2
  exit 1
}

IMAGE_REF=""
PUSH=0
MANIFEST_PATH=/tmp/openclaw-build-manifest.json
EVIDENCE_DIR=""
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
    --evidence-dir)
      (($# >= 2)) || { usage; exit 2; }
      EVIDENCE_DIR=$2
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
EVIDENCE_DIR=${EVIDENCE_DIR:-"${MANIFEST_PATH%.json}.evidence"}
[[ "$EVIDENCE_DIR" != "$MANIFEST_PATH" ]] || fail "evidence directory must differ from manifest path"

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
SBOM_ATTESTATION_GENERATOR=$(jq -r '.tooling.sbomGenerator | .image + "@" + .linuxArm64Digest' "$LOCK_FILE")
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
ATTESTATION_DIGEST=""
if ((PUSH)); then
  "${build[@]}" --metadata-file "$tmp_dir/build-metadata.json" \
    --provenance=mode=max --sbom="generator=$SBOM_ATTESTATION_GENERATOR" --push "$REPO_ROOT"
  INDEX_DIGEST=$(docker buildx imagetools inspect "$IMAGE_REF" | awk '$1 == "Digest:" {print $2; exit}')
  [[ "$INDEX_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "could not resolve pushed OCI index digest"
  raw_index=$(docker buildx imagetools inspect "$IMAGE_REF" --raw)
  ARM64_DIGEST=$(jq -er '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "arm64")] | if length == 1 then .[0].digest else error("expected exactly one linux/arm64 child") end' <<<"$raw_index")
  ATTESTATION_DIGEST=$(jq -er --arg subject "$ARM64_DIGEST" '
    [.manifests[] | select(
      .annotations["vnd.docker.reference.type"] == "attestation-manifest" and
      .annotations["vnd.docker.reference.digest"] == $subject
    )] | if length == 1 then .[0].digest else error("expected exactly one arm64 attestation manifest") end
  ' <<<"$raw_index")
  raw_attestation=$(docker buildx imagetools inspect "$image_repository@$ATTESTATION_DIGEST" --raw)
  jq -e '
    ([.layers[]?.annotations["in-toto.io/predicate-type"]] | any(
      . == "https://spdx.dev/Document"
    )) and
    ([.layers[]?.annotations["in-toto.io/predicate-type"]] | any(
      startswith("https://slsa.dev/provenance/")
    ))
  ' <<<"$raw_attestation" >/dev/null || fail "pushed SBOM/provenance attestations are missing"
  RUNTIME_REF="$image_repository@$ARM64_DIGEST"
  docker pull "$RUNTIME_REF" >/dev/null
else
  "${build[@]}" --metadata-file "$tmp_dir/build-metadata.json" \
    --provenance=false --load "$REPO_ROOT"
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

IMAGE_ID=$(jq -er '.[0].Id' <<<"$inspect_json")
CONFIG_DIGEST=$(jq -er '.[0].Descriptor.annotations["config.digest"] // .[0].Id' <<<"$inspect_json")
INSPECT_MANIFEST_DIGEST=$(jq -r '.[0].Descriptor.digest // empty' <<<"$inspect_json")
ROOTFS_DIFF_IDS=$(jq -c '.[0].RootFS.Layers' <<<"$inspect_json")
ROOTFS_SHA256=$(jq -c '.[0].RootFS.Layers' <<<"$inspect_json" | sha256sum | cut -d' ' -f1)
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "invalid local image identifier"
[[ "$CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "invalid image config digest"
if [[ -n "$INSPECT_MANIFEST_DIGEST" && "$INSPECT_MANIFEST_DIGEST" != "$ARM64_DIGEST" ]]; then
  fail "loaded manifest digest does not match build metadata"
fi
[[ "$ROOTFS_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "invalid rootfs inventory hash"

[[ ! -L "$EVIDENCE_DIR" ]] || fail "evidence directory must not be a symlink"
mkdir -p "$EVIDENCE_DIR"
[[ -z "$(find "$EVIDENCE_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] || \
  fail "evidence directory must be empty: $EVIDENCE_DIR"

docker run --rm --user 0:0 --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --entrypoint /nodejs/bin/node "$RUNTIME_REF" -e '
const fs=require("fs");
const forbidden=[
  "/root/.cache/ms-playwright",
  "/home/node/.cache/ms-playwright",
];
const present=forbidden.filter(candidate=>{
  try{fs.lstatSync(candidate);return true}catch(error){
    if(error.code==="ENOENT")return false;
    throw error;
  }
});
if(present.length)console.error(JSON.stringify({present}));
process.exit(present.length?1:0);
' || fail "privileged-path browser cache inventory failed"

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --entrypoint /nodejs/bin/node "$RUNTIME_REF" -e '
const fs=require("fs"),path=require("path");
const forbiddenNames=new Set([
  "@openclaw/browser-plugin","@typescript/native-preview","esbuild","jscpd",
  "oxfmt","oxlint","oxlint-tsgolint","playwright","playwright-core",
  "rolldown","rollup","ts-node","tsdown","tsx","typescript","vite","vitest",
]);
const forbiddenPrefixes=["@esbuild/","@openai/codex","@rolldown/","@rollup/","@vitest/"];
const isForbidden=name=>
  forbiddenNames.has(name)||forbiddenPrefixes.some(prefix=>name.startsWith(prefix));
const forbiddenPaths=[
  "/bin/sh","/bin/bash","/usr/bin/npm","/usr/local/bin/npm",
  "/usr/local/lib/node_modules/npm","/app/node_modules/npm",
  "/app/node_modules/corepack","/app/node_modules/.bin","/app/node_modules/.pnpm",
  "/app/node_modules/playwright","/app/node_modules/playwright-core",
  "/app/dist/extensions/browser","/app/dist/extensions/codex",
  "/app/dist/extensions/codex-supervisor","/ms-playwright",
  "/usr/bin/chromium","/usr/bin/chromium-browser","/usr/bin/google-chrome",
  "/usr/bin/firefox","/usr/bin/curl","/usr/bin/git","/usr/bin/python3",
];
const presentPaths=forbiddenPaths.filter(candidate=>{
  try{fs.lstatSync(candidate);return true}catch(error){
    if(error.code==="ENOENT")return false;
    throw error;
  }
});
const packages=[];
const forbiddenPackages=[];
const forbiddenDeclarations=[];
const danglingSymlinks=[];
function inventory(root){
  let stat;
  try{stat=fs.lstatSync(root)}catch(error){
    if(error.code==="ENOENT")return;
    throw error;
  }
  if(stat.isSymbolicLink()){
    try{fs.statSync(root)}catch(error){
      if(error.code==="ENOENT")danglingSymlinks.push(root);
      else throw error;
    }
    return;
  }
  if(!stat.isDirectory())return;
  try{
    const packagePath=path.join(root,"package.json");
    const metadata=JSON.parse(fs.readFileSync(packagePath,"utf8"));
    if(metadata.name&&metadata.version)packages.push(`${metadata.name}@${metadata.version}`);
    if(isForbidden(metadata.name||"")){
      forbiddenPackages.push({path:root,name:metadata.name,version:metadata.version||null});
    }
    for(const section of [
      "dependencies","optionalDependencies","devDependencies",
      "peerDependencies","peerDependenciesMeta",
    ]){
      for(const name of Object.keys(metadata[section]||{})){
        if(isForbidden(name))forbiddenDeclarations.push({path:packagePath,section,name});
      }
    }
  }catch(error){
    if(error.code!=="ENOENT")throw error;
  }
  for(const entry of fs.readdirSync(root))inventory(path.join(root,entry));
}
inventory("/app");
inventory("/opt/teamagent");
console.log(JSON.stringify({
  node:process.version,uid:process.getuid(),gid:process.getgid(),
  execve:typeof process.execve,packages:[...new Set(packages)].sort(),
  presentPaths,forbiddenPackages,forbiddenDeclarations,danglingSymlinks,
}));
process.exit(
  presentPaths.length===0&&forbiddenPackages.length===0&&
  forbiddenDeclarations.length===0&&danglingSymlinks.length===0&&
  process.getuid()===65532&&process.getgid()===65532&&
  typeof process.execve==="function"?0:1
);' >"$tmp_dir/runtime-probe.json" || fail "distroless/nonroot/runtime inventory contract failed"

jq -e . "$tmp_dir/runtime-probe.json" >/dev/null || fail "runtime inventory is not valid JSON"
jq -n \
  --arg manifestDigest "$ARM64_DIGEST" \
  --arg imageId "$IMAGE_ID" \
  --arg configDigest "$CONFIG_DIGEST" \
  --arg rootfsSha256 "$ROOTFS_SHA256" \
  --argjson diffIds "$ROOTFS_DIFF_IDS" \
  --slurpfile probe "$tmp_dir/runtime-probe.json" \
  '{schemaVersion:1,subject:{platform:"linux/arm64",manifestDigest:$manifestDigest,imageId:$imageId,configDigest:$configDigest,rootfs:{diffIds:$diffIds,sha256:$rootfsSha256}},runtime:$probe[0]}' \
  >"$EVIDENCE_DIR/runtime-inventory.json"

run_args=(--rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges
  --tmpfs /tmp:rw,noexec,nosuid,size=512m
  -e SLACK_BOT_TOKEN=xoxb-offline-smoke
  -e SLACK_APP_TOKEN=xapp-offline-smoke
  -e OPENCLAW_GATEWAY_TOKEN=offline-gateway-smoke
  -e TEAMAGENT_MCP_BEARER=offline-mcp-smoke
  -e 'SLACK_DM_ALLOWLIST=*'
  -e AWS_EC2_METADATA_DISABLED=true)

docker run "${run_args[@]}" \
  -e UNEXPECTED_SECRET=must-not-cross-entrypoint \
  -e AWS_ACCESS_KEY_ID=must-not-cross-entrypoint \
  -e AWS_SECRET_ACCESS_KEY=must-not-cross-entrypoint \
  -e NODE_OPTIONS=--no-warnings \
  -e NODE_PATH=/tmp/must-not-cross-entrypoint \
  -e OPENCLAW_SKIP_CHANNELS=1 \
  -e HTTPS_PROXY=http://127.0.0.1:9 \
  -e NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
  -e ECS_CONTAINER_METADATA_URI_V4=http://169.254.170.2/v4/test \
  -e AWS_CONTAINER_CREDENTIALS_RELATIVE_URI=/v2/credentials/test \
  "$RUNTIME_REF" /nodejs/bin/node -e 'console.log(JSON.stringify(process.env))' \
  >"$tmp_dir/child-env.json"
jq -e '
  def allowed: [
    "ALL_PROXY","AWS_CA_BUNDLE","AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI","AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_DEFAULT_REGION","AWS_EC2_METADATA_DISABLED","AWS_EXECUTION_ENV","AWS_REGION",
    "ECS_AGENT_URI","ECS_CONTAINER_METADATA_URI","ECS_CONTAINER_METADATA_URI_V4",
    "HOME","HTTPS_PROXY","HTTP_PROXY","NODE_COMPILE_CACHE","NODE_ENV",
    "NODE_EXTRA_CA_CERTS","NODE_USE_ENV_PROXY","NO_PROXY","OPENCLAW_CONFIG_PATH",
    "OPENCLAW_GATEWAY_TOKEN","OPENCLAW_RUNTIME_DIR","OPENCLAW_STATE_DIR",
    "OPENCLAW_WORKSPACE_DIR","PATH","SLACK_APP_TOKEN","SLACK_BOT_TOKEN",
    "SLACK_DM_ALLOWLIST","SSL_CERT_DIR","SSL_CERT_FILE","TEAMAGENT_MCP_BEARER",
    "TMPDIR","XDG_CACHE_HOME","all_proxy","http_proxy","https_proxy","no_proxy"
  ];
  ([keys[] as $key | select((allowed | index($key)) == null) | $key] | length) == 0 and
  has("UNEXPECTED_SECRET") == false and
  has("AWS_ACCESS_KEY_ID") == false and
  has("AWS_SECRET_ACCESS_KEY") == false and
  has("NODE_OPTIONS") == false and
  has("NODE_PATH") == false and
  has("LD_PRELOAD") == false and
  has("OPENCLAW_SKIP_CHANNELS") == false and
  .NODE_ENV == "production" and
  .AWS_DEFAULT_REGION == .AWS_REGION and
  .OPENCLAW_RUNTIME_DIR == "/tmp/teamagent-openclaw" and
  .HTTPS_PROXY == "http://127.0.0.1:9" and
  .ECS_CONTAINER_METADATA_URI_V4 == "http://169.254.170.2/v4/test" and
  .AWS_CONTAINER_CREDENTIALS_RELATIVE_URI == "/v2/credentials/test"
' "$tmp_dir/child-env.json" >/dev/null || fail "child environment allowlist failed"

if docker run "${run_args[@]}" "$RUNTIME_REF" /nodejs/bin/node -e 'process.exit(42)' \
  >"$tmp_dir/exit-42.log" 2>&1; then
  fail "entrypoint unexpectedly converted child exit 42 to success"
else
  child_exit=$?
fi
[[ "$child_exit" == 42 ]] || fail "entrypoint did not preserve child exit 42 (got $child_exit)"

if docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m "$RUNTIME_REF" \
  >"$tmp_dir/missing-secrets.log" 2>&1; then
  fail "entrypoint unexpectedly started without required secrets"
else
  missing_secret_exit=$?
fi
[[ "$missing_secret_exit" == 78 ]] || \
  fail "missing runtime secrets must exit 78 (got $missing_secret_exit)"

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

gateway_args=(-d --network none --read-only --cap-drop ALL --security-opt no-new-privileges
  --tmpfs /tmp:rw,noexec,nosuid,size=512m
  -e SLACK_BOT_TOKEN=xoxb-offline-smoke
  -e SLACK_APP_TOKEN=xapp-offline-smoke
  -e OPENCLAW_GATEWAY_TOKEN=offline-gateway-smoke
  -e TEAMAGENT_MCP_BEARER=offline-mcp-smoke
  -e 'SLACK_DM_ALLOWLIST=*'
  -e AWS_EC2_METADATA_DISABLED=true)
# Network-none cannot exercise Slack authentication. Plugin loading is verified
# above; disable the channel only in this generated throwaway config so readyz
# and graceful SIGTERM test the gateway itself without a guaranteed Slack error.
gateway_launcher='
const fs=require("fs");
const configPath=process.env.OPENCLAW_CONFIG_PATH;
const config=JSON.parse(fs.readFileSync(configPath,"utf8"));
config.channels.slack.enabled=false;
fs.writeFileSync(configPath,JSON.stringify(config,null,2)+"\n",{mode:0o600});
process.execve(process.execPath,[
  process.execPath,"/app/openclaw.mjs","gateway","--bind","loopback","--port","18789"
],process.env);'
gateway_container=$(docker run "${gateway_args[@]}" "$RUNTIME_REF" \
  /nodejs/bin/node -e "$gateway_launcher")
docker inspect "$gateway_container" | jq -e '
  .[0].HostConfig.ReadonlyRootfs == true and
  (.[0].HostConfig.CapDrop | index("ALL")) != null and
  (.[0].HostConfig.SecurityOpt | index("no-new-privileges")) != null and
  .[0].HostConfig.NetworkMode == "none" and
  (.[0].HostConfig.Tmpfs["/tmp"] | contains("noexec")) and
  (.[0].HostConfig.Tmpfs["/tmp"] | contains("nosuid"))
' >/dev/null || fail "gateway container isolation contract failed"
gateway_ok=0
for _ in $(seq 1 45); do
  if docker exec "$gateway_container" node -e "fetch('http://127.0.0.1:18789/readyz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" >/dev/null 2>&1; then
    gateway_ok=1
    break
  fi
  [[ $(docker inspect -f '{{.State.Running}}' "$gateway_container" 2>/dev/null || true) == true ]] || break
  sleep 1
done
docker logs "$gateway_container" >"$tmp_dir/gateway.log" 2>&1 || true
((gateway_ok)) || { tail -120 "$tmp_dir/gateway.log" >&2; fail "offline gateway readiness smoke failed"; }
if grep -E 'spawn npm|Config observe anomaly|auto-enabled plugins|browser configured' "$tmp_dir/gateway.log" >/dev/null || \
   grep -F -e xoxb-offline-smoke -e xapp-offline-smoke -e offline-gateway-smoke -e offline-mcp-smoke "$tmp_dir/gateway.log" >/dev/null; then
  tail -120 "$tmp_dir/gateway.log" >&2
  fail "gateway attempted package repair or enabled the browser plugin"
fi
docker stop --time 30 "$gateway_container" >/dev/null
gateway_exit=$(docker inspect -f '{{.State.ExitCode}}' "$gateway_container")
[[ "$gateway_exit" == 0 ]] || fail "gateway SIGTERM shutdown must exit 0 (got $gateway_exit)"
docker rm "$gateway_container" >/dev/null
gateway_container=""

trivy --cache-dir "$TRIVY_CACHE_DIR" image --quiet --scanners vuln --format cyclonedx \
  --output "$EVIDENCE_DIR/sbom.cdx.json" "$RUNTIME_REF"
jq -e \
  --arg imageId "$IMAGE_ID" \
  --arg trivyVersion "$TRIVY_VERSION" \
  --argjson diffIds "$ROOTFS_DIFF_IDS" '
  (.metadata.tools.components | any(
    .group == "aquasecurity" and .name == "trivy" and .version == $trivyVersion
  )) and
  ([.metadata.component.properties[] |
    select(.name == "aquasecurity:trivy:ImageID") | .value] == [$imageId]) and
  (([.metadata.component.properties[] |
    select(.name == "aquasecurity:trivy:DiffID") | .value] | sort) == ($diffIds | sort))
' "$EVIDENCE_DIR/sbom.cdx.json" >/dev/null || fail "SBOM subject/generator does not match runtime image"
jq '[
  .components[] |
  select((.purl? != null) and (.purl | startswith("pkg:npm/"))) |
  (((if (.group // "") == "" then "" else (.group + "/") end) + .name) + "@" + .version)
] | unique | sort' "$EVIDENCE_DIR/sbom.cdx.json" >"$tmp_dir/sbom-packages.json"
jq -e --slurpfile sbom "$tmp_dir/sbom-packages.json" \
  '.runtime.packages == $sbom[0]' "$EVIDENCE_DIR/runtime-inventory.json" >/dev/null || \
  fail "SBOM npm inventory does not exactly match physical runtime packages"

trivy --cache-dir "$TRIVY_CACHE_DIR" image --quiet --scanners vuln \
  --severity CRITICAL,HIGH --format json \
  --output "$EVIDENCE_DIR/vulnerabilities.json" "$RUNTIME_REF"
jq -e --arg imageId "$IMAGE_ID" --argjson diffIds "$ROOTFS_DIFF_IDS" \
  '.Metadata.ImageID == $imageId and .Metadata.DiffIDs == $diffIds' \
  "$EVIDENCE_DIR/vulnerabilities.json" >/dev/null || fail "vulnerability scan subject mismatch"
vulnerability_count=$(jq '[.Results[]?.Vulnerabilities[]?] | length' "$EVIDENCE_DIR/vulnerabilities.json")
if ((vulnerability_count)); then
  jq -r '.Results[]? as $r | $r.Vulnerabilities[]? | "\(.Severity) \(.VulnerabilityID) \(.PkgName) \($r.Target)"' "$EVIDENCE_DIR/vulnerabilities.json" >&2
  fail "Critical/High vulnerability gate failed ($vulnerability_count findings)"
fi

trivy --cache-dir "$TRIVY_CACHE_DIR" image --quiet --scanners secret --format json \
  --output "$EVIDENCE_DIR/secrets.json" "$RUNTIME_REF"
jq -e --arg imageId "$IMAGE_ID" --argjson diffIds "$ROOTFS_DIFF_IDS" \
  '.Metadata.ImageID == $imageId and .Metadata.DiffIDs == $diffIds' \
  "$EVIDENCE_DIR/secrets.json" >/dev/null || fail "secret scan subject mismatch"
secret_count=$(jq '[.Results[]?.Secrets[]?] | length' "$EVIDENCE_DIR/secrets.json")
if ((secret_count)); then
  jq -r '.Results[]? as $r | $r.Secrets[]? | "\(.RuleID) \($r.Target)"' "$EVIDENCE_DIR/secrets.json" >&2
  fail "embedded secret gate failed ($secret_count findings)"
fi

jq -S . "$tmp_dir/build-metadata.json" >"$EVIDENCE_DIR/build-metadata.json"
jq -S . "$tmp_dir/config.json" >"$EVIDENCE_DIR/config-validation.json"
jq -S . "$tmp_dir/plugins.json" >"$EVIDENCE_DIR/plugins.json"
cp "$tmp_dir/gateway.log" "$EVIDENCE_DIR/gateway.log"

RUNTIME_NODE=$(jq -er '.runtime.node' "$EVIDENCE_DIR/runtime-inventory.json")
RUNTIME_UID=$(jq -er '.runtime.uid' "$EVIDENCE_DIR/runtime-inventory.json")
RUNTIME_GID=$(jq -er '.runtime.gid' "$EVIDENCE_DIR/runtime-inventory.json")
RUNTIME_PACKAGE_COUNT=$(jq -er '.runtime.packages | length' "$EVIDENCE_DIR/runtime-inventory.json")
SBOM_COMPONENT_COUNT=$(jq -er '.components | length' "$EVIDENCE_DIR/sbom.cdx.json")
INVENTORY_SHA256=$(sha256sum "$EVIDENCE_DIR/runtime-inventory.json" | cut -d' ' -f1)
SBOM_SHA256=$(sha256sum "$EVIDENCE_DIR/sbom.cdx.json" | cut -d' ' -f1)
VULNERABILITY_SCAN_SHA256=$(sha256sum "$EVIDENCE_DIR/vulnerabilities.json" | cut -d' ' -f1)
SECRET_SCAN_SHA256=$(sha256sum "$EVIDENCE_DIR/secrets.json" | cut -d' ' -f1)
BUILD_METADATA_SHA256=$(sha256sum "$EVIDENCE_DIR/build-metadata.json" | cut -d' ' -f1)
CONFIG_VALIDATION_SHA256=$(sha256sum "$EVIDENCE_DIR/config-validation.json" | cut -d' ' -f1)
PLUGINS_SHA256=$(sha256sum "$EVIDENCE_DIR/plugins.json" | cut -d' ' -f1)
GATEWAY_LOG_SHA256=$(sha256sum "$EVIDENCE_DIR/gateway.log" | cut -d' ' -f1)

mkdir -p "$(dirname -- "$MANIFEST_PATH")"
jq -n \
  --arg createdAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg image "$IMAGE_REF" \
  --arg runtimeRef "$RUNTIME_REF" \
  --arg indexDigest "$INDEX_DIGEST" \
  --arg arm64Digest "$ARM64_DIGEST" \
  --arg imageId "$IMAGE_ID" \
  --arg configDigest "$CONFIG_DIGEST" \
  --arg rootfsSha256 "$ROOTFS_SHA256" \
  --argjson rootfsDiffIds "$ROOTFS_DIFF_IDS" \
  --arg sourceCommit "$SOURCE_COMMIT" \
  --arg sourceBranch "$SOURCE_BRANCH" \
  --arg sourceArchiveSha256 "$SOURCE_ARCHIVE_SHA256" \
  --arg sourceArtifactVersion "$SOURCE_ARTIFACT_VERSION" \
  --arg dockerfileSha256 "$DOCKERFILE_SHA256" \
  --arg pluginsLockSha256 "$PLUGINS_LOCK_SHA256" \
  --arg openclawVersion "$OPENCLAW_VERSION" \
  --arg trivyVersion "$TRIVY_VERSION" \
  --arg sbomAttestationGenerator "$SBOM_ATTESTATION_GENERATOR" \
  --arg attestationDigest "$ATTESTATION_DIGEST" \
  --arg runtimeNode "$RUNTIME_NODE" \
  --argjson runtimeUid "$RUNTIME_UID" \
  --argjson runtimeGid "$RUNTIME_GID" \
  --argjson runtimePackageCount "$RUNTIME_PACKAGE_COUNT" \
  --argjson sbomComponentCount "$SBOM_COMPONENT_COUNT" \
  --arg inventoryPath "$EVIDENCE_DIR/runtime-inventory.json" \
  --arg inventorySha256 "$INVENTORY_SHA256" \
  --arg sbomPath "$EVIDENCE_DIR/sbom.cdx.json" \
  --arg sbomSha256 "$SBOM_SHA256" \
  --arg vulnerabilityPath "$EVIDENCE_DIR/vulnerabilities.json" \
  --arg vulnerabilitySha256 "$VULNERABILITY_SCAN_SHA256" \
  --arg secretPath "$EVIDENCE_DIR/secrets.json" \
  --arg secretSha256 "$SECRET_SCAN_SHA256" \
  --arg buildMetadataPath "$EVIDENCE_DIR/build-metadata.json" \
  --arg buildMetadataSha256 "$BUILD_METADATA_SHA256" \
  --arg configValidationPath "$EVIDENCE_DIR/config-validation.json" \
  --arg configValidationSha256 "$CONFIG_VALIDATION_SHA256" \
  --arg pluginsPath "$EVIDENCE_DIR/plugins.json" \
  --arg pluginsSha256 "$PLUGINS_SHA256" \
  --arg gatewayLogPath "$EVIDENCE_DIR/gateway.log" \
  --arg gatewayLogSha256 "$GATEWAY_LOG_SHA256" \
  --argjson pushed "$PUSH" \
  '{
    schemaVersion:2,
    createdAt:$createdAt,
    image:{
      requested:$image,runtimeRef:$runtimeRef,indexDigest:$indexDigest,
      manifestDigest:$arm64Digest,imageId:$imageId,configDigest:$configDigest,
      rootfs:{diffIds:$rootfsDiffIds,sha256:$rootfsSha256}
    },
    source:{
      commit:$sourceCommit,branch:$sourceBranch,archiveSha256:$sourceArchiveSha256,
      artifactVersion:$sourceArtifactVersion,dockerfileSha256:$dockerfileSha256,
      pluginsLockSha256:$pluginsLockSha256
    },
    runtime:{
      platform:"linux/arm64",openclawVersion:$openclawVersion,node:$runtimeNode,
      uid:$runtimeUid,gid:$runtimeGid,packageCount:$runtimePackageCount,
      forbiddenPackageOrPluginArtifacts:0,danglingSymlinks:0,
      privilegedPathInventory:true,readOnlySmoke:true,
      capDropAllSmoke:true,noNewPrivilegesSmoke:true,offlineGatewaySmoke:true,
      readyzSmoke:true,sigtermExitPropagationSmoke:true
    },
    buildAttestations:{
      provenance:($pushed == 1),sbom:($pushed == 1),
      manifestDigest:(if $pushed == 1 then $attestationDigest else null end),
      sbomGenerator:(if $pushed == 1 then $sbomAttestationGenerator else null end)
    },
    sbom:{
      format:"CycloneDX 1.6",generator:{name:"trivy",version:$trivyVersion},
      subjectImageId:$imageId,subjectConfigDigest:$configDigest,
      subjectManifestDigest:$arm64Digest,
      componentCount:$sbomComponentCount,
      physicalNpmInventoryExactMatch:true,path:$sbomPath,sha256:$sbomSha256
    },
    scan:{
      trivyVersion:$trivyVersion,subjectImageId:$imageId,subjectConfigDigest:$configDigest,
      subjectManifestDigest:$arm64Digest,critical:0,high:0,secrets:0,
      vulnerabilityEvidence:{path:$vulnerabilityPath,sha256:$vulnerabilitySha256},
      secretEvidence:{path:$secretPath,sha256:$secretSha256}
    },
    evidence:{
      runtimeInventory:{path:$inventoryPath,sha256:$inventorySha256},
      buildMetadata:{path:$buildMetadataPath,sha256:$buildMetadataSha256},
      configValidation:{path:$configValidationPath,sha256:$configValidationSha256},
      plugins:{path:$pluginsPath,sha256:$pluginsSha256},
      gatewayLog:{path:$gatewayLogPath,sha256:$gatewayLogSha256}
    }
  }' \
  >"$MANIFEST_PATH"

MANIFEST_SHA256=$(sha256sum "$MANIFEST_PATH" | cut -d' ' -f1)
sha256sum "$MANIFEST_PATH" >"$MANIFEST_PATH.sha256"
echo "[openclaw-build] PASS image=$RUNTIME_REF manifest=$MANIFEST_PATH manifest_sha256=$MANIFEST_SHA256"
