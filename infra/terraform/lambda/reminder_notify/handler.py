"""予定リマインド通知 Lambda（v0.3 Task 5）— SQS(teamagent-reminders.fifo) consumer。

EventBridge Scheduler が「予定開始 N 分前」に SQS へ投げた payload を受け、
本人の Slack DM へ chat.postMessage する。stdlib + boto3 のみ（依存パッケージ無し）。

payload（adapters/scheduler_client.py が唯一の生成元）:
  リマインド: {"v": 1, "channel": "D…", "start_hm": "14:00", "url": "https://…", "title": "定例MTG"}
  朝ダイジェスト: {"v": 1, "kind": "digest", "user_ref": "<不可逆hash>", "date": "YYYY-MM-DD"}

kind=digest の分岐（個人別配信時刻・DELTA §1）:
  新しい Lambda は作らない。既存の発火経路にここで 1 分岐足し、morning-digest の
  ECS Scheduled Task を **その 1 人分だけ** RunTask する（digest 本体は Fargate 側の
  scripts/run_morning_digest_fargate.py が既存のまま実行する）。
  ⚠️ payload に channel は入っていない。宛先は Fargate 側が user_ref → 本人 email →
  users.lookupByEmail → conversations.open で **解決し直す**（Scheduler 由来の値を
  宛先として信用する経路を作らない）。
  ⚠️ user_ref は不可逆 hash。ここでは形式検証だけして env としてそのまま渡す
  （Lambda はメールアドレスを一度も見ない）。
  必要な env（未設定なら何もせず skip＝既定 OFF）:
    DIGEST_CLUSTER_ARN / DIGEST_TASK_DEFINITION_ARN / DIGEST_SUBNET_IDS /
    DIGEST_SECURITY_GROUP_IDS / DIGEST_CONTAINER_NAME

設計:
  - batch_size=1（部分失敗の複雑さを持たない）。失敗は raise → SQS リトライ → DLQ
    （DLQ 滞留は CloudWatch Alarm → ops SNS へ・v0.3 §2.5）
  - Slack bot token は Secrets Manager から cold start 時に1回取得
    （env SLACK_BOT_TOKEN 直指定はローカルテスト用）
  - title（≤60字・本人の予定を本人 DM に出す用途）は DM 本文に載せるが、
    **CloudWatch には出さない**（print は件数のみ・PII をログに残さない）。2026-07-14 改訂。
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

_TOKEN_CACHE: dict[str, str] = {}


def _slack_token() -> str:
    direct = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if direct:
        return direct
    if "token" in _TOKEN_CACHE:
        return _TOKEN_CACHE["token"]
    import boto3

    name = os.environ["SLACK_BOT_TOKEN_SECRET_NAME"]
    resp = boto3.client("secretsmanager").get_secret_value(SecretId=name)
    token = str(resp.get("SecretString", "")).strip()
    if not token:
        raise RuntimeError("slack bot token secret is empty")
    _TOKEN_CACHE["token"] = token
    return token


def _post_message(channel: str, text: str) -> None:
    body = json.dumps({"channel": channel, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=body,
        headers={
            "Authorization": f"Bearer {_slack_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        # 失敗は raise → SQS リトライ（最終的に DLQ → ops 通知）。
        raise RuntimeError(f"slack post failed: {payload.get('error', 'unknown')}")


_USER_REF_LEN = 32


def _run_single_user_digest(body: dict[str, Any]) -> bool:
    """kind=digest: morning-digest task を 1 人分だけ起動する。

    戻り値は「起動した」か。env 未設定・payload 不正は False（何もしない）。
    """
    user_ref = str(body.get("user_ref") or "").strip().lower()
    date = str(body.get("date") or "").strip()
    # 形式検証（fail-closed）。hex 32 桁・YYYY-MM-DD 以外は捨てる。
    if len(user_ref) != _USER_REF_LEN or any(c not in "0123456789abcdef" for c in user_ref):
        print(json.dumps({"event": "digest_skip_invalid", "reason": "bad_user_ref"}))
        return False
    if len(date) != 10 or date[4] != "-" or date[7] != "-":
        print(json.dumps({"event": "digest_skip_invalid", "reason": "bad_date"}))
        return False

    cluster = os.environ.get("DIGEST_CLUSTER_ARN", "").strip()
    task_def = os.environ.get("DIGEST_TASK_DEFINITION_ARN", "").strip()
    subnets = [s for s in os.environ.get("DIGEST_SUBNET_IDS", "").split(",") if s.strip()]
    groups = [g for g in os.environ.get("DIGEST_SECURITY_GROUP_IDS", "").split(",") if g.strip()]
    container = os.environ.get("DIGEST_CONTAINER_NAME", "morning-digest").strip()
    if not (cluster and task_def and subnets and groups):
        # 既定 OFF: 個人別配信が点いていない環境では何もしない（予約自体も作られない）。
        print(json.dumps({"event": "digest_skip_disabled"}))
        return False

    import boto3

    resp = boto3.client("ecs").run_task(
        cluster=cluster,
        taskDefinition=task_def,
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": groups,
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": container,
                    "environment": [
                        {"name": "MORNING_DIGEST_MODE", "value": "single"},
                        {"name": "MORNING_DIGEST_USER_REF", "value": user_ref},
                        {"name": "MORNING_DIGEST_DATE", "value": date},
                    ],
                }
            ]
        },
    )
    # ⚠️ RunTask は HTTP 200 を返しつつ failures[] に起動失敗（容量不足・ENI 枯渇等）を
    #    載せる。戻り値を見ずに成功扱いにすると SQS メッセージが消え、拾い手のいない
    #    日に 1 通が無音で落ちる（planner が既定時刻の人を bulk に残すようになった今、
    #    予約発火ぶんの拾い直しは他に無い）。raise → SQS リトライ → DLQ へ載せる。
    failures = resp.get("failures") or []
    if failures:
        # reason は AWS 由来の定型文（PII なし）。個人は依然として出さない。
        reasons = sorted({str(f.get("reason") or "unknown") for f in failures})
        print(json.dumps({"event": "digest_task_failed", "reasons": reasons}))
        raise RuntimeError(f"ecs run_task failed: {','.join(reasons)}")
    # ⚠️ user_ref も date も出さない（ログから個人を追えないようにする）。件数だけ。
    print(json.dumps({"event": "digest_task_started"}))
    return True


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    records = event.get("Records") or []
    for record in records:
        body = json.loads(record["body"])
        if str(body.get("kind") or "") == "digest":
            # False は「起動しなかった」（env 未設定・payload 不正）。起動失敗は
            # _run_single_user_digest が raise するので、ここには来ない。
            if not _run_single_user_digest(body):
                print(json.dumps({"event": "digest_not_started"}))
            continue
        channel = str(body.get("channel") or "")
        if not channel.startswith("D"):
            # DM channel（D…）以外へは投稿しない（defense-in-depth・レビュー L1）。
            # 形式不正はリトライしても直らない＝スキップ（DLQ を汚さない）。
            print(json.dumps({"event": "reminder_skip_invalid", "reason": "not_dm_channel"}))
            continue
        start_hm = str(body.get("start_hm") or "")
        url = str(body.get("url") or "")

        # title/loc は本人の予定（本人 DM 表示用）。山括弧はエスケープしてリンク偽装を防ぐ。
        # end_hm/loc は 2026-08-14 拡張（ユーザー要望「実際の予定を表示させたい」）。
        # 旧 producer の payload には無いキーなので .get で後方互換。
        def _safe(value: Any, limit: int) -> str:
            return str(value or "").replace("<", "＜").replace(">", "＞").strip()[:limit]

        title = _safe(body.get("title"), 60)
        end_hm = str(body.get("end_hm") or "")
        loc = _safe(body.get("loc"), 60)
        if start_hm and end_hm:
            when = f"（{start_hm}〜{end_hm}"
        elif start_hm:
            when = f"（{start_hm}〜"
        else:
            when = ""
        if when:
            when += f"・{loc}）" if loc else "）"
        elif loc:
            when = f"（{loc}）"
        if title:
            text = f"🔔 まもなく: *{title}* {when}".rstrip()
        else:
            text = f"🔔 まもなく予定があります{when}"
        if url:
            text += f"\n<{url}|開く>"
        _post_message(channel, text)
        # ⚠️ title（PII）はログに出さない。channel は D で始まる DM id のみ。件数だけ記録。
        print(json.dumps({"event": "reminder_sent", "had_title": bool(title)}))
    return {"ok": True, "count": len(records)}
