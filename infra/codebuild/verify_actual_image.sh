#!/usr/bin/env bash
# Verify one exact quarantine digest and emit a release-receipt subject.
set -euo pipefail
umask 077

REGION="ap-northeast-1"
ACCOUNT_ID="718959508629"
REGISTRY="718959508629.dkr.ecr.ap-northeast-1.amazonaws.com"
PIPELINE=""
PROMOTION_CHANNEL=""
SUBJECT_NAME=""
QUARANTINE_REPOSITORY=""
CANDIDATE_REPOSITORY=""
RELEASE_REPOSITORY=""
COMMIT=""
CONTRACT=""
CONTRACT_SHA256=""
BUILD_CONTEXT_SHA256=""
RUNTIME_CONTRACT=""
EXPECTED_RUNTIME_CONTRACT_SHA256=""
APPROVAL_EVIDENCE_JSON=""
IMAGE_DIGEST=""
SIGNING_KEY_ARN=""
OUTPUT=""

die() {
  echo "FATAL: $*" >&2
  exit 1
}

value() {
  [ "$#" -ge 2 ] && [ -n "${2-}" ] || die "$1 requires a value"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --pipeline) value "$@"; PIPELINE="$2"; shift 2 ;;
    --channel) value "$@"; PROMOTION_CHANNEL="$2"; shift 2 ;;
    --subject) value "$@"; SUBJECT_NAME="$2"; shift 2 ;;
    --quarantine-repository) value "$@"; QUARANTINE_REPOSITORY="$2"; shift 2 ;;
    --candidate-repository) value "$@"; CANDIDATE_REPOSITORY="$2"; shift 2 ;;
    --release-repository) value "$@"; RELEASE_REPOSITORY="$2"; shift 2 ;;
    --commit) value "$@"; COMMIT="$2"; shift 2 ;;
    --contract) value "$@"; CONTRACT="$2"; shift 2 ;;
    --contract-sha256) value "$@"; CONTRACT_SHA256="$2"; shift 2 ;;
    --build-context-sha256) value "$@"; BUILD_CONTEXT_SHA256="$2"; shift 2 ;;
    --runtime-contract) value "$@"; RUNTIME_CONTRACT="$2"; shift 2 ;;
    --approval-evidence-json) value "$@"; APPROVAL_EVIDENCE_JSON="$2"; shift 2 ;;
    --image-digest) value "$@"; IMAGE_DIGEST="$2"; shift 2 ;;
    --signing-key-arn) value "$@"; SIGNING_KEY_ARN="$2"; shift 2 ;;
    --output) value "$@"; OUTPUT="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done

for required in \
  PIPELINE PROMOTION_CHANNEL SUBJECT_NAME QUARANTINE_REPOSITORY CANDIDATE_REPOSITORY \
  RELEASE_REPOSITORY COMMIT \
  CONTRACT CONTRACT_SHA256 IMAGE_DIGEST SIGNING_KEY_ARN OUTPUT; do
  [ -n "${!required}" ] || die "$required is required"
done
case "$PROMOTION_CHANNEL" in verified-candidate|active|rollback) ;; *) die "invalid promotion channel" ;; esac
case "$PIPELINE:$SUBJECT_NAME:$QUARANTINE_REPOSITORY:$CANDIDATE_REPOSITORY:$RELEASE_REPOSITORY" in
  mcp:core:teamagent-mcp-quarantine:teamagent-mcp-verified-candidates:teamagent-mcp) ;;
  mcp:media:teamagent-media-worker-quarantine:teamagent-media-worker-verified-candidates:teamagent-media-worker) ;;
  tiktok:tiktok:teamagent-dev-tiktok-acquire-quarantine:teamagent-dev-tiktok-acquire-verified-candidates:teamagent-dev-tiktok-acquire) ;;
  openclaw:core:teamagent-openclaw-quarantine:teamagent-openclaw-verified-candidates:teamagent-openclaw) ;;
  openclaw:media:teamagent-openclaw-media-quarantine:teamagent-openclaw-media-verified-candidates:teamagent-openclaw-media) ;;
  *) die "pipeline subject repositories are outside the fixed allowlist" ;;
