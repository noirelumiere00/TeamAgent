#!/usr/bin/env bash
# Two-phase, crash-reconcilable worker release switch with exact local rollback metadata.

set -euo pipefail
umask 077

INSTALL_ROOT="${TEAMAGENT_INSTALL_ROOT:-/opt/teamagent}"
CURRENT_LINK="$INSTALL_ROOT/current"
LEGACY_RESTART_ENV="$INSTALL_ROOT/restart.env"
PROMOTION_ROOT="$INSTALL_ROOT/promotion-attestation"
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

write_status() {
  local transaction_dir="$1" status="$2" temporary
  temporary="$(mktemp "$transaction_dir/.status.XXXXXXXX")"
  printf '%s\n' "$status" >"$temporary"
  mv -f "$temporary" "$transaction_dir/status"
}

read_status() {
  local transaction_dir="$1"
  [[ -f "$transaction_dir/status" ]] || return 1
  printf '%s\n' "$(<"$transaction_dir/status")"
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

snapshot_promotion_state() {
  local transaction_dir="$1"
  if [[ -d "$PROMOTION_ROOT" && ! -L "$PROMOTION_ROOT" ]]; then
    cp -a "$PROMOTION_ROOT" "$transaction_dir/previous-promotion-attestation"
    printf '%s\n' 1 >"$transaction_dir/had-promotion-state"
  else
    [[ ! -e "$PROMOTION_ROOT" ]] || return 1
    printf '%s\n' 0 >"$transaction_dir/had-promotion-state"
  fi
  if [[ -f "$LEGACY_RESTART_ENV" && ! -L "$LEGACY_RESTART_ENV" ]]; then
    cp -a "$LEGACY_RESTART_ENV" "$transaction_dir/previous-restart.env"
    printf '%s\n' 1 >"$transaction_dir/had-restart-env"
  else
    [[ ! -e "$LEGACY_RESTART_ENV" ]] || return 1
    printf '%s\n' 0 >"$transaction_dir/had-restart-env"
  fi
}

restore_promotion_state() {
  local transaction_dir="$1"
  rm -rf -- "$PROMOTION_ROOT"
  if [[ "$(<"$transaction_dir/had-promotion-state")" == "1" ]]; then
    cp -a "$transaction_dir/previous-promotion-attestation" "$PROMOTION_ROOT"
  fi
  if [[ "$(<"$transaction_dir/had-restart-env")" == "1" ]]; then
    cp -a "$transaction_dir/previous-restart.env" "$LEGACY_RESTART_ENV"
  else
    rm -f "$LEGACY_RESTART_ENV"
  fi
}

create_promotion_markers() {
  local transaction_id="$1" restart_nonce="$2" service temporary
  rm -rf -- "$PROMOTION_ROOT"
  mkdir -m 0700 "$PROMOTION_ROOT"
  for service in bot connect; do
    temporary="$(mktemp "$PROMOTION_ROOT/.$service.pending.XXXXXXXX")"
    {
      printf 'TEAMAGENT_HMAC_RESTART_NONCE=%s\n' "$restart_nonce"
      printf 'TEAMAGENT_RELEASE_TRANSACTION_ID=%s\n' "$transaction_id"
      printf 'TEAMAGENT_PROMOTION_SERVICE=%s\n' "$service"
    } >"$temporary"
    chmod 0600 "$temporary"
    mv -f "$temporary" "$PROMOTION_ROOT/$service.pending"
  done
}

restore_transaction() {
  local transaction_dir="$1" previous_release
  previous_release="$(<"$transaction_dir/previous-release")"
  [[ -n "$previous_release" && -d "$previous_release/app" ]] || return 1
  ln -s "$previous_release" "$INSTALL_ROOT/.current-rollback-$$"
  mv -Tf "$INSTALL_ROOT/.current-rollback-$$" "$CURRENT_LINK"
  restore_promotion_state "$transaction_dir"
  restore_units "$transaction_dir"
  systemctl restart teamagent-bot teamagent-connect
  wait_ready
}

release_transaction_lock() {
  local transaction_id="$1" transaction_dir="$2"
  [[ -d "$transaction_dir" && -f "$ACTIVE_LOCK/transaction-id" ]] || return 0
  [[ "$(<"$ACTIVE_LOCK/transaction-id")" == "$transaction_id" ]] || return 1
  rm -rf -- "$ACTIVE_LOCK"
}

rollback_idempotent() {
  local transaction_id="$1" transaction_dir="$TRANSACTION_ROOT/$1" status
  [[ -d "$transaction_dir" ]] || return 1
  status="$(read_status "$transaction_dir")" || return 1
  if [[ "$status" == "rolled_back" ]]; then
    # A retry proves the live symlink/units/services again; the status file alone is not authority.
    restore_transaction "$transaction_dir" || {
      write_status "$transaction_dir" rollback_failed
      return 70
    }
    release_transaction_lock "$transaction_id" "$transaction_dir"
    return 0
  fi
  [[ "$status" != "committed" ]] || return 1
  restore_transaction "$transaction_dir" || {
    write_status "$transaction_dir" rollback_failed
    return 70
  }
  write_status "$transaction_dir" rolled_back
  release_transaction_lock "$transaction_id" "$transaction_dir"
}

rollback_after_failure() {
  local original_status="$1" transaction_id="$2"
  trap - EXIT
  if rollback_idempotent "$transaction_id"; then
    exit "$original_status"
  fi
  exit 70
}

mkdir -p "$TRANSACTION_ROOT"
exec 9>"$TRANSACTION_ROOT/.command.lock"
flock -x 9

case "${1:-}" in
  switch)
    [[ "$#" == "6" ]] || fail
    transaction_id="$2"
    final_release="$3"
    release_tree_digest="$4"
    input_release_digest="$5"
    restart_nonce="$6"
    valid_transaction_id "$transaction_id" || fail
    valid_sha256 "$release_tree_digest" || fail
    valid_sha256 "$input_release_digest" || fail
    valid_sha256 "$restart_nonce" || fail
    [[ "$READY_ATTEMPTS" =~ ^[1-9][0-9]*$ && "$READY_INTERVAL_S" =~ ^[0-9]+$ ]] || fail
    [[ -d "$final_release/app" && "$(basename "$final_release")" == "$release_tree_digest" ]] || fail
    [[ "$(<"$final_release/.release-tree-sha256")" == "$release_tree_digest" ]] || fail
    [[ "$(<"$final_release/.release-input-sha256")" == "$input_release_digest" ]] || fail
    [[ -f "$final_release/teamagent-bot.service" ]] || fail
    [[ -f "$final_release/teamagent-connect.service" ]] || fail
    transaction_dir="$TRANSACTION_ROOT/$transaction_id"
    if [[ -d "$transaction_dir" ]]; then
      status="$(read_status "$transaction_dir")" || fail
      recorded_release="$(<"$transaction_dir/new-release")"
      recorded_tree="$(<"$transaction_dir/release-tree-digest")"
      [[ "$recorded_release" == "$final_release" && "$recorded_tree" == "$release_tree_digest" ]] \
        || fail
      if [[ "$status" == "ready" && "$(readlink -f "$CURRENT_LINK")" == "$final_release" ]]; then
        exit 0
      fi
      [[ "$status" != "committed" ]] || exit 0
      fail
    fi
    previous_release="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
    [[ -n "$previous_release" && -d "$previous_release/app" ]] || fail

    mkdir "$ACTIVE_LOCK" || fail
    active_id_temporary="$(mktemp "$ACTIVE_LOCK/.transaction-id.XXXXXXXX")"
    printf '%s\n' "$transaction_id" >"$active_id_temporary"
    mv -f "$active_id_temporary" "$ACTIVE_LOCK/transaction-id"
    transaction_staging="$(mktemp -d "$TRANSACTION_ROOT/.$transaction_id.prepare.XXXXXXXX")"
    trap 'rm -rf -- "$transaction_staging" "$ACTIVE_LOCK"' EXIT
    printf '%s\n' "$previous_release" >"$transaction_staging/previous-release"
    printf '%s\n' "$final_release" >"$transaction_staging/new-release"
    printf '%s\n' "$release_tree_digest" >"$transaction_staging/release-tree-digest"
    printf '%s\n' "$input_release_digest" >"$transaction_staging/release-input-digest"
    snapshot_promotion_state "$transaction_staging"
    snapshot_units "$transaction_staging"
    write_status "$transaction_staging" prepared
    if ! mv "$transaction_staging" "$transaction_dir"; then
      rm -rf -- "$ACTIVE_LOCK"
      fail
    fi
    transaction_staging=""
    trap 'rollback_after_failure "$?" "$transaction_id"' EXIT

    create_promotion_markers "$transaction_id" "$restart_nonce"
    rm -f "$LEGACY_RESTART_ENV"
    ln -s "$final_release" "$INSTALL_ROOT/.current-new-$$"
    mv -Tf "$INSTALL_ROOT/.current-new-$$" "$CURRENT_LINK"
    install_release_units "$final_release"
    write_status "$transaction_dir" switched

    systemctl stop teamagent-connect 2>/dev/null || true
    ( command -v fuser >/dev/null && fuser -k 8788/tcp ) 2>/dev/null \
      || pkill -f teamagent.connect_web 2>/dev/null || true
    systemctl restart teamagent-bot teamagent-connect
    wait_ready
    [[ -f "$PROMOTION_ROOT/bot.attested" && -f "$PROMOTION_ROOT/connect.attested" ]] || fail
    write_status "$transaction_dir" ready
    trap - EXIT
    ;;
  status)
    [[ "$#" == "2" ]] || fail
    transaction_id="$2"
    valid_transaction_id "$transaction_id" || fail
    transaction_dir="$TRANSACTION_ROOT/$transaction_id"
    status="$(read_status "$transaction_dir")" || fail
    current="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
    new_release="$(<"$transaction_dir/new-release")"
    previous_release="$(<"$transaction_dir/previous-release")"
    if [[ "$current" == "$new_release" ]]; then
      current_state=new
    elif [[ "$current" == "$previous_release" ]]; then
      current_state=previous
    else
      current_state=unknown
    fi
    printf '{"current":"%s","status":"%s","transaction_id":"%s"}\n' \
      "$current_state" "$status" "$transaction_id"
    ;;
  commit)
    [[ "$#" == "2" ]] || fail
    transaction_id="$2"
    valid_transaction_id "$transaction_id" || fail
    transaction_dir="$TRANSACTION_ROOT/$transaction_id"
    status="$(read_status "$transaction_dir")" || fail
    if [[ "$status" == "committed" ]]; then
      final_release="$(<"$transaction_dir/new-release")"
      [[ "$(readlink -f "$CURRENT_LINK")" == "$final_release" ]] || fail
      [[ ! -e "$PROMOTION_ROOT" ]] || fail
      wait_ready || fail
      release_transaction_lock "$transaction_id" "$transaction_dir"
      exit 0
    fi
    [[ "$status" == "ready" ]] || fail
    final_release="$(<"$transaction_dir/new-release")"
    [[ "$(readlink -f "$CURRENT_LINK")" == "$final_release" ]] || fail
    wait_ready || fail
    [[ -f "$PROMOTION_ROOT/bot.attested" && -f "$PROMOTION_ROOT/connect.attested" ]] || fail
    service_main_pids || fail
    "$final_release/app/scripts/worker_promotion_attest.sh" \
      bot "$BOT_MAIN_PID" commit || fail
    "$final_release/app/scripts/worker_promotion_attest.sh" \
      connect "$CONNECT_MAIN_PID" commit || fail
    rm -rf -- "$PROMOTION_ROOT"
    rm -f "$LEGACY_RESTART_ENV"
    write_status "$transaction_dir" committed
    release_transaction_lock "$transaction_id" "$transaction_dir"
    ;;
  rollback)
    [[ "$#" == "2" ]] || fail
    valid_transaction_id "$2" || fail
    rollback_idempotent "$2"
    ;;
  reconcile)
    [[ "$#" == "3" && "$3" == "rollback" ]] || fail
    valid_transaction_id "$2" || fail
    if [[ ! -d "$TRANSACTION_ROOT/$2" ]]; then
      # A cancelled/terminal SSM command that never created its transaction cannot have switched
      # current: the complete prepared transaction is atomically renamed before any mutation.
      if [[ -e "$ACTIVE_LOCK" || -L "$ACTIVE_LOCK" ]]; then
        [[ -d "$ACTIVE_LOCK" && ! -L "$ACTIVE_LOCK" ]] || fail
        active_id="$(cat "$ACTIVE_LOCK/transaction-id" 2>/dev/null || true)"
        [[ -z "$active_id" || "$active_id" == "$2" ]] || fail
        rm -rf -- "$ACTIVE_LOCK"
      fi
      rm -rf -- "$TRANSACTION_ROOT/.$2.prepare."*
      exit 0
    fi
    rollback_idempotent "$2"
    ;;
  *)
    fail
    ;;
esac
