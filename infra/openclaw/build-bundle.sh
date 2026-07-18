#!/usr/bin/env bash
# Canonical OpenClaw core/media bundle interface.
#
# The hardened core runtime can be validated locally with build-image.sh.
# Production still requires a separately implemented media subject and an
# exact two-subject receipt. Keep this interface fail-closed until that work
# lands and the reviewed bundle contract is explicitly activated.
set -euo pipefail

die() {
  echo "FATAL: $*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
usage: build-bundle.sh \
  --bundle-contract PATH \
  --core-image REPOSITORY:TAG \
  --media-image REPOSITORY:TAG \
  --push \
  --manifest PATH
EOF
}

bundle_contract=""
core_image=""
media_image=""
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
      media_image="$2"
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
  [ -n "$media_image" ] &&
  [ -n "$manifest" ] &&
  [ "$push" = true ] || {
  usage
  exit 2
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "$script_dir/../.." && pwd -P)"
canonical_contract="$repo_root/infra/codebuild/openclaw_bundle_contract.json"
provenance="$repo_root/infra/codebuild/openclaw_provenance.py"

[ -f "$bundle_contract" ] && [ ! -L "$bundle_contract" ] ||
  die "bundle contract is missing or a symlink"
[ "$(cd -- "$(dirname -- "$bundle_contract")" && pwd -P)/$(basename -- "$bundle_contract")" = \
  "$canonical_contract" ] || die "only the canonical OpenClaw bundle contract is accepted"
[ -f "$provenance" ] && [ ! -L "$provenance" ] ||
  die "OpenClaw provenance verifier is missing or a symlink"

# This is intentionally before Docker, registry authentication, filesystem
# output, or any external mutation.
python3 "$provenance" assert-release-ready --contract "$canonical_contract" ||
  die "OpenClaw core/media release contract is not active"

die "OpenClaw media subject and exact two-subject receipt emitter are not implemented"
