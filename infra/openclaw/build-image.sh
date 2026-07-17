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

for command in docker jq python3 sha256sum trivy; do
  command -v "$command" >/dev/null || fail "required command not found: $command"
done
MANIFEST_PATH=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$MANIFEST_PATH")
EVIDENCE_DIR=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$EVIDENCE_DIR")
[[ "$EVIDENCE_DIR" != "$MANIFEST_PATH" ]] || fail "evidence directory must differ from manifest path"
[[ ! -L "$MANIFEST_PATH" && ! -L "$MANIFEST_PATH.sha256" ]] || \
  fail "manifest output path must not be a symlink"
[[ "$(dirname -- "$MANIFEST_PATH")" == "$(dirname -- "$EVIDENCE_DIR")" ]] || \
  fail "evidence directory must be a sibling of the release manifest"
EVIDENCE_MANIFEST_PREFIX=$(basename -- "$EVIDENCE_DIR")
[[ "$EVIDENCE_MANIFEST_PREFIX" =~ ^[A-Za-z0-9._-]+$ ]] || \
  fail "evidence directory basename contains unsafe characters"
docker buildx version >/dev/null 2>&1 || fail "docker buildx is unavailable"
jq -e '.schemaVersion == 1' "$LOCK_FILE" >/dev/null || fail "invalid plugins lock"
EXPECTED_BUILDX_VERSION=$(jq -r '.tooling.buildx.version' "$LOCK_FILE")
BUILDX_VERSION=$(docker buildx version | awk '{print $2}')
if [[ "$BUILDX_VERSION" != "v$EXPECTED_BUILDX_VERSION" &&
      "$BUILDX_VERSION" != "v$EXPECTED_BUILDX_VERSION-"* ]]; then
  fail "Docker Buildx v$EXPECTED_BUILDX_VERSION is required (found $BUILDX_VERSION)"
fi
EXPECTED_TRIVY_VERSION=$(jq -r '.tooling.trivy.version' "$LOCK_FILE")
TRIVY_VERSION=$(trivy --version --format json | jq -er '.Version')
[[ "$TRIVY_VERSION" == "$EXPECTED_TRIVY_VERSION" ]] || \
  fail "Trivy $EXPECTED_TRIVY_VERSION is required (found $TRIVY_VERSION)"

