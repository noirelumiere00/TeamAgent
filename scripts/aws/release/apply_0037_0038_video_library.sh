#!/usr/bin/env bash
# migration 0037（URL 取り込みの土台・#792）と 0038（事例推定・読み替え表・#811）を本番 RDS に当てる（2026-11-04）。
# apply_0035_0036 の複製。追加のみ（表 8 つ・vl_videos と vl_campaigns への列追加・ビューの列互換の更新）。既存の行は消さない。
#
#   ./apply_0037_0038_video_library.sh           # 読み取りだけ（記録・実行者の属性・vl_* の有無）
#   ./apply_0037_0038_video_library.sh --apply   # 0037→0038 をそれぞれ 1 トランザクションで適用＋記録＋検証
# SQL は origin/dev から git show で取り出す（checksum を dev と一致させる）。
#
# migrate.py を使わない理由は 0034 と同じ（未適用の 0029 が一緒に当たるのを避け、この 2 本だけを当てる）。
# 経路: SSM ポートフォワード（踏み台）→ RDS teamagent-dev。パスワードは Secrets Manager から実行時に読み、画面・ファイルに出さない。
set -euo pipefail

REGION=ap-northeast-1
BASTION=i-0318356170700582b
RDS_HOST=teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com
LOCAL_PORT=15440
WT=${WT:-$HOME/dev/worktrees/teamagent-vl-cases}  # psycopg のある作業場所（SQL は origin/dev から取り出す）
MODE=${1:-check}
SQLDIR=$(mktemp -d)
git -C "$HOME/dev/teamagent" fetch -q origin
for f in 0037_video_library_url_import.sql 0038_video_campaign_links.sql; do
  git -C "$HOME/dev/teamagent" show "origin/dev:infra/migrations/$f" > "$SQLDIR/$f"
done
PY="$WT/.venv/bin/python"
[ -x "$PY" ] || { echo "python が無い: $PY"; exit 1; }

echo "== 踏み台: $(aws ec2 describe-instances --region $REGION --instance-ids $BASTION \
  --query 'Reservations[0].Instances[0].State.Name' --output text)"

aws ssm start-session --region $REGION --target $BASTION \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$RDS_HOST\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}" \
  >/dev/null 2>&1 &
TUNNEL=$!
trap 'kill $TUNNEL 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  nc -z 127.0.0.1 $LOCAL_PORT 2>/dev/null && break
  sleep 1
done
nc -z 127.0.0.1 $LOCAL_PORT || { echo "トンネルが開かない"; exit 1; }

PGPW=$(aws secretsmanager get-secret-value --region $REGION --secret-id teamagent/dev/db_password \
  --query SecretString --output text)
export PGPW MODE SQLDIR LOCAL_PORT

"$PY" - <<'PYEOF'
import hashlib, os, sys
from datetime import UTC, datetime
from pathlib import Path
import psycopg

dsn = f"postgresql://teamagent:{os.environ['PGPW']}@127.0.0.1:{os.environ['LOCAL_PORT']}/teamagent?sslmode=require"
d = Path(os.environ["SQLDIR"]); mode = os.environ["MODE"]
targets = [("0037", "0037_video_library_url_import.sql"), ("0038", "0038_video_campaign_links.sql")]
tables = ["vl_url_import_batches", "vl_url_import_rows", "vl_public_fetch_control", "vl_public_fetch_daily_usage",
          "vl_public_metrics", "vl_video_analysis_requests", "vl_video_campaign_links", "vl_case_aliases"]

with psycopg.connect(dsn, autocommit=False) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        applied = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT rolcreaterole, rolbypassrls, rolsuper FROM pg_roles WHERE rolname = current_user")
        attrs = cur.fetchone()
        cur.execute("SELECT count(*) FROM pg_roles WHERE rolname = 'teamagent_app'")
        app_role = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_class WHERE relname LIKE 'vl\\_%'")
        vl_objects = cur.fetchone()[0]
    conn.rollback()
    print("== 適用前: 記録", sorted(v for v in applied if v >= "0034"),
          "/ 実行者 CREATEROLE・BYPASSRLS・SUPER =", attrs,
          "/ teamagent_app ロール", "あり" if app_role else "なし",
          "/ 既存の vl_* オブジェクト数", vl_objects)
    if mode != "--apply":
        print("（読み取りのみ。適用は --apply）"); sys.exit(0)
    if not app_role:
        print("teamagent_app ロールが無いので中止"); sys.exit(1)
    for ver, fname in targets:
        if ver in applied:
            print(f"{ver} は記録済み。飛ばす"); continue
        sql = (d / fname).read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute("INSERT INTO schema_migrations (version, filename, checksum_sha, applied_at) VALUES (%s, %s, %s, %s)",
                        (ver, fname, hashlib.sha256(sql.encode("utf-8")).hexdigest(), datetime.now(UTC)))
        conn.commit()
        print(f"🏁 {ver} を適用して記録した")
    # 検証: 表ごとに RLS・FORCE・teamagent_app の SELECT・PUBLIC に権限が無いこと、ビューの security_invoker、区分の初期値
    ok = True
    with conn.cursor() as cur:
        for t in tables:
            cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", (t,))
            row = cur.fetchone()
            cur.execute("SELECT has_table_privilege('teamagent_app', %s, 'SELECT')", (f"public.{t}",))
            sel = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM information_schema.role_table_grants WHERE table_name = %s AND grantee = 'PUBLIC'", (t,))
            pub = cur.fetchone()[0]
            good = row is not None and row[0] is True and row[1] is True and sel is True and pub == 0
            ok &= good
            print(f"   {t}: RLS={row[0] if row else None} FORCE={row[1] if row else None} app_select={sel} PUBLIC={pub} {'OK' if good else 'NG'}")
        cur.execute("SELECT reloptions FROM pg_class WHERE relname = 'vl_videos_effective'")
        opts = cur.fetchone()
        view_ok = opts is not None and "security_invoker=true" in (opts[0] or [])
        cur.execute("SELECT has_table_privilege('teamagent_app', 'public.vl_videos_effective', 'SELECT')")
        view_sel = cur.fetchone()[0]
        view_ok = view_ok and view_sel is True
        cur.execute("SELECT code FROM vl_business_categories ORDER BY sort_order")
        codes = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT count(*) FROM vl_videos_effective")
        n_eff = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM vl_videos")
        n_raw = cur.fetchone()[0]
        ok &= view_ok and codes == ["media", "tto", "kirinuki", "other", "organic"] and n_eff == n_raw
        print(f"   ビューの行数={n_eff} / vl_videos={n_raw}")
        print(f"   vl_videos_effective: security_invoker+app_select={view_ok} / 区分の初期値={codes}")
    conn.rollback()
    print(f"== 検証: 期待どおり={ok}")
    print("戻し方: 0036 のビュー定義を当て直し、0037/0038 で足した表を DROP・列を DROP し、schema_migrations の 0037・0038 行を削除（追加だけなので既存データは無傷）")
    sys.exit(0 if ok else 1)
PYEOF
