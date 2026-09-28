"""表示の共通部品（Slack 文面・HTML レポート・LLM への入力で同じ言い方をそろえる）。"""

from __future__ import annotations

import datetime as _dt

CATEGORY_LABEL: dict[str, str] = {
    "brand_official": "公式",
    "media": "メディア",
    "news": "報道",
    "creator": "クリエイター",
    "influencer": "インフルエンサー",
    "ugc": "一般",
    "other": "その他",
    "unknown": "未分類",
}
PLATFORM_LABEL: dict[str, str] = {"tiktok": "TikTok", "instagram": "Instagram"}
JST = _dt.timezone(_dt.timedelta(hours=9))


def category_label(category: str) -> str:
    return CATEGORY_LABEL.get(category, category)


def _one_decimal(value: float) -> str:
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def fmt_count(n: int) -> str:
    """35.1万 / 15万 / 520万 / 8,200 の形。100万以上は小数を付けない。1億以上は「1.2億」。"""
    if n >= 100_000_000:
        return f"{_one_decimal(n / 100_000_000)}億"
    if n >= 1_000_000:
        return f"{round(n / 10_000)}万"
    if n >= 10_000:
        return f"{_one_decimal(n / 10_000)}万"
    return f"{n:,}"


def fmt_pct(share: float) -> str:
    """0-1 の割合を整数 % の文字列に。"""
    return f"{round(share * 100)}%"


def fmt_age(days: int) -> str:
    if days < 1:
        return "今日"
    if days < 60:
        return f"{days}日前"
    if days < 365:
        return f"{days // 30}か月前"
    years, rest = divmod(days, 365)
    months = rest // 30
    return f"{years}年{months}か月前" if months else f"{years}年前"


def fmt_date(epoch: int) -> str:
    return _dt.datetime.fromtimestamp(epoch, JST).strftime("%Y-%m-%d") if epoch > 0 else ""


def fmt_duration(sec: int) -> str:
    if sec <= 0:
        return ""
    minutes, seconds = divmod(sec, 60)
    return f"{minutes}分{seconds:02d}秒" if minutes else f"{seconds}秒"


def fmt_ranks(ranks: list[int], *, limit: int = 5) -> str:
    shown = "・".join(str(r) for r in ranks[:limit])
    return f"{shown}位" + (f" ほか{len(ranks) - limit}本" if len(ranks) > limit else "")


def account_label(author: str, author_name: str) -> str:
    """「表示名（@handle）」。表示名が無い・同じなら @handle だけ。"""
    handle = f"@{author}" if author else "不明"
    if author_name and author_name.strip() and author_name.strip().lower() != author.lower():
        return f"{author_name.strip()}（{handle}）"
    return handle


__all__ = [
    "CATEGORY_LABEL",
    "JST",
    "PLATFORM_LABEL",
    "account_label",
    "category_label",
    "fmt_age",
    "fmt_count",
    "fmt_date",
    "fmt_duration",
    "fmt_pct",
    "fmt_ranks",
]
