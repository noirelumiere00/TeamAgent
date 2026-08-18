"""管理画面の read-only 集計クエリ（usage_events / runtime_metrics / oauth_tokens）。

すべて ``PgVectorClient.connection(app_role='teamagent_dashboard', user_role='admin')``
経由で SELECT する（migration 0007/0008 の RLS が admin GUC のみ可視を担保、
dashboard ロールは SELECT のみ・暗号化列は GRANT 対象外）。本文は 2026-08-13 の
ユーザー裁定による ``usage_events.query_text`` だけを管理者向け質問フィードで
取得し、他の本文・復号情報は扱わない。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 作業束（Courant 式）: MCP tool 名 → 4 束の決定論マッピング
# ---------------------------------------------------------------------------
# usage_events.skill には mcp_gateway.server.dispatch_tool が **tool 名をそのまま**
# 書く（server.py の ``_record_usage(skill=name)``）。したがってここのキーは MCP tool 名
# ＝ Skill クラスの ``name`` ClassVar と 1:1 で対応する。
#
# 分類は「LLM に推測させない」= 定数表のみ。表に無い tool は必ず「その他」へ落ちる
# （fail-open）。新ツールを足しても画面は壊れず、件数は必ずどこかの束に入る。
WORK_TYPE_INVESTIGATE = "調べる"
WORK_TYPE_CREATE = "作る"
WORK_TYPE_ORGANIZE = "整える"
WORK_TYPE_ASSIST = "秘書"
WORK_TYPE_OTHER = "その他"

# 表示順（横棒バーの並び）。「その他」は常に最後。
WORK_TYPE_ORDER: tuple[str, ...] = (
    WORK_TYPE_INVESTIGATE,
    WORK_TYPE_CREATE,
    WORK_TYPE_ORGANIZE,
    WORK_TYPE_ASSIST,
    WORK_TYPE_OTHER,
)

# (a) 指示で確定している中核マッピング。ここは仕様そのもので、勝手に増減させない。
_WORK_TYPE_TOOLS: dict[str, tuple[str, ...]] = {
    WORK_TYPE_INVESTIGATE: (
        "search",
        "web_research",
        "x_voice_search",
        "x_needs_mining",
        "search_surface_check",
        "tiktok_search",
        "tiktok_comment_mining",
        "video_algorithm",
        "video_analysis",
    ),
    WORK_TYPE_CREATE: (
        "proposal_draft",
        "proposal_review",
        "proposal_builder_submit",
        "proposal_builder_status",
        "mail_draft",
        "mail_reply",
        "video_capture",
    ),
    WORK_TYPE_ORGANIZE: (
        "slack_summary",
        "attachment_assist",
        "clientkarte",
        "knowledge_deliver",
        "knowledge_search_url",
    ),
    WORK_TYPE_ASSIST: (
        "mail_summary",
        "mail_followup",
        "mail_to_internal_context",
        "calendar_event",
        "calendar_freebusy",
        "schedule_propose",
        "morning_digest",
        "oauth_connect",
    ),
}

# (b) (a) に載っていない **factory 登録済みツール**の割り当て。中核の兄弟に当たるもの
# （同じ束の別入口・非同期版・status 版）だけをここへ置く。(a) と分けているのは
# 「仕様で決まった分」と「実装側で補った分」を後から見分けられるようにするため。
#   - tiktok_acquire / x_buzz_measure とその *_status: 収集・計測＝調べる
#   - recommend: 過去提案のベクトル近傍提示＝調べる
#   - proposal_builder / proposal_deck / proposal_campaign: 成果物生成＝作る
#   - video_approval: 納品動画の一次FB。proposal_review と同型の「レビュー」＝作る
#   - operation_log: Slack 会話を CRM 転記用に構造化＝整える
#   - mail_constraints / workspace_search: 本人の受信箱・予定を引く秘書業務＝秘書
_SIBLING_WORK_TYPE_TOOLS: dict[str, tuple[str, ...]] = {
    WORK_TYPE_INVESTIGATE: (
        "tiktok_acquire",
        "tiktok_acquire_status",
        "x_buzz_measure",
        "x_buzz_measure_status",
        "recommend",
    ),
    WORK_TYPE_CREATE: (
        "proposal_builder",
        "proposal_deck",
        "proposal_campaign",
        "video_approval",
    ),
    WORK_TYPE_ORGANIZE: ("operation_log",),
    WORK_TYPE_ASSIST: (
        "mail_constraints",
        "workspace_search",
    ),
}


def _build_work_type_index() -> dict[str, str]:
    """tool → 束 の逆引きを組む。重複定義は起動時に落とす（表の事故を早期検出）。"""
    index: dict[str, str] = {}
    for table in (_WORK_TYPE_TOOLS, _SIBLING_WORK_TYPE_TOOLS):
        for work_type, tools in table.items():
            for tool in tools:
                if tool in index:  # pragma: no cover - 定数表の重複はレビューで弾く
                    raise ValueError(f"work type mapping duplicated: {tool}")
                index[tool] = work_type
    return index


WORK_TYPE_BY_TOOL: dict[str, str] = _build_work_type_index()


def work_type_of(tool: str | None) -> str:
    """tool 名を作業束へ落とす。未知・空は「その他」（fail-open・例外を投げない）。"""
    if not tool:
        return WORK_TYPE_OTHER
    return WORK_TYPE_BY_TOOL.get(str(tool), WORK_TYPE_OTHER)


def _select_conn(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    """dict-row の注入済み接続で SELECT する。"""
    with conn.cursor() as cur:
        cur.execute(sql, params if params is not None else [])
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def kpis(conn: Any) -> dict[str, Any]:
    """今日・直近7日の件数、利用者数、コスト、エラー数を返す。

    日付の境界は JST 基準。
    """
    rows = _select_conn(
        conn,
        """
        SELECT
            COUNT(*) AS total_events,
            COUNT(*) FILTER (
                WHERE (occurred_at AT TIME ZONE 'Asia/Tokyo')::date
                      = (NOW() AT TIME ZONE 'Asia/Tokyo')::date
            ) AS today_requests,
            COUNT(DISTINCT COALESCE(user_email, user_id, '(unknown)')) FILTER (
                WHERE (occurred_at AT TIME ZONE 'Asia/Tokyo')::date
                      = (NOW() AT TIME ZONE 'Asia/Tokyo')::date
            ) AS today_users,
            COALESCE(SUM(cost_usd) FILTER (
                WHERE (occurred_at AT TIME ZONE 'Asia/Tokyo')::date
                      = (NOW() AT TIME ZONE 'Asia/Tokyo')::date
            ), 0) AS today_cost_usd,
            COUNT(*) FILTER (
                WHERE (occurred_at AT TIME ZONE 'Asia/Tokyo')::date
                      = (NOW() AT TIME ZONE 'Asia/Tokyo')::date
                  AND status <> 'ok'
            ) AS today_errors,
            COUNT(*) FILTER (
                WHERE occurred_at >= NOW() - INTERVAL '7 days'
            ) AS seven_day_requests,
            COUNT(DISTINCT COALESCE(user_email, user_id, '(unknown)')) FILTER (
                WHERE occurred_at >= NOW() - INTERVAL '7 days'
            ) AS seven_day_users,
            COALESCE(SUM(cost_usd) FILTER (
                WHERE occurred_at >= NOW() - INTERVAL '7 days'
            ), 0) AS seven_day_cost_usd,
            COUNT(*) FILTER (
                WHERE occurred_at >= NOW() - INTERVAL '7 days'
                  AND status <> 'ok'
            ) AS seven_day_errors
        FROM usage_events
        """,
    )
    row = rows[0] if rows else {}
    today_users = int(row.get("today_users", 0) or 0)
    return {
        "total_events": int(row.get("total_events", 0) or 0),
        "today_requests": int(row.get("today_requests", 0) or 0),
        "today_users": today_users,
        # 既存 DashboardQueries.kpis の利用側にも合わせる別名。
        "active_users": today_users,
        "today_cost_usd": float(row.get("today_cost_usd", 0) or 0),
        "today_errors": int(row.get("today_errors", 0) or 0),
        "seven_day_requests": int(row.get("seven_day_requests", 0) or 0),
        "seven_day_users": int(row.get("seven_day_users", 0) or 0),
        "seven_day_cost_usd": float(row.get("seven_day_cost_usd", 0) or 0),
        "seven_day_errors": int(row.get("seven_day_errors", 0) or 0),
    }


def daily_series(conn: Any, days: int = 30) -> list[dict[str, Any]]:
    """日次の件数とコスト（JST）。"""
    rows = _select_conn(
        conn,
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
            "day": str(row["day"]),
            "requests": int(row["requests"]),
            "cost_usd": float(row["cost_usd"] or 0),
        }
        for row in rows
    ]


def skill_breakdown(conn: Any, days: int = 7) -> list[dict[str, Any]]:
    """skill 別の件数・コスト・p50/p95 レイテンシ。"""
    rows = _select_conn(
        conn,
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
            "skill": str(row["skill"]),
            "n": int(row["n"]),
            "cost_usd": float(row["cost_usd"] or 0),
            "p50_ms": int(row["p50_ms"]) if row["p50_ms"] is not None else None,
            "p95_ms": int(row["p95_ms"]) if row["p95_ms"] is not None else None,
        }
        for row in rows
    ]


def work_type_breakdown(conn: Any, days: int = 7) -> list[dict[str, Any]]:
    """作業束（調べる/作る/整える/秘書/その他）別の件数・コスト・割合。

    集計は skill_breakdown と同型（同じ WHERE・同じ GROUP BY skill）。tool→束 の対応だけ
    Python 側の定数表で畳む＝SQL に分類ロジックを持ち込まない（表を直せば SQL 不変で追随）。
    中核4束は 0 件でも必ず行として返す（バーが消えて「無かったこと」にならないように）。
    「その他」は 1 件以上あるときだけ出す（未知ツールが出た事実を可視化する）。
    """
    rows = _select_conn(
        conn,
        """
        SELECT skill,
               COUNT(*) AS n,
               ROUND(SUM(cost_usd), 4) AS cost_usd
        FROM usage_events
        WHERE occurred_at >= NOW() - (%s || ' days')::interval
        GROUP BY skill
        """,
        [str(int(days))],
    )
    requests: dict[str, int] = {work_type: 0 for work_type in WORK_TYPE_ORDER}
    costs: dict[str, float] = {work_type: 0.0 for work_type in WORK_TYPE_ORDER}
    for row in rows:
        work_type = work_type_of(row.get("skill"))
        requests[work_type] += int(row["n"] or 0)
        costs[work_type] += float(row["cost_usd"] or 0)
    total = sum(requests.values())
    out: list[dict[str, Any]] = []
    for work_type in WORK_TYPE_ORDER:
        n = requests[work_type]
        if work_type == WORK_TYPE_OTHER and n == 0:
            continue
        out.append(
            {
                "work_type": work_type,
                "requests": n,
                "cost_usd": round(costs[work_type], 4),
                "share": (n / total) if total else 0.0,
            }
        )
    return out


def user_breakdown(conn: Any, *, days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
    """ユーザ別の件数・コスト（コスト降順）。"""
    rows = _select_conn(
        conn,
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
            "who": str(row["who"]),
            "requests": int(row["requests"]),
            "cost_usd": float(row["cost_usd"] or 0),
        }
        for row in rows
    ]


def error_list(conn: Any, limit: int = 50) -> list[dict[str, Any]]:
    """直近のエラー/拒否を返す（本文は含めない）。"""
    rows = _select_conn(
        conn,
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
            "occurred_at": row["occurred_at"],
            "request_id": str(row["request_id"]),
            "skill": str(row["skill"]),
            "status": str(row["status"]),
            "error_code": row["error_code"],
            "who": str(row["who"]),
        }
        for row in rows
    ]


def recent_questions(
    conn: Any, limit: int = 200, *, who: str | None = None
) -> list[dict[str, Any]]:
    """質問本文を含む直近の usage_events を管理ページ向けに返す。

    ``who`` を渡すと利用者ドリルダウン（``/admin?user=<email>``）。値は**必ず**
    プレースホルダで渡す（文字列連結しない）。user_breakdown が返す
    ``COALESCE(user_email, user_id, '(unknown)')`` と同じ式で、大小文字を無視して
    突き合わせる（migration 0010 の email 大小文字非依存と同じ流儀）。
    """
    params: dict[str, Any] = {"limit": int(limit)}
    who_clause = ""
    if who:
        params["who"] = str(who)
        who_clause = "  AND lower(COALESCE(user_email, user_id, '(unknown)')) = lower(%(who)s)\n"
    rows = _select_conn(
        conn,
        """
        SELECT occurred_at,
               COALESCE(user_email, user_id, '(unknown)') AS who,
               skill, query_text, status, latency_ms, cost_usd
        FROM usage_events
        WHERE query_text IS NOT NULL
        """
        + who_clause
        + """
        ORDER BY occurred_at DESC
        LIMIT %(limit)s
        """,
        params,
    )
    return [
        {
            "occurred_at": row["occurred_at"],
            "who": str(row["who"]),
            "skill": str(row["skill"]),
            "query_text": str(row["query_text"]),
            "status": str(row["status"]),
            "latency_ms": int(row["latency_ms"]) if row["latency_ms"] is not None else None,
            "cost_usd": float(row["cost_usd"] or 0),
        }
        for row in rows
    ]


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


__all__ = [
    "WORK_TYPE_ASSIST",
    "WORK_TYPE_BY_TOOL",
    "WORK_TYPE_CREATE",
    "WORK_TYPE_INVESTIGATE",
    "WORK_TYPE_ORDER",
    "WORK_TYPE_ORGANIZE",
    "WORK_TYPE_OTHER",
    "DashboardQueries",
    "daily_series",
    "error_list",
    "kpis",
    "recent_questions",
    "skill_breakdown",
    "user_breakdown",
    "work_type_breakdown",
    "work_type_of",
]
