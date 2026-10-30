"""ad_label_check の I/O スキーマ（Pydantic v2）。"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

MAX_URLS = 20


class AdLabelCheckInput(BaseModel):
    urls: list[str] = Field(
        min_length=1,
        description=(
            "貼られた投稿URL（TikTok/Instagram/YouTube 混在可・改行区切りのまま可・"
            f"最大{MAX_URLS}件）"
        ),
    )

    @field_validator("urls")
    @classmethod
    def _split_lines(cls, v: list[str]) -> list[str]:
        # 改行区切りの貼り付けが 1 要素で届いても 1 URL ずつに分ける（順序保持・重複除去）。
        urls = [_canonical(u) for raw in v for u in str(raw).split() if u.strip()]
        return list(dict.fromkeys(urls))[:MAX_URLS]


def _canonical(url: str) -> str:
    """Slack の <url|表示名> 形を外し、スキーム無し・http を https へ揃える。

    Slack は「tiktok.com/@a/video/1」のように貼られた URL を http:// のリンクにして届ける。
    """
    url = url.strip().strip("<>").split("|", 1)[0]
    if url.lower().startswith("http://"):
        return "https://" + url[len("http://") :]
    if not url.lower().startswith("https://") and re.match(r"(?:[\w-]+\.)+[a-z]{2,}/", url, re.I):
        return "https://" + url
    return url


class AdLabelResult(BaseModel):
    url: str
    platform: str = Field(description="tiktok | instagram | youtube | unsupported")
    found: bool = False
    label: bool | None = Field(default=None, description="True=表示あり/False=表示なし/None=取れず")
    partners: list[str] = Field(default_factory=list, description="タイアップ相手（IG のみ）")
    disclosure_tags: list[str] = Field(default_factory=list, description="見つかった #PR 等")
    needs_review: bool = False


class AdLabelCheckOutput(BaseModel):
    results: list[AdLabelResult] = Field(default_factory=list)
    slack_summary: str = ""
    total_cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)
