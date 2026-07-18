#!/usr/bin/env bash
# Canonical connect-web promotion path.
#
# build-candidate creates an exact git archive and asks CodeBuild to publish a
# candidate.  It never updates ECS.  deploy-digest accepts only an immutable ECR
# digest and verifies a SHA-pinned, ACCEPTED release-gate document before the
# first AWS call.  ECR/Fargate evidence is therefore produced between the two
# modes, not bypassed by one build-and-deploy command.
set -euo pipefail

R=ap-northeast-1
CLUSTER=teamagent-dev
SVC=teamagent-dev-connect-web
TD_FAMILY=teamagent-dev-connect-web
ECR="718959508629.dkr.ecr.$R.amazonaws.com/teamagent-mcp"
BUCKET=teamagent-dev-raw-files
CB_PROJECT=teamagent-dev-image-builder
HD=vectorinc.co.jp
SLACK_REDIRECT="https://connect.newstv.co.jp/slack/oauth/callback"
CID_ARN="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/connect_slack_client_id-aTZTb2"
CSEC_ARN="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/connect_slack_secret-fOlJIt"
CSTATE_ARN="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/slack_oauth_state_secret-yGYkUF"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERIFY_GATE="$REPO_ROOT/infra/deploy/verify_release_gate.py"

MODE=""
EXPECTED_COMMIT=""
EXPECTED_TREE=""
EXPECTED_BRANCH=""
IMAGE_TAG=""
IMAGE_URI=""
RELEASE_GATE=""
RELEASE_GATE_SHA256=""
TMP_ROOT=""

usage() {
  cat <<'EOF'
usage:
  deploy_connectweb_unified.sh build-candidate \
    --expected-commit <40hex> --expected-tree <40hex> --expected-branch <branch> \
    [--image-tag <immutable-candidate-tag>]

  deploy_connectweb_unified.sh deploy-digest \
    --expected-commit <40hex> --expected-tree <40hex> --expected-branch <branch> \
    --image-uri <ECR-repository@sha256:64hex> \
    --release-gate <accepted-release-gate.json> \
    --release-gate-sha256 <64hex>

build-candidate uploads/builds only. deploy-digest performs no build and refuses
tags or incomplete/unreviewed local, ECR scan, and Fargate evidence.
EOF
}

cleanup() {
  if [[ -n "$TMP_ROOT" ]]; then
    rm -rf "$TMP_ROOT"
  fi
}
trap cleanup EXIT INT TERM

if [[ $# -gt 0 ]]; then
  case "$1" in
    build-candidate|deploy-digest) MODE=$1; shift ;;
    -h|--help) usage; exit 0 ;;
  esac
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --expected-commit) EXPECTED_COMMIT=${2:?--expected-commit requires a value}; shift 2 ;;
    --expected-tree) EXPECTED_TREE=${2:?--expected-tree requires a value}; shift 2 ;;
    --expected-branch) EXPECTED_BRANCH=${2:?--expected-branch requires a value}; shift 2 ;;
    --image-tag) IMAGE_TAG=${2:?--image-tag requires a value}; shift 2 ;;
    --image-uri) IMAGE_URI=${2:?--image-uri requires a value}; shift 2 ;;
    --release-gate) RELEASE_GATE=${2:?--release-gate requires a value}; shift 2 ;;
    --release-gate-sha256)
      RELEASE_GATE_SHA256=${2:?--release-gate-sha256 requires a value}
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ -n "$MODE" ]] || { echo "mode is required" >&2; usage >&2; exit 1; }
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

ACTUAL_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
ACTUAL_TREE=$(git -C "$REPO_ROOT" rev-parse "$EXPECTED_COMMIT^{tree}")
ACTUAL_BRANCH=$(git -C "$REPO_ROOT" branch --show-current)
[[ "$ACTUAL_COMMIT" == "$EXPECTED_COMMIT" ]] || {
  echo "repository HEAD differs from --expected-commit" >&2
  exit 1
}
[[ "$ACTUAL_TREE" == "$EXPECTED_TREE" ]] || {
  echo "commit tree differs from --expected-tree" >&2
  exit 1
}
[[ "$ACTUAL_BRANCH" == "$EXPECTED_BRANCH" ]] || {
  echo "current branch differs from --expected-branch" >&2
  exit 1
}
[[ -z "$(git -C "$REPO_ROOT" status --porcelain)" ]] || {
  echo "repository must be clean" >&2
  exit 1
}

