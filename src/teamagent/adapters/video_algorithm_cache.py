"""TTL 付き video_algorithm 結果キャッシュと処理中リース（Gemini 二重課金防止）。

``video_algorithm`` は同期 MCP timeout（300 秒）後もワーカースレッド内で完走するため、
同じ依頼の再発話が二重課金になり得る。既存 ``AnalysisCache`` と同じ S3 bucket / feature
flag を使い、課金済みの構造化出力を短時間だけ再利用する。完了前の競合窓は S3 の
If-None-Match / If-Match 条件書込によるリースで閉じる（必要 IAM は既存 Get/Put のみ）。

キーには、結果または生成成果物を変え得る全入力と prompt/model version を含める。
TTL は payload 自体に必須で持たせ、期限切れ・TTL 欠落・不正 payload は必ず miss に倒す。
動画・フレームの大きな data URI は既存 AnalysisCache の「動画 bytes 即破棄」境界に合わせ、
結果キャッシュへ保存しない。提案スライド再生成に必要な軽量 cover_data_uri は保持する。
結果 read 自体は既存 AnalysisCache と同じく miss に倒す。一方、処理中リースを確立・更新
できない場合は呼出側が課金処理を fail-closed にし、キャッシュ障害時の二重課金を避ける。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_BASE_PREFIX = "analysis-cache/"
_DEFAULT_TTL_SECONDS = 3600
_DEFAULT_LEASE_SECONDS = 1800
_RESULT_SCHEMA_VERSION = 3
_LEASE_SCHEMA_VERSION = 1

CacheStage = Literal["paid_core", "complete"]


def _env_ttl_seconds() -> int:
    """結果キャッシュ TTL（既定1時間、不正値は既定へ）。

    レポート署名 URL（7日）より十分短くし、検索面の時変性も取り込み過ぎない。
    """

    raw = os.environ.get("VIDEO_ALGORITHM_CACHE_TTL_SECONDS", "").strip()
    try:
        return max(1, int(raw)) if raw else _DEFAULT_TTL_SECONDS
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _env_prefix() -> str:
    base = os.environ.get("ANALYSIS_CACHE_PREFIX") or _DEFAULT_BASE_PREFIX
    return f"{base.rstrip('/')}/video-algorithm/"


def _env_lease_seconds() -> int:
    """処理中リース（既定30分）。OpenClaw の300秒 timeout より必ず長くする。"""

    raw = os.environ.get("VIDEO_ALGORITHM_CACHE_LEASE_SECONDS", "").strip()
    try:
        return max(301, int(raw)) if raw else _DEFAULT_LEASE_SECONDS
    except ValueError:
        return _DEFAULT_LEASE_SECONDS


@dataclass(frozen=True)
class CachedVideoAlgorithmResult:
    """キャッシュから復元した schema 非依存の出力 payload。"""

    output: dict[str, Any]
    stage: CacheStage
    lease_generation: int


@dataclass(frozen=True)
class VideoAlgorithmCacheLease:
    """同一 cache key の課金処理を1実行へ直列化する所有トークン。"""

    object_key: str
    token: str
    generation: int


class VideoAlgorithmCacheLeaseHeldError(RuntimeError):
    """同じ入力の課金処理が別リクエストで進行中。"""


class VideoAlgorithmCacheLeaseLostError(RuntimeError):
    """token/generation/期限/CASから取得済みリースの喪失が確定した。"""


class VideoAlgorithmCacheLeaseUnavailableError(RuntimeError):
    """一過性I/O障害のため、取得済みリースの所有権を現在確認できない。"""


class VideoAlgorithmResultCache:
    """S3 上の TTL 付き結果キャッシュ（ANALYSIS_CACHE_ENABLED=1 のときのみ有効）。"""

    def __init__(
        self,
        *,
        bucket: str | None = None,
        prefix: str | None = None,
        client: Any | None = None,
        ttl_seconds: int | None = None,
        lease_seconds: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._bucket = bucket or os.environ.get("ANALYSIS_CACHE_BUCKET") or ""
        self._prefix = prefix or _env_prefix()
        self._client = client
        self._ttl_seconds = max(1, ttl_seconds if ttl_seconds is not None else _env_ttl_seconds())
        self._lease_seconds = max(
            301,
            lease_seconds if lease_seconds is not None else _env_lease_seconds(),
        )
        self._clock = clock

    @staticmethod
    def enabled() -> bool:
        """Opt in explicitly; sharing ANALYSIS_CACHE_ENABLED left no way out.

        The live tfvars set use_analysis_cache=true, so reusing that flag would
        activate the lease machinery on the next image rebuild with no switch of
        its own — turning it off would need a terraform apply and would also
        disable the older, fail-open analysis cache. A dedicated flag that
        defaults to off keeps the two independent.
        """
        return os.environ.get("VIDEO_ALGORITHM_RESULT_CACHE_ENABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }

    def _ensure_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3")
        return self._client

    @staticmethod
    def cache_key(
        *,
        query: str,
        max_videos: int,
        prompt_version: str,
        model_id: str,
        board_size: int,
        outputs: Sequence[str],
        kw_set: Sequence[str] | None,
        client_name: str | None = None,
        acquire_job_id: str | None = None,
        search_volume: int | None = None,
        requester: str = "",
    ) -> str:
        """全結果決定要素を canonical JSON 化した sha256 キー。

        要求された必須要素（query/max_videos/prompt_version/model_id/board_size/
        outputs/kw_set）に加え、分析プロンプトや取得元を変える任意入力も含める。
        ``requester`` は acquire_job_id の所有者検査をキャッシュで迂回させないための境界。
        """

        raw = json.dumps(
            {
                "query": query,
                "max_videos": max_videos,
                "prompt_version": prompt_version,
                "model_id": model_id,
                "board_size": board_size,
                "outputs": list(outputs),
                "kw_set": list(kw_set or []),
                "client_name": client_name or "",
                "acquire_job_id": acquire_job_id or "",
                "search_volume": search_volume,
                "requester": requester.strip().lower() if acquire_job_id else "",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str, *, request_id: str) -> CachedVideoAlgorithmResult | None:
        if not self._bucket:
            logger.warning("video_algorithm_cache_bucket_missing", request_id=request_id)
            return None
        start = time.perf_counter()
        try:
            resp = self._ensure_client().get_object(
                Bucket=self._bucket,
                Key=f"{self._prefix}{key}.json",
            )
            payload = json.loads(resp["Body"].read().decode("utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != _RESULT_SCHEMA_VERSION
            ):
                return None
            expires_at = payload.get("expires_at_epoch_s")
            # TTL は必須。bool は int の subclass なので明示除外する。
            if (
                not isinstance(expires_at, (int, float))
                or isinstance(expires_at, bool)
                or float(expires_at) <= self._clock()
            ):
                logger.info("video_algorithm_cache_expired", request_id=request_id)
                return None
            output = payload.get("output")
            stage = payload.get("stage")
            lease_generation = payload.get("lease_generation")
            if (
                not isinstance(output, dict)
                or stage not in ("paid_core", "complete")
                or not isinstance(lease_generation, int)
                or isinstance(lease_generation, bool)
                or lease_generation < 1
            ):
                return None
            # 書込時にも除外するが、不正 payload に対して読出側でも境界を固定する。
            output = self._sanitize_output(output)
            logger.info(
                "video_algorithm_cache_hit",
                request_id=request_id,
                stage=stage,
                latency_ms=int((time.perf_counter() - start) * 1000),
            )
            return CachedVideoAlgorithmResult(
                output=output,
                stage=stage,
                lease_generation=lease_generation,
            )
        except Exception as e:
            code = ""
            resp_meta = getattr(e, "response", None)
            if isinstance(resp_meta, dict):
                code = str((resp_meta.get("Error") or {}).get("Code") or "")
            if code in ("NoSuchKey", "404", "AccessDenied", "NoSuchBucket") or type(e).__name__ in (
                "NoSuchKey",
            ):
                return None
            logger.warning(
                "video_algorithm_cache_get_failed",
                request_id=request_id,
                error=type(e).__name__,
                code=code,
            )
            return None

    @staticmethod
    def _sanitize_output(output: Mapping[str, Any]) -> dict[str, Any]:
        """JSON deep-copyし、ローカルパスと大きな動画・フレームdata URIを除外する。"""

        output_payload = json.loads(
            json.dumps(dict(output), ensure_ascii=False, separators=(",", ":"))
        )
        if not isinstance(output_payload, dict):
            raise TypeError("video_algorithm cache output must be an object")
        output_payload["report_html_path"] = None
        videos = output_payload.get("videos")
        if isinstance(videos, list):
            for video in videos:
                if not isinstance(video, dict):
                    continue
                video["video_data_uri"] = ""
                frames = video.get("frames")
                if isinstance(frames, list):
                    for frame in frames:
                        if isinstance(frame, dict):
                            frame["data_uri"] = ""
        return output_payload

    def put(
        self,
        key: str,
        *,
        output: Mapping[str, Any],
        stage: CacheStage,
        lease: VideoAlgorithmCacheLease,
        request_id: str,
    ) -> bool:
        """lease generation 付きCASで結果を保存する。

        新世代ownerの結果を旧ownerが上書きすることと、同一世代の
        ``complete -> paid_core`` downgrade を拒否する。
        """

        if not self._bucket:
            logger.warning("video_algorithm_cache_bucket_missing", request_id=request_id)
            return False
        try:
            self.assert_lease_owned(lease, request_id=request_id)
            client = self._ensure_client()
            now = int(self._clock())
            output_payload = self._sanitize_output(output)
            body = json.dumps(
                {
                    "schema_version": _RESULT_SCHEMA_VERSION,
                    "stage": stage,
                    "lease_generation": lease.generation,
                    "lease_token": lease.token,
                    "created_at_epoch_s": now,
                    "expires_at_epoch_s": now + self._ttl_seconds,
                    "output": output_payload,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")

            object_key = f"{self._prefix}{key}.json"
            etag: str | None = None
            try:
                existing = client.get_object(Bucket=self._bucket, Key=object_key)
            except Exception as error:
                code = self._error_code(error)
                # A missing key surfaces as 403 AccessDenied rather than 404 because
                # the task role has no s3:ListBucket on this prefix — the same reason
                # analysis_cache documents. Re-raising it aborted every cold key here,
                # which sits after the paid Gemini call and before the report is
                # written: the run was billed and produced nothing.
                if (
                    code not in {"NoSuchKey", "404", "AccessDenied", "NoSuchBucket"}
                    and type(error).__name__ != "NoSuchKey"
                ):
                    raise
            else:
                etag = str(existing.get("ETag") or "")
                if not etag:
                    raise VideoAlgorithmCacheLeaseLostError("result ETag missing")
                try:
                    payload = json.loads(existing["Body"].read().decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                    payload = {}
                existing_generation = (
                    payload.get("lease_generation") if isinstance(payload, dict) else 0
                )
                if not isinstance(existing_generation, int) or isinstance(
                    existing_generation, bool
                ):
                    existing_generation = 0
                existing_token = payload.get("lease_token") if isinstance(payload, dict) else None
                existing_stage = payload.get("stage") if isinstance(payload, dict) else None
                if existing_generation > lease.generation or (
                    existing_generation == lease.generation
                    and existing_generation > 0
                    and existing_token != lease.token
                ):
                    raise VideoAlgorithmCacheLeaseLostError("newer result owner exists")
                if (
                    existing_generation == lease.generation
                    and existing_stage == "complete"
                    and stage == "paid_core"
                ):
                    logger.info(
                        "video_algorithm_cache_downgrade_skipped",
                        request_id=request_id,
                        lease_generation=lease.generation,
                    )
                    return True

            condition = {"IfMatch": etag} if etag is not None else {"IfNoneMatch": "*"}
            client.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=body,
                ContentType="application/json; charset=utf-8",
                CacheControl="no-store",
                **condition,
            )
            logger.info(
                "video_algorithm_cache_put",
                request_id=request_id,
                stage=stage,
                lease_generation=lease.generation,
                ttl_seconds=self._ttl_seconds,
            )
            return True
        except (VideoAlgorithmCacheLeaseLostError, VideoAlgorithmCacheLeaseUnavailableError):
            raise
        except Exception as error:
            if self._is_precondition_failed(error):
                raise VideoAlgorithmCacheLeaseLostError("result CAS lost") from error
            logger.warning(
                "video_algorithm_cache_put_failed",
                request_id=request_id,
                error=type(error).__name__,
                code=self._error_code(error),
            )
            raise VideoAlgorithmCacheLeaseUnavailableError(
                f"result cache write unavailable: {type(error).__name__}"
            ) from error

    @staticmethod
    def _error_code(error: Exception) -> str:
        response = getattr(error, "response", None)
        if not isinstance(response, dict):
            return ""
        return str((response.get("Error") or {}).get("Code") or "")

    @classmethod
    def _is_precondition_failed(cls, error: Exception) -> bool:
        return cls._error_code(error) in {"PreconditionFailed", "412"}

    @staticmethod
    def _lease_body(
        *,
        token: str,
        generation: int,
        expires_at: int,
        released: bool = False,
    ) -> bytes:
        return json.dumps(
            {
                "schema_version": _LEASE_SCHEMA_VERSION,
                "token": token,
                "generation": generation,
                "expires_at_epoch_s": expires_at,
                "released": released,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def acquire_lease(
        self,
        key: str,
        *,
        request_id: str,
    ) -> VideoAlgorithmCacheLease | None:
        """同一 key の処理中リースを条件書込で取得する。

        ``None`` は S3 障害/設定不足で排他状態が不明。競合は明示例外にし、呼出側がいずれも
        課金処理を fail-closed にできるよう区別する。期限切れ・解放済み lease は ETag CAS で
        generation を進めて takeover する。
        """

        if not self._bucket:
            logger.warning("video_algorithm_cache_bucket_missing", request_id=request_id)
            return None
        try:
            client = self._ensure_client()
        except Exception as error:
            logger.warning(
                "video_algorithm_cache_lease_unavailable",
                request_id=request_id,
                error=type(error).__name__,
                code=self._error_code(error),
            )
            return None
        object_key = f"{self._prefix}{key}.lease.json"
        token = uuid.uuid4().hex
        generation = 1
        expires_at = int(self._clock()) + self._lease_seconds
        body = self._lease_body(
            token=token,
            generation=generation,
            expires_at=expires_at,
        )
        kwargs = {
            "Bucket": self._bucket,
            "Key": object_key,
            "Body": body,
            "ContentType": "application/json; charset=utf-8",
            "CacheControl": "no-store",
        }
        try:
            client.put_object(**kwargs, IfNoneMatch="*")
            logger.info(
                "video_algorithm_cache_lease_acquired",
                request_id=request_id,
                lease_generation=generation,
                lease_seconds=self._lease_seconds,
            )
            return VideoAlgorithmCacheLease(
                object_key=object_key,
                token=token,
                generation=generation,
            )
        except Exception as error:
            if not self._is_precondition_failed(error):
                logger.warning(
                    "video_algorithm_cache_lease_unavailable",
                    request_id=request_id,
                    error=type(error).__name__,
                    code=self._error_code(error),
                )
                return None

        try:
            existing = client.get_object(Bucket=self._bucket, Key=object_key)
            etag = str(existing.get("ETag") or "")
            payload = json.loads(existing["Body"].read().decode("utf-8"))
            existing_generation = payload.get("generation") if isinstance(payload, dict) else 0
            if (
                not isinstance(existing_generation, int)
                or isinstance(existing_generation, bool)
                or existing_generation < 0
            ):
                existing_generation = 0
            active = (
                isinstance(payload, dict)
                and payload.get("schema_version") == _LEASE_SCHEMA_VERSION
                and payload.get("released") is not True
                and isinstance(payload.get("token"), str)
                and existing_generation >= 1
                and isinstance(payload.get("expires_at_epoch_s"), (int, float))
                and not isinstance(payload.get("expires_at_epoch_s"), bool)
                and float(payload["expires_at_epoch_s"]) > self._clock()
            )
            if active or not etag:
                raise VideoAlgorithmCacheLeaseHeldError(key)
            generation = existing_generation + 1
            takeover_body = self._lease_body(
                token=token,
                generation=generation,
                expires_at=expires_at,
            )
            client.put_object(**{**kwargs, "Body": takeover_body}, IfMatch=etag)
            logger.info(
                "video_algorithm_cache_lease_taken_over",
                request_id=request_id,
                lease_generation=generation,
                lease_seconds=self._lease_seconds,
            )
            return VideoAlgorithmCacheLease(
                object_key=object_key,
                token=token,
                generation=generation,
            )
        except VideoAlgorithmCacheLeaseHeldError:
            raise
        except Exception as error:
            if self._is_precondition_failed(error):
                raise VideoAlgorithmCacheLeaseHeldError(key) from error
            logger.warning(
                "video_algorithm_cache_lease_takeover_failed",
                request_id=request_id,
                error=type(error).__name__,
                code=self._error_code(error),
            )
            return None

    @property
    def lease_heartbeat_seconds(self) -> float:
        """lease TTL の1/3以下、最大60秒で更新する。"""

        return max(1.0, min(60.0, self._lease_seconds / 3))

    @property
    def lease_retry_seconds(self) -> float:
        """一過性更新失敗の再試行間隔。最小TTLでも十分な安全余裕を残す。"""

        return max(0.25, min(5.0, self.lease_heartbeat_seconds / 4))

    def _load_owned_lease(
        self,
        lease: VideoAlgorithmCacheLease,
    ) -> tuple[Any, str]:
        try:
            client = self._ensure_client()
            existing = client.get_object(Bucket=self._bucket, Key=lease.object_key)
        except Exception as error:
            if self._error_code(error) in {"NoSuchKey", "404"} or type(error).__name__ == (
                "NoSuchKey"
            ):
                raise VideoAlgorithmCacheLeaseLostError("lease object no longer exists") from error
            raise VideoAlgorithmCacheLeaseUnavailableError(
                f"lease ownership read unavailable: {type(error).__name__}"
            ) from error
        try:
            payload = json.loads(existing["Body"].read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, AttributeError) as error:
            raise VideoAlgorithmCacheLeaseLostError("lease payload is invalid") from error
        etag = str(existing.get("ETag") or "")
        active = (
            isinstance(payload, dict)
            and payload.get("schema_version") == _LEASE_SCHEMA_VERSION
            and payload.get("token") == lease.token
            and payload.get("generation") == lease.generation
            and payload.get("released") is not True
            and isinstance(payload.get("expires_at_epoch_s"), (int, float))
            and not isinstance(payload.get("expires_at_epoch_s"), bool)
            and float(payload["expires_at_epoch_s"]) > self._clock()
            and bool(etag)
        )
        if not active:
            raise VideoAlgorithmCacheLeaseLostError("lease owner changed or expired")
        return client, etag

    def assert_lease_owned(
        self,
        lease: VideoAlgorithmCacheLease,
        *,
        request_id: str,
    ) -> None:
        """課金境界/結果commit前に token+generation+期限を再確認する。"""

        try:
            self._load_owned_lease(lease)
        except (VideoAlgorithmCacheLeaseLostError, VideoAlgorithmCacheLeaseUnavailableError):
            raise
        except Exception as error:
            raise VideoAlgorithmCacheLeaseUnavailableError(
                f"lease ownership check unavailable: {type(error).__name__}"
            ) from error
        logger.debug(
            "video_algorithm_cache_lease_owned",
            request_id=request_id,
            lease_generation=lease.generation,
        )

    def renew_lease(
        self,
        lease: VideoAlgorithmCacheLease,
        *,
        request_id: str,
    ) -> None:
        """所有権をETag CASで確認しながら期限を延長する。

        token/generation不一致と412は確定loss、それ以外のI/O障害はtransientとして
        呼出側がTTLの安全余裕内で再試行できるよう区別する。
        """

        try:
            client, etag = self._load_owned_lease(lease)
            client.put_object(
                Bucket=self._bucket,
                Key=lease.object_key,
                Body=self._lease_body(
                    token=lease.token,
                    generation=lease.generation,
                    expires_at=int(self._clock()) + self._lease_seconds,
                ),
                ContentType="application/json; charset=utf-8",
                CacheControl="no-store",
                IfMatch=etag,
            )
            logger.debug(
                "video_algorithm_cache_lease_renewed",
                request_id=request_id,
                lease_generation=lease.generation,
            )
        except (VideoAlgorithmCacheLeaseLostError, VideoAlgorithmCacheLeaseUnavailableError):
            raise
        except Exception as error:
            if self._is_precondition_failed(error):
                raise VideoAlgorithmCacheLeaseLostError("lease renewal CAS lost") from error
            raise VideoAlgorithmCacheLeaseUnavailableError(
                f"lease renewal unavailable: {type(error).__name__}"
            ) from error

    def release_lease(self, lease: VideoAlgorithmCacheLease, *, request_id: str) -> None:
        """所有中 lease を ETag CAS で解放する（DeleteObject 権限は不要）。"""

        if not self._bucket:
            return
        try:
            client = self._ensure_client()
            existing = client.get_object(Bucket=self._bucket, Key=lease.object_key)
            payload = json.loads(existing["Body"].read().decode("utf-8"))
            etag = str(existing.get("ETag") or "")
            if (
                not isinstance(payload, dict)
                or payload.get("token") != lease.token
                or payload.get("generation") != lease.generation
                or not etag
            ):
                return
            client.put_object(
                Bucket=self._bucket,
                Key=lease.object_key,
                Body=self._lease_body(
                    token=lease.token,
                    generation=lease.generation,
                    expires_at=int(self._clock()),
                    released=True,
                ),
                ContentType="application/json; charset=utf-8",
                CacheControl="no-store",
                IfMatch=etag,
            )
            logger.info("video_algorithm_cache_lease_released", request_id=request_id)
        except Exception as error:
            logger.warning(
                "video_algorithm_cache_lease_release_failed",
                request_id=request_id,
                error=type(error).__name__,
                code=self._error_code(error),
            )


__all__ = [
    "CachedVideoAlgorithmResult",
    "VideoAlgorithmCacheLease",
    "VideoAlgorithmCacheLeaseHeldError",
    "VideoAlgorithmCacheLeaseLostError",
    "VideoAlgorithmCacheLeaseUnavailableError",
    "VideoAlgorithmResultCache",
]