esac
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "commit must be a full lowercase SHA"
[[ "$CONTRACT_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "invalid contract SHA-256"
if [ "$PIPELINE" = "mcp" ]; then
  [[ "$BUILD_CONTEXT_SHA256" =~ ^[0-9a-f]{64}$ ]] \
    || die "MCP canonical build context SHA-256 is required"
  [ -n "$RUNTIME_CONTRACT" ] || die "MCP inner runtime contract is required"
  [ -f "$RUNTIME_CONTRACT" ] || die "MCP inner runtime contract is missing"
  [ -n "$APPROVAL_EVIDENCE_JSON" ] || die "MCP approval evidence JSON is required"
  jq -e 'type == "object"' <<<"$APPROVAL_EVIDENCE_JSON" >/dev/null \
    || die "MCP approval evidence JSON is invalid"
  RELEASE_APPROVAL_SHA256="$(
    jq -er '.approval_payload_sha256 | select(test("^[0-9a-f]{64}$"))' \
      <<<"$APPROVAL_EVIDENCE_JSON"
  )" || die "MCP approval evidence lacks a valid payload hash"
elif [ -n "$BUILD_CONTEXT_SHA256" ] \
  || [ -n "$RUNTIME_CONTRACT" ] \
  || [ -n "$APPROVAL_EVIDENCE_JSON" ]; then
  die "MCP-only evidence arguments were provided for another pipeline"
fi
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "invalid image digest"
[[ "$SIGNING_KEY_ARN" =~ ^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f-]{36}$ ]] \
  || die "signing key is outside the fixed account and region"
[ -f "$CONTRACT" ] || die "contract is missing"
[ "$(sha256sum "$CONTRACT" | awk '{print $1}')" = "$CONTRACT_SHA256" ] \
  || die "contract bytes do not match the expected SHA-256"
if [ "$PIPELINE" = "mcp" ]; then
  EXPECTED_RUNTIME_CONTRACT_SHA256="$(
    jq -er '
      .source_runtime_contract.sha256 |
      select(test("^[0-9a-f]{64}$"))
    ' "$CONTRACT"
  )" || die "outer contract does not contain a valid inner runtime contract pin"
  [ "$(sha256sum "$RUNTIME_CONTRACT" | awk '{print $1}')" = \
    "$EXPECTED_RUNTIME_CONTRACT_SHA256" ] \
    || die "inner runtime contract bytes do not match the outer contract pin"
fi

for tool in aws cosign curl docker jq oras python3 sha256sum syft trivy; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"
unset AWS_ENDPOINT_URL AWS_ENDPOINT_URL_STS AWS_ENDPOINT_URL_ECR AWS_ENDPOINT_URL_KMS
while IFS= read -r AWS_ENDPOINT_VARIABLE; do
  unset "$AWS_ENDPOINT_VARIABLE"
done < <(compgen -A variable AWS_ENDPOINT_URL)
unset AWS_ENDPOINT_VARIABLE
unset ECR_REGISTRY MCP_REPO TIKTOK_REPO OPENCLAW_REPO OPENCLAW_MEDIA_REPO
unset TRIVY_DB_REPOSITORY TRIVY_JAVA_DB_REPOSITORY
unset COSIGN_EXPERIMENTAL COSIGN_REPOSITORY
export TRIVY_DB_REPOSITORY="public.ecr.aws/aquasecurity/trivy-db:2"
export TRIVY_JAVA_DB_REPOSITORY="public.ecr.aws/aquasecurity/trivy-java-db:1"
export COSIGN_EXPERIMENTAL=1
readonly AWS_IGNORE_CONFIGURED_ENDPOINT_URLS AWS_DEFAULT_REGION AWS_REGION
readonly TRIVY_DB_REPOSITORY TRIVY_JAVA_DB_REPOSITORY
readonly COSIGN_EXPERIMENTAL

