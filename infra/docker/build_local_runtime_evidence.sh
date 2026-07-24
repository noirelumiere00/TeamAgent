#!/bin/sh
set -eu

# Evidence helpers are executed from the extracted build context. Importing one
# helper from another must not mutate that exact context with __pycache__ files
# after build-context.tar has been retained.
PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
HEAD=$(git -C "$REPO_ROOT" rev-parse HEAD)
BRANCH=$(git -C "$REPO_ROOT" branch --show-current)
TREE=$(git -C "$REPO_ROOT" rev-parse "$HEAD^{tree}")
EXPECTED_HEAD=${TEAMAGENT_EXPECTED_COMMIT:-$HEAD}
EXPECTED_TREE=${TEAMAGENT_EXPECTED_TREE:-$TREE}
REVIEW_BASE_REF=${TEAMAGENT_REVIEW_BASE_REF:-origin/dev}
REVIEW_BASE_OID=$(git -C "$REPO_ROOT" rev-parse "$REVIEW_BASE_REF^{commit}")
MERGE_BASE_OID=$(git -C "$REPO_ROOT" merge-base "$REVIEW_BASE_OID" "$HEAD")
SHORT_HEAD=$(printf '%s' "$HEAD" | cut -c1-12)
EVIDENCE_DIR=${1:-"/private/tmp/teamagent-runtime-evidence-$HEAD"}
CORE_IMAGE="teamagent-mcp-core:$SHORT_HEAD"
MEDIA_IMAGE="teamagent-media-worker:$SHORT_HEAD"
EXPECTED_BAKED_APP_HTML_SHA256=716ac25a96516efd6443277c903102d514f3f86729f8706baea41ee48f0ecdeb
BAKED_APP_HTML_VERSION_ID=LOCAL_EVIDENCE_ONLY_NOT_PRODUCTION
APP_HTML_SOURCE=s3
APP_HTML_SHA256=03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c
APP_HTML_VERSION_ID=FTXbcN70D0DCN90TI_hRK1IdQK_HhLee
APP_HTML_MANIFEST_SHA256=aa451e744d26e9dc13c170b019307b0eb10d3645267960fbff41c4038e9b909e
APP_HTML_BUILD_INPUTS_SHA256=6697acf311f0c9a96b41426e81ae05ad221482a6e6f69799281ad3532c2e78bf
CA_FILE=${TEAMAGENT_CA_FILE:-${SSL_CERT_FILE:-}}
GRYPE_VERSION=0.112.0
GRYPE_COMMAND=${TEAMAGENT_GRYPE:-grype}

case "$HEAD" in
  *[!0-9a-f]* | "")
    echo "HEAD is not a full 40-hex commit" >&2
    exit 1
    ;;
esac
test "${#HEAD}" -eq 40
test "$HEAD" = "$EXPECTED_HEAD"
test "$TREE" = "$EXPECTED_TREE"
test -n "$BRANCH"
if test -n "$(git -C "$REPO_ROOT" status --porcelain)"; then
  echo "refusing to build evidence from a dirty worktree" >&2
  exit 1
fi

command -v docker >/dev/null
command -v trivy >/dev/null
GRYPE_BIN=$(command -v "$GRYPE_COMMAND")
test -x "$GRYPE_BIN"
command -v sha256sum >/dev/null
command -v date >/dev/null
if test -e "$EVIDENCE_DIR"; then
  echo "refusing to reuse an existing evidence path: $EVIDENCE_DIR" >&2
  exit 1
fi
umask 077
mkdir "$EVIDENCE_DIR"
TRACKED_SOURCE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/teamagent-tracked-source.XXXXXX")
BUILD_CONTEXT_DIR=$(mktemp -d "${TMPDIR:-/tmp}/teamagent-build-context.XXXXXX")
# Trivy normalizes redundant path separators in ArtifactName. Canonicalize the
# receipt subject too (macOS TMPDIR commonly ends in "/") so the binding is exact.
TRACKED_SOURCE_DIR=$(CDPATH= cd -- "$TRACKED_SOURCE_DIR" && pwd -P)
BUILD_CONTEXT_DIR=$(CDPATH= cd -- "$BUILD_CONTEXT_DIR" && pwd -P)
cleanup_tracked_source() {
  if test -n "${TRACKED_SOURCE_DIR:-}"; then
    rm -rf "$TRACKED_SOURCE_DIR"
  fi
  if test -n "${BUILD_CONTEXT_DIR:-}"; then
    rm -rf "$BUILD_CONTEXT_DIR"
  fi
  if test -n "${PREVERIFY_FILE:-}"; then
    rm -f "$PREVERIFY_FILE"
  fi
  if test -n "${FINAL_VERIFY_FILE:-}"; then
    rm -f "$FINAL_VERIFY_FILE"
  fi
}
trap cleanup_tracked_source EXIT INT TERM
git -C "$REPO_ROOT" archive --format=tar "$HEAD" \
  >"$EVIDENCE_DIR/source-tracked.tar"
