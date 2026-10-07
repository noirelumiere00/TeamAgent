"""レポート成果物の「配信URL」決定を一元化する（短縮URL /r か 従来 presigned か）。

なぜ必要か（2026-07-15 の実機事故）:
    @Aico(openclaw) の LLM は、ツール結果を文脈から**書き直して**Slack へ返すことがある。その際
    presigned URL の長大なクエリ(?AWSAccessKeyId=…&Signature=…&Expires=…)を再タイプできず、
    パスだけ残して落とす。結果 `https://<bucket>.s3.amazonaws.com/vseo-reports/x.html` という
    裸URLが営業に渡り、バケットは公開全ブロックなので **AccessDenied で誰も開けない**。
    実測: レポート本体は4.0MBで正常生成・presign も成功していたのに、URLだけが壊れていた。

対策の本質:
    「URLを短くする」ことではなく **署名を ?query から path へ移す**こと。クエリが無ければ
    LLM の「クエリ＝消してよいトラッキング」ヒューリスティクスが発火しない。

なぜ共通化するか:
    短縮URL化は当初 x_research にしか無く、tiktok_comment_mining / search_surface_check /
    video_algorithm は presigned を返したままだった（＝同じ事故が残る）。**HTML レポートの URL を
    @Aico 経由で人に渡す** skill は必ずここを通す。

対象と残件（誇張しないこと）:
    通る  : x_research(声集め/ニーズ/バズ)・tiktok_comment_mining・search_surface_check・
            video_algorithm._publish（いずれも publish_html_file_result → delivery_url）。
            mcp_gateway/payload_offload（長文退避 JSON・publish_text_result → short_url_or_none。
            LLM へ返す値なので presigned へは落とさず fail-closed）。
    未対応: pptx/pdf 等のバイナリ成果物（video_algorithm._publish_artifact・proposal_deck）。
            report_publish に *_result 版（bucket/key/region を返す発行関数）が無く、そのままでは
            トークン化できないため本対応の対象外。key prefix は allowlist 済み
            (_ALLOWED_KEY_PREFIXES に vseo-proposals/ が在る)なので、*_result 版を足せば同じ経路に
            寄せられる＝将来の残件。これらは今も presigned のままで、openclaw に渡ると同じ壊れ方を
            しうる。
"""

from __future__ import annotations

import os

import structlog

from teamagent.adapters.report_publish import PublishedObject

logger = structlog.get_logger(__name__)

# 「基本7日有効」の最低線（これを下回る配布URLは警告する）。
_MIN_LINK_LIFETIME_S = 6 * 24 * 3600


def short_url_enabled() -> bool:
    """USE_REPORT_SHORTURL: レポートを短縮URL(/r)で配布する段階ゲート（既定 OFF）。

    connect-web に /r ルート＋vseo-s3-read が揃い、**実機 /r をリダイレクト追従して最終 200**
    （＝S3 GetObject が実際に成功する）を確認した後に ON にする。302 だけでは不十分:
    presign はローカル署名操作なので GetObject 権限が無くても 302 は返り、権限不足は 302 を
    追った S3 取得時に 403 として初めて顕在化する。揃う前に ON にすると受信者側で 404/403 に
    劣化するため、OFF の間は従来 presigned を返す。
    """
    return os.environ.get("USE_REPORT_SHORTURL", "").strip().lower() in ("1", "true", "yes", "on")


def _missing_prereqs(base: str, key: str, bucket: str = "") -> list[str]:
    """短縮URL化に足りていない前提を名指しで返す（空リスト＝全て充足）。

    bucket は省略可（空なら検査しない＝従来呼び出しの互換）。渡されたら decode 側と同じ
    allowlist（VSEO_REPORT_BUCKET）で検査する。
    """
    from teamagent.adapters.report_link_token import (
        has_secret,
        is_allowed_bucket,
        is_allowed_key,
    )

    missing: list[str] = []
    if not base:
        # mcp taskdef の CONNECT_BASE_URL 未設定。
        missing.append("CONNECT_BASE_URL")
    if not has_secret():
        # REPORT_LINK_HMAC_SECRET / rotation / TTL のいずれかが未注入・不正。メール action 鍵・
        # DB URL へは fallback しないため、発行側と connect-web へ同じ契約を明示注入する。
        missing.append("REPORT_LINK_HMAC_CONFIG")
    if not is_allowed_key(key):
        # allowlist 外 prefix の成果物にトークンを出すと decode 側が拒否して 404 になる。
        # 空 key もここで弾く（`key and ...` で条件化すると空 key が allowlist を素通りして
        # decode 不能なトークンを発行してしまい、旧実装より弱くなる）。
        missing.append("key_prefix_not_allowed")
    if bucket and not is_allowed_bucket(bucket):
        # decode 側は VSEO_REPORT_BUCKET 以外の bucket を拒否する。別バケット（例:
        # PAYLOAD_OFFLOAD_BUCKET の設定違い）にトークンを出すと 404 になるので発行しない。
        missing.append("bucket_not_allowed")
    return missing


