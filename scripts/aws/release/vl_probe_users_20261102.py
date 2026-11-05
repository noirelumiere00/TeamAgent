import json
import os
import re
import time
import urllib.request

from teamagent.ingest.video_library_thumbs import PAGE_USER_AGENT

for h in os.environ["VL_PROBE"].split(","):
    try:
        req = urllib.request.Request(
            f"https://www.tiktok.com/@{h}", headers={"User-Agent": PAGE_USER_AGENT}
        )
        html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
        m = re.search(
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S
        )
        scope = json.loads(m.group(1))["__DEFAULT_SCOPE__"] if m else {}
        ud = scope.get("webapp.user-detail", {})
        u = (ud.get("userInfo") or {}).get("user") or {}
        st = (ud.get("userInfo") or {}).get("stats") or {}
        items = [k for k in scope if "item" in k.lower() or "post" in k.lower()]
        print(
            "video_library probe "
            + json.dumps(
                {
                    "handle": h,
                    "bytes": len(html),
                    "statusCode": ud.get("statusCode"),
                    "uniqueId": u.get("uniqueId"),
                    "nickname": u.get("nickname"),
                    "followers": st.get("followerCount"),
                    "videos": st.get("videoCount"),
                    "scope_keys": sorted(scope)[:12],
                    "item_keys": items,
                },
                ensure_ascii=False,
            )
        )
    except Exception as e:
        print("video_library probe " + json.dumps({"handle": h, "error": type(e).__name__}))
    time.sleep(1)
