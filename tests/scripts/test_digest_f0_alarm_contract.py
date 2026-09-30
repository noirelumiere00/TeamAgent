"""F0（PR-0a）の CloudWatch 警報の契約テスト: tf の pattern と、コードが実際に出す JSON の対応。

infra/terraform/morning_digest_schedule.tf の 3 つの metric filter の pattern を取り出し、
本物のコードを **別プロセス** で動かして出た JSON ログ（configure_logging を実際に通した
STRUCTLOG_FORMAT=json の出力）に当てて、拾うべき行を拾い、拾うべきでない行を拾わないことを
確かめる。イベント名・reason・section のどれかを片方だけ変えると赤になる（tf も Python も
単体では正しいまま、警報だけが無音に戻る事故を防ぐ）。

動かす本物のコード:
  - 朝ダイジェストの skill（本物の GmailClient / GCalendarClient に、本物の RefreshError /
    HttpError を投げる service を注入。helpers は tests/skills/test_morning_digest_fetch_status.py）
  - runner の main()（DATABASE_URL 欠落と、RDS が例外なしで 0 行を返す形）

pattern の評価は CloudWatch の JSON フィルタの部分集合（``$.a = "x"``・``$.a = 1``・``!=``・
``&&``・``||``・括弧）を小さく実装して行う。ワイルドカードなど未対応の書き方が pattern に
入ったら、評価器が例外で止まる（黙って一致扱いにしない）。

⚠️ tests/scripts/test_terraform_runtime_guard.py とは別ファイル（あちらは実行が長い）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TF = PROJECT_ROOT / "infra" / "terraform" / "morning_digest_schedule.tf"
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"
SKILL_HELPERS = PROJECT_ROOT / "tests" / "skills" / "test_morning_digest_fetch_status.py"

# (metric filter と alarm の名前（同名）, metric 名, threshold, period)
ALARMS = [
    ("morning_digest_fetch_failed", "MorningDigestFetchFailed", 1, 300),
    ("morning_digest_reauth_needed", "MorningDigestReauthNeeded", 3, 3600),
    ("morning_digest_target_fetch_failed", "MorningDigestTargetFetchFailed", 1, 300),
]


# ── tf の読み取り ─────────────────────────────────────────────────────────────


def _block(kind: str, name: str) -> str:
    text = TF.read_text(encoding="utf-8")
    marker = f'resource "{kind}" "{name}" {{'
    start = text.index(marker)
    depth = 0
    for index in range(text.index("{", start), len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated block: {kind}.{name}")


def _pattern(name: str) -> str:
    block = _block("aws_cloudwatch_log_metric_filter", name)
    found = re.search(r'pattern\s*=\s*"((?:[^"\\]|\\.)*)"', block)
    assert found, f"pattern が見つからない: {name}"
    return found.group(1).replace('\\"', '"')


# ── CloudWatch JSON フィルタの部分集合の評価器 ─────────────────────────────────

_TOKEN = re.compile(
    r"\s*(?:(?P<op>\{|\}|\(|\)|&&|\|\||!=|=)"
    r"|(?P<sel>\$\.[A-Za-z0-9_.]+)"
    r'|"(?P<str>(?:[^"\\]|\\.)*)"'
    r"|(?P<num>-?\d+(?:\.\d+)?))"
)

Predicate = Callable[[dict[str, Any]], bool]


def _tokenize(pattern: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    stripped = pattern.rstrip()
    while pos < len(stripped):
        m = _TOKEN.match(stripped, pos)
        if not m or m.end() == pos:
            raise ValueError(f"未対応の書き方: {stripped[pos:]!r}")
        kind = m.lastgroup
        assert kind is not None
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def compile_pattern(pattern: str) -> Predicate:
    tokens = _tokenize(pattern)
    pos = 0

    def peek() -> tuple[str, str] | None:
        return tokens[pos] if pos < len(tokens) else None

    def take(expected: str | None = None) -> tuple[str, str]:
        nonlocal pos
        tok = tokens[pos]
        if expected is not None and tok[1] != expected:
            raise ValueError(f"{expected!r} のはずが {tok[1]!r}")
        pos += 1
        return tok

    def atom() -> Predicate:
        tok = peek()
        if tok == ("op", "("):
            take("(")
            inner = or_expr()
            take(")")
            return inner
        kind, sel = take()
        if kind != "sel":
            raise ValueError(f"セレクタのはずが {sel!r}")
        _, op = take()
        if op not in ("=", "!="):
            raise ValueError(f"未対応の比較: {op!r}")
        vkind, raw = take()
        if vkind == "str":
            if "*" in raw:
                raise ValueError("ワイルドカードは未対応")
            value: Any = raw
        elif vkind == "num":
            value = float(raw)
        else:
            raise ValueError(f"値のはずが {raw!r}")
        path = sel[2:].split(".")

        def pred(doc: dict[str, Any]) -> bool:
            cur: Any = doc
            for key in path:
                if not isinstance(cur, dict) or key not in cur:
                    return False  # 無いフィールドは一致しない
                cur = cur[key]
            if vkind == "num":
                ok = isinstance(cur, (int, float)) and not isinstance(cur, bool)
                hit = ok and float(cur) == value
            else:
                hit = isinstance(cur, str) and cur == value
            return hit if op == "=" else (not hit and cur is not None)

        return pred

    def and_expr() -> Predicate:
        preds = [atom()]
        while peek() == ("op", "&&"):
            take("&&")
            preds.append(atom())
        return lambda doc: all(p(doc) for p in preds)

    def or_expr() -> Predicate:
        preds = [and_expr()]
        while peek() == ("op", "||"):
            take("||")
            preds.append(and_expr())
        return lambda doc: any(p(doc) for p in preds)

    take("{")
    result = or_expr()
    take("}")
    if pos != len(tokens):
        raise ValueError("pattern の末尾に余分な字句がある")
    return result


def test_evaluator_is_not_trivially_true() -> None:
    """評価器そのものの健全性（何でも一致にならない・括弧と || が効く）。"""
    pred = compile_pattern('{ $.event = "e" && ($.reason = "a" || $.reason = "b") }')
    assert pred({"event": "e", "reason": "a"})
    assert pred({"event": "e", "reason": "b"})
    assert not pred({"event": "e", "reason": "c"})
    assert not pred({"event": "x", "reason": "a"})
    assert not pred({"reason": "a"})
    assert compile_pattern("{ $.matched = 0 }")({"matched": 0})
    assert not compile_pattern("{ $.matched = 0 }")({"matched": "0"})
    with pytest.raises(ValueError):
        compile_pattern('{ $.event = "morning_*" }')


# ── 本物のコードを別プロセスで動かし、JSON の出力を取る ─────────────────────────

_DRIVER = textwrap.dedent(
    """
    import datetime as dt, importlib.util, json, sys, types

    from teamagent.observability import logging_config
    logging_config.configure_logging()

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    h = load("f0_alarm_skill_helpers", sys.argv[2])
    from teamagent.skills.morning_digest import calendar_window as calwin
    calwin.now_jst = lambda: h.NOW

    def case(name):
        print(json.dumps({"case": name}), flush=True)

    scope403 = h.http_error(403, "insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
    expired = h.refresh_error("invalid_grant", "Token has been expired or revoked.")
    err500 = h.http_error(500, "backendError")

    case("ok")
    h.run_skill(gmail_service=h.two_thread_service(), calendar_result=h.calendar_ok())
    case("temporary")
    h.run_skill(
        gmail_service=h.two_thread_service(refs=h.http_error(503, "backendError")),
        calendar_result=h.calendar_ok(),
    )
    case("invalid_client")
    bad = h.refresh_error("invalid_client", "Unauthorized")
    h.run_skill(gmail_service=h.two_thread_service(refs=bad), calendar_result=bad)
    case("threads_all_failed")
    h.run_skill(
        gmail_service=h.two_thread_service(
            threads={"t1": err500, "t2": err500}, messages={"m1": err500, "m2": err500}
        ),
        calendar_result=h.calendar_ok(),
    )
    case("expired")
    h.run_skill(gmail_service=h.two_thread_service(refs=expired), calendar_result=expired)
    case("mail_scope")
    h.run_skill(gmail_service=h.two_thread_service(refs=scope403), calendar_result=h.calendar_ok())
    case("calendar_scope")
    h.run_skill(gmail_service=h.two_thread_service(), calendar_result=scope403)

    runner = load("f0_alarm_runner", sys.argv[1])
    case("target_missing")
    runner.main()

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, *a): return None
        def fetchall(self): return []

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def cursor(self): return Cur()

    sys.modules["psycopg"] = types.SimpleNamespace(connect=lambda dsn: Conn())
    import os
    os.environ["DATABASE_URL"] = "postgresql://x@localhost/db"
    case("target_zero_rows")
    runner.main()
    case("end")
    """
)

_CLEAR_ENV = (
    "DATABASE_URL",
    "MORNING_DIGEST_USERS",
    "MORNING_DIGEST_EXCLUDE",
    "MORNING_DIGEST_MODE",
    "MORNING_DIGEST_USER_REF",
    "MORNING_DIGEST_ADMIN_REPORT_EMAILS",
    "MORNING_DIGEST_FETCH_STATUS_EMAILS",
    "MORNING_DIGEST_PERSONALIZED",
    "DRAFT_ON_DEMAND_ONLY",
    "MORNING_DIGEST_BRIEF",
    "MORNING_DIGEST_ACK_FILTER",
)


@pytest.fixture(scope="module")
def logs_by_case() -> dict[str, list[dict[str, Any]]]:
    env = {k: v for k, v in os.environ.items() if k not in _CLEAR_ENV}
    env["STRUCTLOG_FORMAT"] = "json"
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(SCRIPT_PATH), str(SKILL_HELPERS)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=100,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    out: dict[str, list[dict[str, Any]]] = {}
    current = "_before"
    for line in proc.stdout.splitlines():
        if not line.startswith("{"):
            continue
        doc = json.loads(line)  # JSON として読めない行があればここで赤
        if set(doc) == {"case"}:
            current = doc["case"]
            out[current] = []
            continue
        out.setdefault(current, []).append(doc)
    assert "end" in out, proc.stdout[-3000:]
    return out


def _hits(pattern_of: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pred = compile_pattern(_pattern(pattern_of))
    return [d for d in docs if pred(d)]


# ── 契約 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("case", "fetch_failed", "reauth", "target"),
    [
        ("ok", 0, 0, 0),
        ("temporary", 1, 0, 0),  # Gmail 503 → メールだけ temporary
        ("invalid_client", 2, 0, 0),  # 設定不備は再連携に数えない（メールと予定で 2 件）
        ("threads_all_failed", 1, 0, 0),  # 一覧は取れたのに 1 スレッドも読めない
        ("expired", 0, 1, 0),  # 失効はメールの節だけ数える＝1 人 1 件
        ("mail_scope", 0, 1, 0),
        ("calendar_scope", 0, 0, 0),  # 予定だけの権限不足は「大量発生」に数えない
        ("target_missing", 0, 0, 1),  # DATABASE_URL 欠落
        ("target_zero_rows", 0, 0, 1),  # RDS が例外なしで 0 行
    ],
)
def test_tf_patterns_match_what_the_code_really_logs(
    logs_by_case: dict[str, list[dict[str, Any]]],
    case: str,
    fetch_failed: int,
    reauth: int,
    target: int,
) -> None:
    docs = logs_by_case[case]
    assert docs, f"{case}: JSON のログが 1 行も出ていない"
    assert len(_hits("morning_digest_fetch_failed", docs)) == fetch_failed
    assert len(_hits("morning_digest_reauth_needed", docs)) == reauth
    assert len(_hits("morning_digest_target_fetch_failed", docs)) == target


def test_matched_lines_carry_no_contents(logs_by_case: dict[str, list[dict[str, Any]]]) -> None:
    """警報が拾う行に、email・件名・相手の名前は入っていない。"""
    blob = json.dumps(logs_by_case, ensure_ascii=False)
    for leak in ("owner@vectorinc.co.jp", "件名A", "件名B", "取引先 太郎", "taro@client.example"):
        assert leak not in blob, leak


@pytest.mark.parametrize(("name", "metric", "threshold", "period"), ALARMS)
def test_alarm_is_wired_to_its_filter_and_fires_as_designed(
    name: str, metric: str, threshold: int, period: int
) -> None:
    filt = _block("aws_cloudwatch_log_metric_filter", name)
    alarm = _block("aws_cloudwatch_metric_alarm", name)
    assert "aws_cloudwatch_log_group.morning_digest.name" in filt
    assert f'name          = "{metric}"' in filt
    assert "namespace     = local.metric_namespace" in filt
    # 値が 0 だと Sum が閾値に永久に届かない。
    assert 'value         = "1"' in filt
    assert 'default_value = "0"' in filt
    assert f'metric_name         = "{metric}"' in alarm
    assert "namespace           = local.metric_namespace" in alarm
    assert 'statistic           = "Sum"' in alarm
    assert f"period              = {period}" in alarm
    # 2 以上にすると「連続 N 期間」になり、1 日 1 回の朝ダイジェストでは二度と鳴らない。
    assert "evaluation_periods  = 1" in alarm
    assert f"threshold           = {threshold}" in alarm
    assert 'comparison_operator = "GreaterThanOrEqualToThreshold"' in alarm
    # 走っていない時間帯の欠測で ALARM に張り付かない。
    assert re.search(r'treat_missing_data\s*=\s*"notBreaching"', alarm)
    assert re.search(r"alarm_actions\s*=\s*\[aws_sns_topic\.alarms\.arn\]", alarm)