if [[ "$MODE" == "build-candidate" ]]; then
  [[ -z "$IMAGE_URI$RELEASE_GATE$RELEASE_GATE_SHA256" ]] || {
    echo "deploy-only options are not accepted by build-candidate" >&2
    exit 1
  }
  IMAGE_TAG=${IMAGE_TAG:-"runtime-${EXPECTED_COMMIT:0:12}"}
  [[ "$IMAGE_TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
    echo "--image-tag is invalid" >&2
    exit 1
  }
  command -v aws >/dev/null
  command -v git >/dev/null
  command -v sha256sum >/dev/null
  TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/teamagent-canonical-build.XXXXXX")
  SOURCE_ARCHIVE="$TMP_ROOT/source.zip"
  git -C "$REPO_ROOT" archive --format=zip "$EXPECTED_COMMIT" >"$SOURCE_ARCHIVE"
  SOURCE_ARCHIVE_SHA256=$(sha256sum "$SOURCE_ARCHIVE" | cut -d' ' -f1)

  SOURCE_VERSION_ID=$(
    aws s3api put-object \
      --region "$R" \
      --bucket "$BUCKET" \
      --key codebuild/source.zip \
      --body "$SOURCE_ARCHIVE" \
      --query VersionId \
      --output text
  )
  [[ -n "$SOURCE_VERSION_ID" && "$SOURCE_VERSION_ID" != "None" ]] || {
    echo "versioned S3 source upload did not return VersionId" >&2
    exit 1
  }
  BUILD_ID=$(
    aws codebuild start-build \
      --region "$R" \
      --project-name "$CB_PROJECT" \
      --buildspec-override infra/codebuild/buildspec.yml \
      --source-version "$SOURCE_VERSION_ID" \
      --environment-variables-override \
        "name=IMAGE_TAG,value=$IMAGE_TAG,type=PLAINTEXT" \
        "name=GIT_COMMIT,value=$EXPECTED_COMMIT,type=PLAINTEXT" \
        "name=GIT_TREE,value=$EXPECTED_TREE,type=PLAINTEXT" \
        "name=GIT_BRANCH,value=$EXPECTED_BRANCH,type=PLAINTEXT" \
        "name=SOURCE_ARCHIVE_SHA256,value=$SOURCE_ARCHIVE_SHA256,type=PLAINTEXT" \
        "name=EXPECTED_SOURCE_VERSION_ID,value=$SOURCE_VERSION_ID,type=PLAINTEXT" \
      --query 'build.id' \
      --output text
  )
  while :; do
    STATUS=$(
      aws codebuild batch-get-builds \
        --region "$R" \
        --ids "$BUILD_ID" \
        --query 'builds[0].buildStatus' \
        --output text
    )
    case "$STATUS" in
      SUCCEEDED) break ;;
      FAILED|FAULT|STOPPED|TIMED_OUT)
        echo "CodeBuild failed: $STATUS ($BUILD_ID)" >&2
        exit 1
        ;;
      *) sleep 20 ;;
    esac
  done
  DIGEST=$(
    aws ecr describe-images \
      --region "$R" \
      --repository-name teamagent-mcp \
      --image-ids "imageTag=$IMAGE_TAG" \
      --query 'imageDetails[0].imageDigest' \
      --output text
  )
  [[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "CodeBuild did not publish an immutable digest" >&2
    exit 1
  }
  printf 'CANDIDATE_IMAGE_URI=%s@%s\n' "$ECR" "$DIGEST"
  printf 'GIT_COMMIT=%s\nGIT_TREE=%s\nGIT_BRANCH=%s\n' \
    "$EXPECTED_COMMIT" "$EXPECTED_TREE" "$EXPECTED_BRANCH"
  printf 'SOURCE_ARCHIVE_SHA256=%s\n' "$SOURCE_ARCHIVE_SHA256"
  printf 'SOURCE_VERSION_ID=%s\n' "$SOURCE_VERSION_ID"
  printf '%s\n' 'NO_DEPLOY: obtain accepted ECR/Fargate release evidence first.'
  exit 0
fi

[[ -z "$IMAGE_TAG" ]] || { echo "--image-tag is forbidden for deploy-digest" >&2; exit 1; }
[[ "$IMAGE_URI" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9]+([._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$ ]] || {
  echo "--image-uri must be an immutable ECR digest URI" >&2
  exit 1
}
[[ "$IMAGE_URI" == "$ECR@sha256:"* ]] || {
  echo "--image-uri must target the canonical teamagent-mcp repository" >&2
  exit 1
}
[[ -f "$RELEASE_GATE" ]] || { echo "--release-gate file is required" >&2; exit 1; }
[[ "$RELEASE_GATE_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
  echo "--release-gate-sha256 must be 64 lowercase hex" >&2
  exit 1
}
command -v python3 >/dev/null
python3 "$VERIFY_GATE" \
  "$RELEASE_GATE" \
  --expected-sha256 "$RELEASE_GATE_SHA256" \
  --expected-commit "$EXPECTED_COMMIT" \
  --expected-tree "$EXPECTED_TREE" \
  --expected-branch "$EXPECTED_BRANCH" \
  --expected-image-uri "$IMAGE_URI"

# No AWS read or write occurs before the local SHA-pinned release gate above.
command -v aws >/dev/null
command -v jq >/dev/null
aws iam get-role-policy \
  --role-name teamagent-dev-ecs-exec-connect-web \
  --policy-name slack-oauth-secrets \
  >/dev/null

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/teamagent-canonical-deploy.XXXXXX")
CURRENT_TD="$TMP_ROOT/current-task-definition.json"
NEW_TD="$TMP_ROOT/new-task-definition.json"
aws ecs describe-task-definition \
  --region "$R" \
  --task-definition "$TD_FAMILY" \
  --query taskDefinition \
  >"$CURRENT_TD"
CURRENT_ARN=$(jq -r '.taskDefinitionArn' "$CURRENT_TD")
APP_HTML_URI="s3://$BUCKET/codebuild/connect-web-app.html"
jq \
  --arg img "$IMAGE_URI" \
  --arg hd "$HD" \
  --arg rd "$SLACK_REDIRECT" \
  --arg apphtml "$APP_HTML_URI" \
  --arg cid "$CID_ARN" \
  --arg csec "$CSEC_ARN" \
  --arg cst "$CSTATE_ARN" '
  .containerDefinitions[0].image=$img
  | .containerDefinitions[0].entryPoint=[]
  | .containerDefinitions[0].command=["/app/.venv/bin/python","-m","teamagent.connect_web"]
  | .containerDefinitions[0].user="10001:10001"
  | .containerDefinitions[0].readonlyRootFilesystem=true
  | .containerDefinitions[0].privileged=false
  | .containerDefinitions[0].linuxParameters=((.containerDefinitions[0].linuxParameters // {}) + {"capabilities":{"drop":["ALL"]}})
  | .containerDefinitions[0].mountPoints=(
      [((.containerDefinitions[0].mountPoints)//[])[]|select(.containerPath!="/tmp")]
      + [{"sourceVolume":"runtime-tmp","containerPath":"/tmp","readOnly":false}])
  | .containerDefinitions[0].healthCheck={
      "command":["CMD","/app/.venv/bin/python","-c","import urllib.request; urllib.request.urlopen('\''http://127.0.0.1:8788/healthz'\'', timeout=4).close()"],
      "interval":30,"timeout":5,"retries":5,"startPeriod":40}
  | .volumes=([((.volumes)//[])[]|select(.name!="runtime-tmp")]+[{"name":"runtime-tmp"}])
  | .containerDefinitions[0].environment=(
      [((.containerDefinitions[0].environment)//[])[]|select(.name!="CONNECT_SEARCH_ALLOWED_HD" and .name!="SLACK_OAUTH_REDIRECT_URI"
        and .name!="CONNECT_APP_HTML_S3_URI" and .name!="USE_QUERY_PLANNER" and .name!="USE_COHERE_RERANK")]
      + [{"name":"CONNECT_SEARCH_ALLOWED_HD","value":$hd},{"name":"SLACK_OAUTH_REDIRECT_URI","value":$rd},
         {"name":"CONNECT_APP_HTML_S3_URI","value":$apphtml},
         {"name":"USE_QUERY_PLANNER","value":"false"},{"name":"USE_COHERE_RERANK","value":"false"}])
  | .containerDefinitions[0].secrets=(
      [((.containerDefinitions[0].secrets)//[])[]|select(.name!="CONNECT_SLACK_CLIENT_ID" and .name!="CONNECT_SLACK_CLIENT_SECRET" and .name!="SLACK_OAUTH_STATE_SECRET")]
      + [{"name":"CONNECT_SLACK_CLIENT_ID","valueFrom":$cid},{"name":"CONNECT_SLACK_CLIENT_SECRET","valueFrom":$csec},{"name":"SLACK_OAUTH_STATE_SECRET","valueFrom":$cst}])
  | del(.taskDefinitionArn,.revision,.status,.requiresAttributes,.compatibilities,.registeredAt,.registeredBy,.deregisteredAt)
' "$CURRENT_TD" >"$NEW_TD"
NEW_ARN=$(
  aws ecs register-task-definition \
    --region "$R" \
    --cli-input-json "file://$NEW_TD" \
    --query 'taskDefinition.taskDefinitionArn' \
    --output text
)
aws ecs update-service \
  --region "$R" \
  --cluster "$CLUSTER" \
  --service "$SVC" \
  --task-definition "$NEW_ARN" \
  >/dev/null
aws ecs wait services-stable --region "$R" --cluster "$CLUSTER" --services "$SVC"

printf 'DEPLOYED_IMAGE_URI=%s\n' "$IMAGE_URI"
printf 'TASK_DEFINITION=%s\nROLLBACK_TASK_DEFINITION=%s\n' "$NEW_ARN" "$CURRENT_ARN"
printf 'ingest promotion: %s\n' \
  "infra/deploy/register_ingest_td.sh --image-uri $IMAGE_URI --expected-commit $EXPECTED_COMMIT --expected-tree $EXPECTED_TREE --expected-branch $EXPECTED_BRANCH --release-gate $RELEASE_GATE --release-gate-sha256 $RELEASE_GATE_SHA256"
