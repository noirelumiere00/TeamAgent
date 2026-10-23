"""Pure metric helpers for raw tiktok-acquire posts; no I/O or dependencies."""
from collections.abc import Mapping
from math import isfinite
from statistics import median


STAT_KEYS = ("playCount", "diggCount", "commentCount", "shareCount", "collectCount")


def _known_count(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value >= 0 and (not isinstance(value, float) or isfinite(value)))


def normalize_stats(raw_post):
    """Read a complete raw post (stats + sibling missingFields), without mutation.

    Missing/invalid values become None. Legacy zeros without a valid
    missingFields list are unknown; a measured zero needs an explicit list
    that does not mark that key missing. Do not manufacture that list.
    Normalized aggregates without raw stats cannot recover missingness.
    """
    raw_post = raw_post if isinstance(raw_post, Mapping) else {}
    stats = raw_post.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    missing = raw_post.get("missingFields")
    missing_known = isinstance(missing, list) and all(isinstance(k, str) for k in missing)
    missing = set(missing) if missing_known else set()
    result = {}
    for key in STAT_KEYS:
        value = stats.get(key)
        result[key] = (value if _known_count(value)
                       and f"stats.{key}" not in missing
                       and (value != 0 or missing_known) else None)
    return result


def engagement_rate(stats):
    """Return percent from normalized counts, or None when not calculable."""
    values = [stats.get(key) for key in STAT_KEYS]
    if not all(_known_count(value) for value in values) or values[0] == 0:
        return None
    return sum(values[1:]) / values[0] * 100


def median_views(stats_rows):
    """Summarize normalized, related, deduplicated posts from one query."""
    values, missing_n = [], 0
    for stats in stats_rows:
        value = stats.get("playCount")
        if _known_count(value):
            values.append(value)
        else:
            missing_n += 1
    return {"median": median(values) if values else None,
            "valid_n": len(values), "missing_n": missing_n}


def total_views(stats_rows):
    """Sum one query's normalized related posts; a partial sum stays explicit.

    An empty successfully acquired population totals zero. The caller must
    distinguish acquisition failure before passing an empty population here.
    """
    values, missing_n = [], 0
    for stats in stats_rows:
        value = stats.get("playCount")
        if _known_count(value):
            values.append(value)
        else:
            missing_n += 1
    return {"total": sum(values) if values or not missing_n else None,
            "valid_n": len(values), "missing_n": missing_n,
            "complete": missing_n == 0}
