#!/usr/bin/env bash
# Canonical OpenClaw core bundle publisher.
#
# build-image.sh remains the no-push local evidence path. This interface is the
# release-gated path that publishes the contract-defined core subject only to
# its immutable quarantine repository and emits the exact bundle receipt.
set -euo pipefail
umask 077

die() {
  echo "FATAL: $*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
usage: build-bundle.sh \
  --bundle-contract PATH \
  --core-image REPOSITORY:TAG \
  [--media-image REPOSITORY:TAG] \
  --push \
  --manifest RECEIPT_PATH

--media-image is accepted for compatibility with the legacy caller and is
never built or published by the core-only bundle contract.
EOF
}

bundle_contract=""
core_image=""
legacy_media_image=""
manifest=""
push=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --bundle-contract)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      bundle_contract="$2"
      shift 2
      ;;
    --core-image)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      core_image="$2"
      shift 2
      ;;
    --media-image)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      legacy_media_image="$2"
      shift 2
      ;;
    --manifest)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      manifest="$2"
      shift 2
      ;;
    --push)
      push=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      die "unknown argument: $1"
      ;;
  esac
done

[ -n "$bundle_contract" ] &&
  [ -n "$core_image" ] &&
  [ -n "$manifest" ] &&
  [ "$push" = true ] || {
  usage
  exit 2
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "$script_dir/../.." && pwd -P)"
canonical_contract="$repo_root/infra/codebuild/openclaw_bundle_contract.json"
provenance="$repo_root/infra/codebuild/openclaw_provenance.py"
dockerfile="$repo_root/infra/docker/Dockerfile.openclaw"
lock_file="$repo_root/infra/openclaw/plugins-lock.json"

[ -f "$bundle_contract" ] && [ ! -L "$bundle_contract" ] ||
  die "bundle contract is missing or a symlink"
[ "$(cd -- "$(dirname -- "$bundle_contract")" && pwd -P)/$(basename -- "$bundle_contract")" = \
  "$canonical_contract" ] || die "only the canonical OpenClaw bundle contract is accepted"
[ -f "$provenance" ] && [ ! -L "$provenance" ] ||
  die "OpenClaw provenance verifier is missing or a symlink"

# This is intentionally before Docker, AWS, registry authentication,
# filesystem output, or any external mutation.
python3 "$provenance" assert-release-ready --contract "$canonical_contract" ||
  die "OpenClaw core/media release contract is not active"

for command in \
  aws awk basename cut dirname docker git grep jq mkdir mktemp mv python3 rm sha256sum trivy; do
  command -v "$command" >/dev/null 2>&1 ||
    die "required command not found: $command"
done
[ -f "$dockerfile" ] && [ ! -L "$dockerfile" ] ||
  die "OpenClaw Dockerfile is missing or a symlink"
[ -f "$lock_file" ] && [ ! -L "$lock_file" ] ||
  die "OpenClaw plugins lock is missing or a symlink"

bundle_contract_sha256="$(
  python3 "$provenance" contract-sha256 --contract "$canonical_contract"
)" || die "OpenClaw bundle contract is invalid"
[[ "$bundle_contract_sha256" =~ ^[0-9a-f]{64}$ ]] ||
  die "OpenClaw bundle contract returned an invalid SHA-256"

subject_count="$(jq -er '.bundle.subjects | length' "$canonical_contract")" ||
  die "could not read OpenClaw bundle subjects"
[ "$subject_count" -eq 1 ] ||
  die "core bundle emitter requires exactly one contract subject (found $subject_count)"
subject_name="$(jq -er '.bundle.subjects[0].name' "$canonical_contract")" ||
  die "could not read the OpenClaw subject name"
quarantine_repository="$(
  jq -er '.bundle.subjects[0].quarantine_repository' "$canonical_contract"
)" || die "could not read the OpenClaw quarantine repository"
release_repository="$(
  jq -er '.bundle.subjects[0].release_repository' "$canonical_contract"
)" || die "could not read the OpenClaw release repository"
arm64_subject_media_type="$(
  jq -er '.bundle.arm64_subject_media_type' "$canonical_contract"
)" || die "could not read the OpenClaw arm64 subject media type"
scan_gate_critical="$(jq -er '.bundle.scan_gate.critical' "$canonical_contract")" ||
  die "could not read the OpenClaw critical scan gate"
