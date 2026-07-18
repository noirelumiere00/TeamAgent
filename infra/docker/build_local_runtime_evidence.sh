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
APP_HTML_SHA256=03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c
APP_HTML_VERSION_ID=FTXbcN70D0DCN90TI_hRK1IdQK_HhLee
APP_HTML_MANIFEST_SHA256=aa451e744d26e9dc13c170b019307b0eb10d3645267960fbff41c4038e9b909e
APP_HTML_BUILD_INPUTS_SHA256=6697acf311f0c9a96b41426e81ae05ad221482a6e6f69799281ad3532c2e78bf
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
command -v date >/dev/null
if test -e "$EVIDENCE_DIR"; then
  echo "refusing to reuse an existing evidence path: $EVIDENCE_DIR" >&2
  exit 1
fi
umask 077
mkdir "$EVIDENCE_DIR"
TRACKED_SOURCE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/teamagent-tracked-source.XXXXXX")
# Trivy normalizes redundant path separators in ArtifactName. Canonicalize the
# receipt subject too (macOS TMPDIR commonly ends in "/") so the binding is exact.
TRACKED_SOURCE_DIR=$(CDPATH= cd -- "$TRACKED_SOURCE_DIR" && pwd -P)
cleanup_tracked_source() {
  if test -n "${TRACKED_SOURCE_DIR:-}"; then
    rm -rf "$TRACKED_SOURCE_DIR"
  fi
}
trap cleanup_tracked_source EXIT INT TERM
git -C "$REPO_ROOT" archive --format=tar "$HEAD" \
  >"$EVIDENCE_DIR/source-tracked.tar"
tar -x -f "$EVIDENCE_DIR/source-tracked.tar" -C "$TRACKED_SOURCE_DIR"
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

SCAN_STARTED_AT=$(date -u "+%Y-%m-%dT%H:%M:%SZ")
trivy fs \
  --skip-db-update \
  --scanners secret \
  --exit-code 1 \
  --format json \
  --output "$EVIDENCE_DIR/source-tracked-trivy-secret.json" \
  "$TRACKED_SOURCE_DIR"
trivy image --download-db-only
trivy version --format json >"$EVIDENCE_DIR/trivy-version.json"

for pair in "core:$CORE_IMAGE" "media:$MEDIA_IMAGE"; do
  name=${pair%%:*}
  image=${pair#*:}
  image_id=$(docker image inspect --format '{{.Id}}' "$image")
  case "$image_id" in
    sha256:[0-9a-f][0-9a-f]*) ;;
    *)
      echo "$name image has no immutable image ID" >&2
      exit 1
      ;;
  esac
  test "${#image_id}" -eq 71
  revision=$(
    docker image inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "$image_id"
  )
  architecture=$(docker image inspect --format '{{.Architecture}}' "$image_id")
  user=$(docker image inspect --format '{{.Config.User}}' "$image_id")
  volumes=$(docker image inspect --format '{{json .Config.Volumes}}' "$image_id")
  test "$revision" = "$HEAD"
  test "$architecture" = "arm64"
  test "$user" = "10001:10001"
  test "$volumes" = '{"/tmp":{}}'
  if test "$name" = core; then
    baked_label=$(
      docker image inspect \
        --format '{{index .Config.Labels "io.teamagent.contract.baked-app-html-sha256"}}' \
        "$image_id"
    )
    source_label=$(
      docker image inspect \
        --format '{{index .Config.Labels "io.teamagent.contract.app-html-source"}}' \
        "$image_id"
    )
    sha_label=$(
      docker image inspect \
        --format '{{index .Config.Labels "io.teamagent.contract.app-html-sha256"}}' \
        "$image_id"
    )
    version_label=$(
      docker image inspect \
        --format '{{index .Config.Labels "io.teamagent.contract.app-html-version-id"}}' \
        "$image_id"
    )
    manifest_label=$(
      docker image inspect \
        --format '{{index .Config.Labels "io.teamagent.contract.app-html-manifest-sha256"}}' \
        "$image_id"
    )
    build_inputs_label=$(
      docker image inspect \
        --format '{{index .Config.Labels "io.teamagent.contract.app-html-build-inputs-sha256"}}' \
        "$image_id"
    )
    test "$baked_label" = "$BAKED_APP_HTML_SHA256"
    test "$source_label" = "$APP_HTML_SOURCE"
    test "$sha_label" = "$APP_HTML_SHA256"
    test "$version_label" = "$APP_HTML_VERSION_ID"
    test "$manifest_label" = "$APP_HTML_MANIFEST_SHA256"
    test "$build_inputs_label" = "$APP_HTML_BUILD_INPUTS_SHA256"
  fi

  docker image inspect "$image_id" >"$EVIDENCE_DIR/$name-image-inspect.json"
  docker history --no-trunc "$image_id" >"$EVIDENCE_DIR/$name-image-history.txt"
  trivy image \
    --skip-db-update \
    --skip-check-update \
    --scanners vuln \
    --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL \
    --exit-code 0 \
    --format json \
    --output "$EVIDENCE_DIR/$name-trivy-vulnerability.json" \
    "$image_id"
  trivy image \
    --skip-db-update \
    --skip-check-update \
    --scanners secret \
    --exit-code 1 \
    --format json \
    --output "$EVIDENCE_DIR/$name-trivy-secret.json" \
    "$image_id"
  "$SCRIPT_DIR/verify_trivy_zero.py" \
    "$EVIDENCE_DIR/$name-trivy-vulnerability.json" \
    "$EVIDENCE_DIR/$name-trivy-secret.json" \
    --image-id "$image_id" \
    --artifact-name "$image_id" \
    >"$EVIDENCE_DIR/$name-trivy-summary.json"
  trivy image \
    --skip-db-update \
    --skip-check-update \
    --format cyclonedx \
    --output "$EVIDENCE_DIR/$name-sbom.cdx.json" \
    "$image_id"
