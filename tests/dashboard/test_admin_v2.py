"""管理画面の期間・匿名化・モデル分類をサーバーで数える契約。"""

from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from teamagent.dashboard import insights
from teamagent.dashboard.render import render_usage_admin

JST = timezone(timedelta(hours=9))


def test_admin_periods_use_jst_and_equal_previous_duration() -> None:
    now = datetime(2026, 10, 19, 10, 30, tzinfo=JST)
    today = insights.admin_period("today", now=now)
    assert today.start == now.replace(hour=0, minute=0)
    assert today.previous_start == today.start - (now - today.start)
    for key, days in (("7d", 7), ("30d", 30)):
        period = insights.admin_period(key, now=now)
        assert period.end - period.start == timedelta(days=days)
        assert period.start - period.previous_start == period.end - period.start
    with pytest.raises(ValueError):
        insights.admin_period("week", now=now)


def test_admin_masks_email_url_slack_identifiers_and_honorific_names() -> None:
    raw = "山田さんに a@example.com <https://example.com/secret|資料> <@U0123456789|小俣> U0123456789 を送る www.example.com"
    masked = insights.redact_question(raw)
    for secret in ("山田", "a@example.com", "example.com", "U0123456789", "小俣", "secret"):
        assert secret not in masked
    assert "送る" in masked


def test_admin_analysis_caps_questions_and_counts_model_assignments() -> None:
    calls = []

    class Client:
        def converse(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                text='{"themes":[{"title":"資料探し","question_ids":[1,2]},{"title":"重複","question_ids":[2]}],"actions":["検索対象を見直す","検索語を補う","エラーを確認"]}'
            )

    rows = [
        {"query_text": "資料を探す", "status": "ok", "found": False, "who": "secret@example.com"},
        {"query_text": "資料を探し直す", "status": "error", "found": None},
    ] + [{"query_text": "調べる", "status": "ok"} for _ in range(205)]
    # 同一質問は集約して送るが、入力元の上限は200問。
    result = insights.analyze_questions(
        rows, [], {"requests": 207}, client=Client(), request_id="r"
    )
    import json

    payload = json.loads(calls[0]["messages"][0]["content"][0]["text"])
    assert payload["sample_size"] == 200
    assert "secret@example.com" not in repr(calls)
    assert calls[0]["cache_system"] is True
    assert result["themes"][0]["count"] == 2
    assert result["blocked_themes"][0]["count"] == 2
    assert sum(t["count"] for t in result["themes"]) <= 200
    assert len(result["actions"]) == 3


def test_admin_renderer_orders_sections_and_preserves_unknowns() -> None:
    page = render_usage_admin(
        {
            "period": insights.admin_period("7d"),
            "notes": [],
            "questions": [],
            "users": [],
            "errors": [],
        }
    )
    labels = [
        "利用状況",
        "直近7日",
        "利用人数",
        "先に確認するやり取り",
        "日別推移",
        "回答への評価",
        "次に、どこを直す？",
        "報告用の一言",
        "社員ごとの利用",
        "作業の内訳",
        "最近の質問",
        "技術・費用の詳細",
    ]
    positions = [page.index(label) for label in labels]
    assert positions == sorted(positions)
    assert "取得できません" in page
    trend = page[page.index('<section class="panel chart-panel">') :].split("</section>", 1)[0]
    assert "<svg" not in trend  # 未取得を0件のグラフにしない（ナビのアイコンは除く）
    assert 'href="/admin/history"' in page
    assert 'href="/admin/memory"' in page
    assert "cdn.jsdelivr.net" not in page
    assert "@media" in page


@pytest.mark.parametrize("key, days", [("today", 1), ("7d", 8), ("30d", 31)])
def test_admin_daily_chart_uses_actual_values_and_jst_dates(key: str, days: int) -> None:
    period = insights.admin_period(key, now=datetime(2026, 10, 19, 15, 30, tzinfo=JST))
    page = render_usage_admin(
        {"period": period, "daily": [{"day": period.start.date(), "requests": 37}]}
    )
    assert f"{period.start.date()}: 37回" in page
    assert page.count('class="daily-bar') == days
    assert page.count(": 0回</title>") == days - 1
    assert '<text x="24" y="39.0" text-anchor="end">40</text>' in page
    assert "期間の最初と最後の日は一部の時間のみ" in page
    assert "日別の作業回数" in page


