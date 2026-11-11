#!/usr/bin/env bash
# mcp TD 差し替え（image + env）＋ OC bounce — 09-08 再作成（/tmp 掃除で消失）
# 使い方: register_mcp_td_image_env.sh <sha256:...|keep> [KEY=VALUE ...] [-KEY ...]
#   image は "keep" で現状維持（env だけ変える）。-KEY はその env を削除。
# 08-03 の教訓: mcp を再起動したら OC も必ず再デプロイ（ツール一覧の整合）。
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/../common.sh" || exit 1
[ $# -ge 1 ] || { echo "usage: $0 <sha256:...|keep> [KEY=VALUE ...] [-KEY ...]"; exit 1; }
DIGEST="$1"; shift
[ "$DIGEST" = "keep" ] || [[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "FATAL: digest 形式が不正"; exit 1; }
OUT="$RELEASE_WORK_DIR/mcp-td-imgenv/$(date +%Y%m%d-%H%M%S)-$$"; mkdir -p "$OUT"; chmod 700 "$OUT"

echo "=== 実行中ジョブ確認（提案書/お土産/tiktok の running が無い窓で差し替える） ==="
# 2026-09-24: 提案書とお土産のジョブ行は mcp タスク内で動き、再起動すると MCP_RESTARTED で終わる。
# 両者は teamagent-dev-proposal-builder-jobs に相乗りしており、旧正規表現（omiyage|tiktok）に合わなかったので名前で明示する。
# scan が失敗したら 0 件扱いにせず止める（旧版は `|| echo 0` で素通りしていた）。
# 09-24 レビュー D-08: list-tables の失敗を握りつぶさない（tiktok のテーブルが黙って確認対象から外れるのを防ぐ）。
ALL_TABLES="$(aws dynamodb list-tables --query 'TableNames' --output text)" \
  || { echo "FATAL: list-tables に失敗（実行中ジョブを確認できないので止める）"; exit 1; }
JOB_TABLES="$( { echo "$ALL_TABLES" | tr '\t' '\n' | grep -E "teamagent-dev-.*(omiyage|tiktok).*jobs" || true; echo teamagent-dev-proposal-builder-jobs; } | sort -u)"
# 09-24 レビュー D-03: 心拍の止まった放置行（MCP 再起動で落ちたまま queued/running で残った行）まで数えると、
# 正しい便が止まり、FORCE_SWAP で全テーブルの保護を外すしかなくなる。30 分以内に心拍（updated_at）か
# 有効な lease（lease_expires_at）がある行だけを実行中として数え、古い行は放置行として表示だけする。
LIVE_TOTAL=0
for T in $JOB_TABLES; do
  aws dynamodb scan --table-name "$T" --filter-expression "#s IN (:a,:b,:c)" \
    --expression-attribute-names '{"#s":"status"}' \
    --expression-attribute-values '{":a":{"S":"running"},":b":{"S":"dispatched"},":c":{"S":"queued"}}' \
    --output json > "$OUT/jobs-$T.json" \
    || { echo "FATAL: $T の scan に失敗（実行中ジョブを確認できないので止める）"; exit 1; }
  read -r LIVE STALE <<<"$(python3 - "$OUT/jobs-$T.json" <<'PY2'
import datetime, json, sys, time
items = json.load(open(sys.argv[1])).get("Items", [])
now = time.time(); live = stale = 0
def val(item, key):
    v = item.get(key) or {}
    return v.get("S") or v.get("N")
for it in items:
    fresh = False
    lease = val(it, "lease_expires_at")
    if lease and lease.replace(".", "", 1).isdigit() and float(lease) > now:
        fresh = True
    upd = val(it, "updated_at")
    if upd:
        try:
            t = float(upd) if upd.replace(".", "", 1).isdigit() else datetime.datetime.fromisoformat(upd.replace("Z", "+00:00")).timestamp()
            fresh = fresh or (now - t) < 1800
        except ValueError:
            fresh = True  # 読めない時刻は安全側（実行中扱い）
    else:
        fresh = fresh or not lease  # 時刻が無い行は安全側
    live += fresh; stale += (not fresh)
print(live, stale)
PY2
)"
  echo "  ${T} 実行中=${LIVE}（30 分以内に心拍あり）/ 放置行=${STALE}（数えない）"
  LIVE_TOTAL=$((LIVE_TOTAL + LIVE))
done
if [ "$LIVE_TOTAL" != "0" ] && [ -z "${FORCE_SWAP:-}" ]; then echo "FATAL: 実行中ジョブあり（終わってから再実行。どうしても強行するなら FORCE_SWAP=1）"; exit 1; fi

[ "$DIGEST" = "keep" ] || aws ecr describe-images --repository-name teamagent-mcp --image-ids imageDigest="$DIGEST" --query 'imageDetails[0].imageTags' --output json
CUR=$(aws ecs describe-services --cluster teamagent-dev --services teamagent-dev-mcp --query 'services[0].taskDefinition' --output text)
echo "base: ${CUR##*/}"
aws ecs describe-task-definition --task-definition "$CUR" --query 'taskDefinition' --output json > "$OUT/base.json"
python3 - "$OUT/base.json" "$DIGEST" "$@" > "$OUT/new-input.json" <<'PY'
import json, sys
td = json.load(open(sys.argv[1])); digest = sys.argv[2]
for k in ("taskDefinitionArn","revision","status","requiresAttributes","compatibilities","registeredAt","registeredBy"):
    td.pop(k, None)
c = td["containerDefinitions"][0]
repo = c["image"].split("@")[0]
assert repo.endswith("/teamagent-mcp"), repo
if digest != "keep":
    print(f"image: {c['image'].split('@')[1][:19]}… -> {digest[:19]}…", file=sys.stderr)
    c["image"] = f"{repo}@{digest}"
else:
    print("image: keep", file=sys.stderr)
env = {e["name"]: e["value"] for e in c.get("environment", [])}
for kv in sys.argv[3:]:
    if kv.startswith("-"):
        print(f"env {kv[1:]}: {env.get(kv[1:])!r} -> (removed)", file=sys.stderr); env.pop(kv[1:], None); continue
    k, v = kv.split("=", 1)
    print(f"env {k}: {env.get(k)!r} -> {v!r}", file=sys.stderr); env[k] = v
c["environment"] = [{"name": k, "value": v} for k, v in sorted(env.items())]
json.dump(td, sys.stdout, ensure_ascii=False)
PY
NEW_ARN=$(aws --profile "$RELEASE_PROFILE" ecs register-task-definition --cli-input-json "file://$OUT/new-input.json" --query 'taskDefinition.taskDefinitionArn' --output text)
echo "registered: ${NEW_ARN##*/}"
aws --profile "$RELEASE_PROFILE" ecs update-service --cluster teamagent-dev --service teamagent-dev-mcp --task-definition "$NEW_ARN" --query 'service.taskDefinition' --output text >/dev/null
aws ecs wait services-stable --cluster teamagent-dev --services teamagent-dev-mcp
aws ecs describe-services --cluster teamagent-dev --services teamagent-dev-mcp --query 'services[0].{td:taskDefinition,running:runningCount,desired:desiredCount}' --output json
# 2026-09-24: services-stable は「deployment が 1 本・running==desired」しか見ない。サーキットブレーカー（rollback=true）で
# 旧 TD に巻き戻っても成功扱いになるので、稼働中の TD が今登録したものか・rollout が COMPLETED かを確かめる。
# 09-24 r29: services-stable の直後は rolloutState がまだ IN_PROGRESS のことがある（数十秒後に COMPLETED）。
# 1 回見て止めると誤検知になるので、COMPLETED まで最大 10 分待つ。止めるのは FAILED か PRIMARY が新 TD でないときだけ。
for _ in $(seq 1 40); do
  # 09-24 レビュー D-09: PRIMARY の TD と rolloutState は 1 回の呼び出しで同時に読む（別々に読むと食い違う瞬間がある）
  read -r LIVE_TD ROLLOUT <<<"$(aws ecs describe-services --cluster teamagent-dev --services teamagent-dev-mcp --query 'services[0].deployments[?status==`PRIMARY`] | [0].[taskDefinition,rolloutState]' --output text)"
  [ "$LIVE_TD" = "$NEW_ARN" ] || { echo "FATAL: mcp の PRIMARY が新 TD でない（${LIVE_TD##*/}）。巻き戻りの可能性。OC bounce はしない"; exit 1; }
  [ "$ROLLOUT" = "FAILED" ] && { echo "FATAL: mcp の rollout が FAILED。OC bounce はしない"; exit 1; }
  [ "$ROLLOUT" = "COMPLETED" ] && break
  sleep 15
done
[ "$ROLLOUT" = "COMPLETED" ] || { echo "FATAL: mcp の rollout が 10 分で COMPLETED にならない（${ROLLOUT}）。OC bounce はしない"; exit 1; }
echo "  PRIMARY=${LIVE_TD##*/} rollout=${ROLLOUT}"
echo "=== OC bounce（ツール一覧の再取得） ==="
aws --profile "$RELEASE_PROFILE" ecs update-service --cluster teamagent-dev --service teamagent-dev-openclaw --force-new-deployment --query 'service.taskDefinition' --output text >/dev/null
aws ecs wait services-stable --cluster teamagent-dev --services teamagent-dev-openclaw
aws ecs describe-services --cluster teamagent-dev --services teamagent-dev-openclaw --query 'services[0].{td:taskDefinition,running:runningCount}' --output json
echo "🏁 mcp ${NEW_ARN##*/} 差し替え完了（image=${DIGEST}）＋ OC bounce 完了"