scan_gate_high="$(jq -er '.bundle.scan_gate.high' "$canonical_contract")" ||
  die "could not read the OpenClaw high scan gate"
[ "$scan_gate_critical" -eq 0 ] && [ "$scan_gate_high" -eq 0 ] ||
  die "OpenClaw bundle contract does not require a Critical/High zero gate"

# The contract is currently core-only, but the fixed CodeBuild caller still
# passes its former media argument. Accepting it cannot expand the contract or
# create a registry write because no command below consumes its value.
if [ -n "$legacy_media_image" ]; then
  [[ "$legacy_media_image" != *@* ]] ||
    die "legacy media image compatibility argument must be a tag, not a digest"
  legacy_media_component="${legacy_media_image##*/}"
  [[ "$legacy_media_component" == *:* ]] ||
    die "legacy media image compatibility argument requires a tag"
  unset legacy_media_component
fi

[[ "$core_image" != *@* ]] || die "core image must be a tag, not a digest"
core_image_component="${core_image##*/}"
[[ "$core_image_component" == *:* ]] || die "core image tag is required"
requested_tag="${core_image_component##*:}"
repository_ref="${core_image%:*}"
registry="${repository_ref%%/*}"
requested_repository="${repository_ref#*/}"
[ "$registry" != "$repository_ref" ] && [ -n "$requested_repository" ] ||
  die "core image must include a private ECR registry and repository"
[[ "$registry" =~ ^([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com(\.cn)?$ ]] ||
  die "core image registry is not a private Amazon ECR registry"
registry_id="${BASH_REMATCH[1]}"
registry_region="${BASH_REMATCH[2]}"
[ "$requested_repository" = "$quarantine_repository" ] ||
  die "core image repository does not match the contract quarantine repository"
[ "$repository_ref" = "$registry/$quarantine_repository" ] ||
  die "core image repository reference is not canonical"
unset core_image_component

source_uri="$(jq -er '.source.repository' "$canonical_contract")" ||
  die "could not read the OpenClaw source repository"
expected_source_branch="$(jq -er '.source.branch' "$canonical_contract")" ||
  die "could not read the OpenClaw source branch"
source_commit="$(git -C "$repo_root" rev-parse --verify HEAD^{commit})" ||
  die "OpenClaw bundle build requires a Git commit"
source_tree="$(git -C "$repo_root" rev-parse --verify HEAD^{tree})" ||
  die "OpenClaw bundle build requires a Git tree"
worktree_status="$(
  git -C "$repo_root" status \
    --porcelain=v1 --untracked-files=all --ignore-submodules=none
)" || die "could not inspect the OpenClaw Git worktree"
[ -z "$worktree_status" ] ||
  die "refusing to build a dirty or untracked source tree"
if source_branch="$(git -C "$repo_root" symbolic-ref --quiet --short HEAD)"; then
  [ "$source_branch" = "$expected_source_branch" ] ||
    die "OpenClaw bundle build must use the contract source branch"
else
  [ "${CODEBUILD_RESOLVED_SOURCE_VERSION:-}" = "$source_commit" ] &&
    [ "${CODEBUILD_SOURCE_VERSION:-}" = "$source_commit" ] ||
    die "detached builds require exact CodeBuild source identity"
  expected_build_arn_prefix="arn:aws:codebuild:$registry_region:$registry_id:build/teamagent-dev-openclaw-provenance-builder:"
  [[ "${CODEBUILD_BUILD_ARN:-}" == "$expected_build_arn_prefix"* ]] ||
    die "detached build has an unexpected CodeBuild identity"
  [ "$(git -C "$repo_root" remote get-url origin)" = "$source_uri" ] ||
    die "detached build has an unexpected Git origin"
  [ "$(
    git -C "$repo_root" rev-parse \
      --verify "refs/remotes/origin/$expected_source_branch^{commit}"
  )" = "$source_commit" ] ||
    die "detached build is not the exact contract branch head"
  source_branch="$expected_source_branch"
  unset expected_build_arn_prefix
fi
source_archive_sha256="$(
  git -C "$repo_root" archive --format=tar "$source_commit" |
    sha256sum |
    cut -d' ' -f1
)" || die "could not hash the exact Git archive"
source_artifact_version="git-$source_commit"

