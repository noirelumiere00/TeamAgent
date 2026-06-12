#!/bin/sh
# §VSEO有効化: ECS secret で注入された Vertex SA JSON をファイル化して ADC に渡し、MCPサーバを起動。
# load_secrets.sh(EC2本番) と同じ思想のコンテナ版（umask 077・値はログに一切出さない）。
# VERTEX_SA_JSON 未注入なら何もせず通常起動（API key 経路や無効化時と互換）。
set -eu
if [ -n "${VERTEX_SA_JSON:-}" ]; then
  umask 077
  printf '%s' "$VERTEX_SA_JSON" > /tmp/vertex_sa.json
  export GOOGLE_APPLICATION_CREDENTIALS=/tmp/vertex_sa.json
  unset VERTEX_SA_JSON
fi
exec python scripts/run_mcp_http_server.py