[ "$(aws sts get-caller-identity --query Account --output text)" = "$ACCOUNT_ID" ] \
  || die "wrong AWS account"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
EVIDENCE_HELPER="$SCRIPT_DIR/actual_image_evidence.py"
PROVENANCE_HELPER="$SCRIPT_DIR/source_provenance.py"
BUNDLE_PROVENANCE_HELPER="$SCRIPT_DIR/teamagent_bundle_provenance.py"
[ -f "$EVIDENCE_HELPER" ] || die "actual-image evidence helper is missing"
[ -f "$PROVENANCE_HELPER" ] || die "source provenance helper is missing"
[ -f "$BUNDLE_PROVENANCE_HELPER" ] || die "core/media provenance helper is missing"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-actual-image.XXXXXXXX")"
CONTAINER_NAME="teamagent-evidence-${PIPELINE}-${SUBJECT_NAME}-$$"
cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

IMAGE="$REGISTRY/$QUARANTINE_REPOSITORY@$IMAGE_DIGEST"
MANIFEST_RESPONSE="$TMP_DIR/manifest-response.json"
CONFIG="$TMP_DIR/config.json"
BINARY_EXPECTED="$TMP_DIR/binary-expected.tsv"
BINARY_ACTUAL="$TMP_DIR/binary-actual.tsv"
TRIVY_REPORT="$TMP_DIR/trivy.json"
SBOM="$TMP_DIR/sbom.spdx.json"
PROVENANCE="$TMP_DIR/provenance.intoto.json"
SUBJECT_REFERRERS="$TMP_DIR/subject-referrers.json"
SBOM_SIGNATURE_REFERRERS="$TMP_DIR/sbom-signature-referrers.json"
PROVENANCE_SIGNATURE_REFERRERS="$TMP_DIR/provenance-signature-referrers.json"
IMAGE_SIGNATURE_VERIFICATION="$TMP_DIR/image-signature-verification.json"
SBOM_SIGNATURE_VERIFICATION="$TMP_DIR/sbom-signature-verification.json"
PROVENANCE_SIGNATURE_VERIFICATION="$TMP_DIR/provenance-signature-verification.json"

aws ecr batch-get-image \
  --region "$REGION" \
  --registry-id "$ACCOUNT_ID" \
  --repository-name "$QUARANTINE_REPOSITORY" \
  --image-ids "imageDigest=$IMAGE_DIGEST" \
  --accepted-media-types \
    application/vnd.docker.distribution.manifest.v2+json \
    application/vnd.oci.image.manifest.v1+json \
  --output json >"$MANIFEST_RESPONSE"
MEDIA_TYPE="$(jq -er '.images | if length == 1 then .[0].imageManifestMediaType else error("ambiguous image") end' "$MANIFEST_RESPONSE")"
case "$MEDIA_TYPE" in
  application/vnd.docker.distribution.manifest.v2+json|application/vnd.oci.image.manifest.v1+json) ;;
  *) die "subject must be a single ECR-scan-capable image manifest, not an index" ;;