tar -x -f "$EVIDENCE_DIR/source-tracked.tar" -C "$TRACKED_SOURCE_DIR"
tar -x -f "$EVIDENCE_DIR/source-tracked.tar" -C "$BUILD_CONTEXT_DIR"
"$TRACKED_SOURCE_DIR/infra/deploy/verify_source_tree.py" \
  --root "$TRACKED_SOURCE_DIR" \
  --expected-tree "$TREE"
"$BUILD_CONTEXT_DIR/infra/deploy/verify_source_tree.py" \
  --root "$BUILD_CONTEXT_DIR" \
  --expected-tree "$TREE"
mkdir -p "$BUILD_CONTEXT_DIR/src/teamagent/connect_web/static"
cp \
  "$BUILD_CONTEXT_DIR/infra/docker/app-html-runtime-fixture.html" \
  "$BUILD_CONTEXT_DIR/src/teamagent/connect_web/static/app.html"
chmod 0644 "$BUILD_CONTEXT_DIR/src/teamagent/connect_web/static/app.html"
BAKED_APP_HTML_SHA256=$(
  sha256sum "$BUILD_CONTEXT_DIR/src/teamagent/connect_web/static/app.html" | cut -d' ' -f1
)
test "$BAKED_APP_HTML_SHA256" = "$EXPECTED_BAKED_APP_HTML_SHA256"
BUILD_CONTEXT_SHA256=$(
  python3 "$TRACKED_SOURCE_DIR/infra/docker/canonical_build_context.py" \
    "$BUILD_CONTEXT_DIR" \
    "$EVIDENCE_DIR/build-context.tar"
)
test "$(sha256sum "$EVIDENCE_DIR/build-context.tar" | cut -d' ' -f1)" = \
  "$BUILD_CONTEXT_SHA256"
SOURCE_DOCKER_DIR="$TRACKED_SOURCE_DIR/infra/docker"
RUNTIME_CONTRACT="$TRACKED_SOURCE_DIR/infra/codebuild/teamagent_runtime_contract.json"
EXPECTED_RUNTIME_CONTRACT_SHA256=$(
  sha256sum "$RUNTIME_CONTRACT" | cut -d' ' -f1
)
RELEASE_CONTRACT_SHA256=$(
  sha256sum "$TRACKED_SOURCE_DIR/infra/codebuild/teamagent_core_media_release_contract.json" \
    | cut -d' ' -f1
)
python3 "$TRACKED_SOURCE_DIR/infra/codebuild/teamagent_bundle_provenance.py" \
  validate-contract-pair \
  --runtime-contract "$RUNTIME_CONTRACT" \
  --contract \
    "$TRACKED_SOURCE_DIR/infra/codebuild/teamagent_core_media_release_contract.json" \
  --repo-root "$TRACKED_SOURCE_DIR" \
  --expected-commit "$HEAD"
RUNTIME_BUILD_ARGUMENTS="$EVIDENCE_DIR/runtime-build-arguments.txt"
python3 "$TRACKED_SOURCE_DIR/infra/codebuild/source_provenance.py" \
  docker-build-arguments \
  --contract "$RUNTIME_CONTRACT" \
  --expected-contract-sha256 "$EXPECTED_RUNTIME_CONTRACT_SHA256" \
  >"$RUNTIME_BUILD_ARGUMENTS"
for runtime_receipt_argument in \
  RUNTIME_CONTRACT_SHA256 \
  RUNTIME_RECEIPT_B64 \
  RUNTIME_RECEIPT_SHA256; do
  test "$(awk -F= -v name="$runtime_receipt_argument" \
    '$1 == name { count++ } END { print count + 0 }' \
    "$RUNTIME_BUILD_ARGUMENTS")" -eq 1
done
RUNTIME_CONTRACT_SHA256=$(
  sed -n 's/^RUNTIME_CONTRACT_SHA256=//p' "$RUNTIME_BUILD_ARGUMENTS"
)
test "$RUNTIME_CONTRACT_SHA256" = "$EXPECTED_RUNTIME_CONTRACT_SHA256"
RUNTIME_RECEIPT_B64=$(
  sed -n 's/^RUNTIME_RECEIPT_B64=//p' "$RUNTIME_BUILD_ARGUMENTS"
)
RUNTIME_RECEIPT_SHA256=$(
  sed -n 's/^RUNTIME_RECEIPT_SHA256=//p' "$RUNTIME_BUILD_ARGUMENTS"
)
APP_PROVENANCE_SHA256=$(
  python3 "$TRACKED_SOURCE_DIR/infra/codebuild/teamagent_bundle_provenance.py" \
    app-provenance-sha256 \
    --contract \
      "$TRACKED_SOURCE_DIR/infra/codebuild/teamagent_core_media_release_contract.json" \
    --deploy-log "$TRACKED_SOURCE_DIR/infra/deploy_log.md"
)
set --
if test -n "$CA_FILE"; then
  test -f "$CA_FILE"
  set -- --secret "id=teamagent_ca,src=$CA_FILE"