done
SCAN_FINISHED_AT=$(date -u "+%Y-%m-%dT%H:%M:%SZ")
trivy version --format json >"$EVIDENCE_DIR/trivy-version-after.json"
cmp "$EVIDENCE_DIR/trivy-version.json" "$EVIDENCE_DIR/trivy-version-after.json"
rm "$EVIDENCE_DIR/trivy-version-after.json"

CORE_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$CORE_IMAGE")
MEDIA_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$MEDIA_IMAGE")
TEAMAGENT_CORE_IMAGE="$CORE_IMAGE_ID" \
TEAMAGENT_MEDIA_IMAGE="$MEDIA_IMAGE_ID" \
  "$SCRIPT_DIR/run_runtime_smokes.sh" \
  >"$EVIDENCE_DIR/runtime-smokes.log"

git -C "$REPO_ROOT" show --no-patch --format=fuller "$HEAD" \
  >"$EVIDENCE_DIR/git-commit.txt"
git -C "$REPO_ROOT" diff-tree \
  --root --no-commit-id --name-status -r --first-parent "$HEAD" \
  >"$EVIDENCE_DIR/git-files.txt"
test -s "$EVIDENCE_DIR/git-files.txt"
"$SCRIPT_DIR/generate_runtime_receipt.py" \
  "$EVIDENCE_DIR" \
  --repo-root "$REPO_ROOT" \
  --head "$HEAD" \
  --branch "$BRANCH" \
  --source-scan-artifact-name "$TRACKED_SOURCE_DIR" \
  --started-at "$SCAN_STARTED_AT" \
  --finished-at "$SCAN_FINISHED_AT"
"$SCRIPT_DIR/verify_runtime_evidence.py" \
  "$EVIDENCE_DIR" \
  --expected-head "$HEAD" \
  --repo-root "$REPO_ROOT" \
  --skip-checksums \
  >"$EVIDENCE_DIR/verification.json"
(
  cd "$EVIDENCE_DIR"
  for file in ./*; do
    test "$file" = ./SHA256SUMS && continue
    sha256sum "$file"
  done >SHA256SUMS
)
"$SCRIPT_DIR/verify_runtime_evidence.py" \
  "$EVIDENCE_DIR" \
  --expected-head "$HEAD" \
  --repo-root "$REPO_ROOT"

printf 'HEAD=%s\n' "$HEAD"
printf 'CORE_IMAGE=%s\n' "$CORE_IMAGE_ID"
printf 'MEDIA_IMAGE=%s\n' "$MEDIA_IMAGE_ID"
printf 'EVIDENCE_DIR=%s\n' "$EVIDENCE_DIR"
