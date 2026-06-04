"""管理画面の read-only 集計クエリ（usage_events / runtime_metrics / oauth_tokens）。

すべて ``PgVectorClient.connection(app_role='teamagent_dashboard', user_role='admin')`` 経由で
SELECT する（migration 0007/0008 の RLS が admin GUC のみ可視を担保、dashboard ロールは
SELECT のみ・暗号化列は GRANT 対象外）。**復号・本文取得は一切行わない**。
"""

from __future__ import annotations

from typing import Any


class DashboardQueries:
    """管理画面が読む集計クエリ群。pgvector は read-only ロールで接続する。"""

    def __init__(
        self, pgvector: Any, *, app_role: str = "teamagent_dashboard", user_role: str = "admin"
    ) -> None:
        self._pg = pgvector
        self._app_role = app_role
        self._user_role = user_role

    def _select(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        with self._pg.connection(app_role=self._app_role, user_role=self._user_role) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params if params is not None else [])
                rows = cur.fetchall()
        return [dict(r) for r in rows]

    # ---- KPI 帯 ----------------------------------------------------
    def kpis(self) -> dict[str, Any]:
        """今日の件数・アクティブ人数・当月累計コスト・直近24hエラー率。"""
        today = self._select(
            """
            SELECT COUNT(*) AS requests,
                   COUNT(DISTINCT user_email) AS active_users
            FROM usage_events
            WHERE (occurred_at AT TIME ZONE 'Asia/Tokyo')::date
                  = (NOW() AT TIME ZONE 'Asia/Tokyo')::date
            """
        )
        mtd = self._select(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost FROM usage_events "
            "WHERE occurred_at >= date_trunc('month', NOW())"
        )
        err = self._select(
            """
            SELECT COUNT(*) FILTER (WHERE status <> 'ok')::float
                   / NULLIF(COUNT(*), 0) AS rate,
                   COUNT(*) AS total
            FROM usage_events
            WHERE occurred_at >= NOW() - INTERVAL '24 hours'
            """
        )
        t = today[0] if today else {}
        return {
            "today_requests": int(t.get("requests", 0) or 0),
            "active_users": int(t.get("active_users", 0) or 0),
            "month_cost_usd": float(mtd[0]["cost"]) if mtd else 0.0,
            "error_rate_24h": float(err[0]["rate"])
            if (err and err[0]["rate"] is not None)
            else 0.0,
            "requests_24h": int(err[0]["total"]) if err else 0,
        }

    # ---- 時系列（折れ線）------------------------------------------
    def daily_series(self, days: int = 30) -> list[dict[str, Any]]:
        """日次の件数とコスト（JST）。"""
        rows = self._select(
            """
            SELECT (occurred_at AT TIME ZONE 'Asia/Tokyo')::date AS day,
                   COUNT(*) AS requests,
                   ROUND(SUM(cost_usd), 4) AS cost_usd
            FROM usage_events
            WHERE occurred_at >= NOW() - (%s || ' days')::interval
            GROUP BY day ORDER BY day
            """,
            [str(int(days))],
        )
        return [
            {
                "day": str(r["day"]),
                "requests": int(r["requests"]),
                "cost_usd": float(r["cost_usd"] or 0),
            }
            for r in rows
        ]

    # ---- skill 別 --------------------------------------------------
    def skill_breakdown(self, days: int = 7) -> list[dict[str, Any]]:
        """skill 別の件数・コスト・p50/p95 レイテンシ（直近 days 日）。"""
        rows = self._select(
            """
            SELECT skill,
                   COUNT(*) AS n,
                   ROUND(SUM(cost_usd), 4) AS cost_usd,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)
                       FILTER (WHERE latency_ms IS NOT NULL) AS p50_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                       FILTER (WHERE latency_ms IS NOT NULL) AS p95_ms
            FROM usage_events
            WHERE occurred_at >= NOW() - (%s || ' days')::interval
            GROUP BY skill ORDER BY n DESC
            """,
            [str(int(days))],
        )
        return [
            {
                "skill": str(r["skill"]),
                "n": int(r["n"]),
                "cost_usd": float(r["cost_usd"] or 0),
                "p50_ms": int(r["p50_ms"]) if r["p50_ms"] is not None else None,
                "p95_ms": int(r["p95_ms"]) if r["p95_ms"] is not None else None,
            }
            for r in rows
        ]

    # ---- ユーザ別 --------------------------------------------------
    def user_breakdown(self, *, days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
        """ユーザ別の件数・コスト（直近 days 日・コスト降順）。"""
        rows = self._select(
            """
            SELECT COALESCE(user_email, user_id, '(unknown)') AS who,
                   COUNT(*) AS requests,
                   ROUND(SUM(cost_usd), 4) AS cost_usd
            FROM usage_events
            WHERE occurred_at >= NOW() - (%s || ' days')::interval
            GROUP BY who ORDER BY cost_usd DESC NULLS LAST LIMIT %s
            """,
            [str(int(days)), int(limit)],
        )
        return [
            {
                "who": str(r["who"]),
                "requests": int(r["requests"]),
                "cost_usd": float(r["cost_usd"] or 0),
            }
            for r in rows
        ]

    # ---- エラー一覧 ------------------------------------------------
    def error_list(self, limit: int = 50) -> list[dict[str, Any]]:
        """直近のエラー/拒否（本文なし・request_id 付き）。"""
        rows = self._select(
            """
            SELECT occurred_at, request_id, skill, status, error_code,
                   COALESCE(user_email, user_id, '(unknown)') AS who
            FROM usage_events
            WHERE status <> 'ok'
            ORDER BY occurred_at DESC LIMIT %s
            """,
            [int(limit)],
        )
        return [
            {
                "occurred_at": str(r["occurred_at"]),
                "request_id": str(r["request_id"]),
                "skill": str(r["skill"]),
                "status": str(r["status"]),
                "error_code": r["error_code"],
                "who": str(r["who"]),
            }
            for r in rows
        ]

    # ---- 混雑（runtime_metrics 最新 + 直近ピーク）-----------------
    def runtime_now(self) -> dict[str, Any] | None:
        """最新の runtime_metrics 1行（現在の同時実行・プール）。無ければ None。"""
        rows = self._select("SELECT * FROM runtime_metrics ORDER BY captured_at DESC LIMIT 1")
        return rows[0] if rows else None

    def runtime_peaks(self, hours: int = 1) -> dict[str, Any]:
        """直近 hours のピーク並列・キュー・拒否（区間差分）。"""
        rows = self._select(
            """
            SELECT MAX(gate_peak_in_flight) AS peak_parallel,
                   MAX(gate_peak_waiting) AS peak_queue,
                   MAX(gate_rejected_queue_full) - MIN(gate_rejected_queue_full) AS queue_full,
                   MAX(gate_rejected_timeout) - MIN(gate_rejected_timeout) AS timeouts
            FROM runtime_metrics
            WHERE captured_at >= NOW() - (%s || ' hours')::interval
            """,
            [str(int(hours))],
        )
        r = rows[0] if rows else {}
        return {
            "peak_parallel": int(r.get("peak_parallel") or 0),
            "peak_queue": int(r.get("peak_queue") or 0),
            "queue_full": int(r.get("queue_full") or 0),
            "timeouts": int(r.get("timeouts") or 0),
        }

    # ---- Workspace 連携状況 ---------------------------------------
    def oauth_status(self) -> list[dict[str, Any]]:
        """認可済みユーザと scope 充足（暗号化列は読まない）。"""
        rows = self._select(
            """
            SELECT user_email, created_at,
                   cardinality(scopes) AS n_scopes
            FROM oauth_tokens ORDER BY created_at
            """
        )
        return [
            {
                "user_email": str(r["user_email"]),
                "created_at": str(r["created_at"]),
                "n_scopes": int(r["n_scopes"] or 0),
            }
            for r in rows
        ]


__all__ = ["DashboardQueries"]
