"""/r 短縮リンクの IAM 契約: token の key allowlist と connect-web の GetObject prefix を一致させる。

P8（2026-10-07）の指摘: payload-offload/ を report_link_token の allowlist に足したが、/r が
presigned を再生成する connect-web task role の GetObject は vseo-reports/ と vseo-proposals/
の 2 prefix に限定されたままで、.json（presign→302 経路）は token が通るのに S3 で
AccessDenied(403) になっていた。allowlist（発行側・decode 側）と IAM（connect_web.tf の
VseoReportS3Read・即時ロールアウト用 bootstrap_vseo_s3_iam.sh の inline policy と probe）の
prefix 集合が一致することを terraform を実行せずテキストで固定する（既存の infra 契約テストと同型）。
"""

from __future__ import annotations

import re
from pathlib import Path

from teamagent.adapters.report_link_token import _ALLOWED_KEY_PREFIXES

ROOT = Path(__file__).resolve().parents[2]
CONNECT_WEB = (ROOT / "infra/terraform/connect_web.tf").read_text(encoding="utf-8")
FARGATE = (ROOT / "infra/terraform/fargate.tf").read_text(encoding="utf-8")
BOOTSTRAP = (ROOT / "infra/deploy/bootstrap_vseo_s3_iam.sh").read_text(encoding="utf-8")


def _statement(body: str, sid: str) -> str:
    """sid を持つ terraform statement ブロック本文（次の statement/閉じ括弧まで）。"""
    m = re.search(rf'statement \{{\s*sid\s*=\s*"{re.escape(sid)}".*?\n  \}}', body, re.S)
    assert m, f"statement {sid} が無い"
    return m.group(0)


def _tf_prefixes(statement: str) -> set[str]:
    return set(re.findall(r'"\$\{aws_s3_bucket\.raw_files\.arn\}/([a-z0-9-]+)/\*"', statement))


def test_allowlist_includes_payload_offload_prefix() -> None:
    """発行側/decode 側の allowlist に長文退避の既定 prefix がある（無いと full_url が出ない）。"""
    assert "payload-offload/" in _ALLOWED_KEY_PREFIXES


def test_connect_web_task_role_can_get_every_allowlisted_prefix() -> None:
    """VseoReportS3Read の prefix 集合 == token allowlist（片方だけ足すと 403 か 404 に劣化）。"""
    stmt = _statement(CONNECT_WEB, "VseoReportS3Read")
    assert '"s3:GetObject"' in stmt
    expected = {p.rstrip("/") for p in _ALLOWED_KEY_PREFIXES}
    assert _tf_prefixes(stmt) == expected
    # バケット全体への GetObject を開けない（最小権限のまま）。
    assert "aws_s3_bucket.raw_files.arn,\n" not in stmt
    assert '"${aws_s3_bucket.raw_files.arn}/*"' not in stmt


def test_bootstrap_inline_policy_and_probe_cover_every_allowlisted_prefix() -> None:
    """即時ロールアウト用スクリプトも同じ prefix を付与し、simulate-principal-policy で各 prefix を実証する。"""
    policy = re.search(r"--policy-document \"(.*?)\"\n", BOOTSTRAP, re.S)
    assert policy, "put-role-policy の policy-document が無い"
    granted = set(re.findall(r"arn:aws:s3:::\$BUCKET/([a-z0-9-]+)/\*", policy.group(1)))
    expected = {p.rstrip("/") for p in _ALLOWED_KEY_PREFIXES}
    assert granted == expected
    probed = set(
        re.findall(r'verify_get "arn:aws:s3:::\$BUCKET/([a-z0-9-]+)/_probe\.[a-z]+"', BOOTSTRAP)
    )
    assert probed == expected


def test_mcp_task_role_can_put_payload_offload_when_enabled() -> None:
    """発行側（mcp task）の退避 PutObject は use_payload_offload 条件の dynamic statement に限定。"""
    m = re.search(
        r"for_each = var\.use_payload_offload \? \[1\] : \[\]\s*content \{\s*"
        r'sid\s*=\s*"PayloadOffloadS3".*?\n    \}',
        FARGATE,
        re.S,
    )
    assert m, "PayloadOffloadS3 の dynamic statement が無い"
    stmt = m.group(0)
    assert '"s3:PutObject"' in stmt
    assert '"${aws_s3_bucket.raw_files.arn}/payload-offload/*"' in stmt


def test_payload_offload_requires_scrape_tools_for_bucket_allowlist() -> None:
    """VSEO_REPORT_BUCKET（/r の bucket allowlist）は enable_scrape_tools ブロックでしか注入されない。

    offload だけ ON だと発行側が bucket_not_allowed で full_url を出さない。apply 前に落とす
    precondition が fargate.tf にあること。
    """
    assert "!var.use_payload_offload || var.enable_scrape_tools" in FARGATE
    # 前提そのもの: VSEO_REPORT_BUCKET が mcp env の enable_scrape_tools ブロックの中にだけある。
    env_blocks = re.findall(r"var\.enable_scrape_tools \? \[\n(.*?)\n\s*\] : \[", FARGATE, re.S)
    vseo_env = '{ name = "VSEO_REPORT_BUCKET", value = aws_s3_bucket.raw_files.id }'
    assert any(vseo_env in block for block in env_blocks)
    assert FARGATE.count(vseo_env) == 1
    # PAYLOAD_OFFLOAD_BUCKET と同じ raw_files（別バケットにすると decode が拒否する）。
    assert (
        'PAYLOAD_OFFLOAD_BUCKET", value = var.use_payload_offload ? aws_s3_bucket.raw_files.bucket : ""'
        in FARGATE
    )