SOURCE_COMMIT=${SOURCE_COMMIT:-}
SOURCE_BRANCH=${SOURCE_BRANCH:-}
SOURCE_URI=${SOURCE_URI:-https://github.com/noirelumiere00/teamagent}
SOURCE_ARCHIVE_SHA256=${SOURCE_ARCHIVE_SHA256:-}
SOURCE_ARTIFACT_VERSION=${SOURCE_ARTIFACT_VERSION:-}
ATTESTATION_BUILDER_ID=${ATTESTATION_BUILDER_ID:-}
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
[[ "$SOURCE_URI" == "https://github.com/noirelumiere00/teamagent" ]] || \
  fail "SOURCE_URI must identify the reviewed repository"
[[ "$SOURCE_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]] || \
  fail "SOURCE_ARCHIVE_SHA256 must be a lowercase SHA-256"
[[ -n "$SOURCE_ARTIFACT_VERSION" && ${#SOURCE_ARTIFACT_VERSION} -le 1024 ]] || \
  fail "SOURCE_ARTIFACT_VERSION is required and must not exceed 1024 characters"
[[ "$SOURCE_ARTIFACT_VERSION" =~ ^[A-Za-z0-9._+/=-]+$ ]] || \
  fail "SOURCE_ARTIFACT_VERSION contains unsafe characters"
expected_tag="git-${SOURCE_COMMIT:0:12}"
[[ "$image_tag" == "$expected_tag" ]] || fail "image tag must be $expected_tag"
if ((PUSH)); then
  [[ "$ATTESTATION_BUILDER_ID" =~ ^https://[^[:space:]]+$ ]] || \
    fail "ATTESTATION_BUILDER_ID must be an explicit https URI for --push"
fi

OPENCLAW_VERSION=$(jq -r '.openclaw.version' "$LOCK_FILE")
OPENCLAW_ARM64_DIGEST=$(jq -r '.openclaw.linuxArm64Digest' "$LOCK_FILE")
DISTROLESS_ARM64_DIGEST=$(jq -r '.runtime.linuxArm64Digest' "$LOCK_FILE")
DOCKERFILE_FRONTEND_DIGEST=$(jq -r '.tooling.dockerfileFrontend.digest' "$LOCK_FILE")
SBOM_ATTESTATION_GENERATOR=$(jq -r '.tooling.sbomGenerator | .image + "@" + .linuxArm64Digest' "$LOCK_FILE")
PLUGINS_LOCK_SHA256=$(sha256sum "$LOCK_FILE" | cut -d' ' -f1)
DOCKERFILE_SHA256=$(sha256sum "$DOCKERFILE" | cut -d' ' -f1)
EXPECTED_MATERIAL_DIGESTS=$(jq -c '[
  (.tooling.dockerfileFrontend.digest | sub("^sha256:"; "")),
  (.runtime.linuxArm64Digest | sub("^sha256:"; "")),
  (.openclaw.linuxArm64Digest | sub("^sha256:"; "")),
  (.plugins[].sha256)
] | unique | sort' "$LOCK_FILE")
for pin in "$OPENCLAW_VERSION" "$OPENCLAW_ARM64_DIGEST" "$DISTROLESS_ARM64_DIGEST" "$DOCKERFILE_FRONTEND_DIGEST"; do
  grep -F -- "$pin" "$DOCKERFILE" >/dev/null || fail "Dockerfile does not contain lock pin: $pin"
done

build=(docker buildx build --platform linux/arm64 --pull -f "$DOCKERFILE"
  --build-arg "OPENCLAW_VERSION=$OPENCLAW_VERSION"
  --build-arg "OPENCLAW_ARM64_DIGEST=$OPENCLAW_ARM64_DIGEST"
  --build-arg "DISTROLESS_ARM64_DIGEST=$DISTROLESS_ARM64_DIGEST"
  --build-arg "GIT_COMMIT=$SOURCE_COMMIT"
  --build-arg "GIT_BRANCH=$SOURCE_BRANCH"
  --build-arg "SOURCE_URI=$SOURCE_URI"
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
REGISTRY_PROVENANCE_PATH=""
REGISTRY_SBOM_PATH=""
if ((PUSH)); then
  "${build[@]}" --metadata-file "$tmp_dir/build-metadata.json" \
    --attest "type=provenance,mode=max,builder-id=$ATTESTATION_BUILDER_ID" \
    --attest "type=sbom,generator=$SBOM_ATTESTATION_GENERATOR" \
    --push "$REPO_ROOT"
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

  REGISTRY_PROVENANCE_PATH="$tmp_dir/registry-provenance.json"
  REGISTRY_SBOM_PATH="$tmp_dir/registry-sbom.spdx.json"
  docker buildx imagetools inspect "$IMAGE_REF" \
    --format '{{ json .Provenance }}' >"$REGISTRY_PROVENANCE_PATH"
  docker buildx imagetools inspect "$IMAGE_REF" \
    --format '{{ json .SBOM }}' >"$REGISTRY_SBOM_PATH"
  jq -e \
    --arg builder "$ATTESTATION_BUILDER_ID" \
    --arg commit "$SOURCE_COMMIT" \
    --arg branch "$SOURCE_BRANCH" \
    --arg sourceUri "$SOURCE_URI" \
    --arg archive "$SOURCE_ARCHIVE_SHA256" \
    --arg artifactVersion "$SOURCE_ARTIFACT_VERSION" \
    --arg frontendDigest "$DOCKERFILE_FRONTEND_DIGEST" \
    --arg lock "$PLUGINS_LOCK_SHA256" \
    --argjson expectedMaterials "$EXPECTED_MATERIAL_DIGESTS" '
    [.. | objects |
      select(
        .buildType? == "https://mobyproject.org/buildkit@v1" and
        (.builder? | type) == "object" and
        (.invocation? | type) == "object"
      )
    ] as $predicates |
    ($predicates | length) == 1 and
    ($predicates[0] as $p |
      $p.builder.id == $builder and
      $p.invocation.configSource.entryPoint == "Dockerfile.openclaw" and
      $p.invocation.environment.platform == "linux/arm64" and
      $p.invocation.parameters.args["build-arg:GIT_COMMIT"] == $commit and
      $p.invocation.parameters.args["build-arg:GIT_BRANCH"] == $branch and
      $p.invocation.parameters.args["build-arg:SOURCE_URI"] == $sourceUri and
      $p.invocation.parameters.args["build-arg:SOURCE_ARCHIVE_SHA256"] == $archive and
      $p.invocation.parameters.args["build-arg:SOURCE_ARTIFACT_VERSION"] == $artifactVersion and
      $p.invocation.parameters.args["build-arg:PLUGINS_LOCK_SHA256"] == $lock and
      $p.invocation.parameters.args.source == ("docker/dockerfile:1.7@" + $frontendDigest) and
      ([ $p.materials[]?.digest.sha256 ] | unique) as $actualMaterials |
      ($expectedMaterials - $actualMaterials | length) == 0
    )
  ' "$REGISTRY_PROVENANCE_PATH" >/dev/null || \
    fail "registry provenance source/builder/material validation failed"
  jq -e '
    [.. | objects | select(.spdxVersion? and (.packages? | type) == "array")] as $documents |
    ($documents | length) == 1 and
    ($documents[0].spdxVersion | startswith("SPDX-2.")) and
    ($documents[0].packages | length) > 0 and
    ($documents[0].documentDescribes | length) > 0
  ' "$REGISTRY_SBOM_PATH" >/dev/null || fail "registry SPDX attestation payload validation failed"
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

jq -e \
  --arg builder "$ATTESTATION_BUILDER_ID" \
  --arg commit "$SOURCE_COMMIT" \
  --arg branch "$SOURCE_BRANCH" \
  --arg sourceUri "$SOURCE_URI" \
  --arg archive "$SOURCE_ARCHIVE_SHA256" \
  --arg artifactVersion "$SOURCE_ARTIFACT_VERSION" \
  --arg frontendDigest "$DOCKERFILE_FRONTEND_DIGEST" \
  --arg lock "$PLUGINS_LOCK_SHA256" \
  --argjson expectedMaterials "$EXPECTED_MATERIAL_DIGESTS" \
  --argjson pushed "$PUSH" '
  ."buildx.build.provenance" as $p |
  $p.buildType == "https://mobyproject.org/buildkit@v1" and
  (if $pushed == 1 then $p.builder.id == $builder else true end) and
  $p.invocation.configSource.entryPoint == "Dockerfile.openclaw" and
  $p.invocation.environment.platform == "linux/arm64" and
  $p.invocation.parameters.args["build-arg:GIT_COMMIT"] == $commit and
  $p.invocation.parameters.args["build-arg:GIT_BRANCH"] == $branch and
  $p.invocation.parameters.args["build-arg:SOURCE_URI"] == $sourceUri and
  $p.invocation.parameters.args["build-arg:SOURCE_ARCHIVE_SHA256"] == $archive and
  $p.invocation.parameters.args["build-arg:SOURCE_ARTIFACT_VERSION"] == $artifactVersion and
  $p.invocation.parameters.args["build-arg:PLUGINS_LOCK_SHA256"] == $lock and
  $p.invocation.parameters.args.source == ("docker/dockerfile:1.7@" + $frontendDigest) and
  ([ $p.materials[]?.digest.sha256 ] | unique) as $actualMaterials |
  ($expectedMaterials - $actualMaterials | length) == 0
' "$tmp_dir/build-metadata.json" >/dev/null || fail "build metadata source/material validation failed"

inspect_json=$(docker image inspect "$RUNTIME_REF")
jq -e \
  --arg commit "$SOURCE_COMMIT" \
  --arg branch "$SOURCE_BRANCH" \
  --arg sourceUri "$SOURCE_URI" \
  --arg archive "$SOURCE_ARCHIVE_SHA256" \
  --arg artifactVersion "$SOURCE_ARTIFACT_VERSION" \
  --arg lock "$PLUGINS_LOCK_SHA256" \
  '.[0].Architecture == "arm64" and
   .[0].Os == "linux" and
   .[0].Config.User == "65532:65532" and
   .[0].Config.Volumes["/tmp"] == {} and
   ([.[0].Config.Env[] | select(test("^(SLACK_BOT_TOKEN|SLACK_APP_TOKEN|OPENCLAW_GATEWAY_TOKEN|TEAMAGENT_MCP_BEARER)="))] | length) == 0 and
   .[0].Config.Labels["org.opencontainers.image.source"] == $sourceUri and
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
  "@openclaw/browser-plugin","@typescript/native-preview","esbuild","jiti","jscpd",
  "oxfmt","oxlint","oxlint-tsgolint","playwright","playwright-core",
  "rolldown","rollup","ts-node","tsdown","tsx","typescript","vite","vitest",
]);
const forbiddenPrefixes=[
  "@esbuild/","@openai/codex","@rolldown/","@rollup/","@types/","@vitest/",
];
const isForbidden=name=>
  forbiddenNames.has(name)||forbiddenPrefixes.some(prefix=>name.startsWith(prefix));
const forbiddenPaths=[
  "/bin/sh","/bin/bash","/usr/bin/npm","/usr/local/bin/npm",
  "/usr/local/lib/node_modules/npm","/app/node_modules/npm",
  "/app/node_modules/corepack","/app/node_modules/.bin","/app/node_modules/.pnpm",
  "/app/node_modules/jiti","/app/node_modules/@types",
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
const packageInstances=[];
const forbiddenPackages=[];
const forbiddenDeclarations=[];
const nonRootBinDeclarations=[];
const danglingSymlinks=[];
const developmentPayload=[];
const browserImplementationArtifacts=[];
const developmentDirectoryPattern=/^(?:__fixtures__|__snapshots__|__tests__|bench(?:marks?)?|coverage|examples?|fixtures?|specs?|tests?)$/iu;
const developmentFilePatterns=[
  /\.(?:d\.)?(?:cts|mts|ts|tsx)$/iu,
  /\.map$/iu,
  /\.(?:bench|benchmark|test|spec)\.(?:cjs|js|jsx|mjs|ts|tsx)$/iu,
  /^(?:bench|benchmark|test|tests|spec)\.(?:cjs|js|jsx|mjs|ts|tsx)$/iu,
  /\.snap$/iu,
  /\.flow$/iu,
  /^(?:tsconfig(?:\.[^.]+)?\.json|vite\.config\..+|vitest\.config\..+)$/iu,
];
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
  if(stat.isFile()){
    const basename=path.basename(root);
    if(developmentFilePatterns.some(pattern=>pattern.test(basename))){
      developmentPayload.push(root);
    }
    if(
      root.startsWith("/app/dist/")&&
      /\.(?:c|m)?js$/u.test(root)
    ){
      const source=fs.readFileSync(root,"utf8");
      if(
        source.includes("//#region extensions/browser/")||
        source.includes("function registerBrowserPlugin(")||
        source.includes("registerBrowserCli(program")||
        source.includes("createBrowserPluginService(")||
        /(?:from\s*|import\s*\(\s*|require\(\s*)["'"'"'][^"'"'"']*(?:playwright|pw-ai|chrome-mcp)[^"'"'"']*["'"'"']/iu.test(source)
      ) browserImplementationArtifacts.push(root);
    }
    return;
  }
  if(!stat.isDirectory())return;
  if(developmentDirectoryPattern.test(path.basename(root))){
    developmentPayload.push(`${root}/`);
    return;
  }
  try{
    const packagePath=path.join(root,"package.json");
    const metadata=JSON.parse(fs.readFileSync(packagePath,"utf8"));
    if(metadata.name&&metadata.version){
      packages.push(`${metadata.name}@${metadata.version}`);
      packageInstances.push({
        path:packagePath.replace(/^\/+/u,""),
        name:metadata.name,
        version:metadata.version,
      });
    }
    if(isForbidden(metadata.name||"")){
      forbiddenPackages.push({path:root,name:metadata.name,version:metadata.version||null});
    }
    if(metadata.bin&&packagePath!=="/app/package.json"){
      nonRootBinDeclarations.push({path:packagePath,name:metadata.name||null,bin:metadata.bin});
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
let jitiResolvable=false;
try{require.resolve("jiti",{paths:["/app"]});jitiResolvable=true}catch(error){
  if(error.code!=="MODULE_NOT_FOUND")throw error;
}
const cliMetadata=JSON.parse(fs.readFileSync("/app/dist/cli-startup-metadata.json","utf8"));
const browserHelpMetadataPresent=
  Object.hasOwn(cliMetadata,"browserHelpText")||
  Object.hasOwn(cliMetadata,"browserHelpSourceSignature");
const pruneReport=JSON.parse(
  fs.readFileSync("/opt/teamagent/runtime-prune-report.json","utf8"),
);
const pruneReportValid=
  pruneReport.schemaVersion===1&&
  pruneReport.browser.residualUnreachableBrowserCandidates===0&&
  pruneReport.browser.reachableRegistrationChunks===0&&
  pruneReport.browser.controlUiMissingLocalImports===0&&
  Array.isArray(pruneReport.browser.controlUiReachableAssets)&&
  pruneReport.browser.controlUiReachableAssets.length>0&&
  pruneReport.browser.controlUiReachableAssets.length===
    pruneReport.browser.controlUiReachableModuleCount&&
  Array.isArray(pruneReport.browser.preservedControlUiBrowserChunks)&&
  pruneReport.browser.preservedControlUiBrowserChunks.length>0&&
  pruneReport.browser.preservedControlUiBrowserChunks.every(candidate=>
    candidate.path.startsWith("/app/dist/control-ui/")&&
    /^[0-9a-f]{64}$/u.test(candidate.sha256)&&
    Array.isArray(candidate.implementationSignals)&&
    candidate.implementationSignals.length===0
  )&&
  pruneReport.browser.cliHelpMetadataRemoved===true&&
  pruneReport.packages.residualForbidden===0&&
  pruneReport.packages.residualNonRootBinDeclarations===0&&
  pruneReport.developmentPayload.residualPathCount===0;
packageInstances.sort((left,right)=>
  left.path.localeCompare(right.path)||
  left.name.localeCompare(right.name)||
  left.version.localeCompare(right.version)
);
fs.writeFileSync(1,JSON.stringify({
  node:process.version,uid:process.getuid(),gid:process.getgid(),
  execve:typeof process.execve,packages:[...new Set(packages)].sort(),
  packageInstances,presentPaths,forbiddenPackages,forbiddenDeclarations,
  nonRootBinDeclarations,danglingSymlinks,developmentPayload,
  browserImplementationArtifacts,browserHelpMetadataPresent,jitiResolvable,
  pruneReport,
})+"\n");
process.exit(
  presentPaths.length===0&&forbiddenPackages.length===0&&
  forbiddenDeclarations.length===0&&nonRootBinDeclarations.length===0&&
  danglingSymlinks.length===0&&developmentPayload.length===0&&
  browserImplementationArtifacts.length===0&&!browserHelpMetadataPresent&&
  !jitiResolvable&&pruneReportValid&&
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

python3 "$REPO_ROOT/tests/scripts/test_openclaw_runtime_image.py" \
  --image "$RUNTIME_REF" \
  --output "$EVIDENCE_DIR/actual-image-contract.json"
jq -e --arg imageId "$IMAGE_ID" '
  .schemaVersion == 1 and
  .imageId == $imageId and
  ([.checks[] | select(. != true)] | length) == 0
' "$EVIDENCE_DIR/actual-image-contract.json" >/dev/null || \
  fail "dedicated actual-image contract report failed"

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

if docker run "${run_args[@]}" "$RUNTIME_REF" /app/openclaw.mjs browser --help \
  >"$tmp_dir/browser-help.log" 2>&1; then
  fail "browser CLI help unexpectedly succeeded"
else
  browser_help_exit=$?
fi
[[ "$browser_help_exit" != 0 ]] || fail "browser CLI must be unavailable"
if grep -E -i 'Manage OpenClaw.s dedicated browser|Playwright|browser (status|start|tabs|snapshot)' \
  "$tmp_dir/browser-help.log" >/dev/null; then
  cat "$tmp_dir/browser-help.log" >&2
  fail "browser fast-path help payload remains executable"
fi

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
  process.execPath,"/opt/teamagent/gateway-runtime.mjs",
  "gateway","--bind","loopback","--port","18789"
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
docker exec "$gateway_container" node -e '
const fs=require("fs");
const children=fs.readFileSync("/proc/1/task/1/children","utf8").trim();
if(children){
  console.error(`gateway PID 1 unexpectedly supervises child processes: ${children}`);
  process.exit(1);
}' >/dev/null || fail "gateway PID 1 child-process contract failed"
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
    select(.name == "aquasecurity:trivy:DiffID") | .value] | sort) == ($diffIds | sort)) and
  .bomFormat == "CycloneDX" and
  (.specVersion | test("^1\\.[0-9]+$")) and
  ([.components[]?."bom-ref"] as $refs |
    ($refs | length) == ($refs | unique | length)) and
  (
    ([.components[]?."bom-ref", .metadata.component."bom-ref"] |
      map(select(. != null)) | unique) as $knownRefs |
    ([.dependencies[]? | .ref, .dependsOn[]?] |
      map(select(. != null)) - $knownRefs | length) == 0
  )
' "$EVIDENCE_DIR/sbom.cdx.json" >/dev/null || fail "SBOM subject/generator does not match runtime image"
jq '[
  .components[] |
  select((.purl? != null) and (.purl | startswith("pkg:npm/"))) |
  {
    path: (
      [.properties[]? |
        select(.name == "aquasecurity:trivy:FilePath") |
        .value
      ] |
      if length == 1 then .[0] else
        error("npm component must have exactly one Trivy FilePath")
      end
    ),
    name: (
      (if (.group // "") == "" then "" else (.group + "/") end) + .name
    ),
    version: .version
  }
] | sort_by(.path, .name, .version)' \
  "$EVIDENCE_DIR/sbom.cdx.json" >"$EVIDENCE_DIR/sbom-npm-inventory.json"
jq -e --slurpfile sbom "$EVIDENCE_DIR/sbom-npm-inventory.json" '
  (.runtime.packageInstances | sort_by(.path, .name, .version)) ==
    ($sbom[0] | sort_by(.path, .name, .version)) and
  (.runtime.packageInstances | length) == ($sbom[0] | length)
' "$EVIDENCE_DIR/runtime-inventory.json" >/dev/null || \
  fail "SBOM npm path/name/version multiset does not exactly match the physical runtime"

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
cp "$tmp_dir/browser-help.log" "$EVIDENCE_DIR/browser-help.log"
if ((PUSH)); then
  jq -S . "$REGISTRY_PROVENANCE_PATH" >"$EVIDENCE_DIR/registry-provenance.json"
  jq -S . "$REGISTRY_SBOM_PATH" >"$EVIDENCE_DIR/registry-sbom.spdx.json"
fi

RUNTIME_NODE=$(jq -er '.runtime.node' "$EVIDENCE_DIR/runtime-inventory.json")
RUNTIME_UID=$(jq -er '.runtime.uid' "$EVIDENCE_DIR/runtime-inventory.json")
RUNTIME_GID=$(jq -er '.runtime.gid' "$EVIDENCE_DIR/runtime-inventory.json")
RUNTIME_PACKAGE_COUNT=$(jq -er '.runtime.packages | length' "$EVIDENCE_DIR/runtime-inventory.json")
RUNTIME_PACKAGE_INSTANCE_COUNT=$(jq -er '.runtime.packageInstances | length' "$EVIDENCE_DIR/runtime-inventory.json")
SBOM_COMPONENT_COUNT=$(jq -er '.components | length' "$EVIDENCE_DIR/sbom.cdx.json")
SBOM_NPM_INSTANCE_COUNT=$(jq -er 'length' "$EVIDENCE_DIR/sbom-npm-inventory.json")
SBOM_FORMAT=$(jq -er '.bomFormat + " " + .specVersion' "$EVIDENCE_DIR/sbom.cdx.json")
INVENTORY_SHA256=$(sha256sum "$EVIDENCE_DIR/runtime-inventory.json" | cut -d' ' -f1)
SBOM_SHA256=$(sha256sum "$EVIDENCE_DIR/sbom.cdx.json" | cut -d' ' -f1)
SBOM_NPM_INVENTORY_SHA256=$(sha256sum "$EVIDENCE_DIR/sbom-npm-inventory.json" | cut -d' ' -f1)
VULNERABILITY_SCAN_SHA256=$(sha256sum "$EVIDENCE_DIR/vulnerabilities.json" | cut -d' ' -f1)
SECRET_SCAN_SHA256=$(sha256sum "$EVIDENCE_DIR/secrets.json" | cut -d' ' -f1)
BUILD_METADATA_SHA256=$(sha256sum "$EVIDENCE_DIR/build-metadata.json" | cut -d' ' -f1)
CONFIG_VALIDATION_SHA256=$(sha256sum "$EVIDENCE_DIR/config-validation.json" | cut -d' ' -f1)
PLUGINS_SHA256=$(sha256sum "$EVIDENCE_DIR/plugins.json" | cut -d' ' -f1)
GATEWAY_LOG_SHA256=$(sha256sum "$EVIDENCE_DIR/gateway.log" | cut -d' ' -f1)
BROWSER_HELP_LOG_SHA256=$(sha256sum "$EVIDENCE_DIR/browser-help.log" | cut -d' ' -f1)
ACTUAL_IMAGE_CONTRACT_SHA256=$(sha256sum "$EVIDENCE_DIR/actual-image-contract.json" | cut -d' ' -f1)
REGISTRY_PROVENANCE_SHA256=""
REGISTRY_SBOM_SHA256=""
if ((PUSH)); then
  REGISTRY_PROVENANCE_SHA256=$(sha256sum "$EVIDENCE_DIR/registry-provenance.json" | cut -d' ' -f1)
  REGISTRY_SBOM_SHA256=$(sha256sum "$EVIDENCE_DIR/registry-sbom.spdx.json" | cut -d' ' -f1)
fi

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
  --arg sourceUri "$SOURCE_URI" \
  --arg sourceArchiveSha256 "$SOURCE_ARCHIVE_SHA256" \
  --arg sourceArtifactVersion "$SOURCE_ARTIFACT_VERSION" \
  --arg dockerfileSha256 "$DOCKERFILE_SHA256" \
  --arg pluginsLockSha256 "$PLUGINS_LOCK_SHA256" \
  --arg openclawVersion "$OPENCLAW_VERSION" \
  --arg trivyVersion "$TRIVY_VERSION" \
  --arg sbomAttestationGenerator "$SBOM_ATTESTATION_GENERATOR" \
  --arg attestationDigest "$ATTESTATION_DIGEST" \
  --arg attestationBuilderId "$ATTESTATION_BUILDER_ID" \
  --arg buildxVersion "$BUILDX_VERSION" \
  --arg runtimeNode "$RUNTIME_NODE" \
  --argjson runtimeUid "$RUNTIME_UID" \
  --argjson runtimeGid "$RUNTIME_GID" \
  --argjson runtimePackageCount "$RUNTIME_PACKAGE_COUNT" \
  --argjson runtimePackageInstanceCount "$RUNTIME_PACKAGE_INSTANCE_COUNT" \
  --argjson sbomComponentCount "$SBOM_COMPONENT_COUNT" \
  --argjson sbomNpmInstanceCount "$SBOM_NPM_INSTANCE_COUNT" \
  --arg sbomFormat "$SBOM_FORMAT" \
  --arg inventoryPath "$EVIDENCE_MANIFEST_PREFIX/runtime-inventory.json" \
  --arg inventorySha256 "$INVENTORY_SHA256" \
  --arg sbomPath "$EVIDENCE_MANIFEST_PREFIX/sbom.cdx.json" \
  --arg sbomSha256 "$SBOM_SHA256" \
  --arg sbomNpmInventoryPath "$EVIDENCE_MANIFEST_PREFIX/sbom-npm-inventory.json" \
  --arg sbomNpmInventorySha256 "$SBOM_NPM_INVENTORY_SHA256" \
  --arg vulnerabilityPath "$EVIDENCE_MANIFEST_PREFIX/vulnerabilities.json" \
  --arg vulnerabilitySha256 "$VULNERABILITY_SCAN_SHA256" \
  --arg secretPath "$EVIDENCE_MANIFEST_PREFIX/secrets.json" \
  --arg secretSha256 "$SECRET_SCAN_SHA256" \
  --arg buildMetadataPath "$EVIDENCE_MANIFEST_PREFIX/build-metadata.json" \
  --arg buildMetadataSha256 "$BUILD_METADATA_SHA256" \
  --arg configValidationPath "$EVIDENCE_MANIFEST_PREFIX/config-validation.json" \
  --arg configValidationSha256 "$CONFIG_VALIDATION_SHA256" \
  --arg pluginsPath "$EVIDENCE_MANIFEST_PREFIX/plugins.json" \
  --arg pluginsSha256 "$PLUGINS_SHA256" \
  --arg gatewayLogPath "$EVIDENCE_MANIFEST_PREFIX/gateway.log" \
  --arg gatewayLogSha256 "$GATEWAY_LOG_SHA256" \
  --arg browserHelpLogPath "$EVIDENCE_MANIFEST_PREFIX/browser-help.log" \
  --arg browserHelpLogSha256 "$BROWSER_HELP_LOG_SHA256" \
  --arg actualImageContractPath "$EVIDENCE_MANIFEST_PREFIX/actual-image-contract.json" \
  --arg actualImageContractSha256 "$ACTUAL_IMAGE_CONTRACT_SHA256" \
  --arg registryProvenancePath "$EVIDENCE_MANIFEST_PREFIX/registry-provenance.json" \
  --arg registryProvenanceSha256 "$REGISTRY_PROVENANCE_SHA256" \
  --arg registrySbomPath "$EVIDENCE_MANIFEST_PREFIX/registry-sbom.spdx.json" \
  --arg registrySbomSha256 "$REGISTRY_SBOM_SHA256" \
  --argjson pushed "$PUSH" \
  '{
    schemaVersion:3,
    createdAt:$createdAt,
    image:{
      requested:$image,runtimeRef:$runtimeRef,indexDigest:$indexDigest,
      manifestDigest:$arm64Digest,imageId:$imageId,configDigest:$configDigest,
      rootfs:{diffIds:$rootfsDiffIds,sha256:$rootfsSha256}
    },
    source:{
      uri:$sourceUri,commit:$sourceCommit,branch:$sourceBranch,archiveSha256:$sourceArchiveSha256,
      artifactVersion:$sourceArtifactVersion,dockerfileSha256:$dockerfileSha256,
      pluginsLockSha256:$pluginsLockSha256
    },
    runtime:{
      platform:"linux/arm64",openclawVersion:$openclawVersion,node:$runtimeNode,
      uid:$runtimeUid,gid:$runtimeGid,packageCount:$runtimePackageCount,
      packageInstanceCount:$runtimePackageInstanceCount,
      forbiddenPackageOrPluginArtifacts:0,danglingSymlinks:0,
      developmentPayloadArtifacts:0,browserReachabilityValidated:true,
      controlUiImportClosureValidated:true,
      controlUiHttpAssetClosureValidated:true,
      actualImageContractPassed:true,privilegedPathInventory:true,
      localDockerReadOnlySmoke:true,localDockerCapDropAllSmoke:true,
      localDockerNoNewPrivilegesSmoke:true,
      fargateNoNewPrivilegesEnforced:false,
      fargateWritablePaths:["/tmp"],offlineGatewaySmoke:true,
      readyzSmoke:true,sigtermExitPropagationSmoke:true
    },
    buildAttestations:{
      registryPublished:($pushed == 1),
      provenance:($pushed == 1),sbom:($pushed == 1),
      subjectValidated:($pushed == 1),
      sourceValidated:($pushed == 1),
      builderValidated:($pushed == 1),
      builderId:(if $pushed == 1 then $attestationBuilderId else null end),
      manifestDigest:(if $pushed == 1 then $attestationDigest else null end),
      sbomGenerator:(if $pushed == 1 then $sbomAttestationGenerator else null end),
      signature:false,
      buildxVersion:$buildxVersion,
      localBuildMetadataSourceValidated:true,
      provenanceEvidence:(if $pushed == 1 then
        {path:$registryProvenancePath,sha256:$registryProvenanceSha256}
      else null end),
      sbomEvidence:(if $pushed == 1 then
        {path:$registrySbomPath,sha256:$registrySbomSha256}
      else null end)
    },
    sbom:{
      format:$sbomFormat,generator:{name:"trivy",version:$trivyVersion},
      subjectImageId:$imageId,subjectConfigDigest:$configDigest,
      subjectManifestDigest:$arm64Digest,
      componentCount:$sbomComponentCount,
      npmPackageInstanceCount:$sbomNpmInstanceCount,
      physicalNpmMultisetExactMatch:true,bomRefIntegrity:true,
      path:$sbomPath,sha256:$sbomSha256,
      npmInventoryEvidence:{
        path:$sbomNpmInventoryPath,sha256:$sbomNpmInventorySha256
      }
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
      gatewayLog:{path:$gatewayLogPath,sha256:$gatewayLogSha256},
      browserHelp:{path:$browserHelpLogPath,sha256:$browserHelpLogSha256},
      actualImageContract:{
        path:$actualImageContractPath,sha256:$actualImageContractSha256
      }
    }
  }' \
  >"$MANIFEST_PATH"

MANIFEST_SHA256=$(sha256sum "$MANIFEST_PATH" | cut -d' ' -f1)
printf '%s  %s\n' "$MANIFEST_SHA256" "$(basename -- "$MANIFEST_PATH")" \
  >"$MANIFEST_PATH.sha256"
echo "[openclaw-build] PASS image=$RUNTIME_REF manifest=$MANIFEST_PATH manifest_sha256=$MANIFEST_SHA256"
