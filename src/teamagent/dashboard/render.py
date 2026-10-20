"""管理画面の HTML レンダリング（依存ゼロ・Python 生成）。

jinja2 を要求しない。値は html.escape で必ずエスケープ（XSS 防御・admin専用でも多層防御）。
チャートは Chart.js（CDN）にデータを JSON で渡して描画する。
動的な値は事前に変数へ整形し、テンプレートでは単純展開のみ（f-string のクォート入れ子を避ける）。
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

_CHART_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4"
_JST = timezone(timedelta(hours=9), name="JST")

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
.question{ white-space:pre-wrap; overflow-wrap:anywhere; min-width:260px; max-width:520px; }
.bars{ display:flex; flex-direction:column; gap:10px; }
.bar-row{ display:grid; grid-template-columns:74px 1fr 118px; align-items:center; gap:10px;
  font-size:13px; }
.bar-track{ background:var(--bg); border:1px solid var(--line); border-radius:999px;
  height:12px; overflow:hidden; }
.bar-fill{ height:100%; background:var(--accent); border-radius:999px; }
.bar-value{ text-align:right; color:var(--muted); font-variant-numeric:tabular-nums; }
td a, .filter a{ color:var(--accent); text-decoration:none; }
td a:hover, .filter a:hover{ text-decoration:underline; }
.filter{ color:var(--muted); font-size:12px; margin:0 0 10px; }
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _money(v: Any, digits: int = 3) -> str:
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "0"


def _jst(v: Any) -> str:
    """timestamptz/datetime を JST の管理画面表示へ整形する。"""
    if v is None:
        return ""
    value = v
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)[:19]
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_JST).strftime("%Y-%m-%d %H:%M:%S")


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


def _pct(share: Any) -> float:
    """割合(0..1) を 0..100 の百分率へ。異常値は 0 に倒し、必ず 0..100 に丸め込む。"""
    try:
        value = float(share) * 100.0
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return max(0.0, min(100.0, value))


def _admin_user_href(who: Any) -> str:
    """/admin の利用者ドリルダウン URL（? 以降は percent-encode してから HTML escape）。"""
    return "/admin?user=" + _e(quote(str(who or ""), safe=""))


def _work_type_block(rows: list[dict[str, Any]], *, title: str) -> str:
    """作業束の横棒バー（外部ライブラリ非依存・CSS の幅指定だけで描く）。"""
    bars = []
    for row in rows:
        percent = _pct(row.get("share", 0))
        bars.append(
            '<div class="bar-row"><div>'
            + _e(row.get("work_type"))
            + '</div><div class="bar-track"><div class="bar-fill" style="width:'
            + f"{percent:.1f}"
            + '%"></div></div><div class="bar-value">'
            + f"{percent:.1f}"
            + "% / "
            + _e(row.get("requests", 0))
            + "件</div></div>"
        )
    inner = (
        '<div class="bars">' + "".join(bars) + "</div>"
        if bars
        else '<p class="note">まだ記録がありません。</p>'
    )
    return '<div class="card"><h2>' + _e(title) + "</h2>" + inner + "</div>"


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


def _user_block(users: list[dict[str, Any]], *, drilldown: bool = False) -> str:
    """ユーザ別内訳。``drilldown`` のときだけ /admin?user=... へのリンクにする。

    /（ダッシュボード本体）にはドリルダウン先が無いので既定は素のテキストのまま
    （既存の出力バイトを変えない）。
    """
    rows = []
    for u in users:
        who = _e(u.get("who"))
        cell = (
            ('<a href="' + _admin_user_href(u.get("who")) + '">' + who + "</a>")
            if drilldown
            else who
        )
        rows.append(
            "<tr><td>" + cell + '</td><td class="num">' + _e(u.get("requests", 0)) + "</td>"
            '<td class="num">$' + _e(_money(u.get("cost_usd", 0))) + "</td></tr>"
        )
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
        '<p class="note">質問文は裁定済みの利用状況記録でのみ保存します。'
        "回答本文は保存せず、"
        "詳細スタックは Sentry を request_id で検索してください。</p></div>"
    )
    return _page("エラー一覧", body, email=email)


def render_admin_nav(active: str) -> str:
    links = (
        ("usage", "/admin", "利用状況"),
        ("history", "/admin/history", "会話履歴"),
        ("memory", "/admin/memory", "本人メモ"),
    )
    return (
        '<aside class="sidebar" aria-label="Aico 管理画面"><div class="brand">'
        '<span class="brandmark" aria-hidden="true"><svg viewBox="0 0 28 28" fill="none">'
        '<path d="M7 20 13 7h3l6 13M10 15h9" stroke="white" stroke-width="3" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="22" cy="6" r="2" fill="#c7d2ff"/></svg></span>'
        '<span class="brand-word">Aico</span></div><p class="brand-sub">管理コンソール</p>'
        '<p class="nav-label">ワークスペース</p><nav aria-label="管理画面">'
        + "".join(
            f'<a href="{href}" aria-label="{label}"'
            + (' aria-current="page"' if key == active else "")
            + f'>{_admin_icon(key)}<span class="nav-text">{label}</span></a>'
            for key, href, label in links
        )
        + '</nav><div class="sidebar-foot"><span class="avatar" aria-hidden="true">A</span>'
        "<div>管理者<small>Aico ワークスペース</small></div></div></aside>"
    )


def render_admin_page(title: str, body: str, *, active: str, scripts: str = "") -> str:
    """利用状況・会話履歴・本人メモの共通レイアウト。"""
    label = {"usage": "利用状況", "history": "会話履歴", "memory": "本人メモ"}[active]
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{_e(title)} | Aico 管理画面</title><style>{_ADMIN_STYLE}</style></head><body>"
        '<a class="skip-link" href="#main">本文へ移動</a>'
        + render_admin_nav(active)
        + '<div class="workspace"><header class="topbar"><div class="breadcrumbs">'
        '<span>管理コンソール</span><span aria-hidden="true">/</span>'
        f"<strong>{_e(label)}</strong>"
        '</div></header><main id="main"'
        + (' class="admin-content"' if active != "usage" else "")
        + f">{body}</main></div>{scripts}</body></html>"
    )


def _history_href(user_id: Any) -> str:
    from teamagent.adapters.dm_history import valid_user_id

    return (
        "/admin/history?user=" + str(user_id)
        if isinstance(user_id, str) and valid_user_id(user_id)
        else "/admin/history"
    )


def _difference(value: int | None, previous: int | None) -> str:
    if value is None or previous is None:
        return "前期差を取得できません"
    return f"前期差 {value - previous:+,}（前期 {previous:,}）"


def _admin_time(value: Any) -> str:
    full = _jst(value)
    try:
        stamp = datetime.fromisoformat(full).replace(tzinfo=_JST)
    except ValueError:
        return f'<span class="when">{_e(full)}</span>'
    return (
        f'<time datetime="{stamp.isoformat()}" title="{_e(full)} JST">'
        f"{stamp.strftime('%m/%d %H:%M')}</time>"
    )


def _daily_svg(rows: list[dict[str, Any]], period: Any) -> str:
    values = {str(row["day"]): int(row["requests"]) for row in rows}
    day = period.start.date()
    last = (period.end - timedelta(microseconds=1)).date()
    series = []
    while day <= last:
        series.append((day.isoformat(), values.get(day.isoformat(), 0)))
        day += timedelta(days=1)
    if not series:
        return '<p class="note">この期間の記録はまだありません。</p>'
    peak = max(1, max(n for _, n in series))
    ceiling = ((peak + 4) // 5) * 5
    width = 300 / len(series)
    bars = []
    for tick in (0, ceiling // 2, ceiling):
        y = 155 - tick / ceiling * 120
        bars.append(
            f'<path d="M30 {y:.1f}H330"/><text x="24" y="{y + 4:.1f}" '
            f'text-anchor="end">{tick}</text>'
        )
    for i, (label, count) in enumerate(series):
        height = count / ceiling * 120
        x = 30 + (i + 0.5) * width
        cls = "daily-bar last" if i == len(series) - 1 else "daily-bar"
        bars.append(
            f'<rect class="{cls}" x="{x - width * 0.32:.1f}" y="{155 - height:.1f}" '
            f'width="{width * 0.64:.1f}" height="{height:.1f}" rx="3">'
            f"<title>{_e(label)}: {count}回</title></rect>"
        )
        if len(series) <= 8 or i in {0, len(series) // 2, len(series) - 1}:
            bars.append(
                f'<text x="{x:.1f}" y="{147 - height:.1f}" text-anchor="middle">{count}</text>'
                f'<text x="{x:.1f}" y="180" text-anchor="middle">'
                f"{_e(label[5:].replace('-', '/'))}</text>"
            )
    description = "、".join(f"{label}: {count}回" for label, count in series)
    return (
        '<svg class="daily-chart" viewBox="0 0 350 195" role="img" '
        'aria-labelledby="daily-title daily-desc"><title id="daily-ti'
        'tle">日別の作業回数</title>'
        f'<desc id="daily-desc">{_e(description)}</desc>'
        + "".join(bars)
        + '</svg><p class="chart-note">日別の作業回数・JST。'
        "期間の最初と最後の日は一部の時間のみの場合があります。</p>"
    )


def _admin_work_mix(rows: list[dict[str, Any]], *, title: str) -> str:
    segments = []
    items = []
    offset = 0.0
    for i, row in enumerate(rows):
        percent = _pct(row.get("share", 0))
        color = f"mix-color-{min(i, 4)}"
        segments.append(
            f'<rect class="{color}" x="{offset:.1f}" width="{percent:.1f}" height="15"/>'
        )
        offset += percent
        items.append(
            f'<div class="mix-row"><span class="mix-label"><i class="swatch {color}" '
            f'aria-hidden="true"></i>{_e(row["work_type"])}</span>'
            f'<strong class="num">{percent:.1f}%</strong>'
            f'<span class="num">{_e(row["requests"])}件</span></div>'
        )
    total = sum(int(row["requests"]) for row in rows)
    content = (
        '<svg class="stacked-bar" viewBox="0 0 100 15" preserveAspectRatio="none" '
        'aria-hidden="true">' + "".join(segments) + "</svg>" + "".join(items)
        if rows
        else '<p class="note">まだ記録がありません。</p>'
    )
    return (
        f'<section class="panel"><div class="panel-head"><div><h2>{_e(title)}</h2>'
        f"<p>全{total:,}回に占める割合</p></div></div>"
        f'<div class="work-mix">{content}</div></section>'
    )


_ADMIN_STYLE = """