[[ "$source_commit" =~ ^[0-9a-f]{40}$ ]] ||
  die "source commit must be a full lowercase Git SHA"
[[ "$source_tree" =~ ^[0-9a-f]{40}$ ]] ||
  die "source tree must be a full lowercase Git SHA"
[[ "$source_branch" =~ ^[A-Za-z0-9._/-]+$ ]] ||
  die "source branch contains unsafe characters"
[[ "$source_archive_sha256" =~ ^[0-9a-f]{64}$ ]] ||
  die "source archive SHA-256 is invalid"
[[ "$source_artifact_version" =~ ^[A-Za-z0-9._+/=-]+$ ]] ||
  die "source artifact version contains unsafe characters"

expected_tag="candidate-$source_commit-$subject_name"
[ "$requested_tag" = "$expected_tag" ] ||
  die "core image tag must be $expected_tag"
[ "$core_image" = "$registry/$quarantine_repository:$expected_tag" ] ||
  die "core image reference does not match the contract-derived candidate"

docker buildx version >/dev/null 2>&1 ||
  die "docker buildx is unavailable"
jq -e '.schemaVersion == 1' "$lock_file" >/dev/null ||
  die "invalid OpenClaw plugins lock"
expected_buildx_version="$(jq -er '.tooling.buildx.version' "$lock_file")" ||
  die "could not read the locked Docker Buildx version"
buildx_version="$(docker buildx version | awk '{print $2}')" ||
  die "could not determine the Docker Buildx version"
if [[ "$buildx_version" != "v$expected_buildx_version" &&
      "$buildx_version" != "v$expected_buildx_version-"* ]]; then
  die "Docker Buildx v$expected_buildx_version is required (found $buildx_version)"
fi
expected_trivy_version="$(jq -er '.tooling.trivy.version' "$lock_file")" ||
  die "could not read the locked Trivy version"
trivy_version="$(trivy --version --format json | jq -er '.Version')" ||
  die "could not determine the Trivy version"
[ "$trivy_version" = "$expected_trivy_version" ] ||
  die "Trivy $expected_trivy_version is required (found $trivy_version)"

openclaw_version="$(jq -er '.openclaw.version' "$lock_file")" ||
  die "could not read the locked OpenClaw version"
openclaw_arm64_digest="$(jq -er '.openclaw.linuxArm64Digest' "$lock_file")" ||
  die "could not read the locked OpenClaw arm64 digest"
runtime_arm64_digest="$(jq -er '.runtime.linuxArm64Digest' "$lock_file")" ||
  die "could not read the locked runtime base arm64 digest"
dockerfile_frontend_digest="$(
  jq -er '.tooling.dockerfileFrontend.digest' "$lock_file"
)" || die "could not read the locked Dockerfile frontend digest"
plugins_lock_sha256="$(sha256sum "$lock_file" | cut -d' ' -f1)" ||
  die "could not hash the OpenClaw plugins lock"
for digest in \
  "$openclaw_arm64_digest" \
  "$runtime_arm64_digest" \
  "$dockerfile_frontend_digest"; do
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    die "OpenClaw plugins lock contains an invalid image digest"
done
[[ "$plugins_lock_sha256" =~ ^[0-9a-f]{64}$ ]] ||
  die "OpenClaw plugins lock SHA-256 is invalid"
for pin in \
  "$openclaw_version" \
  "$openclaw_arm64_digest" \
  "$runtime_arm64_digest" \
  "$dockerfile_frontend_digest"; do
  grep -F -- "$pin" "$dockerfile" >/dev/null ||
    die "Dockerfile does not contain lock pin: $pin"
done

[ "$manifest" != "/" ] && [[ "$manifest" != */ ]] ||
  die "receipt output must be a file path"
[ ! -e "$manifest" ] && [ ! -L "$manifest" ] ||
  die "receipt output already exists"

tmp_dir="$(mktemp -d /tmp/openclaw-bundle.XXXXXX)" ||
  die "could not create OpenClaw bundle temporary directory"
receipt_tmp=""
buildx_builder=""
cleanup() {
  if [ -n "$receipt_tmp" ]; then
    rm -f -- "$receipt_tmp" >/dev/null 2>&1 || true
  fi
  if [ -n "$buildx_builder" ]; then
    docker buildx rm --force "$buildx_builder" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$tmp_dir" >/dev/null 2>&1 || true
}
trap cleanup EXIT

