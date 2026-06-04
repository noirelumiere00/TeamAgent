"""管理画面の HTML レンダリング（依存ゼロ・Python 生成）。

jinja2 を要求しない。値は html.escape で必ずエスケープ（XSS 防御・admin専用でも多層防御）。
チャートは Chart.js（CDN）にデータを JSON で渡して描画する。
動的な値は事前に変数へ整形し、テンプレートでは単純展開のみ（f-string のクォート入れ子を避ける）。
"""

from __future__ import annotations

import html
import json
from typing import Any

_CHART_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4"

_STYLE = """
:root { --bg:#0f1420; --card:#1a2233; --ink:#e8edf7; --muted:#93a1bd; --accent:#4f8cff;
        --good:#36c08a; --warn:#f5b14c; --bad:#f9667a; --line:#283450; }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif; }
header { display:flex; align-items:center; justify-content:space-between;
  padding:14px 22px; border-bottom:1px solid var(--line); }
header .brand { font-weight:700; letter-spacing:.04em; }
header nav a { color:var(--muted); text-decoration:none; margin-left:18px; }
header nav a:hover { color:var(--ink); }
.who { color:var(--muted); font-size:13px; }
main { padding:22px; max-width:1100px; margin:0 auto; }
.kpis { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:22px; }
.kpi { background:var(--card); border:1px solid var(--line);
  border-radius:12px; padding:16px 18px; }
.kpi .label { color:var(--muted); font-size:12px; }
.kpi .value { font-size:30px; font-weight:700; margin-top:6px; }
.kpi .sub { color:var(--muted); font-size:11px; margin-top:4px; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:16px 18px; margin-bottom:18px; }
.card h2 { font-size:14px; margin:0 0 12px; color:var(--muted); font-weight:600; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:600; }
td.num,th.num { text-align:right; font-variant-numeric:tabular-nums; }
.badge { padding:2px 8px; border-radius:999px; font-size:11px; }
.b-error{ background:rgba(249,102,122,.18); color:var(--bad); }
.b-queue_full{ background:rgba(245,177,76,.18); color:var(--warn); }
.b-timeout{ background:rgba(245,177,76,.18); color:var(--warn); }
.b-ok{ background:rgba(54,192,138,.18); color:var(--good); }
.muted{ color:var(--muted); } .right{ text-align:right; }
a.btn{ display:inline-block; background:var(--accent); color:#fff; text-decoration:none;
  padding:10px 18px; border-radius:9px; font-weight:600; }
.note{ color:var(--muted); font-size:12px; margin-top:14px; line-height:1.6; }
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _money(v: Any, digits: int = 3) -> str:
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "0"


def _page(title: str, body: str, *, email: str | None = None, scripts: str = "") -> str:
    nav = ""
    who = ""
    if email:
        nav = (
            '<nav><a href="/">ダッシュボード</a><a href="/errors">エラー</a>'
            '<a href="/logout">ログアウト</a></nav>'
        )
        who = '<span class="who">' + _e(email) + "</span>"
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>" + _e(title) + "</title><style>" + _STYLE + "</style></head><body>"
        '<header><div class="brand">🤖 TeamAgent 管理画面</div>' + nav + who + "</header>"
        "<main>" + body + "</main>" + scripts + "</body></html>"
    )


def render_login(
    *, client_id: str | None, error: str | None = None, dev_bypass: bool = False
) -> str:
    """ログイン画面。client_id があれば Google サインインボタン、無ければ案内。"""
    err = ('<p class="note" style="color:#f9667a">' + _e(error) + "</p>") if error else ""
    if dev_bypass:
        body = (
            '<div class="card"><h2>ログイン（開発モード）</h2>'
            "<p>DASHBOARD_DEV_BYPASS=1 のため認証はスキップされています（ローカル開発専用）。</p>"
            '<p><a class="btn" href="/">ダッシュボードを開く</a></p>'
            '<p class="note">本番では DASHBOARD_DEV_BYPASS を外し '
            "Google ログインを有効化してください。</p>" + err + "</div>"
        )
        return _page("ログイン", body)
    if not client_id:
        body = (
            '<div class="card"><h2>ログイン未設定</h2>'
            "<p class='note'>DASHBOARD_GOOGLE_CLIENT_ID が未設定です。ウェブアプリ型 OAuth "
            "クライアントを作成し env に設定してください（runbook 参照）。一時的に "
            "DASHBOARD_DEV_BYPASS=1 でローカル閲覧も可能です。</p>" + err + "</div>"
        )
        return _page("ログイン", body)
    scripts = (
        '<script src="https://accounts.google.com/gsi/client" async></script>'
        "<script>function onCred(r){"
        "document.getElementById('credential').value=r.credential;"
        "document.getElementById('idform').submit();}</script>"
    )
    body = (
        '<div class="card"><h2>管理者ログイン</h2>'
        '<p class="note">許可されたアカウント（オーナー/管理者）のみアクセスできます。</p>'
        '<div id="g_id_onload" data-client_id="' + _e(client_id) + '" data-callback="onCred"></div>'
        '<div class="g_id_signin" data-type="standard" data-size="large"></div>'
        '<form id="idform" method="post" action="/auth/verify">'
        '<input type="hidden" id="credential" name="credential" value=""></form>' + err + "</div>"
    )
    return _page("ログイン", body, scripts=scripts)


def _kpi_block(k: dict[str, Any]) -> str:
    err_pct = _e(_money(k.get("error_rate_24h", 0) * 100, 1))
    return (
        '<div class="kpis">'
        '<div class="kpi"><div class="label">今日のリクエスト</div><div class="value">'
        + _e(k.get("today_requests", 0))
        + "</div></div>"
        '<div class="kpi"><div class="label">アクティブ利用者(今日)</div><div class="value">'
        + _e(k.get("active_users", 0))
        + "</div></div>"
        '<div class="kpi"><div class="label">当月コスト(推算)</div><div class="value">$'
        + _e(_money(k.get("month_cost_usd", 0), 2))
        + '</div><div class="sub">Bedrock/Gemini 合算・概算</div></div>'
        '<div class="kpi"><div class="label">エラー率(24h)</div><div class="value">'
        + err_pct
        + '%</div><div class="sub">'
        + _e(k.get("requests_24h", 0))
        + " 件中</div></div></div>"
    )


def _congestion_block(rt: dict[str, Any], pk: dict[str, Any]) -> str:
    in_flight = _e(rt.get("gate_in_flight", "-")) + " / " + _e(rt.get("gate_concurrency", "-"))
    pool = _e(rt.get("pool_in_use", "-")) + " / " + _e(rt.get("pool_max_size", "-"))
    return (
        '<div class="card"><h2>同時実行 / 混雑（直近1時間のピーク）</h2><table>'
        '<tr><th>現在の並列</th><th>現在の待ち</th><th class="num">ピーク並列</th>'
        '<th class="num">ピーク待ち</th><th class="num">キュー満杯拒否</th>'
        '<th class="num">待ちタイムアウト</th></tr><tr>'
        "<td>" + in_flight + "</td><td>" + _e(rt.get("gate_waiting", "-")) + "</td>"
        '<td class="num">' + _e(pk.get("peak_parallel", 0)) + "</td>"
        '<td class="num">' + _e(pk.get("peak_queue", 0)) + "</td>"
        '<td class="num">' + _e(pk.get("queue_full", 0)) + "</td>"
        '<td class="num">' + _e(pk.get("timeouts", 0)) + "</td></tr></table>"
        '<table style="margin-top:10px"><tr><th>DB接続(使用/最大)</th><th class="num">アイドル</th>'
        '<th class="num">接続待ちtimeout</th><th class="num">reset失敗</th></tr><tr>'
        "<td>" + pool + "</td>"
        '<td class="num">' + _e(rt.get("pool_idle", "-")) + "</td>"
        '<td class="num">' + _e(rt.get("pool_timeouts", "-")) + "</td>"
        '<td class="num">' + _e(rt.get("pool_reset_failures", "-")) + "</td></tr></table></div>"
    )


def _skill_block(skills: list[dict[str, Any]]) -> str:
    rows = []
    for s in skills:
        p50 = s.get("p50_ms")
        p95 = s.get("p95_ms")
        rows.append(
            "<tr><td>" + _e(s.get("skill")) + '</td><td class="num">' + _e(s.get("n", 0)) + "</td>"
            '<td class="num">$' + _e(_money(s.get("cost_usd", 0))) + "</td>"
            '<td class="num">' + _e(p50 if p50 is not None else "-") + "</td>"
            '<td class="num">' + _e(p95 if p95 is not None else "-") + "</td></tr>"
        )
    return (
        '<div class="card"><h2>Skill 別（直近7日）</h2><table>'
        '<tr><th>Skill</th><th class="num">件数</th><th class="num">コスト</th>'
        '<th class="num">p50(ms)</th><th class="num">p95(ms)</th></tr>'
        + "".join(rows)
        + "</table></div>"
    )


def _user_block(users: list[dict[str, Any]]) -> str:
    rows = [
        "<tr><td>" + _e(u.get("who")) + '</td><td class="num">' + _e(u.get("requests", 0)) + "</td>"
        '<td class="num">$' + _e(_money(u.get("cost_usd", 0))) + "</td></tr>"
        for u in users
    ]
    return (
        '<div class="card"><h2>ユーザ別（直近30日・コスト順）</h2><table>'
        '<tr><th>ユーザ</th><th class="num">件数</th><th class="num">コスト</th></tr>'
        + "".join(rows)
        + "</table></div>"
    )


def _oauth_block(oauth: list[dict[str, Any]]) -> str:
    rows = [
        "<tr><td>"
        + _e(o.get("user_email"))
        + '</td><td class="num">'
        + _e(o.get("n_scopes", 0))
        + '/7</td><td class="muted">'
        + _e(str(o.get("created_at", ""))[:10])
        + "</td></tr>"
        for o in oauth
    ]
    return (
        '<div class="card"><h2>Workspace 連携状況（' + _e(len(oauth)) + "名 認可済み）</h2><table>"
        '<tr><th>ユーザ</th><th class="num">scope</th><th>連携日</th></tr>'
        + "".join(rows)
        + "</table>"
        '<p class="note">表示は連携の有無と scope 数のみ。トークン(暗号化)は画面・DBロールとも'
        "復号・取得しません。</p></div>"
    )


def render_dashboard(data: dict[str, Any]) -> str:
    """ダッシュボード本体。data は app.py が queries から組み立てた dict。"""
    email = data.get("email")
    daily = data.get("daily", [])
    chart_html = (
        '<div class="card"><h2>日次リクエスト数 / コスト（30日）</h2>'
        '<canvas id="dailyChart" height="90"></canvas></div>'
    )
    scripts = (
        '<script src="' + _CHART_CDN + '"></script><script>'
        "const daily=" + json.dumps(daily) + ";"
        "const ctx=document.getElementById('dailyChart');"
        "if(ctx)new Chart(ctx,{type:'line',data:{labels:daily.map(d=>d.day),datasets:["
        "{label:'リクエスト数',data:daily.map(d=>d.requests),borderColor:'#4f8cff',"
        "backgroundColor:'rgba(79,140,255,.15)',yAxisID:'y',tension:.3,fill:true},"
        "{label:'コスト($)',data:daily.map(d=>d.cost_usd),borderColor:'#36c08a',"
        "yAxisID:'y1',tension:.3}]},options:{responsive:true,"
        "plugins:{legend:{labels:{color:'#93a1bd'}}},scales:{"
        "x:{ticks:{color:'#93a1bd'},grid:{color:'#283450'}},"
        "y:{position:'left',ticks:{color:'#93a1bd'},grid:{color:'#283450'}},"
        "y1:{position:'right',ticks:{color:'#36c08a'},grid:{display:false}}}}});</script>"
    )
    body = (
        _kpi_block(data.get("kpis", {}))
        + chart_html
        + _congestion_block(data.get("runtime_now") or {}, data.get("runtime_peaks", {}))
        + '<div class="grid2">'
        + _skill_block(data.get("skills", []))
        + _user_block(data.get("users", []))
        + "</div>"
        + _oauth_block(data.get("oauth", []))
        + '<p class="note">コストは料金表ベースの推算です。請求の確定値は AWS Cost Explorer / '
        "Google 請求と突合してください。エラー詳細は Sentry（request_id で照合）を参照。</p>"
    )
    return _page("ダッシュボード", body, email=email, scripts=scripts)


def render_errors(rows: list[dict[str, Any]], *, email: str | None = None) -> str:
    """エラー/拒否の一覧（本文なし・request_id 付き）。"""
    trs = []
    for r in rows:
        status = str(r.get("status", ""))
        trs.append(
            '<tr><td class="muted">'
            + _e(str(r.get("occurred_at", ""))[:19])
            + '</td><td><span class="badge b-'
            + _e(status)
            + '">'
            + _e(status)
            + "</span></td><td>"
            + _e(r.get("skill"))
            + "</td><td>"
            + _e(r.get("error_code"))
            + "</td><td>"
            + _e(r.get("who"))
            + '</td><td class="muted">'
            + _e(r.get("request_id"))
            + "</td></tr>"
        )
    body = (
        '<div class="card"><h2>直近のエラー / 拒否（'
        + _e(len(rows))
        + "件）</h2><table><tr><th>時刻</th><th>種別</th><th>Skill</th><th>code</th>"
        "<th>ユーザ</th><th>request_id</th></tr>" + "".join(trs) + "</table>"
        '<p class="note">本文・回答は保存していません。詳細スタックは Sentry を request_id で'
        "検索してください。</p></div>"
    )
    return _page("エラー一覧", body, email=email)


__all__ = ["render_dashboard", "render_errors", "render_login"]
