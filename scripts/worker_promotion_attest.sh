#!/usr/bin/env bash
# Consume only an active release-transaction promotion marker. Ordinary restarts do not attest.

set -euo pipefail
umask 077

INSTALL_ROOT="${TEAMAGENT_INSTALL_ROOT:-/opt/teamagent}"
MARKER_ROOT="$INSTALL_ROOT/promotion-attestation"
SERVICE="${1:-}"
MAIN_PID="${2:-}"
PHASE="${3:-startup}"

fail() {
  echo "worker promotion attestation failed" >&2
  exit 1
}

[[ "$SERVICE" == "bot" || "$SERVICE" == "connect" ]] || fail
[[ "$MAIN_PID" =~ ^[1-9][0-9]*$ ]] || fail
[[ "$PHASE" == "startup" || "$PHASE" == "commit" ]] || fail
pending="$MARKER_ROOT/$SERVICE.pending"
attested="$MARKER_ROOT/$SERVICE.attested"
if [[ ! -e "$pending" && ! -L "$pending" && ! -e "$attested" && ! -L "$attested" ]]; then
  # This is an ordinary systemd start (boot, crash recovery, or a committed release restart).
  exit 0
fi
[[ -d "$MARKER_ROOT" && ! -L "$MARKER_ROOT" ]] || fail
chmod 0700 "$MARKER_ROOT"
exec 9>"$MARKER_ROOT/.$SERVICE.lock"
flock -x 9

if [[ ! -e "$pending" && ! -L "$pending" && ! -e "$attested" && ! -L "$attested" ]]; then
  # A rollback/commit consumed the marker while this process waited for the service lock.
  exit 0
fi
[[ ! -L "$pending" && ! -L "$attested" ]] || fail
if [[ -f "$pending" && -f "$attested" ]]; then
  fail
fi
marker="$pending"
[[ -f "$marker" ]] || marker="$attested"
[[ "$(stat -c '%u:%a' "$marker")" == "0:600" ]] || fail

set -a
# All files are root-owned release inputs. The one-shot marker contains only the restart nonce and
# transaction ID; runtime.env binds the measured immutable release tree and executable.
source "$INSTALL_ROOT/current/teamagent.env.base"
source "$INSTALL_ROOT/current/hmac.env"
source "$INSTALL_ROOT/current/runtime.env"
source "$marker"
source "$INSTALL_ROOT/current/app/scripts/load_secrets.sh" MAIL_ACTION,REPORT_LINK
set +a

[[ "${TEAMAGENT_PROMOTION_SERVICE:-}" == "$SERVICE" ]] || fail
[[ "${TEAMAGENT_HMAC_RESTART_NONCE:-}" =~ ^[a-f0-9]{64}$ ]] || fail
[[ "${TEAMAGENT_RELEASE_TRANSACTION_ID:-}" =~ \
  ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] || fail
export TEAMAGENT_HMAC_SERVICE="$SERVICE"
export TEAMAGENT_HMAC_SERVICE_HEALTH=1
export TEAMAGENT_HMAC_MAIN_PID="$MAIN_PID"
export TEAMAGENT_BOT_HEARTBEAT_PATH=/run/teamagent/bot-heartbeat.json
if [[ "$PHASE" == "commit" ]]; then
  export TEAMAGENT_HMAC_RESTART_REQUIRE_COMPLETE=1
fi

attested_ok=false
for _attempt in $(seq 1 60); do
  kill -0 "$MAIN_PID" || break
  if [[ "$SERVICE" == "bot" ]]; then
    [[ -f "$TEAMAGENT_BOT_HEARTBEAT_PATH" ]] || {
      sleep 1
      continue
    }
  else
    curl -fsS http://127.0.0.1:8788/healthz >/dev/null || {
      sleep 1
      continue
    }
    ss -H -ltnp "sport = :8788" | grep -F "pid=$MAIN_PID," >/dev/null || {
      sleep 1
      continue
    }
  fi
  if "$INSTALL_ROOT/current/app/.venv/bin/python" \
    "$INSTALL_ROOT/current/app/scripts/check_hmac_runtime_state.py" \
    --domains MAIL_ACTION,REPORT_LINK --worker-attestation; then
    attested_ok=true
    break
  fi
  sleep 1
done
[[ "$attested_ok" == "true" ]] || fail

if [[ "$marker" == "$pending" ]]; then
  mv -f "$pending" "$attested"
fi
