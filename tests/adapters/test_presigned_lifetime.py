"""presigned URL の寿命判定（「基本7日有効」を無言で下回らせない）のテスト。

背景（2026-09-01 実測）: connect-web が /r のリダイレクト先として発行した presigned が
約30分で AccessDenied になった。ExpiresIn には 7日 を渡していたが、**一時認証情報(STS)で
署名すると寿命はトークン側で頭打ち**になるため。無言だと「7日のつもりのリンク」が数十分で死ぬ。
"""

from __future__ import annotations

import datetime as dt

from teamagent.adapters.report_publish import presigned_expiry_epoch, presigned_lifetime_s

_NOW = 1_788_226_000


def test_sigv2_expires_is_parsed() -> None:
    url = "https://b.s3.amazonaws.com/k.html?AWSAccessKeyId=A&Signature=S&Expires=1788225266"
    assert presigned_expiry_epoch(url) == 1788225266


def test_sigv4_date_plus_expires_is_parsed() -> None:
    start = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.UTC)
    url = "https://b.s3.amazonaws.com/k.html?X-Amz-Date=20260901T000000Z&X-Amz-Expires=604800"
    assert presigned_expiry_epoch(url) == int(start.timestamp()) + 604800


def test_lifetime_is_negative_when_expired() -> None:
    url = "https://b.s3.amazonaws.com/k.html?Expires=1788225266"
    assert presigned_lifetime_s(url, now=_NOW) < 0


def test_seven_day_link_is_positive_and_long() -> None:
    url = f"https://b.s3.amazonaws.com/k.html?Expires={_NOW + 604800}"
    assert presigned_lifetime_s(url, now=_NOW) == 604800


def test_unsigned_url_returns_none() -> None:
    assert presigned_expiry_epoch("https://b.s3.amazonaws.com/k.html") is None
    assert presigned_lifetime_s("https://b.s3.amazonaws.com/k.html") is None


def test_malformed_values_return_none() -> None:
    assert presigned_expiry_epoch("https://b/k?Expires=abc") is None
    assert presigned_expiry_epoch("https://b/k?X-Amz-Date=xx&X-Amz-Expires=1") is None