def test_admin_attention_labels_links_unknowns_and_escaping_survive_layout() -> None:
    period = insights.admin_period("7d")
    row = {
        "occurred_at": period.end,
        "who": "<社員>",
        "user_id": "U0123456789",
        "query_text": "<質問>",
        "skill": "search",
        "status": "ok",
        "found": False,
    }
    page = render_usage_admin(
        {
            "period": period,
            "summary": {
                "current": {
                    "users": 1,
                    "requests": 17,
                    "attention_events": 2,
                    "not_found": 1,
                    "unknown_search": 11,
                    "cost_usd": 0.05,
                },
                "previous": {"users": 1, "requests": 20, "attention_events": 1},
            },
            "feedback": {
                "up": 3,
                "down": 1,
                "previous_down": 0,
                "downs": [{"created_at": period.end, "who": "unknown", "query": "<評価>"}],
            },
            "errors": [row, {**row, "status": "error"}],
            "questions": [row],
        }
    )
    assert 'class="badge empty">検索で該当なし' in page
    assert 'class="badge error">エラー・拒否・該当なし' in page
    assert 'class="badge feedback">👎 評価' in page
    assert 'href="/admin/history?user=U0123456789"' in page
    assert 'href="/admin/history">会話を読む' in page
    assert "判定の記録がない検索<strong>11件</strong>（不明・0件扱いしません）" in page
    assert "要注意は3件" in page
    assert "利用は増加" not in page
    assert "&lt;質問&gt;" in page and "&lt;評価&gt;" in page and "&lt;社員&gt;" in page
    assert "<質問>" not in page and "<社員>" not in page
    assert page.index("先に確認するやり取り") < page.index("最近の質問</h2>")

    class Attributes(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            assert not any(name == "style" or name.startswith("on") for name, _ in attrs)
            if tag in {"script", "link", "img"}:
                assert not any(name in {"src", "href"} for name, _ in attrs)

    Attributes().feed(page)


def test_admin_analysis_matches_negative_feedback_and_rejects_invented_counts() -> None:
    class Client:
        def __init__(self, text: str) -> None:
            self.text = text

        def converse(self, **kwargs):
            return SimpleNamespace(text=self.text)

    questions = [{"query_text": "資料", "who": "user@example.com", "status": "ok"}]
    downs = [{"query": "資料", "who": "USER@example.com"}]
    valid = '{"themes":[{"title":"資料","question_ids":[1]}],"actions":["確認","改善","検証"]}'
    result = insights.analyze_questions(questions, downs, {}, client=Client(valid), request_id="r")
    assert result["blocked_themes"] == [{"title": "資料", "count": 1}]
    with pytest.raises(ValueError):
        insights.analyze_questions(
            questions,
            downs,
            {},
            client=Client(valid.replace('"question_ids":[1]', '"question_ids":[999]')),
            request_id="r",
        )
    with pytest.raises(ValueError):
        insights.analyze_questions(
            questions,
            downs,
            {},
            client=Client(valid.replace('"title":"資料"', '"title":"資料","count":999')),
            request_id="r",
        )


def test_admin_renderer_links_only_valid_slack_ids_and_escapes_xss() -> None:
    period = insights.admin_period("7d")
    page = render_usage_admin(
        {
            "period": period,
            "users": [
                {
                    "who": "<script>alert(1)</script>",
                    "requests": 1,
                    "previous_requests": 0,
                    "user_id": '" onmouseover="bad',
                }
            ],
            "questions": [
                {
                    "occurred_at": period.end,
                    "who": "社員",
                    "skill": "search",
                    "query_text": "<img src=x onerror=alert(1)>",
                },
                {
                    "occurred_at": "<bad time>",
                    "who": "社員",
                    "skill": "search",
                    "query_text": "日時の記録が不正な質問",
                },
            ],
        }
    )
    assert "<script>alert(1)</script>" not in page
    assert "<img src=x" not in page
    assert "history?user=" not in page
    assert "&lt;img" in page
    assert '<span class="when">&lt;bad time&gt;</span>' in page
    assert "innerHTML" not in page


def test_admin_analysis_empty_sample_never_calls_bedrock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        insights.BedrockClient,
        "from_env",
        lambda **kwargs: pytest.fail("Bedrock must not be called"),
    )
    with pytest.raises(ValueError, match="no_questions"):
        insights.analyze_questions([], [], {}, request_id="r")


def test_admin_masks_non_http_urls_and_ids_adjacent_to_japanese() -> None:
    masked = insights.redact_question(
        "資料はU0123456789が持つ。ftp://files.example.invalid/private と gdrive://DOC123 を参照"
    )
    for secret in ("U0123456789", "files.example.invalid", "DOC123"):
        assert secret not in masked
