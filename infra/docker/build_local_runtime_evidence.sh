#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
HEAD=$(git -C "$REPO_ROOT" rev-parse HEAD)
BRANCH=$(git -C "$REPO_ROOT" branch --show-current)
SHORT_HEAD=$(printf '%s' "$HEAD" | cut -c1-12)
EVIDENCE_DIR=${1:-"/private/tmp/teamagent-runtime-evidence-$HEAD"}
CORE_IMAGE="teamagent-mcp-core:$SHORT_HEAD"
MEDIA_IMAGE="teamagent-media-worker:$SHORT_HEAD"
APP_HTML="$REPO_ROOT/src/teamagent/connect_web/static/app.html"
BAKED_APP_HTML_SHA256=$(sha256sum "$APP_HTML" | cut -d' ' -f1)
EXPECTED_BAKED_APP_HTML_SHA256=716ac25a96516efd6443277c903102d514f3f86729f8706baea41ee48f0ecdeb
APP_HTML_SOURCE=s3
APP_HTML_SHA256=46f0079783cde24b066c7823b7d6672bad12b33debf933a4d7a7ff04b7a3b067
APP_HTML_VERSION_ID=I1qOb7Kwl.pMg71wqFxbHnbbTqMWjQcY
APP_HTML_MANIFEST_SHA256=15663a838b1bd648443949244c02e66ccfd6cb7b684390baeb1a86efcdd6d4a2
APP_HTML_BUILD_INPUTS_SHA256=1ca6f0213155d8d4dbef4220f641dbb38310fe79473f6c013ef4e54dfa6a87e2
CA_FILE=${TEAMAGENT_CA_FILE:-${SSL_CERT_FILE:-}}

case "$HEAD" in
  *[!0-9a-f]* | "")
    echo "HEAD is not a full 40-hex commit" >&2
    exit 1
    ;;
esac
test "${#HEAD}" -eq 40
test "$BAKED_APP_HTML_SHA256" = "$EXPECTED_BAKED_APP_HTML_SHA256"
if test -n "$(git -C "$REPO_ROOT" status --porcelain)"; then
  echo "refusing to build evidence from a dirty worktree" >&2
  exit 1
fi

command -v docker >/dev/null
command -v trivy >/dev/null
command -v sha256sum >/dev/null
mkdir -p "$EVIDENCE_DIR"
TRACKED_SOURCE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/teamagent-tracked-source.XXXXXX")
cleanup_tracked_source() {
  if test -n "${TRACKED_SOURCE_DIR:-}"; then
    rm -rf "$TRACKED_SOURCE_DIR"
  fi
}
trap cleanup_tracked_source EXIT INT TERM
git -C "$REPO_ROOT" archive "$HEAD" | tar -x -C "$TRACKED_SOURCE_DIR"
trivy fs \
  --scanners secret \
  --exit-code 1 \
  --format json \
  --output "$EVIDENCE_DIR/source-tracked-trivy-secret.json" \
  "$TRACKED_SOURCE_DIR"
cleanup_tracked_source
TRACKED_SOURCE_DIR=
set --
if test -n "$CA_FILE"; then
  test -f "$CA_FILE"
  set -- --secret "id=teamagent_ca,src=$CA_FILE"
fi

docker buildx build \
  "$@" \
  --platform linux/arm64 \
  --file "$SCRIPT_DIR/Dockerfile.teamagent-mcp" \
  --build-arg "GIT_COMMIT=$HEAD" \
  --build-arg "GIT_BRANCH=$BRANCH" \
  --build-arg "BAKED_APP_HTML_SHA256=$BAKED_APP_HTML_SHA256" \
  --build-arg "APP_HTML_SOURCE=$APP_HTML_SOURCE" \
  --build-arg "APP_HTML_SHA256=$APP_HTML_SHA256" \
  --build-arg "APP_HTML_VERSION_ID=$APP_HTML_VERSION_ID" \
  --build-arg "APP_HTML_MANIFEST_SHA256=$APP_HTML_MANIFEST_SHA256" \
  --build-arg "APP_HTML_BUILD_INPUTS_SHA256=$APP_HTML_BUILD_INPUTS_SHA256" \
  --provenance=mode=max \
  --sbom=true \
  --metadata-file "$EVIDENCE_DIR/core-build-metadata.json" \
  --tag "$CORE_IMAGE" \
  --load \
  "$REPO_ROOT"