trivy_cache_dir="${TRIVY_CACHE_DIR:-$tmp_dir/trivy-cache}"
mkdir -p -- "$trivy_cache_dir" ||
  die "could not create the Trivy cache directory"
export TRIVY_DB_REPOSITORY="${TRIVY_DB_REPOSITORY:-public.ecr.aws/aquasecurity/trivy-db:2,mirror.gcr.io/aquasec/trivy-db:2,ghcr.io/aquasecurity/trivy-db:2}"

# Registry lookup must not be redirected to a configured endpoint. The exact
# ECR registry in --core-image remains the Docker push destination.
export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true
unset AWS_ENDPOINT_URL AWS_ENDPOINT_URL_ECR
while IFS= read -r aws_endpoint_variable; do
  unset "$aws_endpoint_variable"
done < <(compgen -A variable AWS_ENDPOINT_URL || true)
unset aws_endpoint_variable

tag_lookup_error="$tmp_dir/tag-lookup.err"
if existing_digest="$(
  AWS_PAGER="" aws ecr describe-images \
    --region "$registry_region" \
    --registry-id "$registry_id" \
    --repository-name "$quarantine_repository" \
    --image-ids "imageTag=$expected_tag" \
    --query 'imageDetails[0].imageDigest' \
    --output text 2>"$tag_lookup_error"
)"; then
  [[ "$existing_digest" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    die "existing quarantine tag returned an invalid digest"
  die "immutable quarantine tag already exists: $quarantine_repository:$expected_tag"
else
  grep -F "ImageNotFoundException" "$tag_lookup_error" >/dev/null ||
    die "could not prove that the quarantine tag is absent"
fi

# Buildx's default docker driver rejects push-by-digest. Use an isolated
# docker-container builder so the parent index can be published without ever
# consuming the immutable candidate tag.
buildx_builder="openclaw-bundle-$(basename -- "$tmp_dir")"
docker buildx create \
  --name "$buildx_builder" \
  --driver docker-container \
  --platform linux/arm64 \
  --bootstrap >/dev/null ||
  die "could not create the isolated OpenClaw Buildx builder"
# awk must consume the whole stream. Exiting at the first match closes the pipe
# while docker is still writing, which kills it with SIGPIPE; under pipefail that
# fails the substitution with status 141 and no stderr at all, so the build dies
# without a diagnosable cause. Keep the first Driver: value and read to the end.
buildx_driver="$(
  docker buildx inspect "$buildx_builder" |
    awk '$1 == "Driver:" && driver == "" { driver = $2 } END { print driver }'
)" || die "could not inspect the isolated OpenClaw Buildx builder"
[ "$buildx_driver" = "docker-container" ] ||
  die "isolated OpenClaw Buildx builder has an unsupported driver"
unset buildx_driver

build=(
  docker buildx build
  --builder "$buildx_builder"
  --platform linux/arm64
  --pull
  -f "$dockerfile"
  --build-arg "OPENCLAW_VERSION=$openclaw_version"
  --build-arg "OPENCLAW_ARM64_DIGEST=$openclaw_arm64_digest"
  --build-arg "RUNTIME_ARM64_DIGEST=$runtime_arm64_digest"
  --build-arg "GIT_COMMIT=$source_commit"
  --build-arg "GIT_BRANCH=$source_branch"
  --build-arg "SOURCE_TREE=$source_tree"
  --build-arg "SOURCE_URI=$source_uri"
  --build-arg "SOURCE_ARCHIVE_SHA256=$source_archive_sha256"
  --build-arg "SOURCE_ARTIFACT_VERSION=$source_artifact_version"
  --build-arg "PLUGINS_LOCK_SHA256=$plugins_lock_sha256"
  --build-arg "RELEASE_CONTRACT_SHA256=$bundle_contract_sha256"
  --provenance=mode=max
  --metadata-file "$tmp_dir/build-metadata.json"
  --output "type=registry,name=$repository_ref,push-by-digest=true,oci-mediatypes=true"
)
if ! "${build[@]}" "$repo_root"; then
  failed_push_lookup_error="$tmp_dir/failed-push-tag-lookup.err"
  if failed_push_digest="$(
    AWS_PAGER="" aws ecr describe-images \
      --region "$registry_region" \
      --registry-id "$registry_id" \
      --repository-name "$quarantine_repository" \
      --image-ids "imageTag=$expected_tag" \
      --query 'imageDetails[0].imageDigest' \
      --output text 2>"$failed_push_lookup_error"
  )"; then
    [[ "$failed_push_digest" =~ ^sha256:[0-9a-f]{64}$ ]] ||
      die "failed push left an immutable tag with an invalid digest"
    die "immutable quarantine tag appeared during the build: $quarantine_repository:$expected_tag"
  fi
  grep -F "ImageNotFoundException" "$failed_push_lookup_error" >/dev/null ||
    die "OpenClaw build failed and the quarantine tag state could not be determined"
  die "OpenClaw core image build or untagged quarantine push failed"
