#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
DOCKERFILE="$REPO_ROOT/infra/docker/Dockerfile.openclaw"
LOCK_FILE="$REPO_ROOT/infra/openclaw/plugins-lock.json"
BUNDLE_CONTRACT="$REPO_ROOT/infra/codebuild/openclaw_bundle_contract.json"
PROVENANCE_HELPER="$REPO_ROOT/infra/codebuild/openclaw_provenance.py"

usage() {
  echo "usage: $0 --image <local-image:tag> [--manifest <path>] [--evidence-dir <path>]" >&2
}

fail() {
  echo "[openclaw-build] FATAL: $*" >&2
  exit 1
}

IMAGE_REF=""
MANIFEST_PATH=/tmp/openclaw-build-manifest.json
EVIDENCE_DIR=""
while (($#)); do
  case "$1" in
    --image)
      (($# >= 2)) || { usage; exit 2; }
      IMAGE_REF=$2
      shift 2
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

tmp_dir=$(mktemp -d /tmp/openclaw-build.XXXXXX)
gateway_container=""
export_container=""
cleanup() {
  if [[ -n "$gateway_container" ]]; then docker rm -f "$gateway_container" >/dev/null 2>&1 || true; fi
  if [[ -n "$export_container" ]]; then docker rm -f "$export_container" >/dev/null 2>&1 || true; fi
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

for command in docker git jq python3 sha256sum trivy; do
  command -v "$command" >/dev/null || fail "required command not found: $command"
done
[[ -f "$BUNDLE_CONTRACT" && ! -L "$BUNDLE_CONTRACT" ]] || \
  fail "OpenClaw bundle contract is missing or a symlink"
[[ -f "$PROVENANCE_HELPER" && ! -L "$PROVENANCE_HELPER" ]] || \
  fail "OpenClaw provenance helper is missing or a symlink"
BUNDLE_CONTRACT_SHA256=$(
  python3 "$PROVENANCE_HELPER" contract-sha256 --contract "$BUNDLE_CONTRACT"
) || fail "OpenClaw bundle contract is invalid"
[[ "$BUNDLE_CONTRACT_SHA256" =~ ^[0-9a-f]{64}$ ]] || \
  fail "OpenClaw bundle contract returned an invalid SHA-256"
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

SOURCE_URI=https://github.com/noirelumiere00/TeamAgent.git
SOURCE_COMMIT=$(git -C "$REPO_ROOT" rev-parse --verify HEAD^{commit}) || \
  fail "OpenClaw evidence build requires a Git commit"
SOURCE_TREE=$(git -C "$REPO_ROOT" rev-parse --verify HEAD^{tree}) || \
  fail "OpenClaw evidence build requires a Git tree"
SOURCE_ARCHIVE_SHA256=$(
  git -C "$REPO_ROOT" archive --format=tar "$SOURCE_COMMIT" |
    sha256sum |
    cut -d' ' -f1
) || fail "could not hash the exact Git archive"
SOURCE_ARTIFACT_VERSION=git-$SOURCE_COMMIT
BUILD_IDENTITY=local-git-worktree
[[ -z "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all --ignore-submodules=none)" ]] || \
  fail "refusing to build a dirty or untracked source tree"
if SOURCE_BRANCH=$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD); then
  [[ "$SOURCE_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || \
    fail "SOURCE_BRANCH contains unsafe characters"
else
  [[ "${CODEBUILD_RESOLVED_SOURCE_VERSION:-}" == "$SOURCE_COMMIT" &&
      "${CODEBUILD_SOURCE_VERSION:-}" == "$SOURCE_COMMIT" ]] || \
    fail "detached builds require exact CodeBuild source identity"
  [[ "${CODEBUILD_BUILD_ARN:-}" == \
    arn:aws:codebuild:ap-northeast-1:718959508629:build/teamagent-dev-openclaw-provenance-builder:* ]] || \
    fail "detached build has an unexpected CodeBuild identity"
  [[ "$(git -C "$REPO_ROOT" remote get-url origin)" == \
    "https://github.com/noirelumiere00/TeamAgent.git" ]] || \
    fail "detached build has an unexpected Git origin"
  [[ "$(git -C "$REPO_ROOT" rev-parse --verify refs/remotes/origin/dev^{commit})" == \
    "$SOURCE_COMMIT" ]] || fail "detached build is not exact origin/dev"
  SOURCE_BRANCH=dev
  BUILD_IDENTITY=$CODEBUILD_BUILD_ARN
fi

[[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || fail "SOURCE_COMMIT must be a full lowercase Git SHA"
[[ "$SOURCE_TREE" =~ ^[0-9a-f]{40}$ ]] || fail "SOURCE_TREE must be a full lowercase Git SHA"
[[ "$SOURCE_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || fail "SOURCE_BRANCH contains unsafe characters"
[[ "$SOURCE_URI" == "https://github.com/noirelumiere00/TeamAgent.git" ]] || \
  fail "SOURCE_URI must identify the reviewed repository"
[[ "$SOURCE_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]] || \
  fail "SOURCE_ARCHIVE_SHA256 must be a lowercase SHA-256"
[[ -n "$SOURCE_ARTIFACT_VERSION" && ${#SOURCE_ARTIFACT_VERSION} -le 1024 ]] || \
  fail "SOURCE_ARTIFACT_VERSION is required and must not exceed 1024 characters"
[[ "$SOURCE_ARTIFACT_VERSION" =~ ^[A-Za-z0-9._+/=-]+$ ]] || \
  fail "SOURCE_ARTIFACT_VERSION contains unsafe characters"
expected_tag="git-${SOURCE_COMMIT:0:12}"
validation_tag_pattern="^git-${SOURCE_COMMIT:0:12}-build-[1-9][0-9]*$"
if [[ "$image_tag" != "$expected_tag" && ! "$image_tag" =~ $validation_tag_pattern ]]; then
  fail "local image tag must be $expected_tag or git-${SOURCE_COMMIT:0:12}-build-N"
fi

OPENCLAW_VERSION=$(jq -r '.openclaw.version' "$LOCK_FILE")
OPENCLAW_ARM64_DIGEST=$(jq -r '.openclaw.linuxArm64Digest' "$LOCK_FILE")
RUNTIME_ARM64_DIGEST=$(jq -r '.runtime.linuxArm64Digest' "$LOCK_FILE")
DOCKERFILE_FRONTEND_DIGEST=$(jq -r '.tooling.dockerfileFrontend.digest' "$LOCK_FILE")
PLUGINS_LOCK_SHA256=$(sha256sum "$LOCK_FILE" | cut -d' ' -f1)
DOCKERFILE_SHA256=$(sha256sum "$DOCKERFILE" | cut -d' ' -f1)
EXPECTED_MATERIALS=$(jq -c '
  [
    {
      uri: (
        "pkg:docker/docker/dockerfile@1.7?digest=" +
        .tooling.dockerfileFrontend.digest +
        "&platform=linux%2Farm64"
      ),
      sha256: (.tooling.dockerfileFrontend.digest | sub("^sha256:"; ""))
    },
    {
      uri: (
        "pkg:docker/docker/dockerfile@1.7?digest=" +
        .tooling.dockerfileFrontend.digest
      ),
      sha256: (.tooling.dockerfileFrontend.digest | sub("^sha256:"; ""))
    },
    {
      # Expected purl for the Chainguard runtime base. buildx derives it from
      # the FROM reference (repository@tag?digest=...&platform=...). If the
      # first real build fails on material validation, diff the provenance
      # actual output in evidence materials.json against this expectation and
      # align this template with the buildx-emitted purl before promoting.
      uri: (
        "pkg:docker/cgr.dev/chainguard/node@latest?digest=" +
        .runtime.linuxArm64Digest +
        "&platform=linux%2Farm64"
      ),
      sha256: (.runtime.linuxArm64Digest | sub("^sha256:"; ""))
    },
    {
      uri: (
        "pkg:docker/ghcr.io/openclaw/openclaw@" +
        .openclaw.version +
        "?digest=" +
        .openclaw.linuxArm64Digest +
        "&platform=linux%2Farm64"
      ),
      sha256: (.openclaw.linuxArm64Digest | sub("^sha256:"; ""))
    },
    (
      .plugins[] |
      {uri:.tarball,sha256:.sha256}
    )
  ] | sort_by(.uri, .sha256)
' "$LOCK_FILE")
for pin in "$OPENCLAW_VERSION" "$OPENCLAW_ARM64_DIGEST" "$RUNTIME_ARM64_DIGEST" "$DOCKERFILE_FRONTEND_DIGEST"; do
  grep -F -- "$pin" "$DOCKERFILE" >/dev/null || fail "Dockerfile does not contain lock pin: $pin"
done

build=(docker buildx build --platform linux/arm64 --pull -f "$DOCKERFILE"
  --build-arg "OPENCLAW_VERSION=$OPENCLAW_VERSION"
  --build-arg "OPENCLAW_ARM64_DIGEST=$OPENCLAW_ARM64_DIGEST"
  --build-arg "RUNTIME_ARM64_DIGEST=$RUNTIME_ARM64_DIGEST"
  --build-arg "GIT_COMMIT=$SOURCE_COMMIT"
  --build-arg "GIT_BRANCH=$SOURCE_BRANCH"
  --build-arg "SOURCE_TREE=$SOURCE_TREE"
  --build-arg "SOURCE_URI=$SOURCE_URI"
  --build-arg "SOURCE_ARCHIVE_SHA256=$SOURCE_ARCHIVE_SHA256"
  --build-arg "SOURCE_ARTIFACT_VERSION=$SOURCE_ARTIFACT_VERSION"
  --build-arg "PLUGINS_LOCK_SHA256=$PLUGINS_LOCK_SHA256"
  --build-arg "RELEASE_CONTRACT_SHA256=$BUNDLE_CONTRACT_SHA256"
  -t "$IMAGE_REF")

TRIVY_CACHE_DIR=${TRIVY_CACHE_DIR:-$tmp_dir/trivy-cache}
mkdir -p "$TRIVY_CACHE_DIR"
export TRIVY_DB_REPOSITORY=${TRIVY_DB_REPOSITORY:-public.ecr.aws/aquasecurity/trivy-db:2,mirror.gcr.io/aquasec/trivy-db:2,ghcr.io/aquasecurity/trivy-db:2}

"${build[@]}" --metadata-file "$tmp_dir/build-metadata.json" \
  --provenance=false --load "$REPO_ROOT"
# The shared promoter receives this already-gated local image.  This helper has
# no registry push path, so a canonical git-$SHA cannot appear before scans.
ARM64_DIGEST=$(jq -er '."containerimage.digest"' "$tmp_dir/build-metadata.json")
[[ "$ARM64_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || \
  fail "could not resolve local arm64 OCI child digest"
RUNTIME_REF=$IMAGE_REF

jq -e \
  --arg commit "$SOURCE_COMMIT" \
  --arg tree "$SOURCE_TREE" \
  --arg branch "$SOURCE_BRANCH" \
  --arg sourceUri "$SOURCE_URI" \
  --arg archive "$SOURCE_ARCHIVE_SHA256" \
  --arg artifactVersion "$SOURCE_ARTIFACT_VERSION" \
  --arg frontendDigest "$DOCKERFILE_FRONTEND_DIGEST" \
  --arg lock "$PLUGINS_LOCK_SHA256" \
  --arg releaseContract "$BUNDLE_CONTRACT_SHA256" \
  --arg openclawVersion "$OPENCLAW_VERSION" \
  --arg openclawDigest "$OPENCLAW_ARM64_DIGEST" \
  --arg runtimeBaseDigest "$RUNTIME_ARM64_DIGEST" \
  --argjson expectedMaterials "$EXPECTED_MATERIALS" '
  ."buildx.build.provenance" as $p |
  $p.buildType == "https://mobyproject.org/buildkit@v1" and
  $p.invocation.configSource.entryPoint == "Dockerfile.openclaw" and
  $p.invocation.environment.platform == "linux/arm64" and
  ($p.invocation.parameters.args | keys | sort) == ([
    "build-arg:RUNTIME_ARM64_DIGEST",
    "build-arg:GIT_BRANCH",
    "build-arg:GIT_COMMIT",
    "build-arg:OPENCLAW_ARM64_DIGEST",
    "build-arg:OPENCLAW_VERSION",
    "build-arg:PLUGINS_LOCK_SHA256",
    "build-arg:RELEASE_CONTRACT_SHA256",
    "build-arg:SOURCE_ARCHIVE_SHA256",
    "build-arg:SOURCE_ARTIFACT_VERSION",
    "build-arg:SOURCE_TREE",
    "build-arg:SOURCE_URI",
    "cmdline",
    "source"
  ] | sort) and
  $p.invocation.parameters.args["build-arg:GIT_COMMIT"] == $commit and
  $p.invocation.parameters.args["build-arg:GIT_BRANCH"] == $branch and
  $p.invocation.parameters.args["build-arg:SOURCE_TREE"] == $tree and
  $p.invocation.parameters.args["build-arg:OPENCLAW_VERSION"] == $openclawVersion and
  $p.invocation.parameters.args["build-arg:OPENCLAW_ARM64_DIGEST"] == $openclawDigest and
  $p.invocation.parameters.args["build-arg:RUNTIME_ARM64_DIGEST"] == $runtimeBaseDigest and
  $p.invocation.parameters.args["build-arg:SOURCE_URI"] == $sourceUri and
  $p.invocation.parameters.args["build-arg:SOURCE_ARCHIVE_SHA256"] == $archive and
  $p.invocation.parameters.args["build-arg:SOURCE_ARTIFACT_VERSION"] == $artifactVersion and
  $p.invocation.parameters.args["build-arg:PLUGINS_LOCK_SHA256"] == $lock and
  $p.invocation.parameters.args["build-arg:RELEASE_CONTRACT_SHA256"] == $releaseContract and
  $p.invocation.parameters.args.source == ("docker/dockerfile:1.7@" + $frontendDigest) and
  ([ $p.materials[] | {uri:.uri,sha256:.digest.sha256} ] |
    sort_by(.uri, .sha256)) == $expectedMaterials
' "$tmp_dir/build-metadata.json" >/dev/null || fail "build metadata source/material validation failed"

jq -n \
  --argjson expected "$EXPECTED_MATERIALS" \
  --slurpfile metadata "$tmp_dir/build-metadata.json" '
  ($metadata[0]."buildx.build.provenance".materials |
    map({uri:.uri,sha256:.digest.sha256}) |
    sort_by(.uri, .sha256)) as $actual |
  {
    schemaVersion:1,
    expected:$expected,
    actual:$actual,
    expectedCount:($expected | length),
    actualCount:($actual | length),
    extra:($actual - $expected),
    missing:($expected - $actual),
    exactSetMatch:($actual == $expected),
    extraExecutableOrRemoteMaterialDetected:($actual != $expected)
  }
' >"$tmp_dir/materials.json"

inspect_json=$(docker image inspect "$RUNTIME_REF")
jq -e \
  --arg commit "$SOURCE_COMMIT" \
  --arg tree "$SOURCE_TREE" \
  --arg branch "$SOURCE_BRANCH" \
  --arg sourceUri "$SOURCE_URI" \
  --arg archive "$SOURCE_ARCHIVE_SHA256" \
  --arg artifactVersion "$SOURCE_ARTIFACT_VERSION" \
  --arg lock "$PLUGINS_LOCK_SHA256" \
  --arg releaseContract "$BUNDLE_CONTRACT_SHA256" \
  '.[0].Architecture == "arm64" and
   .[0].Os == "linux" and
   .[0].Config.User == "65532:65532" and
   .[0].Config.Volumes["/tmp"] == {} and
   ([.[0].Config.Env[] | select(test("^(SLACK_BOT_TOKEN|SLACK_APP_TOKEN|OPENCLAW_GATEWAY_TOKEN|TEAMAGENT_MCP_BEARER|TEAMAGENT_CALLER_CLAIM_SECRET|SLACK_TEAM_ID)="))] | length) == 0 and
   .[0].Config.Labels["org.opencontainers.image.source"] == $sourceUri and
   .[0].Config.Labels["org.opencontainers.image.revision"] == $commit and
   .[0].Config.Labels["io.teamagent.source.branch"] == $branch and
   .[0].Config.Labels["io.teamagent.source.tree"] == $tree and
   .[0].Config.Labels["io.teamagent.source.archive.sha256"] == $archive and
   .[0].Config.Labels["io.teamagent.source.artifact.version"] == $artifactVersion and
   .[0].Config.Labels["io.teamagent.openclaw.plugins-lock.sha256"] == $lock and
   .[0].Config.Labels["io.teamagent.build.contract-sha256"] == $releaseContract and
   .[0].Config.Labels["io.teamagent.runtime.kind"] == "core" and
   .[0].Config.Labels["io.teamagent.runtime.readonly-rootfs-required"] == "true"' \
  <<<"$inspect_json" >/dev/null || fail "runtime image metadata contract failed"

IMAGE_ID=$(jq -er '.[0].Id' <<<"$inspect_json")
CONFIG_DIGEST=$(jq -er '.[0].Descriptor.annotations["config.digest"] // .[0].Id' <<<"$inspect_json")
INSPECT_MANIFEST_DIGEST=$(jq -r '.[0].Descriptor.digest // empty' <<<"$inspect_json")
ROOTFS_DIFF_IDS=$(jq -c '.[0].RootFS.Layers' <<<"$inspect_json")
ROOTFS_DIFF_IDS_SHA256=$(jq -c '.[0].RootFS.Layers' <<<"$inspect_json" | sha256sum | cut -d' ' -f1)
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "invalid local image identifier"
[[ "$CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "invalid image config digest"
if [[ -n "$INSPECT_MANIFEST_DIGEST" && "$INSPECT_MANIFEST_DIGEST" != "$ARM64_DIGEST" ]]; then
  fail "loaded manifest digest does not match build metadata"
fi
[[ "$ROOTFS_DIFF_IDS_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "invalid rootfs diff-id set hash"

[[ ! -L "$EVIDENCE_DIR" ]] || fail "evidence directory must not be a symlink"
mkdir -p "$EVIDENCE_DIR"
[[ -z "$(find "$EVIDENCE_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] || \
  fail "evidence directory must be empty: $EVIDENCE_DIR"

docker run --rm --user 0:0 --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --entrypoint /usr/bin/node "$RUNTIME_REF" -e '
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
  --security-opt no-new-privileges --entrypoint /usr/bin/node "$RUNTIME_REF" -e '
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
  pruneReport.schemaVersion===2&&
  pruneReport.browser.residualUnreachableBrowserCandidates===0&&
  pruneReport.browser.reachableRegistrationChunks===0&&
  pruneReport.browser.reachableBrowserNamedPayloadCount>0&&
  pruneReport.browser.reachableBrowserPayloadZero===false&&
  pruneReport.browser.reachableBrowserImplementationModules===0&&
  pruneReport.browser.browserCliCommandRegistered===false&&
  pruneReport.browser.genericOpenClawCliRetained===true&&
  pruneReport.browser.browserExecutableOrPlaywrightPresent===false&&
  pruneReport.browser.usableBrowserControlPath===false&&
  Array.isArray(pruneReport.browser.retainedFailClosedFacade)&&
  pruneReport.browser.retainedFailClosedFacade.length===1&&
  pruneReport.browser.retainedFailClosedFacade.every(candidate=>
    candidate.publicFacade===true&&
    Array.isArray(candidate.implementationSignals)&&
    candidate.implementationSignals.length===0
  )&&
  pruneReport.browser.controlUiMissingLocalImports===0&&
  Array.isArray(pruneReport.browser.controlUiReachableModuleAssets)&&
  pruneReport.browser.controlUiReachableModuleAssets.length>0&&
  pruneReport.browser.controlUiReachableModuleAssets.length===
    pruneReport.browser.controlUiReachableModuleCount&&
  Array.isArray(pruneReport.browser.controlUiServedAssets)&&
  pruneReport.browser.controlUiServedAssets.length>0&&
  pruneReport.browser.controlUiServedAssets.length===
    pruneReport.browser.controlUiServedAssetCount&&
  pruneReport.browser.controlUiServedAssets.some(candidate=>
    candidate.httpPath==="/"&&candidate.path.endsWith("/index.html")
  )&&
  pruneReport.browser.controlUiServedAssets.every(candidate=>
    candidate.path.startsWith("/app/dist/control-ui/")&&
    candidate.httpPath.startsWith("/")&&
    Number.isSafeInteger(candidate.size)&&candidate.size>=0&&
    /^[0-9a-f]{64}$/u.test(candidate.sha256)&&
    Number.isSafeInteger(candidate.servedSize)&&candidate.servedSize>=0&&
    /^[0-9a-f]{64}$/u.test(candidate.servedSha256)&&
    ["identity","insert data-openclaw-terminal-enabled=\"false\" after <html"]
      .includes(candidate.httpTransform)
  )&&
  pruneReport.browser.controlUiDynamicAssetRegistrationsCoveredByWholeTree===true&&
  pruneReport.browser.controlUiBootstrapConfigRuntimeContractRequired===true&&
  Array.isArray(pruneReport.browser.preservedControlUiBrowserChunks)&&
  pruneReport.browser.preservedControlUiBrowserChunks.length>0&&
  pruneReport.browser.preservedControlUiBrowserChunks.every(candidate=>
    candidate.path.startsWith("/app/dist/control-ui/")&&
    /^[0-9a-f]{64}$/u.test(candidate.sha256)&&
    Array.isArray(candidate.implementationSignals)&&
    candidate.implementationSignals.length===0
  )&&
  pruneReport.browser.cliHelpMetadataRemoved===true&&
  pruneReport.pluginOperations.closureComputedBeforeMetadataRewrite===true&&
  pruneReport.pluginOperations.postPruneClosureExactMatch===true&&
  pruneReport.pluginOperations.moduleCount>0&&
  pruneReport.pluginOperations.moduleCount===
    pruneReport.pluginOperations.modules.length&&
  pruneReport.pluginOperations.unresolvedImports.length===0&&
  pruneReport.pluginOperations.unresolvedComputedImports.length===0&&
  pruneReport.packages.closureComputedBeforeMetadataRewrite===true&&
  pruneReport.packages.prePruneProductionClosure.length>0&&
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
);' >"$tmp_dir/runtime-probe.json" || fail "chainguard/nonroot/runtime inventory contract failed"

jq -e . "$tmp_dir/runtime-probe.json" >/dev/null || fail "runtime inventory is not valid JSON"
jq -n \
  --arg manifestDigest "$ARM64_DIGEST" \
  --arg imageId "$IMAGE_ID" \
  --arg configDigest "$CONFIG_DIGEST" \
  --arg rootfsDiffIdsSha256 "$ROOTFS_DIFF_IDS_SHA256" \
  --argjson diffIds "$ROOTFS_DIFF_IDS" \
  --slurpfile probe "$tmp_dir/runtime-probe.json" \
  '{schemaVersion:1,subject:{platform:"linux/arm64",manifestDigest:$manifestDigest,imageId:$imageId,configDigest:$configDigest,rootfs:{diffIds:$diffIds,diffIdsSha256:$rootfsDiffIdsSha256}},runtime:$probe[0]}' \
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

docker run --rm --platform linux/arm64 --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --mount "type=bind,src=$REPO_ROOT/infra/openclaw/plugin-operation-smoke.mjs,dst=/opt/openclaw-plugin-operation-smoke.mjs,readonly" \
  --entrypoint /usr/bin/node "$RUNTIME_REF" \
  /opt/openclaw-plugin-operation-smoke.mjs \
  >"$EVIDENCE_DIR/plugin-operation-smoke.json" || \
  fail "representative Slack/Bedrock operation module smoke failed"
jq -e '
  .schemaVersion == 1 and .passed == true and
  .network == "disabled-by-container" and
  .slack.providerCallsStubbed == true and
  .slack.operations == ["conversations.history","chat.update"] and
  .bedrock.providerCallsStubbed == true and
  .bedrock.operations == [
    "ListFoundationModelsCommand",
    "ListInferenceProfilesCommand"
  ]
' "$EVIDENCE_DIR/plugin-operation-smoke.json" >/dev/null || \
  fail "Slack/Bedrock operation smoke evidence is incomplete"

run_args=(--rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges
  --tmpfs /tmp:rw,noexec,nosuid,size=512m
  -e SLACK_BOT_TOKEN=xoxb-offline-smoke
  -e SLACK_APP_TOKEN=xapp-offline-smoke
  -e OPENCLAW_GATEWAY_TOKEN=offline-gateway-smoke
  -e TEAMAGENT_MCP_BEARER=offline-mcp-smoke
  -e TEAMAGENT_CALLER_CLAIM_SECRET=offline-caller-claim-secret-32-bytes
  -e SLACK_TEAM_ID=T0123456789
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
  "$RUNTIME_REF" /usr/bin/node -e 'console.log(JSON.stringify(process.env))' \
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
    "SLACK_DM_ALLOWLIST","SLACK_TEAM_ID","SSL_CERT_DIR","SSL_CERT_FILE",
    "TEAMAGENT_CALLER_CLAIM_SECRET","TEAMAGENT_MCP_BEARER",
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

if docker run "${run_args[@]}" "$RUNTIME_REF" /usr/bin/node -e 'process.exit(42)' \
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
  ([.plugins[] | select(.id == "teamagent-caller-identity" and .version == "1.0.0" and .status == "loaded")] | length) == 1 and
  ([.plugins[] | select(.id == "browser" and .status == "loaded")] | length) == 0' \
  "$tmp_dir/plugins.json" >/dev/null || fail "reviewed plugin compatibility smoke failed"

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
  -e TEAMAGENT_CALLER_CLAIM_SECRET=offline-caller-claim-secret-32-bytes
  -e SLACK_TEAM_ID=T0123456789
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
  /usr/bin/node -e "$gateway_launcher")
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
   grep -F -e xoxb-offline-smoke -e xapp-offline-smoke -e offline-gateway-smoke -e offline-mcp-smoke \
     -e offline-caller-claim-secret-32-bytes "$tmp_dir/gateway.log" >/dev/null; then
  tail -120 "$tmp_dir/gateway.log" >&2
  fail "gateway attempted package repair or enabled the browser plugin"
fi
docker stop --time 30 "$gateway_container" >/dev/null
gateway_exit=$(docker inspect -f '{{.State.ExitCode}}' "$gateway_container")
[[ "$gateway_exit" == 0 ]] || fail "gateway SIGTERM shutdown must exit 0 (got $gateway_exit)"
docker rm "$gateway_container" >/dev/null
gateway_container=""

export_container=$(docker create --platform linux/arm64 "$RUNTIME_REF")
docker export --output "$tmp_dir/rootfs.tar" "$export_container"
docker rm "$export_container" >/dev/null
export_container=""
[[ -s "$tmp_dir/rootfs.tar" ]] || fail "merged rootfs export is empty"

trivy --cache-dir "$TRIVY_CACHE_DIR" image --quiet --scanners vuln --format cyclonedx \
  --output "$EVIDENCE_DIR/trivy-package-sbom.cdx.json" "$RUNTIME_REF"
python3 "$REPO_ROOT/infra/openclaw/generate-filesystem-sbom.py" \
  --rootfs-tar "$tmp_dir/rootfs.tar" \
  --trivy-sbom "$EVIDENCE_DIR/trivy-package-sbom.cdx.json" \
  --inventory-output "$EVIDENCE_DIR/rootfs-inventory.json" \
  --sbom-output "$EVIDENCE_DIR/sbom.cdx.json" \
  --equivalence-output "$EVIDENCE_DIR/sbom-equivalence.json" \
  --image-id "$IMAGE_ID" \
  --manifest-digest "$ARM64_DIGEST" \
  --config-digest "$CONFIG_DIGEST"
jq -e \
  --arg imageId "$IMAGE_ID" \
  --arg manifestDigest "$ARM64_DIGEST" \
  --arg configDigest "$CONFIG_DIGEST" '
  .schemaVersion == 1 and
  .subject.imageId == $imageId and
  .subject.manifestDigest == $manifestDigest and
  .subject.configDigest == $configDigest and
  .inventory.entryCount > 0 and
  .inventory.entryCount == .sbom.filesystemComponentCount and
  (.inventory.sha256 | test("^[0-9a-f]{64}$")) and
  (.sbom.sha256 | test("^[0-9a-f]{64}$")) and
  .sbom.bomRefsUnique == true and
  .sbom.danglingDependencyRefs == 0 and
  .pathTypeModeOwnerSizeLinkContentMultisetExact == true and
  .wholeFilesystemExactMatch == true
' "$EVIDENCE_DIR/sbom-equivalence.json" >/dev/null || \
  fail "whole-filesystem SBOM is not exactly equivalent to the merged rootfs export"
jq -e \
  --arg imageId "$IMAGE_ID" \
  --arg manifestDigest "$ARM64_DIGEST" \
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
  ([.metadata.properties[] |
    select(.name == "io.teamagent.openclaw.subjectManifestDigest") |
    .value] == [$manifestDigest]) and
  ([.metadata.properties[] |
    select(.name == "io.teamagent.openclaw.wholeFilesystemEntryCount") |
    .value | tonumber] | length) == 1 and
  ([.compositions[]? |
    select(.aggregate == "complete") |
    .assemblies | length] | length) >= 1 and
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

KNOWN_LIVE_FINDINGS='[
  {"id":"CVE-2026-34182","packageFamily":"openssl"},
  {"id":"CVE-2026-12087","packageFamily":"perl"},
  {"id":"CVE-2026-13221","packageFamily":"perl"},
  {"id":"CVE-2026-57433","packageFamily":"perl"},
  {"id":"CVE-2026-6100","packageFamily":"python3.11"},
  {"id":"CVE-2026-33845","packageFamily":"gnutls28"},
  {"id":"CVE-2026-42010","packageFamily":"gnutls28"},
  {"id":"CVE-2026-55200","packageFamily":"libssh2"}
]'
jq -n \
  --arg imageId "$IMAGE_ID" \
  --arg manifestDigest "$ARM64_DIGEST" \
  --arg configDigest "$CONFIG_DIGEST" \
  --argjson known "$KNOWN_LIVE_FINDINGS" \
  --slurpfile scan "$EVIDENCE_DIR/vulnerabilities.json" '
  ([ $scan[0].Results[]?.Vulnerabilities[]? ] // []) as $findings |
  ([ $findings[]?.VulnerabilityID ] | unique | sort) as $candidateIds |
  ($known | map(.id) | unique | sort) as $knownIds |
  {
    schemaVersion:1,
    subject:{
      platform:"linux/arm64",
      manifestCount:1,
      manifestDigest:$manifestDigest,
      imageId:$imageId,
      configDigest:$configDigest
    },
    latestLiveObservedBaseline:{
      criticalFindingCount:8,
      highFindingCount:22,
      knownFindings:$known
    },
    candidate:{
      criticalFindingCount:(
        [$findings[] | select(.Severity == "CRITICAL")] | length
      ),
      highFindingCount:(
        [$findings[] | select(.Severity == "HIGH")] | length
      ),
      knownLiveFindingIdsPresent:($candidateIds - ($candidateIds - $knownIds)),
      knownLiveFindingIdsAbsent:($knownIds - $candidateIds),
      allKnownLiveFindingsAbsent:
        (($candidateIds - ($candidateIds - $knownIds)) == []),
      totalCriticalHighZero:($findings == [])
    }
  }' >"$EVIDENCE_DIR/live-vulnerability-baseline.json"
jq -e --argjson known "$KNOWN_LIVE_FINDINGS" '
  .schemaVersion == 1 and
  .subject.platform == "linux/arm64" and
  .subject.manifestCount == 1 and
  .latestLiveObservedBaseline.criticalFindingCount == 8 and
  .latestLiveObservedBaseline.highFindingCount == 22 and
  .candidate.criticalFindingCount == 0 and
  .candidate.highFindingCount == 0 and
  .candidate.knownLiveFindingIdsPresent == [] and
  .candidate.knownLiveFindingIdsAbsent == ($known | map(.id) | unique | sort) and
  .candidate.allKnownLiveFindingsAbsent == true and
  .candidate.totalCriticalHighZero == true
' "$EVIDENCE_DIR/live-vulnerability-baseline.json" >/dev/null || \
  fail "candidate does not eliminate the observed live C8/H22 findings"

jq -S . "$tmp_dir/build-metadata.json" >"$EVIDENCE_DIR/build-metadata.json"
jq -S . "$tmp_dir/materials.json" >"$EVIDENCE_DIR/materials.json"
jq -S . "$tmp_dir/config.json" >"$EVIDENCE_DIR/config-validation.json"
jq -S . "$tmp_dir/plugins.json" >"$EVIDENCE_DIR/plugins.json"
cp "$tmp_dir/gateway.log" "$EVIDENCE_DIR/gateway.log"
cp "$tmp_dir/browser-help.log" "$EVIDENCE_DIR/browser-help.log"

SOURCE_TRUST_MODE=local-git
if [[ "$BUILD_IDENTITY" == arn:aws:codebuild:* ]]; then
  SOURCE_TRUST_MODE=codebuild-exact-origin-dev
fi
jq -n \
  --arg mode "$SOURCE_TRUST_MODE" \
  --arg uri "$SOURCE_URI" \
  --arg commit "$SOURCE_COMMIT" \
  --arg tree "$SOURCE_TREE" \
  --arg branch "$SOURCE_BRANCH" \
  --arg archiveSha256 "$SOURCE_ARCHIVE_SHA256" \
  --arg artifactVersion "$SOURCE_ARTIFACT_VERSION" \
  --arg buildIdentity "$BUILD_IDENTITY" \
  --arg releaseContractSha256 "$BUNDLE_CONTRACT_SHA256" \
  '{
    schemaVersion:1,
    mode:$mode,
    source:{
      repositoryUri:$uri,
      commit:$commit,
      tree:$tree,
      branch:$branch,
      archiveSha256:$archiveSha256,
      artifactVersion:$artifactVersion
    },
    buildIdentity:$buildIdentity,
    releaseContractSha256:$releaseContractSha256,
    signedSourceManifestRequiredBeforeRegistryPromotion:true,
    transportMetadataTrusted:false,
    promotionMustReverifySignedSourceManifestAndSourceRoot:true,
    selfCertificationAccepted:false
  }' >"$EVIDENCE_DIR/source-binding.json"

RUNTIME_INVENTORY_SHA256=$(sha256sum "$EVIDENCE_DIR/runtime-inventory.json" | cut -d' ' -f1)
ROOTFS_INVENTORY_SHA256=$(sha256sum "$EVIDENCE_DIR/rootfs-inventory.json" | cut -d' ' -f1)
ROOTFS_ENTRY_COUNT=$(jq -er '.entryCount' "$EVIDENCE_DIR/rootfs-inventory.json")

python3 "$REPO_ROOT/infra/openclaw/index-evidence.py" \
  --evidence-dir "$EVIDENCE_DIR" \
  --output "$EVIDENCE_DIR/evidence-index.json" \
  --image-id "$IMAGE_ID" \
  --manifest-digest "$ARM64_DIGEST" \
  --config-digest "$CONFIG_DIGEST" \
  --rootfs-inventory-sha256 "$ROOTFS_INVENTORY_SHA256"
EVIDENCE_INDEX_SHA256=$(sha256sum "$EVIDENCE_DIR/evidence-index.json" | cut -d' ' -f1)

mkdir -p "$(dirname -- "$MANIFEST_PATH")"
jq -n \
  --arg createdAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg image "$IMAGE_REF" \
  --arg arm64Digest "$ARM64_DIGEST" \
  --arg imageId "$IMAGE_ID" \
  --arg configDigest "$CONFIG_DIGEST" \
  --argjson rootfsDiffIds "$ROOTFS_DIFF_IDS" \
  --arg rootfsDiffIdsSha256 "$ROOTFS_DIFF_IDS_SHA256" \
  --arg rootfsInventorySha256 "$ROOTFS_INVENTORY_SHA256" \
  --argjson rootfsEntryCount "$ROOTFS_ENTRY_COUNT" \
  --arg sourceCommit "$SOURCE_COMMIT" \
  --arg sourceTree "$SOURCE_TREE" \
  --arg sourceBranch "$SOURCE_BRANCH" \
  --arg sourceUri "$SOURCE_URI" \
  --arg sourceArchiveSha256 "$SOURCE_ARCHIVE_SHA256" \
  --arg sourceArtifactVersion "$SOURCE_ARTIFACT_VERSION" \
  --arg sourceTrustMode "$SOURCE_TRUST_MODE" \
  --arg buildIdentity "$BUILD_IDENTITY" \
  --arg dockerfileSha256 "$DOCKERFILE_SHA256" \
  --arg pluginsLockSha256 "$PLUGINS_LOCK_SHA256" \
  --arg releaseContractSha256 "$BUNDLE_CONTRACT_SHA256" \
  --arg openclawVersion "$OPENCLAW_VERSION" \
  --arg trivyVersion "$TRIVY_VERSION" \
  --arg buildxVersion "$BUILDX_VERSION" \
  --arg evidencePrefix "$EVIDENCE_MANIFEST_PREFIX" \
  --arg evidenceIndexSha256 "$EVIDENCE_INDEX_SHA256" \
  --arg runtimeInventorySha256 "$RUNTIME_INVENTORY_SHA256" \
  --slurpfile runtimeInventory "$EVIDENCE_DIR/runtime-inventory.json" \
  --slurpfile materials "$EVIDENCE_DIR/materials.json" \
  --slurpfile equivalence "$EVIDENCE_DIR/sbom-equivalence.json" \
  --slurpfile sbom "$EVIDENCE_DIR/sbom.cdx.json" \
  --slurpfile evidenceIndex "$EVIDENCE_DIR/evidence-index.json" \
  --slurpfile actualImage "$EVIDENCE_DIR/actual-image-contract.json" \
  --slurpfile operationSmoke "$EVIDENCE_DIR/plugin-operation-smoke.json" \
  --slurpfile liveVulnerabilityBaseline "$EVIDENCE_DIR/live-vulnerability-baseline.json" \
  '{
    schemaVersion:5,
    createdAt:$createdAt,
    deploymentCredential:false,
    image:{
      requestedLocalTag:$image,
      platform:"linux/arm64",
      manifestDigest:$arm64Digest,
      imageId:$imageId,
      configDigest:$configDigest,
      rootfs:{
        diffIds:$rootfsDiffIds,
        diffIdsSha256:$rootfsDiffIdsSha256,
        inventorySha256:$rootfsInventorySha256,
        entryCount:$rootfsEntryCount
      }
    },
    source:{
      uri:$sourceUri,
      commit:$sourceCommit,
      tree:$sourceTree,
      branch:$sourceBranch,
      archiveSha256:$sourceArchiveSha256,
      artifactVersion:$sourceArtifactVersion,
      trustMode:$sourceTrustMode,
      buildIdentity:$buildIdentity,
      dockerfileSha256:$dockerfileSha256,
      pluginsLockSha256:$pluginsLockSha256,
      releaseContractSha256:$releaseContractSha256,
      transportMetadataTrusted:false,
      promotionReverificationRequired:true,
      evidence:{path:($evidencePrefix + "/source-binding.json")}
    },
    promotion:{
      status:"LOCAL_GATES_PASSED",
      registryPublished:false,
      quarantinePublished:false,
      canonicalTagPublished:false,
      canonicalTagImmutable:null,
      registryReferrersPublished:false,
      signedProvenancePublished:false,
      signedSbomPublished:false,
      imageSignaturePublished:false,
      sharedPromoterRequired:true
    },
    materials:{
      expectedCount:$materials[0].expectedCount,
      actualCount:$materials[0].actualCount,
      exactSetMatch:$materials[0].exactSetMatch,
      extraExecutableOrRemoteMaterialDetected:
        $materials[0].extraExecutableOrRemoteMaterialDetected,
      extra:$materials[0].extra,
      missing:$materials[0].missing,
      evidence:{path:($evidencePrefix + "/materials.json")}
    },
    runtime:{
      platform:"linux/arm64",
      openclawVersion:$openclawVersion,
      node:$runtimeInventory[0].runtime.node,
      uid:$runtimeInventory[0].runtime.uid,
      gid:$runtimeInventory[0].runtime.gid,
      packageCount:($runtimeInventory[0].runtime.packages | length),
      packageInstanceCount:
        ($runtimeInventory[0].runtime.packageInstances | length),
      forbiddenPackageOrPluginArtifacts:0,
      danglingSymlinks:0,
      developmentPayloadArtifacts:0,
      dependencyClosureComputedBeforeMetadataRewrite:
        $runtimeInventory[0].runtime.pruneReport.packages.closureComputedBeforeMetadataRewrite,
      pluginOperationClosureExact:
        $runtimeInventory[0].runtime.pruneReport.pluginOperations.postPruneClosureExactMatch,
      representativePluginOperationSmokePassed:$operationSmoke[0].passed,
      browser:{
        reachableNamedSharedPayloadRetained:
          ($runtimeInventory[0].runtime.pruneReport.browser.reachableBrowserNamedPayloadCount > 0),
        zeroReachablePayloadClaim:false,
        implementationModules:
          $runtimeInventory[0].runtime.pruneReport.browser.reachableBrowserImplementationModules,
        cliRegistered:
          $runtimeInventory[0].runtime.pruneReport.browser.browserCliCommandRegistered,
        executableOrPlaywrightPresent:
          $runtimeInventory[0].runtime.pruneReport.browser.browserExecutableOrPlaywrightPresent,
        usableControlPath:
          $runtimeInventory[0].runtime.pruneReport.browser.usableBrowserControlPath,
        failClosedFacadeRetained:true
      },
      controlUiImportClosureValidated:true,
      controlUiFullAssetClosureValidated:true,
      controlUiServedAssetCount:
        $runtimeInventory[0].runtime.pruneReport.browser.controlUiServedAssetCount,
      actualImageContractPassed:
        (([ $actualImage[0].checks[] | select(. != true) ] | length) == 0),
      privilegedPathInventory:true,
      localDockerReadOnlySmoke:true,
      localDockerCapDropAllSmoke:true,
      localDockerNoNewPrivilegesSmoke:true,
      fargateNoNewPrivilegesEnforced:false,
      fargateNoNewPrivilegesResidualRisk:
        "ECS/Fargate task definitions cannot enforce Docker no-new-privileges",
      fargateWritablePaths:["/tmp"],
      offlineGatewaySmoke:true,
      readyzSmoke:true,
      sigtermExitPropagationSmoke:true
    },
    localBuildProvenance:{
      signed:false,
      registrySubject:false,
      buildxVersion:$buildxVersion,
      sourceAndExactMaterialsValidated:true,
      evidence:{path:($evidencePrefix + "/build-metadata.json")}
    },
    sbom:{
      format:($sbom[0].bomFormat + " " + $sbom[0].specVersion),
      generator:{name:"trivy",version:$trivyVersion},
      subjectImageId:$imageId,
      subjectConfigDigest:$configDigest,
      subjectManifestDigest:$arm64Digest,
      componentCount:($sbom[0].components | length),
      filesystemComponentCount:
        $equivalence[0].sbom.filesystemComponentCount,
      rootfsEntryCount:$equivalence[0].inventory.entryCount,
      wholeFilesystemExactMatch:$equivalence[0].wholeFilesystemExactMatch,
      pathTypeModeOwnerSizeLinkContentMultisetExact:
        $equivalence[0].pathTypeModeOwnerSizeLinkContentMultisetExact,
      physicalNpmMultisetExactMatch:true,
      bomRefIntegrity:
        ($equivalence[0].sbom.bomRefsUnique and
         ($equivalence[0].sbom.danglingDependencyRefs == 0)),
      evidence:{
        sbom:{path:($evidencePrefix + "/sbom.cdx.json")},
        inventory:{path:($evidencePrefix + "/rootfs-inventory.json")},
        equivalence:{path:($evidencePrefix + "/sbom-equivalence.json")},
        npmMultiset:{path:($evidencePrefix + "/sbom-npm-inventory.json")}
      }
    },
    scan:{
      trivyVersion:$trivyVersion,
      subjectImageId:$imageId,
      subjectConfigDigest:$configDigest,
      subjectManifestDigest:$arm64Digest,
      critical:0,
      high:0,
      secrets:0,
      exactSingleLinuxArm64Subject:
        ($liveVulnerabilityBaseline[0].subject.platform == "linux/arm64" and
         $liveVulnerabilityBaseline[0].subject.manifestCount == 1),
      latestLiveObservedBaseline:
        $liveVulnerabilityBaseline[0].latestLiveObservedBaseline,
      knownLiveFindingIdsAbsent:
        $liveVulnerabilityBaseline[0].candidate.knownLiveFindingIdsAbsent,
      allKnownLiveFindingsAbsent:
        $liveVulnerabilityBaseline[0].candidate.allKnownLiveFindingsAbsent,
      vulnerabilityEvidence:{path:($evidencePrefix + "/vulnerabilities.json")},
      secretEvidence:{path:($evidencePrefix + "/secrets.json")},
      liveBaselineEvidence:{
        path:($evidencePrefix + "/live-vulnerability-baseline.json")
      }
    },
    evidence:{
      allRegularEvidenceFilesBound:$evidenceIndex[0].allRegularEvidenceFilesBound,
      entryCount:$evidenceIndex[0].entryCount,
      index:{
        path:($evidencePrefix + "/evidence-index.json"),
        sha256:$evidenceIndexSha256
      },
      runtimeInventory:{
        path:($evidencePrefix + "/runtime-inventory.json"),
        sha256:$runtimeInventorySha256
      },
      promoterMustSignIndexAndBindExactSubject:true
    }
  }' >"$MANIFEST_PATH"

jq -e \
  --arg imageId "$IMAGE_ID" \
  --arg manifestDigest "$ARM64_DIGEST" \
  --arg sourceCommit "$SOURCE_COMMIT" \
  --arg sourceTree "$SOURCE_TREE" \
  --arg releaseContractSha256 "$BUNDLE_CONTRACT_SHA256" \
  --arg evidenceIndexSha256 "$EVIDENCE_INDEX_SHA256" '
  .schemaVersion == 5 and
  .deploymentCredential == false and
  .image.imageId == $imageId and
  .image.manifestDigest == $manifestDigest and
  (.image.rootfs | has("mergedExportTarSha256") | not) and
  .source.commit == $sourceCommit and
  .source.tree == $sourceTree and
  .source.releaseContractSha256 == $releaseContractSha256 and
  .promotion == {
    status:"LOCAL_GATES_PASSED",
    registryPublished:false,
    quarantinePublished:false,
    canonicalTagPublished:false,
    canonicalTagImmutable:null,
    registryReferrersPublished:false,
    signedProvenancePublished:false,
    signedSbomPublished:false,
    imageSignaturePublished:false,
    sharedPromoterRequired:true
  } and
  .materials.exactSetMatch == true and
  .materials.extraExecutableOrRemoteMaterialDetected == false and
  .materials.extra == [] and .materials.missing == [] and
  .runtime.dependencyClosureComputedBeforeMetadataRewrite == true and
  .runtime.pluginOperationClosureExact == true and
  .runtime.representativePluginOperationSmokePassed == true and
  .runtime.browser.zeroReachablePayloadClaim == false and
  .runtime.browser.implementationModules == 0 and
  .runtime.browser.cliRegistered == false and
  .runtime.browser.executableOrPlaywrightPresent == false and
  .runtime.browser.usableControlPath == false and
  .runtime.controlUiServedAssetCount > 0 and
  .sbom.wholeFilesystemExactMatch == true and
  .sbom.pathTypeModeOwnerSizeLinkContentMultisetExact == true and
  .scan.critical == 0 and .scan.high == 0 and .scan.secrets == 0 and
  .scan.exactSingleLinuxArm64Subject == true and
  .scan.allKnownLiveFindingsAbsent == true and
  (.scan.knownLiveFindingIdsAbsent | length) == 8 and
  .evidence.allRegularEvidenceFilesBound == true and
  .evidence.index.sha256 == $evidenceIndexSha256
' "$MANIFEST_PATH" >/dev/null || fail "local release manifest contract failed"

MANIFEST_SHA256=$(sha256sum "$MANIFEST_PATH" | cut -d' ' -f1)
printf '%s  %s\n' "$MANIFEST_SHA256" "$(basename -- "$MANIFEST_PATH")" \
  >"$MANIFEST_PATH.sha256"
echo "[openclaw-build] PASS local_image=$RUNTIME_REF local_gates=passed registry_published=false manifest=$MANIFEST_PATH manifest_sha256=$MANIFEST_SHA256"
