#!/usr/bin/env bash
# 各入口から source する。ログ・一時ファイルは repo 外に置く。
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$KIT_DIR/../../../.." && pwd)"
export RELEASE_WORK_DIR="${RELEASE_WORK_DIR:-$HOME/.teamagent-release}"
export RELEASE_PAD="${RELEASE_PAD:-$RELEASE_WORK_DIR/teamagent-build}"
export AWS_PROFILE="${AWS_PROFILE:-aiiadev}"
export RELEASE_PROFILE="${RELEASE_PROFILE:-aiiadev-mfa}"
AWS_CA_BUNDLE="${AWS_CA_BUNDLE:-$(python3 -c 'import certifi;print(certifi.where())')}" || return 1
export AWS_CA_BUNDLE SSL_CERT_FILE="$AWS_CA_BUNDLE"
export AWS_REGION=ap-northeast-1 AWS_PAGER=""
export AWS_RETRY_MODE=adaptive AWS_MAX_ATTEMPTS=10
umask 077
mkdir -p "$RELEASE_WORK_DIR/tmp" || return 1
export TMPDIR="$RELEASE_WORK_DIR/tmp/"
