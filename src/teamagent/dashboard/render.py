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
        (
            '<nav aria-label="管理画面" style="displ'
            "ay:flex;flex-wrap:wrap;gap:12px;mar"
            'gin-bottom:20px">'
        )
        + "".join(
            f'<a href="{href}" style="color:#8ab8ff;padding:8px 4px"'
            + (' aria-current="page"' if key == active else "")
            + f">{label}</a>"
            for key, href, label in links
        )
        + "</nav>"
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
    width = 900 / len(series)
    bars = []
    for i, (label, count) in enumerate(series):
        height = count / peak * 118
        x = 30 + i * width
        bars.append(
            f'<rect x="{x:.1f}" y="{145 - height:.1f}" '
            f'width="{max(1, width - 5):.1f}" height="{height:.1f}" '
            f'rx="3" fill="#6da2ff"><title>{_e(label)}: {count}回</title></rect>'
        )
        if i in {0, len(series) // 2, len(series) - 1}:
            anchor = "start" if i == 0 else "end" if i == len(series) - 1 else "middle"
            bars.append(
                f'<text x="{x:.1f}" y="170" fill="#bcc9df" font-size="15" '
                f'text-anchor="{anchor}">{_e(label[5:])}</text>'
            )
    description = "、".join(f"{label}: {count}回" for label, count in series)
    return (
        (
            '<svg viewBox="0 0 960 185" role="im'
            'g" aria-labelledby="daily-title dai'
            'ly-desc"><title id="daily-title">日別'
            'ツール実行数</title><desc id="daily-desc"'
            ">"
        )
        + _e(description)
        + ('</desc><path d="M30 145H940" stroke="#53617a"/>')
        + "".join(bars)
        + (
            '</svg><p class="note">日別ツール実行数・JST。'
            "期間の最初と最後の日は一部の時間のみの場合があります。</p>"
        )
    )


_ADMIN_STYLE = """
.usage-admin h1{font-size:24px;
margin:0 0 20px}
.usage-admin h2{font-size:18px;
color:var(--ink)}

.usage-admin a{color:#8ab8ff}
.usage-admin button,.usage-admin .period-link{font:inherit;
cursor:pointer;

 border:1px solid #53617a;
background:#202e46;
color:var(--ink);
border-radius:8px;
padding:10px 14px;
display:inline-block;
text-decoration:none}

.usage-admin button:disabled{opacity:.6;
cursor:wait}
.usage-admin :focus-visible{outline:3px solid #a4c9ff;
outline-offset:3px}

.usage-admin .periods{display:flex;
flex-wrap:wrap;
gap:8px}
.usage-admin [aria-current=page]{font-weight:700;
text-decoration:underline}

.usage-admin .kpis{grid-template-columns:repeat(3,minmax(0,1fr))}
.usage-admin .kpi .value{font-size:34px}

.usage-admin .kpi .sub{font-size:13px;
line-height:1.6}
.usage-admin .lead{font-size:18px;
line-height:1.8}

.usage-admin .actions{display:flex;
flex-wrap:wrap;
gap:10px}
.usage-admin .note{font-size:13px}

.usage-admin .review-row,.usage-admin .question-row{padding:14px 0;
border-top:1px solid var(--line);
overflow-wrap:anywhere}

.usage-admin .review-head{display:flex;
flex-wrap:wrap;
gap:10px;
align-items:center}
.usage-admin .review-row p{line-height:1.7;
white-space:pre-wrap}

.usage-admin .question{min-width:0;
max-width:none}
.usage-admin summary{cursor:pointer;
padding:12px 0;
color:#a4c9ff}

.usage-admin .table-wrap{overflow-x:auto}
.usage-admin svg{width:100%;
height:auto;
display:block}

.usage-admin .stat-list{display:grid;
grid-template-columns:repeat(2,minmax(0,1fr));
gap:8px;
line-height:1.7}

.usage-admin .stat-list div{border-bottom:1px solid var(--line);
padding:6px 0}
.usage-admin .bar-row{grid-template-columns:64px minmax(0,1fr) 110px}

@media(max-width:600px){main{padding:14px}
.usage-admin svg text{font-size:36px}
header{padding:12px 14px}
.usage-admin .kpis{grid-template-columns:1fr;
gap:10px}

 .usage-admin .card,.usage-admin .kpi{padding:14px}
.usage-admin .stat-list{grid-template-columns:1fr}

 .usage-admin table.responsive,.usage-admin table.responsive tbody,
.usage-admin table.responsive tr,.usage-admin table.responsive td{display:block;
width:100%}

 .usage-admin table.responsive th{display:none}
.usage-admin table.responsive tr{padding:10px 0;
border-bottom:1px solid var(--line)}

 .usage-admin table.responsive td{border:0;
text-align:left;
padding:5px 0;
overflow-wrap:anywhere}

 .usage-admin table.responsive td::before{content:attr(data-label);
color:#bcc9df;
margin-right:12px}

 .usage-admin .bar-row{grid-template-columns:54px minmax(0,1fr) 100px;
font-size:12px}
}

"""

_ADMIN_SCRIPT = """
<script>
const copyButton = document.getElementById('copy-summary');
copyButton.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(document.getElementById('report-summary').textContent);
    document.getElementById('copy-status').textContent = 'コピーしました';
  } catch (error) {
    document.getElementById('copy-status').textContent =
      'コピーできませんでした。要約を選択してコピーしてください。';
  }
});
const analyzeButton = document.getElementById('analyze-questions');
analyzeButton.addEventListener('click', async () => {
  const output = document.getElementById('agent-result');
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
      + '件数はサンプル内のツール実行数です。';
    output.append(note);
    for (const [title, rows] of [
      ['よく聞かれたテーマ 上位5', data.themes],
      ['答えられていない可能性のあるテーマ 上位3', data.blocked_themes]
    ]) {
      const heading = document.createElement('h3');
      heading.textContent = title;
      output.append(heading);
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
      output.append(list);
    }
    const heading = document.createElement('h3');
    heading.textContent = '次に直すと良いこと 3つ（提案）';
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


def render_usage_admin(data: dict[str, Any]) -> str:
    """把握→要注意→実際の会話の順に、小俣さん限定の管理画面を描画する。"""
    from teamagent.dashboard.insights import PERIOD_LABELS, admin_period

    period = data.get("period") or admin_period("7d")
    who = str(data.get("filter_user") or "")
    user_param = "&amp;user=" + _e(quote(who, safe="")) if who else ""
    periods = (
        '<div class="periods" aria-label="集計期間">'
        + "".join(
            f'<a class="period-link" href="/admin?period={key}{user_param}"'
            + (' aria-current="page"' if key == period.key else "")
            + f">{label}</a>"
            for key, label in PERIOD_LABELS.items()
        )
        + "</div>"
    )
    period_note = (
        f'<p class="note">{_e(_jst(period.start))} ～ {_e(_jst(period.end))} JST<br>'
        f"比較：{_e(_jst(period.previous_start))} ～ {_e(_jst(period.start))} JST"
        "（同じ長さの直前期間）</p>"
    )
    filter_note = (
        (
            f'<p class="filter">利用者 {_e(who)} で絞り込み中 · '
            f'<a href="/admin?period={period.key}">絞り込みを解除</a></p>'
        )
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
    if current is None:
        sentence = "利用状況を取得できません。表示できる記録から確認してください。"
    else:
        sentence = (
            f"{period.label}は{current['users']:,}人がツールを{current['requests']:,}回実行。"
            + _difference(current["requests"], previous["requests"] if previous else None)
            + "。"
        )
        sentence += (
            f"要注意は{attention:,}件です。"
            if attention is not None
            else "要注意件数は取得できません。"
        )
    intro = (
        ('<section class="card"><h2>報告用の一言</h2><p id="report-summary" class="lead">')
        + _e(sentence)
        + (
            '</p><div class="actions"><button id'
            '="copy-summary" type="button">コピー</'
            'button><button id="analyze-question'
            's" type="button" data-period="'
        )
        + period.key
        + '" data-user="'
        + _e(who)
        + (
            '">Agent で分析</button></div><p id="co'
            'py-status" role="status" class="not'
            'e"></p><p class="note">Agentには集計値と最'
            "新200問までの質問文（識別情報を伏せ字）を送ります。結果は保存しませ"
            'ん。回答本文は分析しません。</p><div id="agent-re'
            'sult" role="status" aria-live="poli'
            'te"></div></section>'
        )
    )
    cards = []
    for label, value, old in (
        (
            "利用人数",
            current["users"] if current else None,
            previous["users"] if previous else None,
        ),
        (
            "ツール実行数",
            current["requests"] if current else None,
            previous["requests"] if previous else None,
        ),
        ("要注意件数", attention, previous_attention),
    ):
        cards.append(
            '<div class="kpi"><div class="label">'
            + label
            + '</div><div class="value">'
            + (f"{value:,}" if value is not None else "—")
            + '</div><div class="sub">'
            + _e(_difference(value, old))
            + "</div></div>"
        )
    kpis = '<section aria-label="利用概要" class="kpis">' + "".join(cards) + "</section>"
    quality_note = (
        '<p class="note">件数は会話数ではなくツール実行数。要注'
        "意は「失敗または該当なしの実行＋👎評価」の検知件数で、同一会話の重複が"
        "あります。評価なしは成功を意味しません。費用は記録されたツール費用です"
        "。</p>"
    )
    chart = (
        _daily_svg(data["daily"], period)
        if "daily" in data
        else '<p class="note">日別推移を取得できません。</p>'
    )
    trend = '<section class="card"><h2>日別推移</h2>' + chart + "</section>"
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
        reviews.append(
            ('<article class="review-row"><div class="review-head"><strong>')
            + _e(row["reason"])
            + '</strong><span class="muted">'
            + _e(_jst(row["occurred_at"]))
            + " JST</span><span>"
            + _e(row["who"])
            + '</span><a href="'
            + _history_href(row.get("user_id"))
            + '">会話を読む</a></div><p>'
            + _e(row.get("query_text") or "質問文の記録はありません。")
            + "</p></article>"
        )
    review_body = (
        "".join(reviews[:10]) or '<p class="note">取得できた範囲に要注意の記録はありません。</p>'
    )
    if len(reviews) > 10:
        review_body += (
            "<details><summary>さらに見る（取得済み"
            + str(len(reviews))
            + "件）</summary>"
            + "".join(reviews[10:])
            + "</details>"
        )
    if feedback is None:
        review_body += '<p class="note">評価データはまだ表示できません。</p>'
    if "errors" not in data:
        review_body += '<p class="note">エラー・該当なしを取得できません。</p>'
    if current:
        review_body += (
            f'<p class="note">検索で該当なし：{current["not_found"]}件。'
            f"判定の記録がない検索：{current['unknown_search']}件"
            "（不明・0件扱いしません）。</p>"
        )
    if feedback is not None:
        review_body += (
            f'<p class="note">回答の評価（Slack・{period.label}）：'
            f"👍 {feedback['up']}件 / 👎 {feedback['down']}件。"
            "同じ人・回答は期間内の最後の評価。検索を使った回答への評価であり、"
            "全回答の正答率ではありません。</p>"
        )
    review_body += (
        '<p class="note">一覧は実行・評価それぞれ最新200件ま'
        "で。Slack IDがない行は会話履歴の一覧を開きます。👎と実行記録か"
        "ら特定の発言との一致は保証しません。</p>"
    )
    review = '<section class="card"><h2>先に確認するやり取り</h2>' + review_body + "</section>"
    user_rows = []
    for row in users:
        href = _admin_user_href(row["who"]) + "&amp;period=" + period.key
        user_rows.append(
            '<tr><td data-label="社員"><a href="'
            + href
            + '">'
            + _e(row["who"])
            + '</a></td><td data-label="実行数">'
            + _e(row["requests"])
            + '</td><td data-label="前期差">'
            + _e(_difference(row["requests"], row.get("previous_requests")))
            + '</td><td data-label="会話"><a href="'
            + _history_href(row.get("user_id"))
            + '">会話を読む</a></td></tr>'
        )
    user_body = (
        (
            '<table class="responsive"><tr><th>社'
            "員</th><th>ツール実行数</th><th>前期差</th><t"
            "h>会話</th></tr>"
        )
        + "".join(user_rows)
        + "</table>"
        if user_rows
        else '<p class="note">この期間の利用者はいません。</p>'
    )
    if "users" not in data:
        user_body = '<p class="note">利用者を取得できません。</p>'
    people = (
        '<section class="card"><h2>誰が使っているか</h2>'
        + user_body
        + (
            '<p class="note">実行数順・最大200人。人数KPIは上'
            "位一覧の合計ではありません。email/Slack IDで記録され、同"
            "一人物が分かれる場合があります。</p></section>"
        )
    )
    work = (
        _work_type_block(data["work_types"], title="何に使っているか")
        if "work_types" in data
        else (
            '<section class="card"><h2>何に使っているか<'
            '/h2><p class="note">作業内訳を取得できません。</'
            "p></section>"
        )
    )
    question_rows = []
    for row in data.get("questions", []):
        question_rows.append(
            ('<article class="question-row"><p class="note">')
            + _e(_jst(row["occurred_at"]))
            + " JST · "
            + _e(row["who"])
            + " · "
            + _e(row["skill"])
            + '</p><p class="question">'
            + _e(row["query_text"])
            + "</p></article>"
        )
    question_body = "".join(question_rows[:10]) or (
        '<p class="note">この利用者・期間の質問はまだ記録がありません。</p>'
    )
    if len(question_rows) > 10:
        question_body += (
            ("<details><summary>残りの質問を見る（直近200件まで）</summary>")
            + "".join(question_rows[10:])
            + "</details>"
        )
    if "questions" not in data:
        question_body = '<p class="note">質問を取得できません。</p>'
    questions = (
        (
            '<section class="card"><h2>最近の質問</h2'
            '><p class="note">queryを記録した実行のみ。全DM'
            "の一覧ではありません。最新10件を表示、展開で最大200件。</p>"
        )
        + question_body
        + "</section>"
    )
    technical = []
    legacy = data.get("kpis")
    if legacy is not None:
        technical.append(
            ('<h3>全体の既存KPI（社員の絞り込み対象外）</h3><div class="stat-list">')
            + "".join(
                "<div>" + label + "：" + _e(legacy.get(key, "—")) + "</div>"
                for label, key in (
                    ("今日の件数", "today_requests"),
                    ("今日のユーザー数", "today_users"),
                    ("今日のコスト USD", "today_cost_usd"),
                    ("今日のエラー数", "today_errors"),
                    ("直近7日の件数", "seven_day_requests"),
                    ("直近7日のユーザー数", "seven_day_users"),
                    ("直近7日のコスト USD", "seven_day_cost_usd"),
                    ("直近7日のエラー数", "seven_day_errors"),
                )
            )
            + "</div>"
        )
    if "skills" in data:
        technical.append(
            '<div class="table-wrap">'
            + _skill_block(data["skills"]).replace(
                "Skill 別（直近7日）", f"Skill 別（{period.label}）"
            )
            + "</div>"
        )
    if "work_types_30d" in data:
        technical.append(
            _work_type_block(data["work_types_30d"], title="作業の内訳（直近30日・全体）")
        )
    error_details = "".join(
        "<tr><td>"
        + _e(_jst(row["occurred_at"]))
        + "</td><td>"
        + _e(row["who"])
        + "</td><td>"
        + _e(row["skill"])
        + "</td><td>"
        + _e(row["status"])
        + "</td><td>"
        + _e(row.get("error_code"))
        + "</td><td>"
        + _e(row.get("request_id"))
        + "</td></tr>"
        for row in data.get("errors", [])
    )
    technical.append(
        (
            '<div class="table-wrap"><h3>エラー・該当な'
            "しの詳細</h3><table><tr><th>時刻 JST</th>"
            "<th>社員</th><th>Skill</th><th>status"
            "</th><th>code</th><th>request_id</t"
            "h></tr>"
        )
        + error_details
        + "</table></div>"
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
        ('<details class="card"><summary>技術・費用の詳細</summary>')
        + "".join(technical)
        + "</details>"
    )
    notes = "".join('<p class="note">' + _e(note) + "</p>" for note in data.get("notes", []))
    empty = (
        '<p class="note">この期間の利用記録はありません。</p>'
        if current is not None and current["requests"] == 0
        else ""
    )
    body = (
        "<style>"
        + _ADMIN_STYLE
        + '</style><div class="usage-admin">'
        + render_admin_nav("usage")
        + "<h1>Aico 利用状況（管理）</h1>"
        + periods
        + period_note
        + filter_note
        + notes
        + empty
        + intro
        + kpis
        + quality_note
        + trend
        + review
        + people
        + work
        + questions
        + details
        + "</div>"
    )
    return _page("Aico 利用状況（管理）", body, scripts=_ADMIN_SCRIPT)


__all__ = [
    "render_admin_nav",
    "render_dashboard",
    "render_errors",
    "render_login",
    "render_usage_admin",
]