docker buildx build \
  "$@" \
  --platform linux/arm64 \
  --file "$SCRIPT_DIR/Dockerfile.teamagent-media-worker" \
  --build-arg "GIT_COMMIT=$HEAD" \
  --build-arg "GIT_BRANCH=$BRANCH" \
  --provenance=mode=max \
  --sbom=true \
  --metadata-file "$EVIDENCE_DIR/media-build-metadata.json" \
  --tag "$MEDIA_IMAGE" \
  --load \
  "$REPO_ROOT"

for pair in "core:$CORE_IMAGE" "media:$MEDIA_IMAGE"; do
  name=${pair%%:*}
  image=${pair#*:}
  revision=$(
    docker image inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "$image"
  )
  architecture=$(docker image inspect --format '{{.Architecture}}' "$image")
  user=$(docker image inspect --format '{{.Config.User}}' "$image")
  volumes=$(docker image inspect --format '{{json .Config.Volumes}}' "$image")
  test "$revision" = "$HEAD"
  test "$architecture" = "arm64"
  test "$user" = "10001:10001"
  test "$volumes" = '{"/tmp":{}}'
  if test "$name" = core; then
    baked_label=$(
      docker image inspect \
        --format '{{index .Config.Labels "io.teamagent.contract.baked-app-html-sha256"}}' \
        "$image"
    )
    source_label=$(
      docker image inspect \
        --format '{{index .Config.Labels "io.teamagent.contract.app-html-source"}}' \
        "$image"
    )
    sha_label=$(
      docker image inspect \
        --format '{{index .Config.Labels "io.teamagent.contract.app-html-sha256"}}' \
        "$image"
    )
    version_label=$(
      docker image inspect \
        --format '{{index .Config.Labels "io.teamagent.contract.app-html-version-id"}}' \
        "$image"
    )
    manifest_label=$(
      docker image inspect \
        --format '{{index .Config.Labels "io.teamagent.contract.app-html-manifest-sha256"}}' \
        "$image"
    )
    build_inputs_label=$(
      docker image inspect \
        --format '{{index .Config.Labels "io.teamagent.contract.app-html-build-inputs-sha256"}}' \
        "$image"
    )
    test "$baked_label" = "$BAKED_APP_HTML_SHA256"
    test "$source_label" = "$APP_HTML_SOURCE"
    test "$sha_label" = "$APP_HTML_SHA256"
    test "$version_label" = "$APP_HTML_VERSION_ID"
    test "$manifest_label" = "$APP_HTML_MANIFEST_SHA256"
    test "$build_inputs_label" = "$APP_HTML_BUILD_INPUTS_SHA256"
  fi

  docker image inspect "$image" >"$EVIDENCE_DIR/$name-image-inspect.json"
  docker history --no-trunc "$image" >"$EVIDENCE_DIR/$name-image-history.txt"
  trivy image \
    --scanners vuln \
    --severity CRITICAL,HIGH \
    --format json \
    --output "$EVIDENCE_DIR/$name-trivy-vulnerability.json" \
    "$image"
  trivy image \
    --scanners secret \
    --format json \
    --output "$EVIDENCE_DIR/$name-trivy-secret.json" \
    "$image"
  "$SCRIPT_DIR/verify_trivy_zero.py" \
    "$EVIDENCE_DIR/$name-trivy-vulnerability.json" \
    "$EVIDENCE_DIR/$name-trivy-secret.json" \
    >"$EVIDENCE_DIR/$name-trivy-summary.json"
  trivy image \
    --format cyclonedx \
    --output "$EVIDENCE_DIR/$name-sbom.cdx.json" \
    "$image"
done

TEAMAGENT_CORE_IMAGE="$CORE_IMAGE" \
TEAMAGENT_MEDIA_IMAGE="$MEDIA_IMAGE" \
  "$SCRIPT_DIR/run_runtime_smokes.sh" \
  >"$EVIDENCE_DIR/runtime-smokes.log"

git -C "$REPO_ROOT" show --no-patch --format=fuller "$HEAD" \
  >"$EVIDENCE_DIR/git-commit.txt"
git -C "$REPO_ROOT" diff-tree --no-commit-id --name-status -r "$HEAD" \
  >"$EVIDENCE_DIR/git-files.txt"
(
  cd "$EVIDENCE_DIR"
  for file in ./*; do
    test "$file" = ./SHA256SUMS && continue
    sha256sum "$file"
  done >SHA256SUMS
)

printf 'HEAD=%s\n' "$HEAD"
printf 'CORE_IMAGE=%s\n' "$CORE_IMAGE"
printf 'MEDIA_IMAGE=%s\n' "$MEDIA_IMAGE"
printf 'EVIDENCE_DIR=%s\n' "$EVIDENCE_DIR"