fi

index_digest="$(jq -er '."containerimage.digest"' "$tmp_dir/build-metadata.json")" ||
  die "could not read the Buildx output index digest"
[[ "$index_digest" =~ ^sha256:[0-9a-f]{64}$ ]] ||
  die "pushed OpenClaw index digest is invalid"

docker buildx imagetools inspect --raw \
  "$repository_ref@$index_digest" >"$tmp_dir/index.json" ||
  die "could not read the pushed OpenClaw image index"
raw_index_sha256="$(sha256sum "$tmp_dir/index.json" | cut -d' ' -f1)" ||
  die "could not hash the pushed OpenClaw image index"
[ "sha256:$raw_index_sha256" = "$index_digest" ] ||
  die "pushed OpenClaw index bytes do not match the ECR digest"
jq -e '
  .schemaVersion == 2 and
  (
    .mediaType == "application/vnd.oci.image.index.v1+json" or
    .mediaType == "application/vnd.docker.distribution.manifest.list.v2+json"
  ) and
  ((.manifests | type) == "array") and
  (.manifests | length) > 0 and
  all(
    .manifests[];
    (type == "object") and
    ((.mediaType | type) == "string") and
    ((.digest | type) == "string") and
    (.digest | test("^sha256:[0-9a-f]{64}$")) and
    ((.size | type) == "number") and
    (.size > 0) and
    ((.platform | type) == "object") and
    ((.platform.os | type) == "string") and
    ((.platform.architecture | type) == "string")
  )
' "$tmp_dir/index.json" >/dev/null ||
  die "pushed OpenClaw parent is not a valid image index"
arm64_digest="$(
  jq -er --arg media_type "$arm64_subject_media_type" '
    [
      .manifests[] |
      select(
        .platform.os == "linux" and
        .platform.architecture == "arm64" and
        (
          (.platform.variant // "") == "" or
          .platform.variant == "v8"
        )
      )
    ] |
    if length != 1 then
      error("image index must contain exactly one linux/arm64 descriptor")
    elif .[0].mediaType != $media_type then
      error("linux/arm64 descriptor has the wrong media type")
    else
      .[0].digest
    end
  ' "$tmp_dir/index.json"
)" || die "could not resolve exactly one OpenClaw arm64 child digest"
[[ "$arm64_digest" =~ ^sha256:[0-9a-f]{64}$ ]] ||
  die "pushed OpenClaw arm64 child digest is invalid"
[ "$index_digest" != "$arm64_digest" ] ||
  die "OpenClaw image index digest equals its arm64 child digest"

docker buildx imagetools inspect --raw \
  "$repository_ref@$arm64_digest" >"$tmp_dir/arm64-manifest.json" ||
  die "could not read the pushed OpenClaw arm64 manifest"
raw_arm64_sha256="$(
  sha256sum "$tmp_dir/arm64-manifest.json" | cut -d' ' -f1
)" || die "could not hash the pushed OpenClaw arm64 manifest"
[ "sha256:$raw_arm64_sha256" = "$arm64_digest" ] ||
  die "pushed OpenClaw arm64 manifest bytes do not match its digest"
jq -e --arg media_type "$arm64_subject_media_type" '
  .schemaVersion == 2 and
  .mediaType == $media_type and
  ((.config | type) == "object") and
  .config.mediaType == "application/vnd.oci.image.config.v1+json" and
  ((.config.digest | type) == "string") and
  (.config.digest | test("^sha256:[0-9a-f]{64}$")) and
  ((.config.size | type) == "number") and
  (.config.size > 0) and
  ((.layers | type) == "array")
' "$tmp_dir/arm64-manifest.json" >/dev/null ||
  die "OpenClaw arm64 child is not the contract-required single OCI manifest"