fi

docker buildx build \
  "$@" \
  --platform linux/arm64 \
  --file infra/docker/Dockerfile.teamagent-mcp \
  --build-arg "GIT_COMMIT=$HEAD" \
  --build-arg "GIT_BRANCH=$BRANCH" \
  --build-arg "BUILD_CONTEXT_SHA256=$BUILD_CONTEXT_SHA256" \
  --build-arg "RUNTIME_CONTRACT_SHA256=$RUNTIME_CONTRACT_SHA256" \
  --build-arg "RUNTIME_RECEIPT_B64=$RUNTIME_RECEIPT_B64" \
  --build-arg "RUNTIME_RECEIPT_SHA256=$RUNTIME_RECEIPT_SHA256" \
  --build-arg "BAKED_APP_HTML_SHA256=$BAKED_APP_HTML_SHA256" \
  --build-arg "BAKED_APP_HTML_VERSION_ID=$BAKED_APP_HTML_VERSION_ID" \
  --build-arg "APP_HTML_SOURCE=$APP_HTML_SOURCE" \
  --build-arg "APP_HTML_SHA256=$APP_HTML_SHA256" \
  --build-arg "APP_HTML_VERSION_ID=$APP_HTML_VERSION_ID" \
  --build-arg "APP_HTML_MANIFEST_SHA256=$APP_HTML_MANIFEST_SHA256" \
  --build-arg "APP_HTML_BUILD_INPUTS_SHA256=$APP_HTML_BUILD_INPUTS_SHA256" \
  --build-arg "RELEASE_CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256" \
  --build-arg "APP_PROVENANCE_SHA256=$APP_PROVENANCE_SHA256" \
  --provenance=mode=max \
  --sbom=true \
  --metadata-file "$EVIDENCE_DIR/core-build-metadata.json" \
  --tag "$CORE_IMAGE" \
  --load \
  - <"$EVIDENCE_DIR/build-context.tar"

docker buildx build \
  "$@" \
  --platform linux/arm64 \
  --file infra/docker/Dockerfile.teamagent-media-worker \
  --build-arg "GIT_COMMIT=$HEAD" \
  --build-arg "GIT_BRANCH=$BRANCH" \
  --build-arg "BUILD_CONTEXT_SHA256=$BUILD_CONTEXT_SHA256" \
  --build-arg "RELEASE_CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256" \
  --build-arg "APP_PROVENANCE_SHA256=$APP_PROVENANCE_SHA256" \
  --provenance=mode=max \
  --sbom=true \
  --metadata-file "$EVIDENCE_DIR/media-build-metadata.json" \
  --tag "$MEDIA_IMAGE" \
  --load \
  - <"$EVIDENCE_DIR/build-context.tar"

test "$(sha256sum "$EVIDENCE_DIR/build-context.tar" | cut -d' ' -f1)" = \
  "$BUILD_CONTEXT_SHA256"

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
"$GRYPE_BIN" version -o json >"$EVIDENCE_DIR/grype-version.json"
python3 - "$EVIDENCE_DIR/grype-version.json" "$GRYPE_VERSION" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    version = json.load(handle)
if version.get("application") != "grype" or version.get("version") != sys.argv[2]:
    raise SystemExit("unexpected Grype binary version")
PY
sha256sum "$GRYPE_BIN" | cut -d' ' -f1 \
  >"$EVIDENCE_DIR/grype-binary-sha256.txt"
"$GRYPE_BIN" db status -o json >"$EVIDENCE_DIR/grype-db-status.json"

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
  context_label=$(
    docker image inspect \
      --format '{{index .Config.Labels "io.teamagent.build.context-sha256"}}' \
      "$image_id"
  )
  test "$context_label" = "$BUILD_CONTEXT_SHA256"
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
  "$SOURCE_DOCKER_DIR/verify_trivy_zero.py" \
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
  "$GRYPE_BIN" \
    --config "$SOURCE_DOCKER_DIR/grype-local-evidence.yaml" \
    "$image_id" \
    --show-suppressed \
    --output json \
    --file "$EVIDENCE_DIR/$name-grype-vulnerability.json"
