"""Hermes 学習係（hermes_runtime の POST /v1/learn）を呼ぶクライアント（DM 本人メモ v1・M5）。

- TLS 必須。Hermes の自己署名証明書を ``HERMES_TLS_CA_PEM`` でトラストアンカーとして渡し、
  ホスト名も検証する（ピン留めは本文を送った後でしか照合できないので採らない）
- ingress bearer は Authorization ヘッダだけに載せる。
  環境のプロキシ設定は読まない（trust_env=False）
- 再試行はしない（送る前の接続失敗に限り 1 回まで）。混雑（503）はそのジョブを捨てる
- 200 の応答は厳格に検査する。外れたら ``HermesLearnError("bad_response")``
- 例外文にもログにも、発話・メモ・bearer を入れない

入出力の上限は hermes_runtime/aico_hermes/schema.py と同じ値を複製している
（hermes_runtime は TeamAgent の wheel に入らないので import しない）。
一致は tests の契約テストで固定する。
"""

from __future__ import annotations

import json
import os
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import httpx
import structlog

logger = structlog.get_logger(__name__)

MAX_UTTERANCES: Final = 5
MAX_UTTERANCE_CHARS: Final = 800
MAX_ENTRY_CHARS: Final = 200
MAX_SNAPSHOT_ENTRIES: Final = 40
USER_CHAR_LIMIT: Final = 1375
MEMORY_CHAR_LIMIT: Final = 2200
ENTRY_DELIMITER: Final = "\n§\n"
HERMES_PORT: Final = 8790
MIN_BEARER_CHARS: Final = 32
_RESPONSE_KEYS: Final = frozenset({"job_id", "user", "memory", "dropped", "elapsed_s"})
_STATUS_CODES: Final = {400: "rejected", 401: "unauthorized", 413: "rejected", 415: "rejected"}


class HermesLearnError(RuntimeError):
    """学習呼び出しの失敗。code は固定の識別子（本文を含めない）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class LearnResponse:
    job_id: str
    user_entries: tuple[str, ...]
    memory_entries: tuple[str, ...]
    dropped: int


def _valid_entry(item: object) -> bool:
    return (
        isinstance(item, str)
        and bool(item.strip())
        and len(item) <= MAX_ENTRY_CHARS
        and item == item.strip()
        and "§" not in item
    )


def _entries_ok(value: object, limit: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_SNAPSHOT_ENTRIES
        and all(_valid_entry(v) for v in value)
        and len(ENTRY_DELIMITER.join(value)) <= limit
    )


def validate_service_url(url: str, *, port: int = HERMES_PORT) -> str:
    """https・決まったポート（本番 8790）・userinfo/query/fragment なしだけを通す。"""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.port != port
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise HermesLearnError("bad_service_url")
    return f"https://{parts.hostname}:{port}"


class HermesLearnClient:
    def __init__(
        self,
        *,
        service_url: str,
        bearer: str,
        ca_pem: str,
        http: httpx.Client | None = None,
        expected_port: int = HERMES_PORT,
    ) -> None:
        if len(bearer) < MIN_BEARER_CHARS:
            raise HermesLearnError("bad_bearer")
        self._base = validate_service_url(service_url, port=expected_port)
        self._bearer = bearer
        if http is None:
            ctx = ssl.create_default_context(cadata=ca_pem)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.check_hostname = True
            http = httpx.Client(
                verify=ctx,
                timeout=httpx.Timeout(connect=3.0, read=115.0, write=5.0, pool=3.0),
                follow_redirects=False,
                trust_env=False,
            )
        self._http = http

    def __repr__(self) -> str:  # bearer を出さない
        return f"HermesLearnClient(base={self._base})"

    @classmethod
    def from_env(cls) -> HermesLearnClient | None:
        """設定が揃っていなければ None（学習を無効にする）。"""
        url = os.environ.get("HERMES_SERVICE_URL", "").strip()
        bearer = os.environ.get("TEAMAGENT_HERMES_INGRESS_BEARER", "").strip()
        ca_pem = os.environ.get("HERMES_TLS_CA_PEM", "").strip()
        if not (url and bearer and ca_pem):
            return None
        try:
            return cls(service_url=url, bearer=bearer, ca_pem=ca_pem)
        except (HermesLearnError, ssl.SSLError, ValueError) as exc:
            code = exc.code if isinstance(exc, HermesLearnError) else "bad_tls_config"
            logger.error("hermes_learn_client_disabled", code=code)
            return None

    def learn(
        self,
        *,
        job_id: str,
        user_entries: Sequence[str],
        memory_entries: Sequence[str],
        utterances: Sequence[str],
    ) -> LearnResponse:
        if not 1 <= len(utterances) <= MAX_UTTERANCES or any(
            not isinstance(u, str) or not u.strip() or len(u) > MAX_UTTERANCE_CHARS
            for u in utterances
        ):
            raise HermesLearnError("bad_request")
        if not _entries_ok(list(user_entries), USER_CHAR_LIMIT) or not _entries_ok(
            list(memory_entries), MEMORY_CHAR_LIMIT
        ):
            raise HermesLearnError("bad_request")
        body = json.dumps(
            {
                "job_id": job_id,
                "snapshot": {"user": list(user_entries), "memory": list(memory_entries)},
                "utterances": list(utterances),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._bearer}",
            "Content-Type": "application/json",
        }
        response = self._post(body, headers)
        return self._parse(response, job_id)

    def _post(self, body: bytes, headers: dict[str, str]) -> httpx.Response:
        for attempt in (1, 2):
            try:
                return self._http.post(f"{self._base}/v1/learn", content=body, headers=headers)
            except httpx.ConnectError:
                # 送る前の接続失敗だけ 1 回やり直す
                # （送った後の失敗はやり直さない＝二重学習を避ける）
                if attempt == 2:
                    raise HermesLearnError("connect_failed") from None
            except httpx.TimeoutException:
                raise HermesLearnError("timeout") from None
            except httpx.HTTPError as exc:
                raise HermesLearnError(f"http_{type(exc).__name__.lower()}"[:40]) from None
        raise HermesLearnError("connect_failed")  # pragma: no cover

    @staticmethod
    def _parse(response: httpx.Response, job_id: str) -> LearnResponse:
        status = response.status_code
        if status == 503:
            raise HermesLearnError("busy")
        if status in _STATUS_CODES:
            raise HermesLearnError(_STATUS_CODES[status])
        if status >= 500:
            raise HermesLearnError("hermes_failed")
        if status != 200:
            raise HermesLearnError("unexpected_status")
        try:
            data = response.json()
        except ValueError:
            raise HermesLearnError("bad_response") from None
        if not isinstance(data, dict) or set(data) != _RESPONSE_KEYS:
            raise HermesLearnError("bad_response")
        if data["job_id"] != job_id:
            raise HermesLearnError("bad_response")
        if not _entries_ok(data["user"], USER_CHAR_LIMIT) or not _entries_ok(
            data["memory"], MEMORY_CHAR_LIMIT
        ):
            raise HermesLearnError("bad_response")
        dropped = data["dropped"]
        if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped < 0:
            raise HermesLearnError("bad_response")
        return LearnResponse(
            job_id=job_id,
            user_entries=tuple(data["user"]),
            memory_entries=tuple(data["memory"]),
            dropped=dropped,
        )