esac
RETURNED_DIGEST="$(jq -er '.images[0].imageId.imageDigest' "$MANIFEST_RESPONSE")"
[ "$RETURNED_DIGEST" = "$IMAGE_DIGEST" ] || die "ECR returned a different subject digest"
CONFIG_DIGEST="$(
  jq -er '.images[0].imageManifest | fromjson | .config.digest' "$MANIFEST_RESPONSE"
)"
[[ "$CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "invalid config digest"
CONFIG_URL="$(
  aws ecr get-download-url-for-layer \
    --region "$REGION" \
    --registry-id "$ACCOUNT_ID" \
    --repository-name "$QUARANTINE_REPOSITORY" \
    --layer-digest "$CONFIG_DIGEST" \
    --query downloadUrl \
    --output text
)"
[[ "$CONFIG_URL" == https://* ]] || die "ECR returned a non-HTTPS config URL"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error \
  --output "$CONFIG" "$CONFIG_URL"
unset CONFIG_URL

case "$PIPELINE" in
  mcp)
    python3 "$BUNDLE_PROVENANCE_HELPER" binary-probes \
      --contract "$CONTRACT" \
      --subject "$SUBJECT_NAME" >"$BINARY_EXPECTED"
    ;;
  tiktok)
    # The readiness guard must keep the contract as the pipeline value. Writing it
    # as `.release.ready == true or error(...)` emits a boolean instead, so the
    # next stage indexes a boolean and dies -- and only once a contract is
    # actually ready, since a blocked one short-circuits into error() first.
    jq -er '
      if .release.ready != true then error("release.ready is false") else . end |
      .image.binary_probes |
      if length > 0 then .[] | [.path, .sha256] | @tsv
      else error("binary probes are missing")
      end
    ' "$CONTRACT" >"$BINARY_EXPECTED"
    ;;
  openclaw)
    jq -er --arg subject "$SUBJECT_NAME" '
      if .release.ready != true then error("release.ready is false") else . end |
      .bundle.subjects[] |
      select(.name == $subject) |
      .binary_probes |
      if length > 0 then .[] | [.path, .sha256] | @tsv
      else error("binary probes are missing")
      end
    ' "$CONTRACT" >"$BINARY_EXPECTED"
    ;;
esac
LC_ALL=C sort -t $'\t' -k1,1 -o "$BINARY_EXPECTED" "$BINARY_EXPECTED"
DUPLICATE_BINARY_PATH="$(
  awk -F '\t' 'seen[$1]++ { print $1; exit }' "$BINARY_EXPECTED"
)"
[ -z "$DUPLICATE_BINARY_PATH" ] || die "duplicate actual-image probe path"
unset DUPLICATE_BINARY_PATH