done
SCAN_FINISHED_AT=$(date -u "+%Y-%m-%dT%H:%M:%SZ")
trivy version --format json >"$EVIDENCE_DIR/trivy-version-after.json"
cmp "$EVIDENCE_DIR/trivy-version.json" "$EVIDENCE_DIR/trivy-version-after.json"
rm "$EVIDENCE_DIR/trivy-version-after.json"
"$GRYPE_BIN" version -o json >"$EVIDENCE_DIR/grype-version-after.json"
"$GRYPE_BIN" db status -o json >"$EVIDENCE_DIR/grype-db-status-after.json"
cmp "$EVIDENCE_DIR/grype-version.json" "$EVIDENCE_DIR/grype-version-after.json"
cmp "$EVIDENCE_DIR/grype-db-status.json" "$EVIDENCE_DIR/grype-db-status-after.json"
rm "$EVIDENCE_DIR/grype-version-after.json" \
  "$EVIDENCE_DIR/grype-db-status-after.json"

CORE_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$CORE_IMAGE")
MEDIA_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$MEDIA_IMAGE")
TEAMAGENT_CORE_IMAGE="$CORE_IMAGE_ID" \
TEAMAGENT_MEDIA_IMAGE="$MEDIA_IMAGE_ID" \
  "$SOURCE_DOCKER_DIR/run_runtime_smokes.sh" \
  >"$EVIDENCE_DIR/runtime-smokes.log"

git -C "$REPO_ROOT" show --no-patch --format=fuller "$HEAD" \
  >"$EVIDENCE_DIR/git-commit.txt"
git -C "$REPO_ROOT" diff-tree \
  --root --no-commit-id --name-status -r --first-parent "$HEAD" \
  >"$EVIDENCE_DIR/git-files.txt"
test -s "$EVIDENCE_DIR/git-files.txt"
git -C "$REPO_ROOT" diff \
  --name-status --find-renames "$REVIEW_BASE_OID...$HEAD" \
  >"$EVIDENCE_DIR/git-base-head-files.txt"
printf 'review_base_ref=%s\nreview_base_oid=%s\nmerge_base_oid=%s\n' \
  "$REVIEW_BASE_REF" "$REVIEW_BASE_OID" "$MERGE_BASE_OID" \
  >"$EVIDENCE_DIR/git-review-base.txt"
"$SOURCE_DOCKER_DIR/generate_runtime_receipt.py" \
  "$EVIDENCE_DIR" \
  --repo-root "$REPO_ROOT" \
  --head "$HEAD" \
  --branch "$BRANCH" \
  --review-base-ref "$REVIEW_BASE_REF" \
  --review-base-oid "$REVIEW_BASE_OID" \
  --merge-base-oid "$MERGE_BASE_OID" \
  --source-scan-artifact-name "$TRACKED_SOURCE_DIR" \
  --started-at "$SCAN_STARTED_AT" \
  --finished-at "$SCAN_FINISHED_AT"
PREVERIFY_FILE=$(mktemp "${TMPDIR:-/tmp}/teamagent-preverify.XXXXXX")
"$SOURCE_DOCKER_DIR/verify_runtime_evidence.py" \
  "$EVIDENCE_DIR" \
  --expected-head "$HEAD" \
  --expected-branch "$BRANCH" \
  --repo-root "$REPO_ROOT" \
  --skip-checksums \
  >"$PREVERIFY_FILE"
rm -f "$PREVERIFY_FILE"
PREVERIFY_FILE=
(
  cd "$EVIDENCE_DIR"
  for file in ./*; do
    test "$file" = ./SHA256SUMS && continue
    test "$file" = ./FINAL_VERIFICATION.json && continue
    sha256sum "$file"
  done >SHA256SUMS
)
FINAL_VERIFY_FILE=$(mktemp "${TMPDIR:-/tmp}/teamagent-final-verify.XXXXXX")
"$SOURCE_DOCKER_DIR/verify_runtime_evidence.py" \
  "$EVIDENCE_DIR" \
  --expected-head "$HEAD" \
  --expected-branch "$BRANCH" \
  --repo-root "$REPO_ROOT" \
  >"$FINAL_VERIFY_FILE"
mv "$FINAL_VERIFY_FILE" "$EVIDENCE_DIR/FINAL_VERIFICATION.json"
FINAL_VERIFY_FILE=

printf 'HEAD=%s\n' "$HEAD"
printf 'CORE_IMAGE=%s\n' "$CORE_IMAGE_ID"
printf 'MEDIA_IMAGE=%s\n' "$MEDIA_IMAGE_ID"
printf 'EVIDENCE_DIR=%s\n' "$EVIDENCE_DIR"
