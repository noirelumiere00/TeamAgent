import json
import os
import re
import urllib.request

from teamagent.ingest.video_library_thumbs import PAGE_USER_AGENT

for item in os.environ["VL_PROBE"].split(","):
    handle, vid = item.split("/")
    req = urllib.request.Request(
        f"https://www.tiktok.com/@{handle}/video/{vid}", headers={"User-Agent": PAGE_USER_AGENT}
    )
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    m = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S
    )
    if not m:
        print("video_library probe", vid, "bytes", len(html), "no-json")
        continue
    it = json.loads(m.group(1))["__DEFAULT_SCOPE__"]["webapp.video-detail"]["itemInfo"][
        "itemStruct"
    ]
    flags = {
        k: it[k]
        for k in it
        if re.search(r"ad|sponsor|commerce|brand|label|promot|disclos", k, re.I)
        and not isinstance(it[k], (dict, list))
    }
    print(
        "video_library probe "
        + json.dumps(
            {
                "vid": vid,
                "bytes": len(html),
                "stats": it.get("stats"),
                "statsV2": it.get("statsV2"),
                "createTime": it.get("createTime"),
                "author": (it.get("author") or {}).get("uniqueId"),
                "flags": flags,
                "keys": sorted(it)[:80],
            },
            ensure_ascii=False,
        )
    )