def _warn_if_shortlived(url: str, *, request_id: str, reason: str) -> None:
    """presigned へ落ちる時、その URL が実際は短命なら名指しで警告する。

    一時認証情報(STS)で署名した presigned は、ExpiresIn に 7日 を渡してもトークン寿命で
    頭打ちになる（実測: 約30分）。「7日有効のつもりのリンクが数十分で死ぬ」を無言にしない。
    """
    from teamagent.adapters.report_publish import presigned_lifetime_s

    lifetime = presigned_lifetime_s(url)
    if lifetime is not None and lifetime < _MIN_LINK_LIFETIME_S:
        logger.warning(
            "report_delivery_shortlived_url",
            request_id=request_id,
            reason=reason,
            lifetime_s=lifetime,
            hint="この URL は7日もたない。/r 短縮URLの前提を揃えて配布し直すこと",
        )


# short_url_or_none が None を返した理由（delivery_url の shortlived 警告の reason に使う）。
# ログの理由ラベルを実態と一致させるため、呼び出し側で推測し直さない。
_REASON_DISABLED = "short_url_disabled"
_REASON_PREREQ = "prereq_missing"
_REASON_ENCODE = "encode_failed"


def _short_url_with_reason(result: PublishedObject, *, request_id: str) -> tuple[str | None, str]:
    """(短縮URL, 理由) を返す。URL が作れたら理由は "ok"。"""
    from teamagent.skills.knowledge_search_url.skill import connect_base_url

    if not short_url_enabled():
        return None, _REASON_DISABLED  # 機能OFF＝意図した無言（後方互換）

    base = connect_base_url()
    missing = _missing_prereqs(base, result.key, result.bucket)
    if missing:
        logger.warning(
            "report_short_url_prereq_missing",
            request_id=request_id,
            missing=",".join(missing),
            hint="USE_REPORT_SHORTURL=1 だが前提が未充足のため短縮URLを発行できない",
        )
        return None, _REASON_PREREQ

    from teamagent.adapters.report_link_token import encode_report_token

    try:
        token = encode_report_token(result.bucket, result.key, region=result.region)
    except Exception:
        logger.warning("report_short_url_encode_failed", request_id=request_id)
        return None, _REASON_ENCODE
    if token is None:
        logger.warning("report_short_url_encode_failed", request_id=request_id)
        return None, _REASON_ENCODE
    return f"{base}/r/{token}", "ok"


def short_url_or_none(result: PublishedObject, *, request_id: str) -> str | None:
    """条件が揃えば /r 短縮URL、揃わなければ **None**（presigned を返さない＝fail-closed）。

    delivery_url の中核。LLM へ返す値（mcp_gateway/payload_offload）のように「署名付き URL を
    構造的に出してはいけない」呼び出し側はこちらを使う。欠けた前提・発行失敗は名指しで
    warning する（無言で None にしない）。
    """
    return _short_url_with_reason(result, request_id=request_id)[0]


def delivery_url(result: PublishedObject, *, request_id: str) -> str:
    """公開済み成果物の配信URLを返す。条件が揃えば /r 短縮URL、揃わなければ presigned。

    **前提が欠けている時は黙って presigned へ落とさず、欠けた前提を名指しで warning する。**
    これが無いと「USE_REPORT_SHORTURL=1 にしたのに直らない」が無言で起き、原因究明が事実上
    不能になる（専用 REPORT_LINK_HMAC_SECRET が無く、フラグだけ立てても不発になる）。
    配信自体は止めない（fail-open）＝ presigned は返す。LLM へ返す値には使わない
    （presigned を構造的に出してはいけない所は short_url_or_none を使う）。
    """
    short, reason = _short_url_with_reason(result, request_id=request_id)
    if short is not None:
        return short
    # reason は short_url_disabled / prereq_missing / encode_failed のいずれか（実態と一致させる。
    # ここで推測し直すと encode 失敗が prereq_missing と記録され、原因究明用のログが嘘をつく）。
    _warn_if_shortlived(result.url, request_id=request_id, reason=reason)
    return result.url
