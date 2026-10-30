"""ad_label_check Skill 本体: 投稿URLのタイアップ表示（媒体ラベル）と #PR 等の表記を確かめる。

URL を媒体ごとに分け、媒体ごとに Apify actor を 1 run（並列）で叩く。
  TikTok    = clockworks/tiktok-scraper     isSponsored（有料パートナーシップ）/ isAd
  Instagram = apify/instagram-scraper       paidPartnership / sponsors
  YouTube   = streamers/youtube-scraper     isPaidContent（プロモーションを含みます）
取れなかった投稿は「表示なし」と書かない（誤った安心を与えないため）。
"""

from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

from pydantic import BaseModel

from teamagent.adapters.apify_client import (
    AdLabelPost,
    ApifyClient,
    ApifyError,
    ad_label_platform,
)
from teamagent.adapters.cost_guard import CostGuard, CostLimitExceededError
from teamagent.skills._shared.rollout import ROLLOUT_DENIED_MESSAGE, rollout_allowed
from teamagent.skills.ad_label_check.schema import (
    AdLabelCheckInput,
    AdLabelCheckOutput,
    AdLabelResult,
)
from teamagent.skills.base import BaseSkill, SkillContext, register

_ALLOWLIST_ENV = "AD_LABEL_CHECK_ALLOWED_EMAILS"
# OpenClaw の tool timeout(300s) 内に 3 媒体の並列 run を収める。
_DEADLINE_S = 200
_SECTIONS = (("tiktok", "TT"), ("instagram", "IG"), ("youtube", "YT"))
# 広告であることを示すハッシュタグ（NFKC + 小文字で完全一致・#PRADA 等は拾わない）。
DISCLOSURE_TAGS = frozenset(
    {
        "pr", "ad", "ads", "sponsored", "sponsor", "promotion", "paidpartnership",
        "supported", "gifted", "pr案件", "案件", "広告", "提供", "プロモーション", "タイアップ",
    }
)  # fmt: skip
_HASHTAG_RE = re.compile(r"[#＃]([^\s#＃]+)")
_BRACKET_RE = re.compile(r"[【\[(（]\s*(PR|広告|プロモーション|タイアップ)\s*[】\])）]", re.I)


def _norm_tag(tag: str) -> str:
    return unicodedata.normalize("NFKC", tag).strip().lstrip("#").rstrip(".,!?、。！？").lower()


def find_disclosures(text: str, hashtags: tuple[str, ...] | list[str] = ()) -> list[str]:
    """キャプション/説明文とハッシュタグ欄から #PR 等の表記を出現順に返す。"""
    found: list[str] = []
    for tag in [*hashtags, *_HASHTAG_RE.findall(text)]:
        if _norm_tag(tag) in DISCLOSURE_TAGS:
            found.append("#" + unicodedata.normalize("NFKC", tag).lstrip("#"))
    found += [f"【{m}】" for m in _BRACKET_RE.findall(unicodedata.normalize("NFKC", text))]
    unique: dict[str, str] = {}
    for tag in found:
        unique.setdefault(tag.lower(), tag)
    return list(unique.values())


def _line(r: AdLabelResult) -> str:
    if not r.found:
        return f"{r.url}　取得できず（削除・非公開・URL違いの可能性）"
    label = {True: "ラベルOK", False: "ラベルなし", None: "ラベル取れず"}[r.label]
    if r.partners:
        label += "（" + "・".join(f"@{p}" for p in r.partners[:3]) + "）"
    tags = (
        f"ハッシュタグ{'・'.join(r.disclosure_tags[:3])} OK"
        if r.disclosure_tags
        else "ハッシュタグなし"
    )
    return f"{r.url}　{label}　{tags}" + ("　⚠️要確認" if r.needs_review else "")


