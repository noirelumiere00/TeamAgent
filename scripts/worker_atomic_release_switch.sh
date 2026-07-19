#!/usr/bin/env bash
# Two-phase atomic worker release switch with durable local rollback metadata.

set -euo pipefail
umask 077

INSTALL_ROOT="${TEAMAGENT_INSTALL_ROOT:-/opt/teamagent}"
CURRENT_LINK="$INSTALL_ROOT/current"
RESTART_ENV="$INSTALL_ROOT/restart.env"
TRANSACTION_ROOT="$INSTALL_ROOT/release-transactions"
ACTIVE_LOCK="$TRANSACTION_ROOT/.active"
SYSTEMD_ROOT="${TEAMAGENT_SYSTEMD_ROOT:-/etc/systemd/system}"
READY_ATTEMPTS="${TEAMAGENT_READY_ATTEMPTS:-60}"
READY_INTERVAL_S="${TEAMAGENT_READY_INTERVAL_S:-2}"
SERVICES=(teamagent-bot teamagent-connect)

fail() {
  echo "worker release transaction failed" >&2
  exit 1
}

valid_sha256() {
  [[ "$1" =~ ^[a-f0-9]{64}$ ]]
}

valid_transaction_id() {
  [[ "$1" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
}

active_services_ready() {
  systemctl is-active --quiet teamagent-bot \
    && systemctl is-active --quiet teamagent-connect \
    && curl -fsS http://127.0.0.1:8788/healthz >/dev/null
}

service_main_pids() {
  CONNECT_MAIN_PID="$(systemctl show --property MainPID --value teamagent-connect)"
  BOT_MAIN_PID="$(systemctl show --property MainPID --value teamagent-bot)"
  [[ "$CONNECT_MAIN_PID" =~ ^[1-9][0-9]*$ \
    && "$BOT_MAIN_PID" =~ ^[1-9][0-9]*$ \
    && "$CONNECT_MAIN_PID" != "$BOT_MAIN_PID" ]]
}

connect_owns_port() {
  ss -H -ltnp "sport = :8788" | grep -F "pid=$CONNECT_MAIN_PID," >/dev/null
}

wait_ready() {
  local attempt
  for attempt in $(seq 1 "$READY_ATTEMPTS"); do
    if active_services_ready && service_main_pids && connect_owns_port; then
      return 0
    fi
    sleep "$READY_INTERVAL_S"
  done
  return 1
}

install_release_units() {
  local release="$1" service temporary
  for service in "${SERVICES[@]}"; do
    [[ -f "$release/$service.service" ]] || return 1
    temporary="$(mktemp "$SYSTEMD_ROOT/.$service.service.XXXXXXXX")"
    install -m 0644 "$release/$service.service" "$temporary"
    mv -f "$temporary" "$SYSTEMD_ROOT/$service.service"
  done
  systemctl daemon-reload
  systemctl enable "${SERVICES[@]}"
}

snapshot_units() {
  local transaction_dir="$1" service path
  mkdir "$transaction_dir/previous-units"
  for service in "${SERVICES[@]}"; do
    path="$SYSTEMD_ROOT/$service.service"
    if [[ -e "$path" || -L "$path" ]]; then
      cp -a "$path" "$transaction_dir/previous-units/$service.service"
      printf '%s\n' 1 >"$transaction_dir/had-$service-unit"
    else
      printf '%s\n' 0 >"$transaction_dir/had-$service-unit"
    fi
  done
}

restore_units() {
  local transaction_dir="$1" service path
  for service in "${SERVICES[@]}"; do
    path="$SYSTEMD_ROOT/$service.service"
    if [[ "$(<"$transaction_dir/had-$service-unit")" == "1" ]]; then
      rm -f "$path"
      cp -a "$transaction_dir/previous-units/$service.service" "$path"
    else
      rm -f "$path"
    fi
  done
  systemctl daemon-reload
}

restore_transaction() {
  local transaction_dir="$1"
  local previous_release had_restart_env
  previous_release="$(<"$transaction_dir/previous-release")"
  had_restart_env="$(<"$transaction_dir/had-restart-env")"
  [[ -n "$previous_release" && -d "$previous_release/app" ]] || return 1

  ln -s "$previous_release" "$INSTALL_ROOT/.current-rollback-$$"
  mv -Tf "$INSTALL_ROOT/.current-rollback-$$" "$CURRENT_LINK"
  if [[ "$had_restart_env" == "1" ]]; then
    cp -a "$transaction_dir/previous-restart.env" "$RESTART_ENV"
  else
    rm -f "$RESTART_ENV"
  fi
  restore_units "$transaction_dir"
  systemctl restart teamagent-bot teamagent-connect
  wait_ready
}

release_transaction_lock() {
  local transaction_id="$1"
  local transaction_dir="$2"
  [[ -d "$transaction_dir" ]] || return 1
  [[ -f "$ACTIVE_LOCK/transaction-id" ]] || return 1
  [[ "$(<"$ACTIVE_LOCK/transaction-id")" == "$transaction_id" ]] || return 1
  rm -rf -- "$ACTIVE_LOCK"
}

rollback_after_failure() {
  local original_status="$1"
  local transaction_id="$2"
  local transaction_dir="$3"
  trap - EXIT
  if restore_transaction "$transaction_dir"; then
    printf '%s\n' rolled_back >"$transaction_dir/status"
    release_transaction_lock "$transaction_id" "$transaction_dir" || true
    exit "$original_status"
  fi
  printf '%s\n' rollback_failed >"$transaction_dir/status" 2>/dev/null || true
  exit 70
}

case "${1:-}" in
  switch)
    [[ "$#" == "5" ]] || fail
    transaction_id="$2"
    final_release="$3"
    release_digest="$4"
    restart_nonce="$5"
    valid_transaction_id "$transaction_id" || fail
    valid_sha256 "$release_digest" || fail
    valid_sha256 "$restart_nonce" || fail
    [[ "$READY_ATTEMPTS" =~ ^[1-9][0-9]*$ && "$READY_INTERVAL_S" =~ ^[0-9]+$ ]] || fail
    [[ -d "$final_release/app" ]] || fail
    [[ "$(<"$final_release/.release-sha256")" == "$release_digest" ]] || fail
    [[ -f "$final_release/teamagent-bot.service" ]] || fail
    [[ -f "$final_release/teamagent-connect.service" ]] || fail
    previous_release="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
    [[ -n "$previous_release" && -d "$previous_release/app" ]] || fail

    mkdir -p "$TRANSACTION_ROOT"
    mkdir "$ACTIVE_LOCK" || fail
    printf '%s\n' "$transaction_id" >"$ACTIVE_LOCK/transaction-id"
    transaction_dir="$TRANSACTION_ROOT/$transaction_id"
    if ! mkdir "$transaction_dir"; then
      rm -rf -- "$ACTIVE_LOCK"
      fail
    fi
    trap 'rm -rf -- "$transaction_dir" "$ACTIVE_LOCK"' EXIT
    printf '%s\n' "$previous_release" >"$transaction_dir/previous-release"
    printf '%s\n' "$final_release" >"$transaction_dir/new-release"
    printf '%s\n' "$release_digest" >"$transaction_dir/release-digest"
    if [[ -f "$RESTART_ENV" ]]; then
      cp -a "$RESTART_ENV" "$transaction_dir/previous-restart.env"
      printf '%s\n' 1 >"$transaction_dir/had-restart-env"
    else
      printf '%s\n' 0 >"$transaction_dir/had-restart-env"
    fi
    snapshot_units "$transaction_dir"
    printf '%s\n' prepared >"$transaction_dir/status"
    trap 'rollback_after_failure "$?" "$transaction_id" "$transaction_dir"' EXIT

    restart_env_new="$(mktemp "$INSTALL_ROOT/.restart.env.XXXXXXXX")"
    printf 'TEAMAGENT_HMAC_RESTART_NONCE=%s\n' "$restart_nonce" >"$restart_env_new"
    chmod 0600 "$restart_env_new"
    mv -f "$restart_env_new" "$RESTART_ENV"
    ln -s "$final_release" "$INSTALL_ROOT/.current-new-$$"
    mv -Tf "$INSTALL_ROOT/.current-new-$$" "$CURRENT_LINK"
    install_release_units "$final_release"
    printf '%s\n' switched >"$transaction_dir/status"

    systemctl stop teamagent-connect 2>/dev/null || true
    ( command -v fuser >/dev/null && fuser -k 8788/tcp ) 2>/dev/null \
      || pkill -f teamagent.connect_web 2>/dev/null || true
    systemctl restart teamagent-bot teamagent-connect
    wait_ready
    printf '%s\n' ready >"$transaction_dir/status"
    trap - EXIT
    ;;
  commit)
    [[ "$#" == "2" ]] || fail
    transaction_id="$2"
    valid_transaction_id "$transaction_id" || fail
    transaction_dir="$TRANSACTION_ROOT/$transaction_id"
    [[ "$(<"$transaction_dir/status")" == "ready" ]] || fail
    final_release="$(<"$transaction_dir/new-release")"
    [[ "$(readlink -f "$CURRENT_LINK")" == "$final_release" ]] || fail
    printf '%s\n' committed >"$transaction_dir/status"
    release_transaction_lock "$transaction_id" "$transaction_dir"
    ;;
  rollback)
    [[ "$#" == "2" ]] || fail
    transaction_id="$2"
    valid_transaction_id "$transaction_id" || fail
    transaction_dir="$TRANSACTION_ROOT/$transaction_id"
    [[ -d "$transaction_dir" ]] || fail
    restore_transaction "$transaction_dir" || exit 70
    printf '%s\n' rolled_back >"$transaction_dir/status"
    release_transaction_lock "$transaction_id" "$transaction_dir"
    ;;
  *)
    fail
    ;;
esac
