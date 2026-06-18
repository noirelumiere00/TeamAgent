"""Fargate Scheduled Task 用 ingest エントリポイント（EC2 run_ingest.sh の Python 版）。

EC2 では ``scripts/run_ingest.sh`` が ``set -a; source load_secrets.sh; set +a`` で env を展開していたが、
Fargate では task definition の ``secrets[].valueFrom`` で SM secret を直接 env 注入する。
ただし ``teamagent/dev/google_oauth`` は JSON 形式（``{client_id, client_secret, refresh_token}``）で
1 つの secret に 3 値が同居しているため、ECS が ``GOOGLE_OAUTH_JSON`` env に丸ごと注入したものを
本スクリプトで parse して 3 つの env に展開し、既存の ``scripts/ingest_sources.py`` を ``main()`` 経由で呼ぶ。

env 注入は task definition 側で完結するので、本スクリプトは shell も load_secrets.sh も呼ばない（=
Fargate の中で 1 プロセスで完結）。
"""

from __future__ import annotations

import json
import os
import sys


def _expand_google_oauth_json() -> None:
    """``GOOGLE_OAUTH_JSON`` を 3 つの env に展開する（既に個別 env があれば上書きしない）。"""
    raw = os.environ.get("GOOGLE_OAUTH_JSON", "").strip()
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[run_ingest_fargate] WARN: GOOGLE_OAUTH_JSON の JSON parse 失敗: {exc}", file=sys.stderr)
        return
    for key, env_name in (
        ("client_id", "GOOGLE_CLIENT_ID"),
        ("client_secret", "GOOGLE_CLIENT_SECRET"),
        ("refresh_token", "GOOGLE_OAUTH_REFRESH_TOKEN"),
    ):
        value = payload.get(key)
        if value and not os.environ.get(env_name):
            os.environ[env_name] = str(value)


def _materialize_vertex_sa() -> None:
    """``VERTEX_SA_JSON`` env をファイル化して ADC に渡す（EC2 load_secrets.sh と同方式）。"""
    raw = os.environ.get("VERTEX_SA_JSON", "").strip()
    if not raw:
        return
    sa_path = os.environ.get("VERTEX_SA_PATH", "/tmp/vertex-sa.json")
    os.makedirs(os.path.dirname(sa_path), exist_ok=True)
    with open(sa_path, "w", encoding="utf-8") as f:
        f.write(raw)
    os.chmod(sa_path, 0o600)
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", sa_path)


def main() -> int:
    _expand_google_oauth_json()
    _materialize_vertex_sa()

    # 引数は ENV で渡す（task definition env で上書き可・既定は EC2 run_ingest.sh と同一）。
    sources = os.environ.get("INGEST_SOURCES", "slack,gdrive,gsheets")
    owner_email = os.environ.get("INGEST_OWNER_EMAIL", "shogo@vectorinc.co.jp")
    dry_run = os.environ.get("INGEST_DRY_RUN", "0") == "1"

    sys.argv = [
        "scripts/ingest_sources.py",
        *(["--commit"] if not dry_run else []),
        "--sources",
        sources,
        "--owner-email",
        owner_email,
    ]
    print(f"[run_ingest_fargate] start sources={sources} owner={owner_email} dry_run={dry_run}", flush=True)

    # ingest_sources.py は __main__ 実行を前提に CLI として書かれているので、import 後に
    # サブモジュールの main() を呼ぶか、runpy で起動する。シンプルに runpy で。
    import runpy

    runpy.run_path("scripts/ingest_sources.py", run_name="__main__")
    print("[run_ingest_fargate] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
