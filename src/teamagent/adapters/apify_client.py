"""Apify Actor 実行アダプタ（3層: Adapter層）。

X(Twitter)投稿収集・Instagram検索面・TikTokコメント(フォールバック)を Apify REST v2 で
実行する。Skill からは本モジュールの型付き入口（search_posts / search_posts_period /
verify_posts / ig_search / tiktok_comments）だけを呼ぶ。httpx への直叩きは Skill 層で禁止。

設計:
- Actor はApify側インフラで実行される＝MCPコンテナからは REST を叩くだけ
  （TikTok直スクレイプと違いクラウドIP遮断の影響を受けない）。
- run 起動 → ポーリング → dataset items 取得。デッドライン超過時は abort API を叩いて
  課金を止める（走りっぱなし防止）。
- FREE tier の apidojo/tweet-scraper は**黙って0件**を返す（x-reaction-research SKILL.md
  実測）。0件時は run の statusMessage を検査し、プラン起因なら APIFY_TIER で顕在化する。
- コスト概算は actor 別単価表（bedrock_client._PRICE_TABLE と同型）×件数。CostGuard を
  注入すると run 前に予算を原子予約（超過=fail-close）・run 後に実費へ精算（settle）する。

env:
  APIFY_API_TOKEN   Apify APIトークン（Secrets Manager 経由で注入）
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from teamagent.adapters.retry import RetryPolicy, call_with_retry

logger = structlog.get_logger(__name__)

_API_BASE = "https://api.apify.com/v2"
_POLL_INTERVAL_S = 3.0
_DEFAULT_DEADLINE_S = 180

# Actor ID（REST パスは username~actor-name 形式）。
ACTOR_X_SEARCH = "scraper_one~x-posts-search"
ACTOR_X_SEARCH_FALLBACK = "data-slayer~twitter-search"
ACTOR_X_PERIOD = "apidojo~tweet-scraper"
ACTOR_X_VERIFY = "xtracto~x-post-detail-scraper"
ACTOR_IG_SEARCH = "apify~instagram-search-scraper"
ACTOR_IG_HASHTAG = "apify~instagram-hashtag-scraper"
ACTOR_TIKTOK_COMMENTS = "clockworks~tiktok-comments-scraper"

# 概算単価（USD/結果1件）。課金の一次情報は Apify 側。台帳記帳と表示用の概算に使う。
# 出典: x-reaction-research SKILL.md ＋ SNS検索スクレイパー検証レポート(2026-07-10)実測。
_ACTOR_PRICE_PER_ITEM: dict[str, float] = {
    ACTOR_X_SEARCH: 0.00025,
    ACTOR_X_SEARCH_FALLBACK: 0.0004,
    ACTOR_X_PERIOD: 0.0004,
    ACTOR_X_VERIFY: 0.0004,
    ACTOR_IG_SEARCH: 0.0023,
    ACTOR_IG_HASHTAG: 0.0023,
    ACTOR_TIKTOK_COMMENTS: 0.001,
}

# statusMessage にこれらが含まれ結果0件なら「プラン起因の沈黙0件」とみなす。
_TIER_HINTS = ("plan", "tier", "paid", "subscription", "upgrade", "rental")


class ApifyError(RuntimeError):
    """Apify 呼び出しの失敗。メッセージは 'APIFY_<CODE>: 説明' 形式。"""


class _DeadlineExceededError(TimeoutError):
    """run_actor_sync の壁時計期限を使い切った（内部制御用）。"""


@dataclass(frozen=True)
class XPost:
    """X投稿の正規化型（actor 差を吸収した共通スキーマ）。"""

    post_id: str
    url: str
    author_handle: str
    author_name: str
    text: str
    like_count: int
    retweet_count: int
    reply_count: int
    created_at: str
    lang: str
    source_actor: str
    # 投稿再現カード用（P1）。取得できた actor でのみ非空。frozen のため tuple。
    author_avatar_url: str = ""
    media_urls: tuple[str, ...] = ()
    media_types: tuple[str, ...] = ()  # media_urls と同順（photo|video|gif|animated_gif）
    is_verified: bool = False
    view_count: int = 0
    quote_count: int = 0


@dataclass(frozen=True)
class IgPost:
    """Instagram投稿（リール含む）の正規化型。"""

    shortcode: str
    url: str
    caption: str
    author: str
    like_count: int
    view_count: int
    comment_count: int
    thumb_url: str
    post_type: str  # reel | image | carousel | unknown
    source_actor: str


@dataclass(frozen=True)
class ApifyRunResult:
    """run_actor_sync の返り値。items は dataset の生 dict 列。"""

    items: list[dict[str, Any]]
    actor_id: str
    run_id: str
    status: str
    estimated_cost_usd: float
    warnings: list[str] = field(default_factory=list)


def _is_httpx_retryable(exc: BaseException) -> bool:
    """一過性（接続断/タイムアウト/429/5xx）のみリトライする。"""
    if isinstance(exc, httpx.TimeoutException | httpx.ConnectError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


def _first(d: dict[str, Any], *keys: str, default: Any = "") -> Any:
    """複数キー候補の最初の非空値を返す（actor間のフィールド名差を吸収）。"""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _as_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _as_dict(v: Any) -> dict[str, Any]:
    """dict でなければ空 dict（Apify GW の data:null / 想定外形状への防御）。"""
    return v if isinstance(v, dict) else {}


def _extract_x_media(d: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """添付メディアの (URL, type) を actor 差を吸収して抽出（最大4・順序保持・重複除去）。

    scraper_one: media[].mediaUrlHttps / data-slayer,apidojo: entities|extendedEntities.media[]
    ・data-slayer動画: media.video.media_url_https（media が dict のケース）。
    """
    buckets: list[dict[str, Any]] = []
    top = d.get("media")
    if isinstance(top, list):
        buckets.extend(x for x in top if isinstance(x, dict))
    elif isinstance(top, dict):
        for v in top.values():  # data-slayer の media.video.* / media.photo.* 形
            if isinstance(v, dict):
                buckets.append(v)
    for key in ("extendedEntities", "extended_entities", "entities"):
        c = d.get(key)
        if isinstance(c, dict):
            cm = c.get("media")
            if isinstance(cm, list):
                buckets.extend(x for x in cm if isinstance(x, dict))
    urls: list[str] = []
    types: list[str] = []
    seen: set[str] = set()
    for it in buckets:
        u = str(
            _first(it, "mediaUrlHttps", "media_url_https", "mediaUrl", "media_url", "url") or ""
        )
        if not u or u in seen:
            continue
        seen.add(u)
        urls.append(u)
        types.append(str(_first(it, "type", "mediaType", default="photo")) or "photo")
        if len(urls) >= 4:
            break
    return tuple(urls), tuple(types)


def _parse_x_item(d: dict[str, Any], source_actor: str) -> XPost | None:
    """X系actorの1件を寛容にパースする（不明形式は None＝呼び側でスキップ）。"""
    # author ネスト: scraper_one/apidojo=author, 一部=user, data-slayer=user_info。
    author_raw = d.get("author") or d.get("user") or d.get("user_info") or {}
    if not isinstance(author_raw, dict):
        author_raw = {}
    handle = str(
        _first(author_raw, "userName", "username", "screen_name", "screenName")
        # top-level 候補: data-slayer は snake `screen_name` を top-level にも返す。
        or _first(d, "authorUsername", "username", "screenName", "screen_name", "userName")
    ).lstrip("@")
    post_id = str(_first(d, "id", "id_str", "tweet_id", "tweetId", "postId"))
    url = str(_first(d, "url", "twitterUrl", "tweetUrl", "postUrl", "link"))
    if not url and post_id and handle:
        url = f"https://x.com/{handle}/status/{post_id}"
    if not post_id and url:
        m = re.search(r"/status(?:es)?/(\d+)", url)
        post_id = m.group(1) if m else ""
    # 本文キー: scraper_one(第一候補actor)は `postText`。これが候補に無いと第一候補の
    # 全件が text="" → None ドロップ → 毎回 data-slayer へ縮退＋二重課金していた（根因A）。
    text = str(_first(d, "text", "fullText", "full_text", "postText", "content", "tweet"))
    if not (post_id or url) or not text:
        return None
    _media_urls, _media_types = _extract_x_media(d)
    return XPost(
        post_id=post_id,
        url=url,
        author_handle=handle,
        author_name=str(_first(author_raw, "name", "displayName") or d.get("authorName") or ""),
        text=text,
        like_count=_as_int(
            _first(
                d,
                "likeCount",
                "favouriteCount",
                "favoriteCount",
                "favorite_count",
                "favourite_count",
                "favorites",  # data-slayer の実キー（これが無いといいね常に0＝根因D）
                "likes",
                default=0,
            )
        ),
        # RT キー: scraper_one は `repostCount`（第一候補復旧時に顕在化）。
        retweet_count=_as_int(
            _first(d, "retweetCount", "repostCount", "retweets", "retweet_count", default=0)
        ),
        reply_count=_as_int(_first(d, "replyCount", "replies", "reply_count", default=0)),
        created_at=str(_first(d, "createdAt", "created_at", "date", "timestamp")),
        lang=str(d.get("lang") or ""),
        source_actor=source_actor,
        # 再現カード用（取得できた actor でのみ埋まる・無ければフォールバック描画）。
        author_avatar_url=str(
            _first(
                author_raw,
                "profileImageUrl",
                "profilePicture",
                "avatar",
                "profile_image_url_https",
                "profileImageUrlHttps",
                "profile_image_url",
            )
            or ""
        ),
        media_urls=_media_urls,
        media_types=_media_types,
        is_verified=bool(
            author_raw.get("isVerified")
            or author_raw.get("is_blue_verified")
            or author_raw.get("verified")
            or d.get("isVerified")
        ),
        view_count=_as_int(_first(d, "viewCount", "views", "view_count", default=0)),
        quote_count=_as_int(_first(d, "quoteCount", "quotes", "quote_count", default=0)),
    )


def _parse_ig_item(d: dict[str, Any], source_actor: str) -> IgPost | None:
    """IG系actorの1件を寛容にパースする。"""
    shortcode = str(_first(d, "shortCode", "shortcode", "code"))
    url = str(_first(d, "url", "postUrl"))
    if not url and shortcode:
        url = f"https://www.instagram.com/p/{shortcode}/"
    if not shortcode and url:
        m = re.search(r"instagram\.com/(?:p|reel)/([^/?]+)", url)
        shortcode = m.group(1) if m else ""
    if not (shortcode or url):
        return None
    ptype = str(_first(d, "type", "productType", "mediaType", default="")).lower()
    if "reel" in ptype or "clips" in ptype or "video" in ptype:
        post_type = "reel"
    elif "sidecar" in ptype or "carousel" in ptype:
        post_type = "carousel"
    elif "image" in ptype or "photo" in ptype:
        post_type = "image"
    else:
        post_type = "unknown"
    return IgPost(
        shortcode=shortcode,
        url=url,
        caption=str(_first(d, "caption", "text", "title", default="")),
        author=str(_first(d, "ownerUsername", "owner_username", "username", default="")).lstrip(
            "@"
        ),
        like_count=_as_int(_first(d, "likesCount", "likeCount", "likes", default=0)),
        view_count=_as_int(
            _first(d, "videoViewCount", "videoPlayCount", "viewCount", "plays", default=0)
        ),
        comment_count=_as_int(_first(d, "commentsCount", "commentCount", default=0)),
        thumb_url=str(_first(d, "displayUrl", "thumbnailUrl", "coverUrl", default="")),
        post_type=post_type,
        source_actor=source_actor,
    )


class ApifyClient:
    """Apify REST v2 の薄いラッパ（run起動→ポーリング→dataset取得→abort）。"""

    def __init__(
        self,
        token: str,
        *,
        ledger: Any | None = None,
        http: httpx.Client | None = None,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        """ledger: CostGuard 互換（reserve/settle か check/record を持つ）。None でガードなし。
        http: テスト注入用 httpx.Client（実課金ゼロでモック可能）。
        """
        self._token = token
        self._ledger = ledger
        self._http = http or httpx.Client(timeout=30.0)
        self._poll_interval_s = poll_interval_s

    @classmethod
    def from_env(cls, *, ledger: Any | None = None) -> ApifyClient:
        token = os.environ.get("APIFY_API_TOKEN", "").strip()
        if not token:
            raise ApifyError(
                "APIFY_MISCONFIGURED: APIFY_API_TOKEN が未設定です。"
                "Secrets Manager の配線（fargate.tf）を確認してください。"
            )
        return cls(token, ledger=ledger)

    # ---- 低レベル: run 実行 -------------------------------------------------

    def _request(
        self, method: str, path: str, *, deadline_at: float | None = None, **kwargs: Any
    ) -> httpx.Response:
        # トークンは Authorization ヘッダで送る（URLクエリに載せない）。
        # httpx の例外メッセージは "for url '{response.url}'" 形式で URL 全体を含むため、
        # クエリにトークンを載せると 4xx/5xx 例外経由で OC/Slack/ログへ平文漏えいする
        # （self-review HIGH 指摘）。ヘッダ送信なら例外文字列にトークンは出ない。
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {self._token}"}

        def _call() -> httpx.Response:
            call_kwargs = dict(kwargs)
            if deadline_at is not None:
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    raise _DeadlineExceededError
                # 1回のHTTP待ちも残り壁時計予算を超えない。Client既定30秒より短い方。
                call_kwargs["timeout"] = min(30.0, max(0.05, remaining))
            resp = self._http.request(method, f"{_API_BASE}{path}", headers=headers, **call_kwargs)
            resp.raise_for_status()
            return resp

        def _sleep_with_deadline(delay: float) -> None:
            if deadline_at is None:
                time.sleep(delay)
                return
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise _DeadlineExceededError
            time.sleep(min(delay, remaining))

        try:
            return call_with_retry(
                _call,
                is_retryable=_is_httpx_retryable,
                policy=RetryPolicy(max_attempts=3, base_delay_s=1.0, max_delay_s=8.0),
                sleep=_sleep_with_deadline,
            )
        except httpx.HTTPError:
            if deadline_at is not None and time.monotonic() >= deadline_at:
                raise _DeadlineExceededError from None
            raise

    def _abort(self, run_id: str, request_id: str) -> None:
        """デッドライン超過時に run を止める（課金停止）。失敗しても本処理は継続。"""
        try:
            self._http.post(
                f"{_API_BASE}/actor-runs/{run_id}/abort",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=5.0,
            )
            logger.warning("apify_run_aborted", request_id=request_id, run_id=run_id)
        except Exception as e:
            logger.warning(
                "apify_abort_failed", request_id=request_id, run_id=run_id, error=type(e).__name__
            )

    def _reserve_cost(
        self, user_email: str, est_cost: float, request_id: str
    ) -> tuple[list[str], Any | None]:
        """CostGuard新APIで原子予約。テスト用の旧duck typeはcheckへ後方互換。"""
        if self._ledger is None:
            return [], None
        reserve = getattr(self._ledger, "reserve", None)
        if callable(reserve):
            warnings, reservation = reserve(
                "apify", user_email, est_cost_usd=est_cost, request_id=request_id
            )
            return list(warnings), reservation
        warnings = self._ledger.check(
            "apify", user_email, est_cost_usd=est_cost, request_id=request_id
        )
        return list(warnings), None

    def _settle_cost(
        self,
        user_email: str,
        *,
        cost_usd: float,
        units: int,
        request_id: str,
        reservation: Any | None,
    ) -> None:
        if self._ledger is None:
            return
        settle = getattr(self._ledger, "settle", None)
        if reservation is not None and callable(settle):
            settle(reservation, cost_usd=cost_usd, units=units, request_id=request_id)
            return
        self._ledger.record(
            "apify", user_email, cost_usd=cost_usd, units=units, request_id=request_id
        )

    def _record_partial(
        self,
        user_email: str,
        est_cost: float,
        request_id: str,
        reservation: Any | None,
    ) -> None:
        """timeout/失敗時に概算コストを台帳へ記帳（run が課金済みのことがあるため上限で計上）。

        件数が確定しないため est_cost（max_items ベースの上振れ）を使う。予算ガードが
        実支出より遅れて発動するのを防ぐ（self-review 指摘）。ledger 未注入なら no-op。
        """
        if est_cost > 0:
            self._settle_cost(
                user_email,
                cost_usd=est_cost,
                units=0,
                request_id=request_id,
                reservation=reservation,
            )

    def run_actor_sync(
        self,
        actor_id: str,
        run_input: dict[str, Any],
        *,
        max_items: int,
        deadline_s: int = _DEFAULT_DEADLINE_S,
        request_id: str = "apify",
        user_email: str = "",
    ) -> ApifyRunResult:
        """actor run を起動し、完了までポーリングして dataset items を返す。

        deadline_s 超過時は abort を叩いて ApifyError(APIFY_TIMEOUT)。
        ledger 注入時は run 前に概算額を原子予約（予算超過=fail-close）、run 後に
        実件数ベースの金額へ精算する。
        """
        unit_price = _ACTOR_PRICE_PER_ITEM.get(actor_id, 0.001)
        est_cost = round(unit_price * max_items, 6)
        warnings, reservation = self._reserve_cost(user_email, est_cost, request_id)

        # 予約は「必ず解放が要る状態」を持つ。reserve〜settle の間で想定外例外（Apify GW が
        # data:null / 非JSON / 想定外形状を返した時の AttributeError/JSONDecodeError 等）が
        # 起きても settle を1回だけ通し、幻の予約が台帳に残って月末まで予算を食う事故を防ぐ
        # （self-review HIGH 指摘）。settled フラグ＋finally で解放を保証する。
        settled = False

        def _settle_once(cost_usd: float, units: int) -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            self._settle_cost(
                user_email,
                cost_usd=cost_usd,
                units=units,
                request_id=request_id,
                reservation=reservation,
            )

        start = time.monotonic()
        deadline_at = start + max(0, deadline_s)
        try:
            try:
                resp = self._request(
                    "POST", f"/acts/{actor_id}/runs", json=run_input, deadline_at=deadline_at
                )
            except _DeadlineExceededError:
                _settle_once(0.0, 0)
                raise ApifyError(
                    f"APIFY_TIMEOUT: {actor_id} を {deadline_s}s 以内に開始できませんでした"
                ) from None
            except httpx.HTTPStatusError as e:
                _settle_once(0.0, 0)
                raise ApifyError(
                    f"APIFY_HTTP: actor起動に失敗しました ({actor_id}, "
                    f"status={e.response.status_code})"
                ) from e
            except httpx.HTTPError as e:
                _settle_once(0.0, 0)
                raise ApifyError(f"APIFY_HTTP: actor起動に失敗しました ({actor_id})") from e
            run = _as_dict(resp.json().get("data") if isinstance(resp.json(), dict) else None)
            run_id = str(run.get("id", ""))
            if not run_id:
                # POST は 2xx だが run が生成されていない（data:null 等）＝起動失敗。
                # 課金は発生していないので予約を全解放（settle 0）して弾く。
                _settle_once(0.0, 0)
                raise ApifyError(f"APIFY_HTTP: actor起動応答が不正です ({actor_id}, run_id無し)")
            logger.info("apify_run_started", request_id=request_id, actor=actor_id, run_id=run_id)

            # ポーリング（READY/RUNNING の間は待つ）。ポーリング/dataset の httpx 例外も
            # ApifyError に変換する（起動POSTと同じ扱い＝URLを含む生の httpx 例外を skill 層へ
            # 漏らさない・self-review HIGH 指摘の二重防御）。timeout/失敗時も概算コストを記帳して
            # 台帳が実支出から乖離しないようにする（run は課金済みのことがあるため）。
            status = str(run.get("status", ""))
            status_message = ""
            dataset_id = str(run.get("defaultDatasetId", ""))
            try:
                while status in ("", "READY", "RUNNING"):
                    remaining = deadline_at - time.monotonic()
                    if remaining <= 0:
                        raise _DeadlineExceededError
                    time.sleep(min(self._poll_interval_s, remaining))
                    body = self._request(
                        "GET", f"/actor-runs/{run_id}", deadline_at=deadline_at
                    ).json()
                    run = _as_dict(body.get("data") if isinstance(body, dict) else None)
                    status = str(run.get("status", ""))
                    status_message = str(run.get("statusMessage") or "")
                    dataset_id = str(run.get("defaultDatasetId", "")) or dataset_id

                if status != "SUCCEEDED":
                    _settle_once(est_cost, 0)  # run は課金済みのことがある＝保守側で est 保持
                    raise ApifyError(
                        f"APIFY_RUN_FAILED: {actor_id} status={status} {status_message}".strip()
                    )

                items_resp = self._request(
                    "GET",
                    f"/datasets/{dataset_id}/items",
                    deadline_at=deadline_at,
                    params={"limit": max_items, "clean": "true"},
                )
            except _DeadlineExceededError:
                self._abort(run_id, request_id)
                _settle_once(est_cost, 0)
                raise ApifyError(
                    f"APIFY_TIMEOUT: {actor_id} が {deadline_s}s 以内に完了しませんでした"
                    "（runは中断済み）"
                ) from None
            except httpx.HTTPError as e:
                _settle_once(est_cost, 0)
                raise ApifyError(
                    f"APIFY_HTTP: {actor_id} の実行中にHTTPエラーが発生しました "
                    f"(status={getattr(getattr(e, 'response', None), 'status_code', '?')})"
                ) from None
            items_raw = items_resp.json()
            items: list[dict[str, Any]] = [it for it in items_raw if isinstance(it, dict)]

            # FREE tier の沈黙0件を顕在化（SKILL.md 実測: プラン不足でも SUCCEEDED+0件になる）
            if not items and any(h in status_message.lower() for h in _TIER_HINTS):
                _settle_once(0.0, 0)
                raise ApifyError(
                    f"APIFY_TIER: {actor_id} が0件を返しました。Apifyプラン起因の可能性があります"
                    f"（statusMessage: {status_message[:120]}）。BRONZE以上のプランが必要です。"
                )

            actual_cost = round(unit_price * len(items), 6)
            _settle_once(actual_cost, len(items))
            logger.info(
                "apify_run_done",
                request_id=request_id,
                actor=actor_id,
                run_id=run_id,
                items=len(items),
                est_cost_usd=actual_cost,
                latency_s=round(time.monotonic() - start, 1),
            )
            return ApifyRunResult(
                items=items,
                actor_id=actor_id,
                run_id=run_id,
                status=status,
                estimated_cost_usd=actual_cost,
                warnings=warnings,
            )
        except ApifyError:
            raise  # 上の各ハンドラで settle 済み
        except Exception as e:
            # 想定外例外（非JSON応答での JSONDecodeError 等）: 生例外は URL/トークンを
            # 含みうるので ApifyError へ変換し、予約は finally で解放する。
            logger.warning(
                "apify_run_unexpected",
                request_id=request_id,
                actor=actor_id,
                error=type(e).__name__,
            )
            raise ApifyError(
                f"APIFY_UNEXPECTED: {actor_id} の実行中に想定外のエラーが発生しました"
            ) from None
        finally:
            _settle_once(0.0, 0)  # まだ精算していなければ予約を解放（幻の予約リーク防止）

    # ---- 型付き入口: X ------------------------------------------------------

    def search_posts(
        self,
        query: str,
        *,
        count: int = 20,
        search_type: str = "top",
        deadline_s: int = _DEFAULT_DEADLINE_S,
        request_id: str = "apify",
        user_email: str = "",
    ) -> tuple[list[XPost], float]:
        """X投稿検索（第一候補 scraper_one → 0件/失敗で data-slayer 縮退）。

        戻り値: (正規化済み投稿, 概算コストUSD)。
        """
        # data-slayer は maxItems を無視し maxPages（1ページ ~20件）で件数を決めるため、
        # maxPages も渡して要求件数を反映する（maxItems は他 actor 互換のため残置・無害）。
        _fb_pages = max(1, (count + 19) // 20)
        chain: list[tuple[str, dict[str, Any]]] = [
            (ACTOR_X_SEARCH, {"query": query, "resultsCount": count, "searchType": search_type}),
            (ACTOR_X_SEARCH_FALLBACK, {"query": query, "maxItems": count, "maxPages": _fb_pages}),
        ]
        total_cost = 0.0
        last_err: ApifyError | None = None
        # deadline_s はチェーン全体（第一候補＋フォールバック）の壁時計予算。各 actor に
        # そのまま渡すと 1本目 timeout 後にフォールバックがさらに deadline_s 走れて合計が
        # 倍になり MCP 300s 天井を破る（self-review HIGH 指摘）。経過分を差し引いて配る。
        started = time.monotonic()
        for actor_id, run_input in chain:
            budget = max(10, int(deadline_s - (time.monotonic() - started)))
            try:
                res = self.run_actor_sync(
                    actor_id,
                    run_input,
                    max_items=count,
                    deadline_s=budget,
                    request_id=request_id,
                    user_email=user_email,
                )
            except ApifyError as e:
                if str(e).startswith("APIFY_TIER") or "BUDGET" in str(e):
                    raise
                logger.warning(
                    "apify_x_search_fallback",
                    request_id=request_id,
                    actor=actor_id,
                    error=str(e)[:120],
                )
                last_err = e
                continue
            total_cost += res.estimated_cost_usd
            posts = [p for p in (_parse_x_item(it, actor_id) for it in res.items) if p]
            if posts:
                return posts, total_cost
        if last_err is not None and total_cost == 0.0:
            raise last_err
        return [], total_cost

    def search_posts_period(
        self,
        terms: list[str],
        *,
        start: str,
        end: str,
        minimum_favorites: int = 0,
        max_items: int = 100,
        deadline_s: int = _DEFAULT_DEADLINE_S,
        request_id: str = "apify",
        user_email: str = "",
    ) -> tuple[list[XPost], float]:
        """期間指定のX投稿検索（apidojo/tweet-scraper。start/end は YYYY-MM-DD）。"""
        run_input: dict[str, Any] = {
            "searchTerms": terms,
            "start": start,
            "end": end,
            "maxItems": max_items,
            "sort": "Latest",
        }
        if minimum_favorites > 0:
            run_input["minimumFavorites"] = minimum_favorites
        res = self.run_actor_sync(
            ACTOR_X_PERIOD,
            run_input,
            max_items=max_items,
            deadline_s=deadline_s,
            request_id=request_id,
            user_email=user_email,
        )
        posts = [p for p in (_parse_x_item(it, ACTOR_X_PERIOD) for it in res.items) if p]
        return posts, res.estimated_cost_usd

    def verify_posts(
        self,
        urls: list[str],
        *,
        deadline_s: int = _DEFAULT_DEADLINE_S,
        request_id: str = "apify",
        user_email: str = "",
    ) -> tuple[dict[str, XPost | None], float]:
        """投稿URL群を xtracto で一括実在検証する。

        戻り値: (url → 検証済みXPost（取得不可は None＝呼び側が「要再確認」化）, コスト)。
        引用投稿の実在検証は必須要件（作文防止）。黙って捨てず None を返す。
        """
        if not urls:
            return {}, 0.0
        res = self.run_actor_sync(
            ACTOR_X_VERIFY,
            {"tweets": urls},
            max_items=len(urls),
            deadline_s=deadline_s,
            request_id=request_id,
            user_email=user_email,
        )
        by_id: dict[str, XPost] = {}
        for it in res.items:
            p = _parse_x_item(it, ACTOR_X_VERIFY)
            if p and p.post_id:
                by_id[p.post_id] = p
        out: dict[str, XPost | None] = {}
        for u in urls:
            m = re.search(r"/status(?:es)?/(\d+)", u)
            out[u] = by_id.get(m.group(1)) if m else None
        return out, res.estimated_cost_usd

    # ---- 型付き入口: Instagram ----------------------------------------------

    def ig_search(
        self,
        keyword: str,
        *,
        limit: int = 50,
        surface: str = "search",
        deadline_s: int = _DEFAULT_DEADLINE_S,
        request_id: str = "apify",
        user_email: str = "",
    ) -> tuple[list[IgPost], float]:
        """IG検索面の取得。surface=search（キーワード検索面）| hashtag（タグ面）。

        hashtag は instagramScraper.ts フォールバックで社内実績のある経路。
        search が日本語KWで空振りする場合は呼び側が surface="hashtag" に切替える。
        """
        if surface == "hashtag":
            actor_id = ACTOR_IG_HASHTAG
            tag = keyword.lstrip("#").replace(" ", "")
            run_input: dict[str, Any] = {"hashtags": [tag], "resultsLimit": limit}
        else:
            actor_id = ACTOR_IG_SEARCH
            run_input = {"search": keyword, "searchType": "hashtag", "searchLimit": limit}
        res = self.run_actor_sync(
            actor_id,
            run_input,
            max_items=limit,
            deadline_s=deadline_s,
            request_id=request_id,
            user_email=user_email,
        )
        posts: list[IgPost] = []
        for it in res.items:
            # instagram-search-scraper はネスト（topPosts/latestPosts）で返す形式がある
            nested = it.get("topPosts") or it.get("latestPosts") or it.get("posts")
            if isinstance(nested, list):
                for sub in nested:
                    if isinstance(sub, dict):
                        p = _parse_ig_item(sub, actor_id)
                        if p:
                            posts.append(p)
                continue
            p = _parse_ig_item(it, actor_id)
            if p:
                posts.append(p)
        return posts[: limit * 2], res.estimated_cost_usd

    # ---- 型付き入口: TikTok コメント（フォールバック用） ----------------------

    def tiktok_comments(
        self,
        video_url: str,
        *,
        max_comments: int = 200,
        deadline_s: int = _DEFAULT_DEADLINE_S,
        request_id: str = "apify",
        user_email: str = "",
    ) -> tuple[list[dict[str, Any]], float]:
        """TikTok動画コメント取得（clockworks）。chromium一次経路の縮退先。

        戻り値 items: [{text, likes, author}, ...]（adapters.tiktok_scraper.TikTokComment 互換）。
        """
        res = self.run_actor_sync(
            ACTOR_TIKTOK_COMMENTS,
            {"postURLs": [video_url], "commentsPerPost": max_comments},
            max_items=max_comments,
            deadline_s=deadline_s,
            request_id=request_id,
            user_email=user_email,
        )
        comments: list[dict[str, Any]] = []
        for it in res.items:
            text = str(_first(it, "text", "comment", default="")).strip()
            if not text:
                continue
            comments.append(
                {
                    "text": text,
                    "likes": _as_int(_first(it, "diggCount", "likesCount", "likes", default=0)),
                    "author": str(_first(it, "uniqueId", "username", "author", default="")).lstrip(
                        "@"
                    ),
                }
            )
        return comments, res.estimated_cost_usd


__all__ = [
    "ACTOR_IG_HASHTAG",
    "ACTOR_IG_SEARCH",
    "ACTOR_TIKTOK_COMMENTS",
    "ACTOR_X_PERIOD",
    "ACTOR_X_SEARCH",
    "ACTOR_X_SEARCH_FALLBACK",
    "ACTOR_X_VERIFY",
    "ApifyClient",
    "ApifyError",
    "ApifyRunResult",
    "IgPost",
    "XPost",
]
