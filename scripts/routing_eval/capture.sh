#!/usr/bin/env bash
# 本物の OpenClaw 2026.7.1（本番と同じ公式イメージ digest と同版の slack / amazon-bedrock plugin）に、
# Slack DM 1 通と同じ形の入力で 1 ターン回させ、Bedrock へ送ろうとした要求を雛形として取り出す。
# 推論はしない（Bedrock は代役）。Slack にも接続しない。
#
# usage: scripts/routing_eval/capture.sh <commit の worktree> <tools.json> <出力ディレクトリ> [場面名]
#   例: scripts/routing_eval/capture.sh /tmp/src_b66e8d6e runs/tools_b66e8d6e.json runs/capture_b66e8d6e
#       （場面名を省くと「新規 DM の 1 通目」。history_scenarios.json の場面名を渡すと、その前置きの
#        ターンを同じ DM で先に回してから評価の発話を送る＝「履歴付き DM」）
#   ROUTING_EVAL_MODE=local にすると Slack を通さない `openclaw agent --local` で取り出す
#   （Slack 経路との差分確認用。評価には使わない）。
#
# 必要なもの: docker（arm64）・uv・curl。AWS の資格情報は使わない。
set -euo pipefail

SRC_TREE=$(cd "$1" && pwd)
TOOLS_JSON=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
OUT_DIR=$3
SCENARIO=${4:-}
MODE=${ROUTING_EVAL_MODE:-slack}
PORT=${ROUTING_EVAL_STUB_PORT:-18790}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

# infra/docker/Dockerfile.openclaw に合わせて固定する（ずれたら下の検査で止まる）。
OPENCLAW_DIGEST="sha256:38b611f494cb32e15aaf456d54c6b6be55db9098c90632aed0bfad4a70009707"
OPENCLAW_IMAGE="ghcr.io/openclaw/openclaw:2026.7.1@$OPENCLAW_DIGEST"
PLUGIN_URLS=(
  "slack https://registry.npmjs.org/@openclaw/slack/-/slack-2026.7.1.tgz 79f3179b6163c1878bef8820673419a0547027c536666e482b02bd8ae5a7980c"
  "amazon-bedrock https://registry.npmjs.org/@openclaw/amazon-bedrock-provider/-/amazon-bedrock-provider-2026.7.1.tgz 1580dc988ec8ef3e430a166274062ddde547a75f9e338d00905021ebb283f31e"
)
SENTINEL="__ROUTING_EVAL_UTTERANCE__"

DOCKERFILE="$REPO/infra/docker/Dockerfile.openclaw"
grep -q "$OPENCLAW_DIGEST" "$DOCKERFILE" \
  || { echo "Dockerfile.openclaw の OpenClaw digest が変わった。capture.sh を合わせること" >&2; exit 1; }

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
OUT_DIR=$(cd "$OUT_DIR" && pwd)
WORK=$(mktemp -d)
trap 'kill "${STUB_PID:-}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

mkdir -p "$WORK/plugins"
for entry in "${PLUGIN_URLS[@]}"; do
  read -r name url sha <<<"$entry"
  grep -q "$sha" "$DOCKERFILE" \
    || { echo "Dockerfile.openclaw の $name plugin の hash が変わった。capture.sh を合わせること" >&2; exit 1; }
  curl -fsSL -o "$WORK/$name.tgz" "$url"
  mkdir -p "$WORK/plugins/$name"
  tar -xzf "$WORK/$name.tgz" --strip-components=1 -C "$WORK/plugins/$name"
  got=$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$WORK/plugins/$name/npm-shrinkwrap.json")
  [ "$got" = "$sha" ] || { echo "$name plugin の shrinkwrap hash 不一致: $got" >&2; exit 1; }
done

SCRIPT_ARGS=()
if [ -n "$SCENARIO" ]; then
  [ "$MODE" = slack ] || { echo "履歴付きの場面は Slack 経路でだけ取り出せる" >&2; exit 1; }
  SCRIPT_ARGS=(--script "$HERE/history_scenarios.json" --scenario "$SCENARIO")