scan_ref="$repository_ref@$arm64_digest"
trivy --cache-dir "$trivy_cache_dir" image --quiet --scanners vuln \
  --severity CRITICAL,HIGH --format json \
  --output "$tmp_dir/vulnerabilities.json" "$scan_ref" ||
  die "Trivy failed to scan the pushed OpenClaw arm64 child"
jq -e --arg image "$scan_ref" '
  .ArtifactName == $image and
  (.ArtifactType == "container_image" or .ArtifactType == "image") and
  ((.Results | type) == "array") and
  (.Results | length) > 0 and
  all(
    .Results[];
    (type == "object") and
    (((.Vulnerabilities // []) | type) == "array") and
    all(
      (.Vulnerabilities // [])[];
      (type == "object") and
      (.Severity == "CRITICAL" or .Severity == "HIGH")
    )
  )
' "$tmp_dir/vulnerabilities.json" >/dev/null ||
  die "Trivy report does not bind a complete scan to the exact arm64 child"
critical_count="$(
  jq -er '[
    .Results[] |
    (.Vulnerabilities // [])[] |
    select(.Severity == "CRITICAL")
  ] | length' "$tmp_dir/vulnerabilities.json"
)" || die "could not count Critical vulnerabilities"
high_count="$(
  jq -er '[
    .Results[] |
    (.Vulnerabilities // [])[] |
    select(.Severity == "HIGH")
  ] | length' "$tmp_dir/vulnerabilities.json"
)" || die "could not count High vulnerabilities"
[[ "$critical_count" =~ ^[0-9]+$ ]] && [[ "$high_count" =~ ^[0-9]+$ ]] ||
  die "Trivy returned invalid Critical/High counts"
if [ "$critical_count" -gt "$scan_gate_critical" ] ||
  [ "$high_count" -gt "$scan_gate_high" ]; then
  jq -r '
    .Results[]? as $result |
    ($result.Vulnerabilities // [])[]? |
    "\(.Severity) \(.VulnerabilityID) \(.PkgName)@\(.InstalledVersion) \(.PkgPath // $result.Target)"
  ' "$tmp_dir/vulnerabilities.json" >&2 || true
  die "Critical/High vulnerability gate failed (critical=$critical_count, high=$high_count)"
fi
[ "$critical_count" -eq 0 ] && [ "$high_count" -eq 0 ] ||
  die "refusing to emit a non-zero OpenClaw scan receipt"

# Re-prove absence immediately before the only operation that creates the
# immutable candidate tag. The index and its children remain addressable by
# digest, while this first tag registration selects the single arm64 manifest.
prepublish_tag_lookup_error="$tmp_dir/prepublish-tag-lookup.err"
if prepublish_digest="$(
  AWS_PAGER="" aws ecr describe-images \
    --region "$registry_region" \
    --registry-id "$registry_id" \
    --repository-name "$quarantine_repository" \
    --image-ids "imageTag=$expected_tag" \
    --query 'imageDetails[0].imageDigest' \
    --output text 2>"$prepublish_tag_lookup_error"
)"; then
  [[ "$prepublish_digest" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    die "candidate tag returned an invalid digest before publication"
  die "immutable quarantine tag appeared before publication: $quarantine_repository:$expected_tag"
else
  grep -F "ImageNotFoundException" "$prepublish_tag_lookup_error" >/dev/null ||
    die "could not re-prove that the quarantine tag is absent"
fi

put_image_error="$tmp_dir/put-arm64-tag.err"
if ! AWS_PAGER="" aws ecr put-image \
  --region "$registry_region" \
  --registry-id "$registry_id" \
  --repository-name "$quarantine_repository" \
  --image-manifest "file://$tmp_dir/arm64-manifest.json" \
  --image-manifest-media-type "$arm64_subject_media_type" \
  --image-tag "$expected_tag" \
  --image-digest "$arm64_digest" \
  --output json >"$tmp_dir/put-arm64-tag.json" 2>"$put_image_error"; then
  failed_tag_lookup_error="$tmp_dir/failed-tag-registration-lookup.err"
  if failed_tag_digest="$(
    AWS_PAGER="" aws ecr describe-images \
      --region "$registry_region" \
      --registry-id "$registry_id" \
      --repository-name "$quarantine_repository" \
      --image-ids "imageTag=$expected_tag" \
      --query 'imageDetails[0].imageDigest' \
      --output text 2>"$failed_tag_lookup_error"
  )"; then
    [[ "$failed_tag_digest" =~ ^sha256:[0-9a-f]{64}$ ]] ||
      die "failed tag registration left an invalid immutable tag digest"
    die "immutable quarantine tag appeared during arm64 registration: $quarantine_repository:$expected_tag"
  fi
  grep -F "ImageNotFoundException" "$failed_tag_lookup_error" >/dev/null ||
    die "arm64 tag registration failed and the quarantine tag state is unknown"
  die "could not register the OpenClaw arm64 manifest under the candidate tag"
fi
jq -e \
  --arg registry_id "$registry_id" \
  --arg repository "$quarantine_repository" \
  --arg tag "$expected_tag" \
  --arg digest "$arm64_digest" \
  --arg media_type "$arm64_subject_media_type" '
    .image.registryId == $registry_id and
    .image.repositoryName == $repository and
    .image.imageId.imageTag == $tag and
    .image.imageId.imageDigest == $digest and
    .image.imageManifestMediaType == $media_type
  ' "$tmp_dir/put-arm64-tag.json" >/dev/null ||
  die "ECR returned an unexpected arm64 candidate tag registration"

quarantine_tag_digest="$(
  AWS_PAGER="" aws ecr describe-images \
    --region "$registry_region" \
    --registry-id "$registry_id" \
    --repository-name "$quarantine_repository" \
    --image-ids "imageTag=$expected_tag" \
    --query 'imageDetails[0].imageDigest' \
    --output text
)" || die "could not resolve the registered OpenClaw arm64 candidate tag"
[[ "$quarantine_tag_digest" =~ ^sha256:[0-9a-f]{64}$ ]] ||
  die "registered OpenClaw arm64 candidate tag returned an invalid digest"
[ "$quarantine_tag_digest" = "$arm64_digest" ] ||
  die "OpenClaw quarantine candidate tag does not select the arm64 manifest"

receipt_dir="$(dirname -- "$manifest")"
receipt_name="$(basename -- "$manifest")"
mkdir -p -- "$receipt_dir" ||
  die "could not create the receipt output directory"
[ -d "$receipt_dir" ] && [ ! -L "$receipt_dir" ] ||
  die "receipt output directory is invalid or a symlink"
[ ! -e "$manifest" ] && [ ! -L "$manifest" ] ||
  die "receipt output appeared during the bundle build"
receipt_tmp="$(mktemp "$receipt_dir/.${receipt_name}.XXXXXX")" ||
  die "could not create the temporary bundle receipt"
jq -n \
  --arg source_commit "$source_commit" \
  --arg contract_sha256 "$bundle_contract_sha256" \
  --arg name "$subject_name" \
  --arg quarantine_repository "$quarantine_repository" \
  --arg release_repository "$release_repository" \
  --arg tag "$expected_tag" \
  --arg index_digest "$index_digest" \
  --arg arm64_digest "$arm64_digest" \
  --argjson critical "$critical_count" \
  --argjson high "$high_count" \
  '{
    schema_version: 1,
    source_commit: $source_commit,
    bundle_contract_sha256: $contract_sha256,
    subjects: [
      {
        name: $name,
        quarantine_repository: $quarantine_repository,
        release_repository: $release_repository,
        tag: $tag,
        index_digest: $index_digest,
        arm64_digest: $arm64_digest,
        scan: {
          critical: $critical,
          high: $high
        }
      }
    ]
  }' >"$receipt_tmp" ||
  die "could not create the OpenClaw bundle receipt"
python3 "$provenance" verify-bundle-receipt \
  --receipt "$receipt_tmp" \
  --contract "$canonical_contract" \
  --expected-commit "$source_commit" \
  --expected-contract-sha256 "$bundle_contract_sha256" \
  >/dev/null ||
  die "generated OpenClaw bundle receipt failed canonical verification"
[ ! -e "$manifest" ] && [ ! -L "$manifest" ] ||
  die "receipt output appeared before publication"
mv -- "$receipt_tmp" "$manifest" ||
  die "could not publish the OpenClaw bundle receipt"
receipt_tmp=""

printf 'OpenClaw core quarantine receipt created: %s\n' "$manifest"