docker pull "$IMAGE" >/dev/null
docker create --name "$CONTAINER_NAME" "$IMAGE" >/dev/null
while IFS=$'\t' read -r BINARY_PATH EXPECTED_BINARY_SHA256; do
  [[ "$BINARY_PATH" =~ ^/[A-Za-z0-9][A-Za-z0-9_./+-]{0,511}$ ]] \
    && [[ "$BINARY_PATH" != *"/../"* ]] \
    || die "unsafe binary probe path"
  [[ "$EXPECTED_BINARY_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "invalid expected binary hash"
  LOCAL_BINARY="$TMP_DIR/binary-${#BINARY_PATH}-$(printf '%s' "$BINARY_PATH" | sha256sum | cut -c1-16)"
  docker cp "$CONTAINER_NAME:$BINARY_PATH" "$LOCAL_BINARY" >/dev/null
  ACTUAL_BINARY_SHA256="$(sha256sum "$LOCAL_BINARY" | awk '{print $1}')"
  [ "$ACTUAL_BINARY_SHA256" = "$EXPECTED_BINARY_SHA256" ] \
    || die "actual-image binary hash mismatch: $BINARY_PATH"
  printf '%s\t%s\n' "$BINARY_PATH" "$ACTUAL_BINARY_SHA256" >>"$BINARY_ACTUAL"
  rm -f -- "$LOCAL_BINARY"
done <"$BINARY_EXPECTED"
[ -s "$BINARY_ACTUAL" ] || die "no actual-image binary hashes were verified"

trivy image \
  --scanners vuln,secret \
  --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL \
  --format json \
  --output "$TRIVY_REPORT" \
  "$IMAGE"
python3 - "$TRIVY_REPORT" "$IMAGE" "$CONTRACT" <<'PY'
import json
import sys

path, expected_image, contract_path = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    report = json.load(handle)
# The gate to enforce is the one the signed contract declares. A contract that
# declares bundle.scan_gate is enforced at exactly those thresholds; one that
# declares nothing keeps the historical all-severities-zero behaviour.
# Only an explicit zero Critical/High gate is accepted, so a contract can never
# be edited to permit Critical or High findings.
with open(contract_path, encoding="utf-8") as handle:
    contract = json.load(handle)
declared_gate = None
if isinstance(contract.get("bundle"), dict):
    declared_gate = contract["bundle"].get("scan_gate")
if declared_gate is not None:
    if not isinstance(declared_gate, dict) or set(declared_gate) != {"critical", "high"}:
        raise SystemExit("FATAL: contract scan gate is malformed")
    for key in ("critical", "high"):
        value = declared_gate[key]
        if value is not True and value is not False and isinstance(value, int) and value == 0:
            continue
        raise SystemExit("FATAL: contract scan gate must require zero Critical and High")
if report.get("ArtifactName") != expected_image:
    raise SystemExit("FATAL: Trivy report does not bind the exact quarantine digest")
if report.get("ArtifactType") not in {"container_image", "image"}:
    raise SystemExit("FATAL: Trivy did not scan an actual container image")
results = report.get("Results")
if not isinstance(results, list) or not results:
    raise SystemExit("FATAL: Trivy report has no scan results")
counts = {
    "UNKNOWN": 0,
    "LOW": 0,
    "MEDIUM": 0,
    "HIGH": 0,
    "CRITICAL": 0,
}
secrets = 0
for result in results:
    if not isinstance(result, dict):
        raise SystemExit("FATAL: malformed Trivy result")
    vulnerabilities = result.get("Vulnerabilities") or []
    discovered_secrets = result.get("Secrets") or []
    if not isinstance(vulnerabilities, list) or not isinstance(discovered_secrets, list):
        raise SystemExit("FATAL: malformed Trivy findings")
    for item in vulnerabilities:
        if not isinstance(item, dict) or item.get("Severity") not in counts:
            raise SystemExit("FATAL: actual-image scan has unsupported severity")
        counts[item["Severity"]] += 1
    secrets += len(discovered_secrets)
if declared_gate is None:
    blocking = sum(counts.values())
else:
    blocking = counts["CRITICAL"] + counts["HIGH"]
    # Distroless Debian ships Low/Medium CVEs with no fixed version at all, so an
    # all-severities-zero gate can never pass. Report them so they stay visible
    # in the build record instead of being silently dropped.
    print(
        f"actual-image scan: severities={counts}, secrets={secrets}; "
        "gate is contract-declared zero Critical/High plus zero secrets",
        flush=True,
    )
if blocking or secrets:
    raise SystemExit(
        f"FATAL: actual-image gate failed: severities={counts}, secrets={secrets}"
    )
PY
syft "$IMAGE" --output spdx-json="$SBOM"
python3 - "$PIPELINE" "$SUBJECT_NAME" "$COMMIT" "$CONTRACT_SHA256" \
  "$BUILD_CONTEXT_SHA256" "$EXPECTED_RUNTIME_CONTRACT_SHA256" \
  "${RELEASE_APPROVAL_SHA256-}" "$IMAGE_DIGEST" "$PROVENANCE" <<'PY'
import json
import sys

(
    pipeline,
    subject_name,
    commit,
    contract_sha256,
    build_context_sha256,
    runtime_contract_sha256,
    release_approval_sha256,
    image_digest,
    output,
) = sys.argv[1:]
repository = {
    "mcp": "https://github.com/noirelumiere00/TeamAgent",
    "openclaw": "https://github.com/noirelumiere00/TeamAgent",
    "tiktok": "https://github.com/noirelumiere00/tiktok-data-service",
}[pipeline]
value = {
    "_type": "https://in-toto.io/Statement/v1",
    "subject": [
        {
            "name": f"{pipeline}/{subject_name}",
            "digest": {"sha256": image_digest.removeprefix("sha256:")},
        }
    ],
    "predicateType": "https://slsa.dev/provenance/v1",
    "predicate": {
        "buildDefinition": {
            "buildType": "https://teamagent.invalid/codebuild/actual-image/v1",
            "externalParameters": {
                "pipeline": pipeline,
                "subject": subject_name,
                "sourceCommit": commit,
                "contractSha256": contract_sha256,
            },
            "internalParameters": {},
            "resolvedDependencies": [
                {
                    "uri": f"git+{repository}@{commit}",
                    "digest": {"gitCommit": commit},
                }
            ],
        },
        "runDetails": {
            "builder": {"id": "teamagent-dev-image-attestor"},
            "metadata": {"invocationId": "redacted"},
        },
    },
}
if pipeline == "mcp":
    value["predicate"]["buildDefinition"]["externalParameters"][
        "buildContextSha256"
    ] = build_context_sha256
    value["predicate"]["buildDefinition"]["externalParameters"][
        "runtimeContractSha256"
    ] = runtime_contract_sha256
    value["predicate"]["buildDefinition"]["externalParameters"][
        "releaseApprovalSha256"
    ] = release_approval_sha256
with open(output, "w", encoding="utf-8") as handle:
    json.dump(value, handle, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
PY

KMS_URI="awskms:///$SIGNING_KEY_ARN"
[ "${CODEBUILD_BUILD_SUCCEEDING:-0}" = "1" ] \
  || die "actual-image gates failed before any image or attestation signing"
cosign sign --yes \
  --registry-referrers-mode=oci-1-1 \
  --key "$KMS_URI" \
  "$IMAGE" >/dev/null
SBOM_SHA256="$(sha256sum "$SBOM" | awk '{print $1}')"
PROVENANCE_SHA256="$(sha256sum "$PROVENANCE" | awk '{print $1}')"
(
  cd "$TMP_DIR"
  oras attach \
    --artifact-type application/spdx+json \
    --annotation "io.teamagent.build.payload-sha256=$SBOM_SHA256" \
    "$IMAGE" \
    "sbom.spdx.json:application/spdx+json" \
    --format json >"$TMP_DIR/sbom-attach.json"
  oras attach \
    --artifact-type application/vnd.in-toto+json \
    --annotation "io.teamagent.build.payload-sha256=$PROVENANCE_SHA256" \
    "$IMAGE" \
    "provenance.intoto.json:application/vnd.in-toto+json" \
    --format json >"$TMP_DIR/provenance-attach.json"
)
SBOM_DIGEST="$(jq -er '.digest | select(test("^sha256:[0-9a-f]{64}$"))' "$TMP_DIR/sbom-attach.json")"
PROVENANCE_DIGEST="$(jq -er '.digest | select(test("^sha256:[0-9a-f]{64}$"))' "$TMP_DIR/provenance-attach.json")"

aws ecr list-image-referrers \
  --region "$REGION" \
  --registry-id "$ACCOUNT_ID" \
  --repository-name "$QUARANTINE_REPOSITORY" \
  --subject-id "imageDigest=$IMAGE_DIGEST" \
  --max-results 50 \
  --output json >"$SUBJECT_REFERRERS"
jq -e --arg digest "$SBOM_DIGEST" --arg payload "$SBOM_SHA256" '
  (.nextToken == null) and
  any(.referrers[];
    .digest == $digest and
    .artifactType == "application/spdx+json" and
    .artifactStatus == "ACTIVE" and
    .annotations["io.teamagent.build.payload-sha256"] == $payload
  )
' "$SUBJECT_REFERRERS" >/dev/null || die "exact SBOM referrer is missing or truncated"
jq -e --arg digest "$PROVENANCE_DIGEST" --arg payload "$PROVENANCE_SHA256" '
  (.nextToken == null) and
  any(.referrers[];
    .digest == $digest and
    .artifactType == "application/vnd.in-toto+json" and
    .artifactStatus == "ACTIVE" and
    .annotations["io.teamagent.build.payload-sha256"] == $payload
  )
' "$SUBJECT_REFERRERS" >/dev/null || die "exact provenance referrer is missing or truncated"
cosign sign --yes --registry-referrers-mode=oci-1-1 --key "$KMS_URI" \
  "$REGISTRY/$QUARANTINE_REPOSITORY@$SBOM_DIGEST" >/dev/null
cosign sign --yes --registry-referrers-mode=oci-1-1 --key "$KMS_URI" \
  "$REGISTRY/$QUARANTINE_REPOSITORY@$PROVENANCE_DIGEST" >/dev/null
aws ecr list-image-referrers \
  --region "$REGION" \
  --registry-id "$ACCOUNT_ID" \
  --repository-name "$QUARANTINE_REPOSITORY" \
  --subject-id "imageDigest=$SBOM_DIGEST" \
  --max-results 50 \
  --output json >"$SBOM_SIGNATURE_REFERRERS"
aws ecr list-image-referrers \
  --region "$REGION" \
  --registry-id "$ACCOUNT_ID" \
  --repository-name "$QUARANTINE_REPOSITORY" \
  --subject-id "imageDigest=$PROVENANCE_DIGEST" \
  --max-results 50 \
  --output json >"$PROVENANCE_SIGNATURE_REFERRERS"

cosign verify --experimental-oci11 --key "$KMS_URI" --output json \
  "$IMAGE" >"$IMAGE_SIGNATURE_VERIFICATION"
cosign verify --experimental-oci11 --key "$KMS_URI" --output json \
  "$REGISTRY/$QUARANTINE_REPOSITORY@$SBOM_DIGEST" >"$SBOM_SIGNATURE_VERIFICATION"
cosign verify --experimental-oci11 --key "$KMS_URI" --output json \
  "$REGISTRY/$QUARANTINE_REPOSITORY@$PROVENANCE_DIGEST" >"$PROVENANCE_SIGNATURE_VERIFICATION"

EVIDENCE_CONTEXT_ARGUMENTS=()
if [ -n "$BUILD_CONTEXT_SHA256" ]; then
  EVIDENCE_CONTEXT_ARGUMENTS=(
    --build-context-sha256 "$BUILD_CONTEXT_SHA256"
    --runtime-contract "$RUNTIME_CONTRACT"
    --approval-evidence-json "$APPROVAL_EVIDENCE_JSON"
  )
fi
python3 "$EVIDENCE_HELPER" \
  --pipeline "$PIPELINE" \
  --channel "$PROMOTION_CHANNEL" \
  --subject "$SUBJECT_NAME" \
  --quarantine-repository "$QUARANTINE_REPOSITORY" \
  --candidate-repository "$CANDIDATE_REPOSITORY" \
  --release-repository "$RELEASE_REPOSITORY" \
  --commit "$COMMIT" \
  --contract "$CONTRACT" \
  --contract-sha256 "$CONTRACT_SHA256" \
  "${EVIDENCE_CONTEXT_ARGUMENTS[@]}" \
  --digest "$IMAGE_DIGEST" \
  --media-type "$MEDIA_TYPE" \
  --config-digest "$CONFIG_DIGEST" \
  --config "$CONFIG" \
  --binary-probes "$BINARY_ACTUAL" \
  --trivy-report "$TRIVY_REPORT" \
  --sbom "$SBOM" \
  --sbom-digest "$SBOM_DIGEST" \
  --provenance "$PROVENANCE" \
  --provenance-digest "$PROVENANCE_DIGEST" \
  --subject-referrers "$SUBJECT_REFERRERS" \
  --sbom-signature-referrers "$SBOM_SIGNATURE_REFERRERS" \
  --provenance-signature-referrers "$PROVENANCE_SIGNATURE_REFERRERS" \
  --image-signature-referrers "$SUBJECT_REFERRERS" \
  --image-signature-verification "$IMAGE_SIGNATURE_VERIFICATION" \
  --sbom-signature-verification "$SBOM_SIGNATURE_VERIFICATION" \
  --provenance-signature-verification "$PROVENANCE_SIGNATURE_VERIFICATION" \
  --signing-key-arn "$SIGNING_KEY_ARN" \
  --output "$OUTPUT"