fi
python3 - "$HERE/history_scenarios.json" "$SCENARIO" "$SENTINEL" >"$WORK/turns.json" <<'PY'
import json, sys
path, scenario, sentinel = sys.argv[1:4]
turns = json.load(open(path, encoding="utf-8"))["scenarios"][scenario]["turns"] if scenario else []
print(json.dumps([t["user"] for t in turns] + [sentinel], ensure_ascii=False))
PY

(cd "$REPO" && uv run --extra mcp python "$HERE/capture_stubs.py" --tools "$TOOLS_JSON" --out-dir "$OUT_DIR" \
  --port "$PORT" ${SCRIPT_ARGS[@]+"${SCRIPT_ARGS[@]}"}) \
  >"$OUT_DIR/stub.log" 2>&1 &
STUB_PID=$!
for _ in $(seq 1 60); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break
  sleep 1
done

docker run --rm \
  --add-host host.docker.internal:host-gateway --hostname routing-eval \
  -v "$SRC_TREE/infra/openclaw:/eval/oc:ro" \
  -v "$HERE:/eval/scripts:ro" \
  -v "$WORK/plugins:/eval/plugins:ro" \
  -v "$WORK/turns.json:/eval/turns.json:ro" \
  -e SENTINEL="$SENTINEL" -e STUB="http://host.docker.internal:$PORT" -e MODE="$MODE" \
  --entrypoint sh "$OPENCLAW_IMAGE" -ec '
    R=/tmp/teamagent-openclaw
    mkdir -p $R/config $R/state/workspace $R/home /tmp/driver/node_modules
    cp -r /eval/plugins /tmp/plugins
    for p in slack amazon-bedrock; do ln -s /app /tmp/plugins/$p/node_modules/openclaw; done
    ln -s /app /tmp/driver/node_modules/openclaw
    cp /eval/scripts/slack_dm_driver.mjs /tmp/driver/
    node /eval/scripts/make_capture_config.mjs /eval/oc/openclaw.config.json5 $R/config/openclaw.json "$STUB" /tmp/plugins
    for f in SOUL.md HEARTBEAT.md IDENTITY.md; do cp /eval/oc/$f $R/state/workspace/$f; done
    export HOME=$R/home OPENCLAW_STATE_DIR=$R/state OPENCLAW_CONFIG_PATH=$R/config/openclaw.json \
      OPENCLAW_WORKSPACE_DIR=$R/state/workspace AWS_BEDROCK_SKIP_AUTH=1 AWS_REGION=ap-northeast-1 \
      AWS_BEDROCK_FORCE_HTTP1=1
    for p in slack amazon-bedrock; do node /app/openclaw.mjs plugins install --link /tmp/plugins/$p >/dev/null; done
    node /app/openclaw.mjs plugins registry --refresh >/dev/null
    if [ "$MODE" = local ]; then
      node /app/openclaw.mjs agent --local --agent teamagent --session-id routing-eval-capture \
        --message "$SENTINEL" --json --timeout 120 || true
    else
      cd /tmp/driver && node slack_dm_driver.mjs /eval/turns.json || true
    fi
  ' >"$OUT_DIR/openclaw_agent.log" 2>&1 || true

kill "$STUB_PID" 2>/dev/null || true
wait "$STUB_PID" 2>/dev/null || true

first=$(ls "$OUT_DIR"/bedrock_request_*.json 2>/dev/null | grep -v _prelude | head -1 || true)
[ -n "$first" ] || { echo "評価の発話の要求が取れなかった。$OUT_DIR/openclaw_agent.log を見ること" >&2; exit 1; }
(cd "$REPO" && uv run python "$HERE/make_template.py" --request "$first" --tools "$TOOLS_JSON" \
  --sentinel "$SENTINEL" --mode "$MODE" --scenario "${SCENARIO:-new_dm}" --out "$OUT_DIR/template.json")