:root {
color-scheme: light dark;
--bg: #f5f6fa; --surface: #fff; --surface-alt: #f8f9fc;
--ink: #20283e; --muted: #626c82; --line: #e4e8f0;
--blue: #405dde; --blue-soft: #edf1ff; --blue-line: #d7dffc;
--green: #26755a; --green-soft: #eaf5ef;
--amber: #946014; --amber-soft: #fff5e3;
--red: #b23c50; --red-soft: #fceef0;
--violet: #7350ac; --violet-soft: #f2edfa;
--nav: #182239; --nav-ink: #c2ccdf;
--bar: #a9b9f3; --shadow: 0 2px 5px #20283e03;
}
@media (prefers-color-scheme: dark) {
:root {
--bg: #111827; --surface: #1b2537; --surface-alt: #202b3f;
--ink: #edf1fa; --muted: #abb6cc; --line: #344056;
--blue: #a2b4ff; --blue-soft: #263451; --blue-line: #435785;
--green: #8cd5b1; --green-soft: #213c36;
--amber: #f1c278; --amber-soft: #403524;
--red: #ffabb9; --red-soft: #432c39;
--violet: #cbb0f3; --violet-soft: #352e49;
--nav: #0e1627; --nav-ink: #c2ccdf;
--bar: #637bbf; --shadow: none;
}
}
* { box-sizing: border-box; }
html { scroll-padding-top: 20px; }
body { margin: 0; font: 14px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI",
"Hiragino Kaku Gothic ProN", "Yu Gothic", Meiryo, sans-serif;
background: var(--bg); color: var(--ink); }
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }
button, summary { cursor: pointer; }
button { font: inherit; }
a, button, summary { -webkit-tap-highlight-color: transparent; }
:focus-visible { outline: 3px solid var(--blue); outline-offset: 4px; }
[hidden] { display: none !important; }
h1, h2, h3, p { margin: 0; }
h2 { font-size: 18px; font-weight: 700; letter-spacing: .015em; }
h3 { font-size: 15px; }
button, a, p, td, dd { overflow-wrap: anywhere; }
.icon { width: 20px; height: 20px; flex: 0 0 auto; fill: none; stroke: currentColor;
stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; vertical-align: middle; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; clip-path:
inset(50%); overflow: hidden; white-space: nowrap; }
.skip-link { position: fixed; top: -80px; left: 16px; z-index: 10; padding: 12px 20px;
background: var(--surface); border: 2px solid var(--blue); border-radius: 8px; }
.skip-link:focus { top: 12px; }
.sidebar { position: fixed; inset: 0 auto 0 0; width: 216px; background: var(--nav); color:
var(--nav-ink); display: flex; flex-direction: column; padding: 30px 18px 20px; }
.brand { display: flex; align-items: center; gap: 11px; padding: 0 12px; color: #fff; font-size:
28px; line-height: 1; font-weight: 750; letter-spacing: -.04em; }
.brandmark { display: grid; place-items: center; width: 34px; height: 36px; border-radius: 11px;
background: #5c75eb; }
.brandmark svg { width: 25px; height: 25px; }
.brand-sub { margin: 14px 0 35px 13px; color: #a8b5cd; font-size: 12px; letter-spacing: .08em; }
.nav-label { margin: 0 14px 10px; font-size: 12px; color: #94a4c2; }
.sidebar nav { display: grid; gap: 6px; }
.sidebar nav a { display: flex; align-items: center; gap: 12px; min-height: 46px; padding: 11px
14px; border-radius: 8px; color: var(--nav-ink); }
.sidebar nav a:hover { background: #253149; text-decoration: none; }
.sidebar nav a[aria-current] { background: #344971; color: #fff; font-weight: 650; box-shadow:
inset 3px 0 #9eb1ff; }
.sidebar :focus-visible { outline-color: #b5c3ff; }
.sidebar-foot { margin-top: auto; border-top: 1px solid #344056; padding: 20px 10px 0; display:
flex; align-items: center; gap: 11px; }
.sidebar-foot .avatar { background: #34415c; color: #e0e7f8; }
.sidebar-foot small { display: block; color: #a8b5cd; font-size: 12px; }
.workspace { margin-left: 216px; }
.topbar { height: 65px; border-bottom: 1px solid var(--line); background: var(--surface);
display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 0 36px;
}
.breadcrumbs { display: flex; align-items: center; gap: 12px; color: var(--muted); font-size:
13px; }
.breadcrumbs strong { color: var(--ink); font-weight: 500; }
.demo-tag { color: var(--muted); border: 1px solid var(--line); border-radius: 5px; padding: 3px
8px; font-size: 12px; white-space: nowrap; }
main { max-width: 1456px; padding: 32px 36px 36px; margin: 0 auto; overflow-wrap: anywhere; }
.page-heading { display: flex; align-items: center; justify-content: space-between; gap: 24px; }
h1 { font-size: 29px; line-height: 1.4; letter-spacing: .02em; font-weight: 750; }
.page-heading p { margin-top: 7px; color: var(--muted); }
.periods { display: flex; flex: 0 0 auto; gap: 3px; padding: 4px; border: 1px solid var(--line);
background: var(--surface); border-radius: 9px; }
.periods a { padding: 7px 17px; min-height: 38px; border-radius: 6px; color: var(--muted);
white-space: nowrap; }
.periods a:hover { background: var(--surface-alt); text-decoration: none; }
.periods a[aria-current] { background: var(--blue); color: var(--surface); font-weight: 650; }
.period-caption { margin: 20px 0 24px; display: flex; flex-wrap: wrap; align-items: center; gap:
6px 18px; font-size: 12px; color: var(--muted); }
.period-caption .selected { color: var(--ink); display: flex; align-items: center; gap: 8px;
font-size: 13px; }
.period-caption .icon { width: 16px; height: 16px; }
.kpis { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }
.kpi { padding: 20px 23px; border: 1px solid var(--line); border-radius: 12px; background:
var(--surface); box-shadow: var(--shadow); }
.kpi-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.kpi h2 { color: var(--muted); font-size: 14px; font-weight: 550; }
.kpi-icon { color: var(--muted); background: var(--surface-alt); border-radius: 8px; padding:
7px; line-height: 1; }
.kpi-value { margin-top: 7px; font-size: 43px; line-height: 1.25; font-weight: 700;
letter-spacing: -.035em; font-variant-numeric: tabular-nums; }
.kpi-value .unit { font-size: 14px; font-weight: 500; letter-spacing: 0; margin-left: 7px;
color: var(--muted); }
.kpi-bottom { margin-top: 12px; display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
font-size: 12px; color: var(--muted); }
.delta { display: inline-flex; gap: 4px; align-items: center; padding: 2px 7px; border-radius:
5px; background: var(--green-soft); color: var(--green); font-size: 12px; font-weight: 650; }
.kpi.activity { border-color: var(--blue-line); }
.activity .kpi-icon { color: var(--blue); background: var(--blue-soft); }
.attention .kpi-icon { color: var(--amber); background: var(--amber-soft); }
.attention .kpi-value { color: var(--amber); }
.attention .kpi-top a { color: var(--amber); }
.metric-note { margin: 12px 2px 24px; color: var(--muted); font-size: 12px; }
.overview-grid { display: grid; grid-template-columns: minmax(0, 1.72fr) minmax(0, 1fr); gap:
22px; align-items: start; }
.panel { min-width: 0; background: var(--surface); border: 1px solid var(--line); border-radius:
12px; box-shadow: var(--shadow); }
.panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
padding: 22px 24px 18px; }
.panel-head h2 { display: flex; align-items: center; flex-wrap: wrap; gap: 9px; }
.panel-head p { margin-top: 4px; color: var(--muted); font-size: 13px; }
.count { display: inline-grid; place-items: center; min-width: 25px; height: 25px; padding: 0
7px; border-radius: 6px; background: var(--amber-soft); color: var(--amber); font-size: 13px;
font-weight: 650; }
.text-link { display: inline-flex; align-items: center; justify-content: center; gap: 6px;
min-height: 36px; font-size: 14px; font-weight: 600; white-space: nowrap; }
.text-link .icon { width: 15px; height: 15px; }
.review-list { padding: 0 24px; }
.review-row { padding: 15px 0; border-top: 1px solid var(--line); display: grid;
grid-template-columns: minmax(0, 1fr) auto; gap: 7px 14px; align-items: center; }
.review-top { grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; }
.badge { display: inline-flex; align-items: center; gap: 5px; width: fit-content; padding: 2px
7px; border-radius: 5px; font-size: 12px; font-weight: 650; }
.badge .icon { width: 13px; height: 13px; }
.badge.error { color: var(--red); background: var(--red-soft); }
.badge.feedback { color: var(--amber); background: var(--amber-soft); }
.badge.empty { color: var(--violet); background: var(--violet-soft); }
.review-top time { color: var(--muted); font-size: 12px; margin-left: auto; }
.review-question { grid-column: 1 / -1; font-size: 15px; font-weight: 600; line-height: 1.65; }
.person { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 12px; }
.avatar { display: inline-grid; place-items: center; width: 32px; height: 32px; border-radius:
50%; background: var(--blue-soft); color: var(--blue); font-size: 12px; font-weight: 650; flex:
0 0 auto; }
.person .avatar { width: 22px; height: 22px; font-size: 12px; background: var(--surface-alt);
color: var(--muted); border: 1px solid var(--line); }
.review-row .text-link { min-height: 28px; }
.review-foot { background: var(--surface-alt); border-top: 1px solid var(--line); padding: 16px
24px; border-radius: 0 0 12px 12px; }
.search-counts { display: flex; flex-wrap: wrap; gap: 5px 20px; font-size: 12px; color:
var(--muted); }
.search-counts strong { color: var(--ink); font-size: 14px; margin-left: 6px; }
.quiet-details { margin-top: 7px; font-size: 12px; color: var(--muted); }
.quiet-details summary { padding: 5px 0; width: fit-content; font-size: 14px; }
.quiet-details p { margin-top: 6px; line-height: 1.8; }
.side-panels { display: grid; gap: 20px; }
.chart-panel .panel-head { padding-bottom: 12px; }
.chart-key { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px;
padding-top: 3px; white-space: nowrap; }
.chart-key::before { content: ''; width: 8px; height: 8px; border-radius: 2px; background:
var(--blue); }
.chart-note { margin: 16px 24px 21px; font-size: 12px; color: var(--muted); }
.feedback-panel .panel-head { padding-bottom: 12px; }
.feedback-stats { display: grid; grid-template-columns: 1fr 1fr; margin: 0 24px; gap: 16px; }
.feedback-item { padding: 8px 0 12px; }
.feedback-item + .feedback-item { padding-left: 18px; border-left: 1px solid var(--line); }
.feedback-label { color: var(--muted); display: flex; align-items: center; gap: 7px; font-size:
13px; }
.feedback-value { font-size: 29px; font-weight: 650; line-height: 1.3; margin-top: 7px; }
.feedback-value span { font-size: 13px; font-weight: 400; color: var(--muted); margin-left: 5px;
}
.feedback-note { border-top: 1px solid var(--line); margin: 4px 24px 0; padding: 13px 0 18px;
color: var(--muted); font-size: 12px; }
.analysis { margin-top: 24px; border: 1px solid var(--blue-line); background: var(--blue-soft);
border-radius: 12px; padding: 22px 24px; }
.analysis-intro { display: flex; align-items: center; gap: 16px; }
.analysis-mark { color: var(--blue); background: var(--surface); border-radius: 12px; width:
44px; height: 44px; display: grid; place-items: center; flex: 0 0 auto; }
.analysis-mark .icon { width: 24px; height: 24px; }
.analysis-copy { flex: 1; }
.analysis h2 { font-size: 17px; }
.analysis-copy p { color: var(--muted); font-size: 13px; margin-top: 4px; }
.button { display: inline-flex; align-items: center; justify-content: center; gap: 8px;
min-height: 42px; padding: 9px 16px; border: 1px solid var(--line); border-radius: 7px;
background: var(--surface); color: var(--ink); font-weight: 600; font-size: 14px;
text-decoration: none; }
.button:hover { background: var(--surface-alt); text-decoration: none; }
.button.primary { background: var(--blue); color: var(--surface); border-color: var(--blue);
white-space: nowrap; }
.button.primary:hover { filter: brightness(.93); }
.button .icon { width: 16px; height: 16px; }
.analysis-privacy { margin: 13px 0 0 60px; color: var(--muted); font-size: 12px; }
.analysis-privacy summary { padding: 4px 0; font-size: 14px; }
.analysis-privacy p { margin-top: 4px; }
.analysis-result { border-top: 1px solid var(--blue-line); margin-top: 18px; padding-top: 18px;
}
.result-heading { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom:
7px; }
.result-heading .demo-tag { border-color: var(--blue-line); }
.result-scope { color: var(--muted); font-size: 12px; }
.theme-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 14px; }
.theme-grid section { border-radius: 8px; background: var(--surface); padding: 16px 18px; }
.theme-grid ol { margin: 10px 0 0; padding-left: 21px; }
.theme-grid li { margin-top: 6px; }
.theme-grid li span { float: right; margin-left: 12px; font-variant-numeric: tabular-nums; }
.next-actions { margin-top: 18px; }
.next-actions ol { margin: 8px 0 0; padding-left: 22px; }
.next-actions li { margin-top: 5px; }
.report { margin-top: 24px; padding: 22px 24px; display: grid; grid-template-columns: minmax(0,
1fr) auto; gap: 12px 24px; align-items: center; }
.report-title { display: flex; align-items: center; gap: 9px; }
.report-title .icon { color: var(--blue); width: 18px; height: 18px; }
.report h2 { font-size: 16px; }
.report-label { color: var(--muted); font-size: 12px; margin-left: 4px; }
#report-summary { margin-top: 8px; line-height: 1.9; }
#copy-status { color: var(--green); font-size: 12px; grid-column: 1 / -1; }
#copy-status:empty { display: none; }
.section-heading { display: flex; align-items: baseline; gap: 10px; margin: 32px 0 14px; }
.section-heading h2 { font-size: 17px; }
.section-heading p { color: var(--muted); font-size: 12px; }
.usage-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr); gap: 22px;
}
.table-wrap { padding: 0 24px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { color: var(--muted); font-size: 12px; font-weight: 500; background: var(--surface-alt); }
th, td { padding: 13px 9px; text-align: left; border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { padding-left: 0; }
th:last-child, td:last-child { padding-right: 0; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.employee-name { display: flex; align-items: center; gap: 9px; }
.employee-name a { color: var(--ink); }
.employee-table .text-link { font-size: 14px; }
.positive { color: var(--green); }
.neutral { color: var(--muted); }
.previous { display: block; color: var(--muted); font-size: 12px; }
.panel-note { margin: 15px 24px 19px; color: var(--muted); font-size: 12px; }
.work-mix { padding: 1px 24px 21px; }
.stacked-bar { height: 15px; display: flex; gap: 3px; border-radius: 5px; overflow: hidden;
margin: 9px 0 24px; }
.mix-row { display: grid; grid-template-columns: minmax(0, 1fr) 60px 58px; align-items: center;
gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--line); font-variant-numeric:
tabular-nums; }
.mix-row:last-child { border-bottom: 0; }
.mix-label { display: flex; gap: 10px; align-items: center; }
.swatch { width: 9px; height: 9px; border-radius: 2px; flex: 0 0 auto; background: #4968db; }
.make .swatch { background: #9492dc; } .organize .swatch { background: #65a5b9; } .assist
.swatch { background: #b7c6df; }
.mix-row .num:last-child { color: var(--muted); font-size: 13px; }
.questions { margin-top: 24px; }
.question-list { list-style: none; padding: 0 24px; margin: 0; }
.question-row { display: grid; grid-template-columns: 95px minmax(0, 1fr) 58px; gap: 16px;
padding: 14px 0; border-top: 1px solid var(--line); align-items: baseline; }
.question-row time { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.question-row .question-person { color: var(--muted); font-size: 12px; text-align: right; }
.more-questions { margin: 0 24px; border-top: 1px solid var(--line); }
.more-questions > summary { padding: 15px 0; color: var(--blue); font-size: 14px; }
.more-questions .question-list { padding: 0; }
.technical { margin-top: 24px; padding: 0 24px; }
.technical > summary { padding: 20px 0; font-size: 14px; font-weight: 600; }
.technical > summary span { color: var(--muted); font-size: 12px; font-weight: 400; margin-left:
12px; }
.technical-body { border-top: 1px solid var(--line); padding: 20px 0; }
.technical-body h3 { margin: 22px 0 10px; }
.technical-body h3:first-child { margin-top: 0; }
.technical-body .note { margin-top: 12px; font-size: 12px; color: var(--muted); }
.technical table { table-layout: fixed; }
.technical th, .technical td { overflow-wrap: anywhere; }
.technical th { padding-left: 8px; }
.technical td:first-child { padding-left: 8px; }
.technical code { font-size: 12px; }
.technical .work-mix { padding: 0; max-width: 580px; }
.error-records { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.error-record { background: var(--surface-alt); padding: 15px; border-radius: 8px; }
.error-record h4 { margin: 0 0 10px; font-size: 14px; }
.error-record dl { margin: 0; display: grid; grid-template-columns: 85px minmax(0, 1fr); gap:
5px 8px; font-size: 12px; }
.error-record dt { color: var(--muted); }
.error-record dd { margin: 0; }
.footer { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 6px 16px;
margin-top: 25px; font-size: 12px; color: var(--muted); }
@media (min-width: 1450px) { main { padding-top: 38px; } .review-row { padding: 18px 0; } }
@media (max-width: 1180px) {
.sidebar { width: 188px; padding-right: 13px; padding-left: 13px; }
.workspace { margin-left: 188px; }
.topbar { padding-right: 26px; padding-left: 26px; }
main { padding: 26px; }
.overview-grid { grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr); gap: 18px; }
.panel-head { padding: 20px; } .review-list { padding: 0 20px; } .review-foot { padding: 14px
20px; }
.chart { margin-right: 18px; margin-left: 14px; }
.review-top time { font-size: 12px; }
.usage-grid { grid-template-columns: 1.3fr 1fr; gap: 18px; }
.kpi { padding: 18px; }
.report-label { display: none; }
}
@media (max-width: 980px) {
.sidebar { width: 76px; align-items: center; padding: 23px 10px; }
.brand { padding: 0; } .brand-word, .brand-sub, .nav-label, .nav-text, .sidebar-foot div {
display: none; }
.sidebar nav { margin-top: 38px; width: 100%; }
.sidebar nav a { justify-content: center; padding: 13px; }
.sidebar-foot { padding: 20px 0 0; width: 100%; justify-content: center; }
.workspace { margin-left: 76px; }
.page-heading { align-items: flex-start; gap: 16px; }
.periods a { padding-right: 12px; padding-left: 12px; }
.overview-grid { grid-template-columns: minmax(0, 1.45fr) minmax(0, 1fr); }
.panel-head { gap: 8px; } .panel-head h2 { font-size: 16px; }
.review-panel .panel-head { flex-wrap: wrap; }
.analysis-intro { flex-wrap: wrap; } .analysis-copy { min-width: 220px; }
.analysis-intro .button { margin-left: 60px; }
.usage-grid { grid-template-columns: 1fr; }
}
@media (max-width: 760px) {
.sidebar { position: static; width: 100%; padding: 15px 20px 0; align-items: stretch; }
.brand { font-size: 23px; gap: 9px; } .brandmark { width: 29px; height: 30px; border-radius:
8px; } .brand-word { display: inline; }
.sidebar nav { display: flex; margin-top: 14px; gap: 5px; }
.sidebar nav a { padding: 10px 13px; min-height: 44px; border-radius: 7px 7px 0 0; gap: 7px; }
.sidebar nav a[aria-current] { box-shadow: inset 0 -3px #9eb1ff; }
.nav-text { display: inline; font-size: 14px; } .sidebar nav .icon { width: 16px; height: 16px;
}
.sidebar-foot { display: none; } .workspace { margin: 0; }
.topbar { height: 46px; padding: 0 20px; } .breadcrumbs { font-size: 12px; gap: 8px; }
main { padding: 23px 20px; }
.page-heading { flex-wrap: wrap; } h1 { font-size: 25px; } .page-heading p { font-size: 13px; }
.periods { width: 100%; } .periods a { flex: 1; text-align: center; min-height: 40px; }
.period-caption { display: block; margin: 12px 0 19px; line-height: 1.9; }
.period-caption .selected { font-size: 12px; }
.kpis { gap: 8px; } .kpi { padding: 13px 12px; border-radius: 9px; }
.kpi h2 { font-size: 14px; } .kpi-icon { display: none; }
.kpi-value { font-size: 33px; margin-top: 10px; } .kpi-value .unit { font-size: 12px;
margin-left: 4px; }
.kpi-bottom { gap: 4px; margin-top: 9px; } .kpi-bottom > span:last-child { flex-basis: 100%; }
.delta { padding: 1px 5px; }
.metric-note { margin-bottom: 20px; line-height: 1.8; }
.overview-grid { grid-template-columns: 1fr; gap: 20px; }
.side-panels { grid-template-columns: 1fr 1fr; gap: 16px; }
.chart { height: 170px; } .chart-note { margin: 14px 18px 18px; }
.feedback-stats { margin: 0 18px; } .feedback-note { margin-right: 18px; margin-left: 18px; }
.analysis { padding: 20px; } .analysis-mark { width: 36px; height: 36px; border-radius: 9px; }
.analysis-intro { gap: 12px; } .analysis-copy { min-width: 0; }
.analysis-intro .button { margin-left: 48px; } .analysis-privacy { margin-left: 48px; }
.report { padding: 20px; gap: 14px; grid-template-columns: 1fr; } .report .button {
justify-self: start; }
.question-row { grid-template-columns: 1fr auto; gap: 4px 12px; }
.question-row p { grid-column: 1 / -1; grid-row: 2; } .question-row .question-person {
grid-column: 2; grid-row: 1; }
.question-list { padding: 0 20px; } .more-questions { margin: 0 20px; }
.technical { padding: 0 20px; } .technical > summary span { display: block; margin-left: 16px; }
.error-records { grid-template-columns: 1fr; }
.theme-grid { grid-template-columns: 1fr; gap: 12px; }
}
@media (max-width: 520px) {
.sidebar { padding: 14px 16px 0; } .sidebar nav { gap: 3px; } .sidebar nav a { padding: 10px
11px; }
main { padding: 21px 16px; } .topbar { padding: 0 16px; }
.breadcrumbs > span:first-child, .breadcrumbs > span:nth-child(2) { display: none; }
.page-heading p { font-size: 14px; }
.kpi { padding: 12px 10px; } .kpi-value { font-size: 31px; }
.kpi-value .unit { margin-left: 3px; } .kpi-bottom { font-size: 12px; }
.kpi .mobile-label { display: none; } .kpi h2 { white-space: nowrap; }
.panel-head { padding: 19px 18px 15px; } .panel-head h2 { font-size: 16px; }
.review-panel .panel-head > .text-link { display: none; }
.review-list { padding: 0 18px; } .review-row { padding: 16px 0; gap: 8px; }
.review-question { font-size: 14px; } .review-top { gap: 5px; }
.review-row .text-link { min-height: 36px; } .review-foot { padding: 14px 18px; }
.search-counts { display: block; } .search-counts p + p { margin-top: 5px; }
.side-panels { grid-template-columns: 1fr; }
.chart { margin: 16px 21px 0 16px; height: 181px; }
.chart-note { margin: 16px 20px 20px; }
.feedback-stats { margin: 0 20px; } .feedback-note { margin: 6px 20px 0; }
.analysis { margin-top: 20px; padding: 19px 18px; }
.analysis h2 { font-size: 16px; } .analysis-copy p { font-size: 13px; }
.analysis-intro { align-items: flex-start; } .analysis-mark { margin-top: 3px; }
.analysis-intro .button { margin: 2px 0 0; width: 100%; } .analysis-privacy { margin-left: 0; }
.report { margin-top: 20px; padding: 19px 18px; } .report .button { width: 100%; }
.section-heading { margin-top: 27px; } .section-heading p { font-size: 12px; }
.table-wrap { padding: 0 16px; } .employee-table th, .employee-table td { padding: 12px 4px; }
.employee-table .avatar { display: none; } .employee-table .employee-name { gap: 0; }
.employee-table .text-link { min-height: 40px; font-size: 14px; } .employee-table .text-link
.icon { display: none; }
.employee-table th:nth-child(2) { width: 52px; } .employee-table th:nth-child(3) { width: 66px;
} .employee-table th:nth-child(4) { width: 83px; }
.panel-note { margin: 14px 18px 18px; } .work-mix { padding: 0 18px 15px; }
.question-list { padding: 0 18px; } .more-questions { margin: 0 18px; }
.questions .panel-head { flex-wrap: wrap; } .questions .panel-head > .demo-tag { display: none;
}
.technical { padding: 0 18px; } .technical table, .technical tbody, .technical tr, .technical td
{ display: block; width: 100%; }
.technical thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path:
inset(50%); white-space: nowrap; } .technical tr { margin-bottom: 12px; background:
var(--surface-alt); border-radius: 7px; overflow: hidden; }
.technical td, .technical td:first-child { display: flex; justify-content: space-between; gap:
16px; padding: 9px 12px; font-size: 14px; text-align: right; }
.technical td::before { content: attr(data-label); font-size: 12px; color: var(--muted);
text-align: left; flex: 0 0 70px; }
.technical td:last-child { border-bottom: 0; }
.footer { margin-top: 22px; }
}
@media (prefers-reduced-motion: no-preference) { a, button { transition: background-color .15s,
border-color .15s; } }

.note, .muted { color: var(--muted); font-size: 12px; }
.note { margin: 12px 0; }
.panel > .note { margin: 16px 24px; }
.filter { margin-bottom: 18px; }
.delta.neutral { color: var(--muted); background: var(--surface-alt); }
.delta.caution { color: var(--amber); background: var(--amber-soft); }
.daily-chart { width: calc(100% - 36px); display: block; margin: 0 18px; }
.daily-bar { fill: var(--bar); }
.daily-bar.last { fill: var(--blue); }
.daily-chart text { fill: var(--muted); font: 15px sans-serif; }
.daily-chart path { stroke: var(--line); fill: none; }
.stacked-bar { width: 100%; display: block; }
.mix-color-0 { fill: #4968db; background: #4968db; }
.mix-color-1 { fill: #9492dc; background: #9492dc; }
.mix-color-2 { fill: #65a5b9; background: #65a5b9; }
.mix-color-3 { fill: #b7c6df; background: #b7c6df; }
.mix-color-4 { fill: #85909f; background: #85909f; }
.review-top { flex-wrap: wrap; }
.review-question { white-space: pre-wrap; }
.question-row { grid-template-columns: 95px minmax(0, 1fr) minmax(58px, .3fr); }
.employee-table { table-layout: fixed; }
.technical table { margin: 12px 0; }
.technical th, .technical td { padding-left: 8px; }
.admin-content { max-width: 1000px; }
.admin-content h1 { margin-bottom: 20px; }
.admin-content input { max-width: 100%; font: inherit; padding: 8px; }
.admin-content button { font: inherit; padding: 8px 16px; }
.admin-content table { table-layout: fixed; }
.ex { border: 1px solid var(--line); border-radius: 8px; padding: 12px; margin: 12px 0; }
.when, .files { color: var(--muted); font-size: 12px; }
.msg { padding: 8px 10px; border-radius: 6px; margin-top: 8px; overflow-wrap: anywhere; }
.user { background: var(--blue-soft); } .aico { background: var(--green-soft); }
.err { color: var(--red); }
@media (max-width: 760px) {
.question-row { grid-template-columns: 1fr auto; }
.analysis-copy { min-width: 0; }
.employee-table th:nth-child(1) { width: 32%; }
.employee-table th:nth-child(2) { width: 17%; }
.employee-table th:nth-child(3) { width: 22%; }
.employee-table th:nth-child(4) { width: 29%; }
}

"""


_ADMIN_SCRIPT = """
<script>
const copyButton = document.getElementById('copy-summary');
copyButton.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(document.getElementById('report-summary').textContent);
    document.getElementById('copy-status').textContent =
      'コピーしました。Slackに貼り付けられます。';
  } catch (error) {
    document.getElementById('copy-status').textContent =
      '自動コピーが使えません。報告文を選択して手動でコピーしてください。';
    const summary = document.getElementById('report-summary');
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(summary);
    selection.removeAllRanges();
    selection.addRange(range);
    summary.focus();
  }
});
const analyzeButton = document.getElementById('analyze-questions');
analyzeButton.addEventListener('click', async () => {
  const output = document.getElementById('agent-result');
  output.hidden = false;
  output.replaceChildren();
  output.textContent = '質問を分析しています…';
  analyzeButton.disabled = true;
  try {
    const response = await fetch('/admin/analyze', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({period: analyzeButton.dataset.period, user: analyzeButton.dataset.user})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '分析を表示できませんでした。');
    output.replaceChildren();
    const note = document.createElement('p');
    note.className = 'note';
    note.textContent = `Agent分析（その場限り）・${data.period}：${data.start} ～ ${data.end}。`
      + `質問の記録${data.total_questions}件のうち最新${data.sample_size}問、`
      + `分類済み${data.classified_size}問。各質問は先頭300文字まで。`
      + '件数はサンプル内の作業回数です。回答本文を見ていないため、成否は断定できません。';
    output.append(note);
    const themes = document.createElement('div');
    themes.className = 'theme-grid';
    output.append(themes);
    for (const [title, rows] of [
      ['よく聞かれたテーマ 上位5', data.themes],
      ['答えられていない可能性のあるテーマ 上位3', data.blocked_themes]
    ]) {
      const heading = document.createElement('h3');
      heading.textContent = title;
      const section = document.createElement('section');
      section.append(heading);
      themes.append(section);
      const list = document.createElement('ol');
      for (const row of rows) {
        const item = document.createElement('li');
        item.textContent = `${row.title}：${row.count}件`;
        list.append(item);
      }
      if (!rows.length) {
        const item = document.createElement('li');
        item.textContent = 'サンプル内に該当するテーマはありません。';
        list.append(item);
      }
      section.append(list);
    }
    const heading = document.createElement('h3');
    heading.textContent = '次に直すとよいこと（提案）';
    output.append(heading);
    const list = document.createElement('ol');
    for (const action of data.actions) {
      const item = document.createElement('li');
      item.textContent = action;
      list.append(item);
    }
    output.append(list);
  } catch (error) {
    output.textContent = error.message || '分析を表示できませんでした。';
  } finally {
    analyzeButton.disabled = false;
  }
});
</script>
"""


def _admin_icon(name: str) -> str:
    paths = {
        "usage": '<rect x="3" y="3" width="7" height="7" rx="1.5"/>'
        '<rect x="14" y="3" width="7" height="7" rx="1.5"/>'
        '<rect x="3" y="14" width="7" height="7" rx="1.5"/>'
        '<rect x="14" y="14" width="7" height="7" rx="1.5"/>',
        "history": '<path d="M21 11a8 8 0 0 1-8 8H8l-5 3V11a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z"/>'
        '<path d="M8 9h8M8 13h5"/>',
        "memory": '<path d="M14 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-9'
        'M14 3v7h7M14 3l7 7M7 14h10M7 17h7"/>',
        "people": '<circle cx="9" cy="7" r="3"/>'
        '<path d="M3 20v-2a6 6 0 0 1 12 0v2M16 4a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 4v2"/>',
        "activity": '<path d="M13 2 4 14h7l-1 8 10-13h-7l1-7"/>',
        "attention": '<path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5M12 17h.01"/>',
        "analysis": '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z"/>',
        "report": '<path d="M5 3h14v18H5zM8 7h8M8 11h8M8 15h5"/>',
        "copy": '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    }
    return f'<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">{paths[name]}</svg>'


def _admin_skill_table(rows: list[dict[str, Any]], period_label: str) -> str:
    headers = ("Skill", "件数", "コスト USD", "p50 (ms)", "p95 (ms)")
    body = []
    for row in rows:
        values = (
            row.get("skill"),
            row.get("n", 0),
            "$" + _money(row.get("cost_usd", 0)),
            row.get("p50_ms") if row.get("p50_ms") is not None else "—",
            row.get("p95_ms") if row.get("p95_ms") is not None else "—",
        )
        body.append(
            "<tr>"
            + "".join(
                f'<td data-label="{label}">{_e(value)}</td>'
                for label, value in zip(headers, values, strict=True)
            )
            + "</tr>"
        )
    return (
        f"<h3>Skill別（{_e(period_label)}）</h3><table><thead><tr>"
        + "".join(f'<th scope="col">{label}</th>' for label in headers)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


def render_usage_admin(data: dict[str, Any]) -> str:
    """把握→要注意→改善→報告の順に、既存の集計値を描画する。"""
    from teamagent.dashboard.insights import PERIOD_LABELS, admin_period

    period = data.get("period") or admin_period("7d")
    who = str(data.get("filter_user") or "")
    user_param = "&amp;user=" + _e(quote(who, safe="")) if who else ""
    periods = (
        '<nav class="periods" aria-label="集計期間">'
        + "".join(
            f'<a href="/admin?period={key}{user_param}"'
            + (' aria-current="page"' if key == period.key else "")
            + f">{label}</a>"
            for key, label in PERIOD_LABELS.items()
        )
        + "</nav>"
    )
    period_note = (
        f'<div class="period-caption"><span class="selected">{_e(_jst(period.start))} → '
        f"{_e(_jst(period.end))} JST</span><span>比較：{_e(_jst(period.previous_start))} → "
        f"{_e(_jst(period.start))} JST（同じ長さの直前期間）</span></div>"
    )
    filter_note = (
        f'<p class="filter">利用者 {_e(who)} で絞り込み中 · '
        f'<a href="/admin?period={period.key}">絞り込みを解除</a></p>'
        if who
        else ""
    )
    summary = data.get("summary")
    current = summary["current"] if summary else None
    previous = summary["previous"] if summary else None
    feedback = data.get("feedback")
    attention = (
        int(current["attention_events"]) + int(feedback["down"])
        if current is not None and feedback is not None
        else None
    )
    previous_attention = (
        int(previous["attention_events"]) + int(feedback["previous_down"])
        if previous is not None and feedback is not None
        else None
    )
    # 人数・回数の両方が増えたときだけ、見本の「利用は増加」を使う。
    increasing = (
        current is not None
        and previous is not None
        and current["users"] > previous["users"]
        and current["requests"] > previous["requests"]
    )
    heading = (
        '<div class="page-heading"><div><h1>利用状況</h1><p>'
        + ("利用は増加。" if increasing else "")
        + "気になるやり取りから確認しましょう。</p></div>"
        + periods
        + "</div>"
    )
    if current is None:
        sentence = "利用状況を取得できません。表示できる記録から確認してください。"
    else:
        sentence = (
            f"{period.label}は{current['users']:,}人がAicoを利用（"
            + _difference(current["users"], previous["users"] if previous else None)
            + f"）。作業回数は{current['requests']:,}回（"
            + _difference(current["requests"], previous["requests"] if previous else None)
            + "）。"
        )
        sentence += (
            f"要注意は{attention:,}件（{_difference(attention, previous_attention)}）でした。"
            if attention is not None
            else "要注意件数は取得できません。"
        )
        sentence += "※作業回数はツール実行数、要注意は同じ会話の重複を含みます。"
    report = (
        '<section class="panel report"><div><div class="report-title">'
        f"{_admin_icon('report')}<h2>報告用の一言</h2>"
        '<span class="report-label">そのままSlackへ</span></div>'
        f'<p id="report-summary" tabindex="-1">{_e(sentence)}</p></div>'
        '<button class="button" id="copy-summary" type="button">'
        f"{_admin_icon('copy')}一言をコピー</button>"
        '<p id="copy-status" role="status"></p></section>'
    )
    analysis = (
        '<section class="analysis"><div class="analysis-intro">'
        f'<div class="analysis-mark" aria-hidden="true">{_admin_icon("analysis")}</div>'
        '<div class="analysis-copy">'
        "<h2>次に、どこを直す？</h2><p>よく聞かれること・答えられない可能性のあるテーマを整理します。</p></div>"
        '<button class="button primary" id="analyze-questions" type="button" '
        f'data-period="{period.key}" data-user="{_e(who)}" aria-controls="agent-result">'
        f'{_admin_icon("analysis")}Agentで分析</button></div><details class="analysis-privacy">'
        "<summary>分析する情報について · ボタンを押したときだけ実行</summary>"
        "<p>集計値と最新200問までの質問文を使います。各質問は先頭300文字まで。"
        "識別情報は伏せ字にし、回答本文は分析しません。結果は保存しません。"
        "答えられていない可能性は推定であり、成否は断定できません。</p></details>"
        '<div id="agent-result" class="analysis-result" role="status"'
        ' aria-live="polite" hidden>'
        "</div></section>"
    )
    cards = []
    for label, unit, value, old_value, cls, icon in (
        (
            "利用人数",
            "人",
            current["users"] if current else None,
            previous["users"] if previous else None,
            "",
            "people",
        ),
        (
            '<span class="mobile-label">Aicoの</span>作業回数',
            "回",
            current["requests"] if current else None,
            previous["requests"] if previous else None,
            "activity",
            "activity",
        ),
        (
            '<a href="#review">要注意</a>',
            "件",
            attention,
            previous_attention,
            "attention",
            "attention",
        ),
    ):
        if value is None or old_value is None:
            change = "<span>前期差を取得できません</span>"
        else:
            diff = value - old_value
            tone = (
                "neutral"
                if diff == 0
                else ("" if (diff < 0 if cls == "attention" else diff > 0) else "caution")
            )
            arrow = "↗" if diff > 0 else "↘" if diff < 0 else "→"
            change = (
                f'<span class="delta {tone}">前期差 {arrow} {diff:+,}{unit}</span>'
                f"<span>前期 {old_value:,}{unit}</span>"
            )
        cards.append(
            f'<article class="kpi {cls}"><div class="kpi-top"><h2>{label}</h2>'
            f'<span class="kpi-icon">{_admin_icon(icon)}</span></div><div class="kpi-value">'
            + (f"{value:,}" if value is not None else "—")
            + f'<span class="unit">{unit}</span></div><p class="kpi-bottom">{change}</p></article>'
        )
    kpis = '<section aria-label="利用概要" class="kpis">' + "".join(cards) + "</section>"
    quality_note = (
        '<p class="metric-note">作業回数はツールの実行数です。1回の依頼で複数の作業を数える場合が'
        "あります。要注意は同じ会話の重複を含みます。</p>"
    )
    chart = (
        _daily_svg(data["daily"], period)
        if "daily" in data
        else '<p class="note">日別推移を取得できません。</p>'
    )
    trend = (
        '<section class="panel chart-panel"><div class="panel-head"><div><h2>日別推移</h2>'
        f'<p>{_e(period.label)}の作業回数</p></div><span class="chart-key">作業回数</span></div>'
        + chart
        + "</section>"
    )
    users = data.get("users", [])
    slack_ids = {str(row["who"]).lower(): row.get("user_id") for row in users}
    attention_rows = []
    for row in (feedback or {}).get("downs", []):
        attention_rows.append(
            {
                **row,
                "occurred_at": row["created_at"],
                "reason": "👎 評価",
                "query_text": row["query"],
                "user_id": slack_ids.get(str(row["who"]).lower()),
            }
        )
    for row in data.get("errors", []):
        reason = "エラー・拒否" if row["status"] != "ok" else "検索で該当なし"
        if row["status"] != "ok" and row.get("found") is False:
            reason += "・該当なし"
        attention_rows.append({**row, "reason": reason})
    attention_rows.sort(key=lambda row: str(row["occurred_at"]), reverse=True)
    reviews = []
    for row in attention_rows:
        badge = (
            "feedback"
            if row["reason"] == "👎 評価"
            else ("empty" if row["status"] == "ok" else "error")
        )
        reviews.append(
            f'<article class="review-row"><div class="review-top"><span class="badge {badge}">'
            f"{_e(row['reason'])}</span>{_admin_time(row['occurred_at'])}</div>"
            '<p class="review-question">'
            f"{_e(row.get('query_text') or '質問文の記録はありません。')}</p>"
            f'<div class="person">{_e(row["who"])}</div>'
            f'<a class="text-link" href="{_history_href(row.get("user_id"))}">'
            "会話を読む ↗</a></article>"
        )
    review_body = (
        "".join(reviews[:10]) or '<p class="note">取得できた範囲に要注意の記録はありません。</p>'
    )
    if len(reviews) > 10:
        review_body += (
            '<details class="quiet-details"><summary>'
            f"さらに見る（取得済み{len(reviews)}件）</summary>"
            + "".join(reviews[10:])
            + "</details>"
        )
    if feedback is None:
        review_body += '<p class="note">評価データはまだ表示できません。</p>'
    if "errors" not in data:
        review_body += '<p class="note">エラー・該当なしを取得できません。</p>'
    search_note = (
        '<div class="search-counts">'
        f"<p>検索で該当なし<strong>{current['not_found']}件</strong></p>"
        f"<p>判定の記録がない検索<strong>{current['unknown_search']}件</strong>（不明・0件扱いしません）</p>"
        "</div>"
        if current
        else ""
    )
    review = (
        '<section class="panel review-panel" id="review"><div class="panel-head"><div>'
        '<h2>先に確認するやり取り <span class="count">'
        + (f"{attention:,}" if attention is not None else "—")
        + '<span class="sr-only">件</span></span></h2><p>困った場面から、会話を確認。</p></div>'
        '<a class="text-link" href="/admin/history">会話履歴 ↗</a></div><'
        'div class="review-list">'
        + review_body
        + '</div><div class="review-foot">'
        + search_note
        + '<details class="quiet-details"><summary>要注意の数え方・会話リンクについて</summary>'
        "<p>要注意は「失敗または該当なしの実行＋👎評価」の検知件数で、同一会話の重複があります。"
        "検索で該当なしでも、会話全体が未解決とは限りません。評価なしは成功を意味しません。"
        "一覧は実行・評価それぞれ最新200件まで。Slack IDがない行は会話履歴の一覧を開きます。"
        "社員の履歴へのリンクであり、👎と実行記録から特定の発言との一致は保証しません。"
        "</p></details></div></section>"
    )
    feedback_panel = (
        '<section class="panel feedback-panel"><div class="panel-head"><div>'
        f"<h2>回答への評価</h2><p>Slackで届いた声・{_e(period.label)}</p></div></div>"
    )
    if feedback is not None:
        feedback_panel += (
            '<div class="feedback-stats">'
            + "".join(
                f'<div class="feedback-item"><p class="feedback-label">{label}</p>'
                f'<p class="feedback-value">{int(feedback[key]):,}<span>件</span></p></div>'
                for label, key in (("👍 評価", "up"), ("👎 評価", "down"))
            )
            + "</div>"
        )
    else:
        feedback_panel += '<p class="note">評価データはまだ表示できません。</p>'
    feedback_panel += (
        '<div class="feedback-note"><p>検索を使った回答への評価です。<br>'
        '全回答の正答率ではありません。</p><details class="quiet-details">'
        "<summary>評価の数え方</summary><p>同じ人・回答は期間内の最後の評価を数えます。"
        "評価がない回答の成否は分かりません。</p></details></div></section>"
    )
    user_rows = []
    for row in users:
        old_value = row.get("previous_requests")
        diff = int(row["requests"]) - int(old_value) if old_value is not None else None
        change = (
            f'<span class="{"positive" if diff > 0 else "neutral"}">{diff:+,}</span>'
            f'<span class="previous">前期 {old_value:,}</span>'
            if diff is not None
            else "前期差を取得できません"
        )
        user_rows.append(
            '<tr><td><div class="employee-name"><span class="avatar" aria-hidden="true">'
            + _e(str(row["who"])[:1])
            + '</span><a href="'
            + _admin_user_href(row["who"])
            + "&amp;period="
            + period.key
            + '">'
            + _e(row["who"])
            + '</a></div></td><td class="num">'
            + _e(row["requests"])
            + '回</td><td class="num">'
            + change
            + '</td><td class="num"><a class="text-link" href="'
            + _history_href(row.get("user_id"))
            + '">会話を読む ↗</a></td></tr>'
        )
    user_body = (
        '<div class="table-wrap"><table class="employee-table"><thead><tr>'
        '<th scope="col">社員</th><th scope="col" class="num">作業回数</th>'
        '<th scope="col" class="num">前期差</th><th scope="col" class="num">会話</th>'
        "</tr></thead><tbody>" + "".join(user_rows) + "</tbody></table></div>"
        if user_rows
        else '<p class="note">この期間の利用者はいません。</p>'
    )
    if "users" not in data:
        user_body = '<p class="note">利用者を取得できません。</p>'
    people = (
        '<section class="panel"><div class="panel-head"><div><h2>社員ごとの利用</h2>'
        "<p>作業回数が多い順</p></div></div>"
        + user_body
        + '<details class="quiet-details panel-note"><summary>社員の集計について</summary>'
        "<p>実行数順・最大200人。人数KPIは上位一覧の合計ではありません。"
        "メールアドレス／Slack IDで記録され、同じ人が分かれる場合があります。"
        "社員名を押すと、その社員の利用状況に絞り込みます。</p></details></section>"
    )
    work = (
        _admin_work_mix(data["work_types"], title="作業の内訳")
        if "work_types" in data
        else '<section class="panel"><div class="panel-head"><h2>作業の内訳</h2></div>'
        '<p class="note">作業内訳を取得できません。</p></section>'
    )
    question_rows = [
        f'<li class="question-row">{_admin_time(row["occurred_at"])}'
        f'<p>{_e(row["query_text"])}</p><span class="question-person">{_e(row["who"])}</span></li>'
        for row in data.get("questions", [])
    ]
    question_body = (
        '<ol class="question-list">' + "".join(question_rows[:10]) + "</ol>"
        if question_rows
        else '<p class="note">この利用者・期間の質問はまだ記録がありません。</p>'
    )
    if len(question_rows) > 10:
        question_body += (
            f'<details class="more-questions"><summary>残り{len(question_rows) - 10}件の質問を見る'
            '（最大200件まで）</summary><ol class="question-list" start="11">'
            + "".join(question_rows[10:])
            + "</ol></details>"
        )
    if "questions" not in data:
        question_body = '<p class="note">質問を取得できません。</p>'
    questions = (
        '<section class="panel questions"><div class="panel-head"><div><h2>最近の質問</h2>'
        "<p>質問文が記録された作業のみ。すべてのDMの一覧ではありません。</p></div>"
        '<span class="demo-tag">最新10件</span></div>' + question_body + "</section>"
    )
    technical = []
    legacy = data.get("kpis")
    if legacy is not None:
        technical.append(
            "<h3>全体の記録（社員の絞り込み対象外）</h3><table><thead><tr>"
            '<th scope="col">期間</th><th scope="col">作業回数</th><th scope="col">利用人数</th>'
            '<th scope="col">記録費用 USD</th><th scope="col">エラー</th></tr></thead><tbody>'
            + "".join(
                '<tr><td data-label="期間">'
                + label
                + "</td>"
                + "".join(
                    f'<td class="num" data-label="{name}">{_e(legacy.get(prefix + key, "—"))}</td>'
                    for name, key in (
                        ("作業回数", "requests"),
                        ("利用人数", "users"),
                        ("記録費用 USD", "cost_usd"),
                        ("エラー", "errors"),
                    )
                )
                + "</tr>"
                for label, prefix in (("今日", "today_"), ("直近7日", "seven_day_"))
            )
            + "</tbody></table>"
        )
    if "skills" in data:
        technical.append(
            '<div class="table-wrap">' + _admin_skill_table(data["skills"], period.label) + "</div>"
        )
    if "work_types_30d" in data:
        technical.append(
            _admin_work_mix(data["work_types_30d"], title="作業の内訳（直近30日・全体）")
        )
    error_details = []
    for row in data.get("errors", []):
        reason = "エラー・拒否" if row["status"] != "ok" else "検索で該当なし"
        error_details.append(
            f'<article class="error-record"><h4>{reason}</h4><dl>'
            + "".join(
                f"<dt>{label}</dt><dd>{_e(value)}</dd>"
                for label, value in (
                    ("時刻 JST", _jst(row["occurred_at"])),
                    ("社員", row["who"]),
                    ("Skill", row["skill"]),
                    ("status", row["status"]),
                    ("code", row.get("error_code")),
                    ("request_id", row.get("request_id")),
                )
            )
            + "</dl></article>"
        )
    technical.append(
        '<h3>エラー・該当なしの詳細</h3><div class="error-records">'
        + "".join(error_details)
        + "</div>"
    )
    technical.append(
        '<details class="quiet-details"><summary>最近の質問の技術情報</summary>'
        + "".join(
            f"<p>{_e(_jst(row['occurred_at']))} JST · {_e(row['who'])} · "
            f"<code>{_e(row['skill'])}</code></p>"
            for row in data.get("questions", [])
        )
        + "</details>"
    )
    if current:
        technical.append(
            f'<p class="note">選択期間の記録費用：${_money(current["cost_usd"], 4)}。'
            "Aico全体の請求額とは限りません。</p>"
        )
    technical.append(
        '<p class="note">Agent分析は日本向けHaiku 4'
        ".5。既存単価表は入力$1/出力$5（100万token）、AWS公表"
        "値$1.10/$5.50と差があります。単価表の補正は今回の範囲外です"
        "。</p>"
    )
    details = (
        (
            '<details class="panel technical"><summary>技術・費用の詳細<span>エラーの'
            '調査や、費用を確認したいときに</span></summary><div class="technical-body">'
        )
        + "".join(technical)
        + "</div></details>"
    )
    notes = "".join('<p class="note">' + _e(note) + "</p>" for note in data.get("notes", []))
    empty = (
        '<p class="note">この期間の利用記録はありません。</p>'
        if current is not None and current["requests"] == 0
        else ""
    )
    body = (
        heading
        + period_note
        + filter_note
        + notes
        + empty
        + kpis
        + quality_note
        + '<div class="overview-grid">'
        + review
        + '<div class="side-panels">'
        + trend
        + feedback_panel
        + "</div></div>"
        + analysis
        + report
        + '<div class="section-heading"><h2>利用をもう少し詳しく</h2>'
        f"<p>{_e(period.label)}</p></div>"
        + '<div class="usage-grid">'
        + people
        + work
        + "</div>"
        + questions
        + details
        + '<footer class="footer"><p>表示時刻はすべて日本時間（JST）</p></footer>'
        '<noscript><p class="panel-note">コピーは報告文を選択して行えます。'
        "Agentで分析するにはJavaScriptを有効にしてください。</p></noscript>"
    )
    return render_admin_page("利用状況", body, active="usage", scripts=_ADMIN_SCRIPT)


__all__ = [
    "render_admin_nav",
    "render_admin_page",
    "render_dashboard",
    "render_errors",
    "render_login",
    "render_usage_admin",
]
