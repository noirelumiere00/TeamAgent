"""usage_events への記録（管理画面の一次データ・1リクエスト1行）。

dispatch の出口で1リクエスト分のメタ（skill / cost / latency / status / user）を
``usage_events`` に追記する。設計上の要件:
- **ユーザ処理を止めない**: 記録の失敗はログのみで握り潰す（監視データの欠落は許容）。
- **イベントループを塞がない**: 同期 DB 書込は ``run_in_executor`` でワーカースレッドへ逃がす。
- **二重書込に安全**: ``ON CONFLICT (request_id) DO NOTHING``（リトライ/再入で重複しない）。
- **本文/PII は原則持ち込まない**: ユーザー裁定済みの query_text のみ例外として
  最大 2000 文字を保存する。回答・トークン等は列に入れない。

書込ロール: ``teamagent_app``（migration 0007 で usage_events に INSERT のみ許可）。
``app.user_role`` は立てない＝admin でない＝SELECT 不可（書くだけ）。
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


_VALID_STATUS = frozenset({"ok", "error", "queue_full", "timeout"})


@dataclass
class UsageTrace:
    """dispatch がコストを出口（ハンドラの記録点）へ運ぶための mutable トレース。

    skill はハンドラ側が detect_skill から確実に取れるので、ここでは LLM コストだけを運ぶ。
    queue_full/timeout で dispatch が走らなかった場合は cost_usd=0 のまま（正しい）。
    """

    cost_usd: float = 0.0


@dataclass(frozen=True)
class UsageEvent:
    """usage_events 1行分（query_text のみ裁定済みの本文例外・最大2000文字）。"""

    request_id: str
    skill: str
    status: str = "ok"
    user_email: str | None = None
    user_id: str | None = None
    cost_usd: float = 0.0
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_code: str | None = None
    throttle_retries: int = 0
    query_chars: int | None = None
    query_text: str | None = None
    via: str | None = None


_INSERT_SQL = """
INSERT INTO usage_events
    (request_id, user_email, user_id, skill, cost_usd, latency_ms,
     input_tokens, output_tokens, status, error_code, throttle_retries,
     query_chars, query_text, via)
VALUES
    (%(request_id)s, %(user_email)s, %(user_id)s, %(skill)s, %(cost_usd)s, %(latency_ms)s,
     %(input_tokens)s, %(output_tokens)s, %(status)s, %(error_code)s, %(throttle_retries)s,
     %(query_chars)s, %(query_text)s, %(via)s)
ON CONFLICT (request_id) DO NOTHING
"""


class UsageRecorder:
    """usage_events への best-effort 記録器。Bot の各ハンドラ出口から使う。"""

    def __init__(
        self, pgvector: Any, *, app_role: str = "teamagent_app", enabled: bool = True
    ) -> None:
        self._pg = pgvector
        self._app_role = app_role
        self._enabled = enabled

    def write(self, event: UsageEvent) -> None:
        """同期 INSERT（テスト可能な実体）。例外は呼び出し側に伝播する。"""
        params = asdict(event)
        if params["status"] not in _VALID_STATUS:
            params["status"] = "ok"  # 未知 status は ok に倒す（CHECK 制約違反で全行落とさない）
        if params["query_text"] is not None:
            params["query_text"] = params["query_text"][:2000]
        with self._pg.connection(app_role=self._app_role) as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, params)

    async def record(self, event: UsageEvent) -> None:
        """best-effort 記録。ユーザ応答を投稿した**後**に呼ぶ想定。失敗は握り潰す。

        同期 DB 書込を executor に逃がしてイベントループを塞がない。応答は既に送信済みなので、
        ここでの数 ms はユーザ体感に影響しない。
        """
        if not self._enabled:
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.write, event)
        except Exception:
            # 監視データの欠落は許容。本文は出さない（request_id のみ）。
            logger.warning("usage_event_write_failed", request_id=event.request_id)


__all__ = ["UsageEvent", "UsageRecorder", "UsageTrace"]
