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

import hashlib
import json
import os
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# 同梱（イメージ焼き込み）yaml。S3 override 未設定時の既定。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BAKED_YAML = _PROJECT_ROOT / "data" / "ingest_sources.yaml"
# S3 から取得した yaml の書き出し先（コンテナ内 tmp）。
_S3_YAML_LOCAL = "/tmp/ingest_sources_s3.yaml"  # Fargate コンテナ内の使い捨て


def _resolve_sources_yaml() -> str | None:
    """ingest_sources.yaml のパスを解決する（入れ込み v2: S3 override）。

    env ``INGEST_SOURCES_S3_URI`` が設定されていれば boto3 で取得してローカル化し、
    そのパスを返す。**取得失敗・env ``INGEST_SOURCES_SHA256``（設定時のみ）との
    sha256 不一致は即 exit 1**（同梱 yaml への silent fallback 禁止＝イメージ焼き込みの
    古い設定で黙って走らせない）。

    env 未設定なら None を返す（呼び出し側は --yaml を渡さず同梱 yaml の既定に任せる）。
    どちらの経路でも「どの内容の yaml で走るか」を sha256 + 取得元付きで structlog に出す。
    """
    s3_uri = os.environ.get("INGEST_SOURCES_S3_URI", "").strip()
    if not s3_uri:
        baked_sha = (
            hashlib.sha256(_BAKED_YAML.read_bytes()).hexdigest() if _BAKED_YAML.exists() else None
        )
        logger.info(
            "ingest_sources_yaml_resolved",
            source="baked",
            path=str(_BAKED_YAML),
            sha256=(baked_sha or "")[:12] or None,
        )
        return None

    if not s3_uri.startswith("s3://") or "/" not in s3_uri[len("s3://") :]:
        print(
            f"[ERROR] INGEST_SOURCES_S3_URI が s3://bucket/key 形式ではありません: {s3_uri}",
            file=sys.stderr,
        )
        sys.exit(1)
    bucket, _, key = s3_uri[len("s3://") :].partition("/")

    # boto3 は遅延 import（ローカル実行や boto3 無しのテスト環境で既定経路を壊さない）。
    # S3 override を指名した実行で boto3 が無い場合も fail-loud で exit 1。
    try:
        import boto3
    except ImportError as exc:
        print(f"[ERROR] INGEST_SOURCES_S3_URI 指定時は boto3 が必要です: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        resp = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        body: bytes = resp["Body"].read()
    except Exception as exc:
        # fallback 禁止: 古い焼き込み yaml で黙って走ると「更新したつもり」事故になる。
        print(
            f"[ERROR] ingest_sources.yaml の S3 取得に失敗しました（fallback 禁止・中止）: "
            f"{s3_uri}: {exc}",
            file=sys.stderr,
        )
        logger.error("ingest_sources_yaml_s3_fetch_failed", uri=s3_uri, error=str(exc))
        sys.exit(1)

    actual_sha = hashlib.sha256(body).hexdigest()
    expected_sha = os.environ.get("INGEST_SOURCES_SHA256", "").strip().lower()
    if expected_sha and actual_sha != expected_sha:
        print(
            "[ERROR] S3 取得した ingest_sources.yaml の sha256 が期待値と不一致です（中止）: "
            f"expected={expected_sha} actual={actual_sha} uri={s3_uri}",
            file=sys.stderr,
        )
        logger.error(
            "ingest_sources_yaml_sha256_mismatch",
            uri=s3_uri,
            expected=expected_sha,
            actual=actual_sha,
        )
        sys.exit(1)

    Path(_S3_YAML_LOCAL).write_bytes(body)
    logger.info(
        "ingest_sources_yaml_resolved",
        source="s3",
        uri=s3_uri,
        path=_S3_YAML_LOCAL,
        sha256=actual_sha[:12],
        sha256_verified=bool(expected_sha),
    )
    return _S3_YAML_LOCAL


def _expand_google_oauth_json() -> None:
    """``GOOGLE_OAUTH_JSON`` を 3 つの env に展開する（既に個別 env があれば上書きしない）。"""
    raw = os.environ.get("GOOGLE_OAUTH_JSON", "").strip()
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"[run_ingest_fargate] WARN: GOOGLE_OAUTH_JSON の JSON parse 失敗: {exc}",
            file=sys.stderr,
        )
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

    # 入れ込み v2: INGEST_SOURCES_S3_URI 設定時は S3 の yaml で上書き
    # （取得失敗 / sha256 不一致は _resolve_sources_yaml 内で即 exit 1・fallback 禁止）。
    yaml_override = _resolve_sources_yaml()

    sys.argv = [
        "scripts/ingest_sources.py",
        *(["--commit"] if not dry_run else []),
        "--sources",
        sources,
        "--owner-email",
        owner_email,
        *(["--yaml", yaml_override] if yaml_override else []),
    ]
    print(
        f"[run_ingest_fargate] start sources={sources} owner={owner_email} dry_run={dry_run}",
        flush=True,
    )

    # ingest_sources.py は __main__ 実行を前提に CLI として書かれているので、import 後に
    # サブモジュールの main() を呼ぶか、runpy で起動する。シンプルに runpy で。
    import runpy

    runpy.run_path("scripts/ingest_sources.py", run_name="__main__")
    print("[run_ingest_fargate] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