@register
class AdLabelCheckSkill(BaseSkill[AdLabelCheckInput, AdLabelCheckOutput]):
    """投稿URLを貼るだけで、各媒体のタイアップ表示と #PR 等の表記を媒体別に返す。"""

    name: ClassVar[str] = "ad_label_check"
    description: ClassVar[str] = (
        "TikTok/Instagram/YouTube の投稿URL（改行区切り・混在可・最大20件）を受け取り、各媒体の"
        "タイアップ表示（有料パートナーシップ・タイアップ投稿・プロモーションを含みます）の有無と、"
        "キャプションの #PR #ad #タイアップ 等の表記を、媒体ごと（TT/IG/YT）に1行ずつ返す。"
        "ステマ表記チェック・広告表示の確認・PR表記の確認に使う。再生数などの効果測定は対象外。"
    )
    input_schema: ClassVar[type[BaseModel]] = AdLabelCheckInput
    output_schema: ClassVar[type[BaseModel]] = AdLabelCheckOutput

    def __init__(self, apify: ApifyClient | None = None) -> None:
        self._apify = apify

    def _get_apify(self) -> ApifyClient:
        if self._apify is None:
            self._apify = ApifyClient.from_env(ledger=CostGuard.from_env())
        return self._apify

    def run(self, input: AdLabelCheckInput, ctx: SkillContext) -> AdLabelCheckOutput:
        log = ctx.bind_logger(self.name)
        user = str(ctx.metadata.get("user_email") or ctx.user_id or "")
        if not rollout_allowed(_ALLOWLIST_ENV, user):
            return AdLabelCheckOutput(slack_summary=ROLLOUT_DENIED_MESSAGE)

        by_platform: dict[str, list[str]] = {}
        unsupported: list[str] = []
        for url in input.urls:
            platform = ad_label_platform(url)
            if platform is None:
                unsupported.append(url)
            else:
                by_platform.setdefault(platform, []).append(url)

        apify = self._get_apify() if by_platform else None
        warnings: list[str] = []
        cost = 0.0
        posts: dict[str, AdLabelPost] = {}

        def fetch(platform: str) -> tuple[list[AdLabelPost], float]:
            assert apify is not None
            return apify.ad_label_posts(
                platform,
                by_platform[platform],
                deadline_s=_DEADLINE_S,
                request_id=ctx.request_id,
                user_email=user,
            )

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {p: pool.submit(fetch, p) for p in by_platform}
            for platform, future in futures.items():
                try:
                    got, c = future.result()
                except CostLimitExceededError:
                    warnings.append("今月の取得費用の上限に達したため、一部を確認できませんでした")
                    continue
                except ApifyError as e:
                    warnings.append(f"{platform} の取得に失敗: {str(e)[:80]}")
                    log.warning("ad_label_fetch_failed", platform=platform, error=str(e)[:120])
                    continue
                cost += c
                posts.update({p.input_url: p for p in got})

        results: list[AdLabelResult] = []
        for url in input.urls:
            platform = ad_label_platform(url) or "unsupported"
            post = posts.get(url)
            if post is None:
                results.append(AdLabelResult(url=url, platform=platform))
                continue
            tags = find_disclosures(post.text, post.hashtags)
            results.append(
                AdLabelResult(
                    url=url,
                    platform=platform,
                    found=post.found,
                    label=post.label,
                    partners=list(post.partners),
                    disclosure_tags=tags,
                    needs_review=post.found and post.label is not True and not tags,
                )
            )

        out = AdLabelCheckOutput(results=results, total_cost_usd=round(cost, 4), warnings=warnings)
        out.slack_summary = self._slack_summary(out)
        log.info("ad_label_check_done", urls=len(results), cost_usd=out.total_cost_usd)
        return out

    def _slack_summary(self, out: AdLabelCheckOutput) -> str:
        blocks: list[str] = []
        for platform, head in _SECTIONS:
            rows = [_line(r) for r in out.results if r.platform == platform]
            if rows:
                blocks.append(f"「{head}」\n" + "\n".join(rows))
        other = [r.url for r in out.results if r.platform == "unsupported"]
        if other:
            blocks.append("「対象外」\n" + "\n".join(other))
        if out.warnings:
            blocks.append("⚠️ " + " / ".join(out.warnings))
        return "\n\n".join(blocks)
