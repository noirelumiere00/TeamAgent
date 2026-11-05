"""直近の投稿をアカウントから取り込む一回きりの補完（2026-11-02・小俣さん「Apify OK・最新動画も更新して」）。

mcp のタスク定義で動かす（APIFY_API_TOKEN と DATABASE_URL がある）。シート由来の動画には触らない:
vl_videos に無い動画だけ挿入し、その動画の数値（公開値＝total_*）を当日の行で入れる。
  VL_APPLY=0  … 対象アカウント数と概算費用だけ（Apify を呼ばない・DB に書かない）
  VL_APPLY=1  … 取得して書く
"""

import json
import os
import re
import sys
import unicodedata
from datetime import UTC, datetime, timedelta, timezone

from teamagent.adapters.apify_client import ACTOR_TIKTOK_VIDEO, ApifyClient
from teamagent.adapters.pgvector_client import PgVectorClient

JST = timezone(timedelta(hours=9))
APPLY = os.environ.get("VL_APPLY") == "1"
OLDEST = os.environ.get("VL_OLDEST", "2026-10-10")
PER_ACCOUNT = int(os.environ.get("VL_PER_ACCOUNT", "30"))
BATCH = 20
EXTRA = [h for h in os.environ.get("VL_EXTRA_HANDLES", "").split(",") if h]
OPERATORS = ("NewsTV", "ベクトル（VCL）", "ベクトル九州", "イニシャル")
TAG = re.compile(r"#([^\s#【】\[\](){}「」『』〈〉《》]+)")
HANDLE = re.compile(r"[A-Za-z0-9_.]{1,24}")


def log(event: str, **kw: object) -> None:
    print(
        "video_library backfill " + json.dumps({"event": event, **kw}, ensure_ascii=False),
        flush=True,
    )


def num(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and v >= 0:
        return float(v)
    if isinstance(v, str) and v.isdigit():
        return float(v)
    return None


def main() -> int:
    pg = PgVectorClient.from_env()
    with pg.connection(
        app_role="teamagent_app",
        user_email=None,
        user_role="admin",
        application_name="vl-backfill-latest",
    ) as c:
        rows = c.execute(
            "SELECT DISTINCT handle FROM vl_accounts WHERE platform='tiktok' AND operator_label = ANY(%s)"
            " AND coalesce(classification,'') NOT LIKE %s",
            (list(OPERATORS), "%未稼働%"),
        ).fetchall()
    handles = sorted({r["handle"] for r in rows} | set(EXTRA))
    handles = [h for h in handles if HANDLE.fullmatch(h)]
    est = round(len(handles) * PER_ACCOUNT * 0.004, 2)
    log(
        "plan",
        accounts=len(handles),
        oldest=OLDEST,
        per_account=PER_ACCOUNT,
        max_cost_usd=est,
        apply=APPLY,
    )
    if not APPLY:
        return 0
    client = ApifyClient.from_env()
    today = datetime.now(JST).date()
    totals = {
        "items": 0,
        "inserted": 0,
        "existing": 0,
        "skipped": 0,
        "pr": 0,
        "runs": 0,
        "failed_batches": 0,
    }
    for i in range(0, len(handles), BATCH):
        part = handles[i : i + BATCH]
        body = {
            "profiles": part,
            "resultsPerPage": PER_ACCOUNT,
            "profileSorting": "latest",
            "profileScrapeSections": ["videos"],
            "excludePinnedPosts": True,
            "oldestPostDateUnified": OLDEST,
            "shouldDownloadCovers": False,
            "shouldDownloadSlideshowImages": False,
            "shouldDownloadVideos": False,
        }
        try:
            res = client.run_actor_sync(
                ACTOR_TIKTOK_VIDEO,
                body,
                max_items=PER_ACCOUNT * len(part),
                deadline_s=900,
                request_id=f"vl-backfill-{i}",
                user_email="<actor-email>",
            )
        except Exception as exc:  # 1 バッチの失敗で全体を止めない
            totals["failed_batches"] += 1
            log("batch_failed", start=i, error=type(exc).__name__)
            continue
        totals["runs"] += 1
        with pg.connection(
            app_role="teamagent_app",
            user_email=None,
            user_role="admin",
            application_name="vl-backfill-latest",
        ) as c:
            for it in res.items:
                totals["items"] += 1
                vid, url = str(it.get("id") or ""), it.get("webVideoUrl")
                author = it.get("authorMeta") or {}
                handle = author.get("name")
                if (
                    not vid.isdigit()
                    or not isinstance(url, str)
                    or not url.startswith("https://www.tiktok.com/@")
                ):
                    totals["skipped"] += 1
                    continue
                caption = it.get("text") or None
                tags = list(
                    dict.fromkeys(TAG.findall(unicodedata.normalize("NFKC", caption or "")))
                )
                is_pr = any(t.casefold() == "pr" for t in tags)
                posted = it.get("createTimeISO")
                meta, music = it.get("videoMeta") or {}, it.get("musicMeta") or {}
                cur = c.execute(
                    """INSERT INTO vl_videos (platform, video_id, url, account_name, account_handle, posted_at,
                         duration_seconds, music_name, music_author, music_original, caption, hashtags, origin, is_pr)
                       VALUES ('tiktok',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'own',%s)
                       ON CONFLICT (platform, video_id) DO NOTHING""",
                    (
                        vid,
                        url.split("?")[0],
                        author.get("nickName") or handle,
                        handle,
                        posted,
                        num(meta.get("duration")),
                        music.get("musicName"),
                        music.get("musicAuthor"),
                        music.get("musicOriginal")
                        if isinstance(music.get("musicOriginal"), bool)
                        else None,
                        caption,
                        tags,
                        is_pr,
                    ),
                )
                if cur.rowcount != 1:
                    totals["existing"] += 1
                    continue
                totals["inserted"] += 1
                totals["pr"] += int(is_pr)
                c.execute(
                    """INSERT INTO vl_metrics (platform, video_id, captured_on, total_plays, total_likes,
                         total_comments, total_shares, total_saves)
                       VALUES ('tiktok',%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (
                        vid,
                        today,
                        num(it.get("playCount")),
                        num(it.get("diggCount")),
                        num(it.get("commentCount")),
                        num(it.get("shareCount")),
                        num(it.get("collectCount")),
                    ),
                )
        log("batch_done", start=i, accounts=len(part), batch_items=len(res.items), **totals)
    log("complete", finished_at=datetime.now(UTC).isoformat(), **totals)
    return 0 if totals["runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
