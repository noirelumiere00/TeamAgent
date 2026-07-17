#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COMPOSE_FILE="$SCRIPT_DIR/compose.runtime-smoke.yml"

: "${TEAMAGENT_CORE_IMAGE:?set TEAMAGENT_CORE_IMAGE to the exact local core tag}"
: "${TEAMAGENT_MEDIA_IMAGE:?set TEAMAGENT_MEDIA_IMAGE to the exact local media tag}"

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

cleanup() {
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}

assert_arm64() {
  image=$1
  architecture=$(docker image inspect --format '{{.Architecture}}' "$image")
  volumes=$(docker image inspect --format '{{json .Config.Volumes}}' "$image")
  test "$architecture" = "arm64"
  test "$volumes" = '{"/tmp":{}}'
}

assert_container_security() {
  container_id=$1
  service=$2
  kind=$3
  expected_network=$4

  runtime_security=$(
    docker inspect \
      --format '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Memory}} {{.Config.User}}' \
      "$container_id"
  )
  test "$runtime_security" = "true 4294967296 10001:10001"
  writable_mounts=$(
    docker inspect \
      --format '{{range .Mounts}}{{if .RW}}{{println .Destination}}{{end}}{{end}}' \
      "$container_id" |
      LC_ALL=C sort
  )
  test "$writable_mounts" = "/tmp"
  tmp_mount=$(
    docker inspect \
      --format '{{range .Mounts}}{{if eq .Destination "/tmp"}}{{.Type}} {{.RW}}{{end}}{{end}}' \
      "$container_id"
  )
  test "$tmp_mount" = "volume true"
  network=$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$container_id")
  if test "$expected_network" = none; then
    test "$network" = none
  fi
  cap_add=$(docker inspect --format '{{json .HostConfig.CapAdd}}' "$container_id")
  cap_drop=$(docker inspect --format '{{json .HostConfig.CapDrop}}' "$container_id")
  security_opt=$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "$container_id")
  test "$cap_add" = null
  if test "$kind" = core; then
    test "$cap_drop" = '["CAP_ALL"]'
    case "$security_opt" in
      *no-new-privileges=true*) ;;
      *) return 1 ;;
    esac
  else
    test "$cap_drop" = \
      '["CAP_AUDIT_WRITE","CAP_CHOWN","CAP_DAC_OVERRIDE","CAP_FOWNER","CAP_FSETID","CAP_KILL","CAP_MKNOD","CAP_NET_BIND_SERVICE","CAP_NET_RAW","CAP_SETFCAP","CAP_SETGID","CAP_SETPCAP","CAP_SETUID"]'
    case "$security_opt" in
      *seccomp=*unconfined*) return 1 ;;
      *seccomp=*) ;;
      *) return 1 ;;
    esac
    case "$security_opt" in
      *no-new-privileges*) return 1 ;;
    esac
  fi
  printf \
    'runtime_security service=%s readonly=true memory=4096MiB user=10001:10001 network=%s writable=/tmp cap_drop=%s\n' \
    "$service" "$network" "$cap_drop"
}

run_one_shot() {
  profile=$1
  service=$2
  kind=$3
  container_id=$(compose --profile "$profile" run --detach --no-deps "$service")
  assert_container_security "$container_id" "$service" "$kind" none
  status=$(docker wait "$container_id")
  docker logs "$container_id"
  docker rm --force "$container_id" >/dev/null
  test "$status" = 0
}

trap cleanup EXIT INT TERM
cleanup
assert_arm64 "$TEAMAGENT_CORE_IMAGE"
assert_arm64 "$TEAMAGENT_MEDIA_IMAGE"

compose --profile core-health up --detach core-health
core_health_id=$(compose --profile core-health ps --quiet core-health)
test -n "$core_health_id"
assert_container_security "$core_health_id" core-health core internal

attempt=0
health=starting
while test "$attempt" -lt 90; do
  health=$(docker inspect --format '{{.State.Health.Status}}' "$core_health_id")
  if test "$health" = "healthy"; then
    break
  fi
  if test "$health" = "unhealthy"; then
    compose logs core-health
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 2
done
if test "$health" != "healthy"; then
  compose logs core-health
  exit 1
fi
compose stop core-health >/dev/null
compose rm --force core-health >/dev/null

run_one_shot core core-smoke core
run_one_shot media media-smoke media
