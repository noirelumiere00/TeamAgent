#!/usr/bin/env bash
# Promote one already-gated immutable core digest to the ingest task definition.
set -euo pipefail

R=ap-northeast-1
TD_FAMILY=teamagent-dev-ingest
ECR="718959508629.dkr.ecr.$R.amazonaws.com/teamagent-mcp"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERIFY_GATE="$REPO_ROOT/infra/deploy/verify_release_gate.py"
IMAGE_URI=""
EXPECTED_COMMIT=""
EXPECTED_TREE=""
EXPECTED_BRANCH=""
RELEASE_GATE=""
RELEASE_GATE_SHA256=""
TMP_ROOT=""

usage() {
  cat <<'EOF'
usage: register_ingest_td.sh \
  --image-uri <ECR-repository@sha256:64hex> \
  --expected-commit <40hex> --expected-tree <40hex> --expected-branch <branch> \
  --release-gate <accepted-release-gate.json> \
  --release-gate-sha256 <64hex>

Tags are intentionally unsupported.
EOF
}

cleanup() {
  if [[ -n "$TMP_ROOT" ]]; then
    rm -rf "$TMP_ROOT"
  fi
}
trap cleanup EXIT INT TERM

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image-uri) IMAGE_URI=${2:?--image-uri requires a value}; shift 2 ;;
    --expected-commit) EXPECTED_COMMIT=${2:?--expected-commit requires a value}; shift 2 ;;
    --expected-tree) EXPECTED_TREE=${2:?--expected-tree requires a value}; shift 2 ;;
    --expected-branch) EXPECTED_BRANCH=${2:?--expected-branch requires a value}; shift 2 ;;
    --release-gate) RELEASE_GATE=${2:?--release-gate requires a value}; shift 2 ;;
    --release-gate-sha256)
      RELEASE_GATE_SHA256=${2:?--release-gate-sha256 requires a value}
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ "$IMAGE_URI" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9]+([._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$ ]] || {
  echo "--image-uri must be an immutable ECR digest URI" >&2
  exit 1
}
[[ "$IMAGE_URI" == "$ECR@sha256:"* ]] || {
  echo "--image-uri must target the canonical teamagent-mcp repository" >&2
  exit 1
}
[[ "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]] || {
  echo "--expected-commit must be full lowercase 40-hex" >&2
  exit 1
}
[[ "$EXPECTED_TREE" =~ ^[0-9a-f]{40}$ ]] || {
  echo "--expected-tree must be full lowercase 40-hex" >&2
  exit 1
}
[[ "$EXPECTED_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || {
  echo "--expected-branch is invalid" >&2
  exit 1
}
[[ -f "$RELEASE_GATE" ]] || { echo "--release-gate file is required" >&2; exit 1; }
[[ "$RELEASE_GATE_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
  echo "--release-gate-sha256 must be 64 lowercase hex" >&2
  exit 1
}
[[ "$(git -C "$REPO_ROOT" rev-parse HEAD)" == "$EXPECTED_COMMIT" ]]
[[ "$(git -C "$REPO_ROOT" rev-parse "$EXPECTED_COMMIT^{tree}")" == "$EXPECTED_TREE" ]]
[[ "$(git -C "$REPO_ROOT" branch --show-current)" == "$EXPECTED_BRANCH" ]]
[[ -z "$(git -C "$REPO_ROOT" status --porcelain)" ]]

command -v python3 >/dev/null
python3 "$VERIFY_GATE" \
  "$RELEASE_GATE" \
  --expected-sha256 "$RELEASE_GATE_SHA256" \
  --expected-commit "$EXPECTED_COMMIT" \
  --expected-tree "$EXPECTED_TREE" \
  --expected-branch "$EXPECTED_BRANCH" \
  --expected-image-uri "$IMAGE_URI"

# The SHA-pinned gate is checked before the first AWS read/write.
command -v jq >/dev/null
command -v aws >/dev/null
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/teamagent-ingest-promotion.XXXXXX")
CURRENT_TD="$TMP_ROOT/current-task-definition.json"
NEW_TD="$TMP_ROOT/new-task-definition.json"
aws ecs describe-task-definition \
  --region "$R" \
  --task-definition "$TD_FAMILY" \
  --query taskDefinition \
  >"$CURRENT_TD"
CURRENT_ARN=$(jq -r '.taskDefinitionArn' "$CURRENT_TD")
jq --arg img "$IMAGE_URI" '
  .containerDefinitions[0].image = $img
  | .containerDefinitions[0].entryPoint = []
  | .containerDefinitions[0].command = ["/app/.venv/bin/python","/app/scripts/run_ingest_fargate.py"]
  | .containerDefinitions[0].user = "10001:10001"
  | .containerDefinitions[0].readonlyRootFilesystem = true
  | .containerDefinitions[0].privileged = false
  | .containerDefinitions[0].linuxParameters = ((.containerDefinitions[0].linuxParameters // {}) + {"capabilities":{"drop":["ALL"]}})
  | .containerDefinitions[0].mountPoints = (
      [((.containerDefinitions[0].mountPoints)//[])[]|select(.containerPath!="/tmp")]
      + [{"sourceVolume":"runtime-tmp","containerPath":"/tmp","readOnly":false}])
  | .volumes = ([((.volumes)//[])[]|select(.name!="runtime-tmp")]+[{"name":"runtime-tmp"}])
  | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
        .registeredAt, .registeredBy, .deregisteredAt)
' "$CURRENT_TD" >"$NEW_TD"
NEW_ARN=$(
  aws ecs register-task-definition \
    --region "$R" \
    --cli-input-json "file://$NEW_TD" \
    --query 'taskDefinition.taskDefinitionArn' \
    --output text
)
printf 'IMAGE_URI=%s\nNEW_TASK_DEFINITION=%s\nPREVIOUS_TASK_DEFINITION=%s\n' \
  "$IMAGE_URI" "$NEW_ARN" "$CURRENT_ARN"
