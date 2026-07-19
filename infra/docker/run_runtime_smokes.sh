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
  expected_memory_mib=$5

  readonly=$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container_id")
  memory_bytes=$(docker inspect --format '{{.HostConfig.Memory}}' "$container_id")
  runtime_user=$(docker inspect --format '{{.Config.User}}' "$container_id")
  expected_memory_bytes=$((expected_memory_mib * 1024 * 1024))
  test "$readonly" = true
  test "$memory_bytes" = "$expected_memory_bytes"
  test "$runtime_user" = "10001:10001"
  writable_mounts=$(
    docker inspect \
      --format '{{range .Mounts}}{{if .RW}}{{println .Destination}}{{end}}{{end}}' \
      "$container_id" |
      sed '/^[[:space:]]*$/d' |
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
    test "$cap_drop" = '["ALL"]'
    case "$security_opt" in
      *no-new-privileges:true*) ;;
      *) return 1 ;;
    esac
  else
    test "$cap_drop" = '["ALL"]'
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
    'runtime_security service=%s readonly=true memory=%sMiB user=10001:10001 network=%s writable=/tmp cap_drop=%s\n' \
    "$service" "$expected_memory_mib" "$network" "$cap_drop"
}

run_one_shot() {
  profile=$1
  service=$2
  kind=$3
  expected_memory_mib=$4
  expected_status=$5
  expected_path=$6
  expected_args=$7
  container_id=$(compose --profile "$profile" run --detach --no-deps "$service")
  assert_container_security "$container_id" "$service" "$kind" none "$expected_memory_mib"
  actual_process=$(
    docker inspect --format '{{.Path}} {{json .Args}}' "$container_id"
  )
  test "$actual_process" = "$expected_path $expected_args"
  status=$(docker wait "$container_id")
  docker logs "$container_id"
  docker rm --force "$container_id" >/dev/null
  test "$status" = "$expected_status"
  printf \
    'runtime_composition service=%s path=%s args=%s exit=%s\n' \
    "$service" "$expected_path" "$expected_args" "$status"
}

run_health_service() {
  profile=$1
  service=$2
  expected_memory_mib=$3
  expected_path=$4
  expected_args=$5

  compose --profile "$profile" up --detach "$service"
  container_id=$(compose --profile "$profile" ps --quiet "$service")
  test -n "$container_id"
  assert_container_security "$container_id" "$service" core internal "$expected_memory_mib"
  actual_process=$(
    docker inspect --format '{{.Path}} {{json .Args}}' "$container_id"
  )
  test "$actual_process" = "$expected_path $expected_args"

  attempt=0
  health=starting
  while test "$attempt" -lt 90; do
    health=$(docker inspect --format '{{.State.Health.Status}}' "$container_id")
    if test "$health" = "healthy"; then
      break
    fi
    if test "$health" = "unhealthy"; then
      compose logs "$service"
      exit 1
    fi
    attempt=$((attempt + 1))
    sleep 2
  done
  if test "$health" != "healthy"; then
    compose logs "$service"
    exit 1
  fi
  compose stop "$service" >/dev/null
  compose rm --force "$service" >/dev/null
  printf \
    'runtime_composition service=%s path=%s args=%s health=healthy\n' \
    "$service" "$expected_path" "$expected_args"
}

trap cleanup EXIT INT TERM
cleanup
assert_arm64 "$TEAMAGENT_CORE_IMAGE"
assert_arm64 "$TEAMAGENT_MEDIA_IMAGE"

run_health_service \
  core-health core-health 4096 \
  /app/.venv/bin/python \
  '["/app/scripts/run_mcp_vertex_entrypoint.py"]'
run_health_service \
  connect-health connect-health 1024 \
  /app/.venv/bin/python \
  '["-m","teamagent.connect_web"]'

run_one_shot \
  core core-smoke core 4096 0 \
  /app/.venv/bin/python \
  '["/smoke/smoke_core.py"]'
run_one_shot \
  core-composition canary-composition core 512 1 \
  /app/.venv/bin/python \
  '["/app/scripts/run_canary_health.py"]'
run_one_shot \
  core-composition ingest-composition core 4096 2 \
  /app/.venv/bin/python \
  '["/app/scripts/run_ingest_fargate.py"]'
run_one_shot \
  core-composition morning-digest-composition core 2048 0 \
  /app/.venv/bin/python \
  '["/app/scripts/run_morning_digest_fargate.py"]'
run_one_shot \
  core-composition x-buzz-composition core 1024 1 \
  /app/.venv/bin/python \
  '["-m","teamagent.workers.x_buzz_job"]'
run_one_shot \
  media-composition media-composition media 4096 2 \
  /app/.venv/bin/python \
  '["-m","teamagent.media.worker"]'
run_one_shot \
  media media-smoke media 4096 0 \
  /app/.venv/bin/python \
  '["/smoke/smoke_media.py"]'
