"""Structure v1 display contract; examples are presentation-only, never persisted."""

from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ANALYSIS_KIND = "structure"
ANALYSIS_VERSION = "v1"
TAG_GROUPS = {
    "hook_type": "冒頭の型（フック）",
    "presentation": "本編の見せ方",
    "topic": "題材・切り口",
    "format": "形式",
    "pain": "悩み",
    "cta": "CTA",
}
# One dictionary owns labels and descriptions, ready for a future settings source.
TAG_DESCRIPTIONS = {
    "question": ("問い型", "冒頭で『なぜ？』『知ってた？』と問いかける"),
    "number": ("数字・価格型", "数字や価格を冒頭で提示する"),
    "personal": ("人物・原体験型", "人物の経験から話を始める"),
    "demo": ("実演", "実際に使う様子を見せる"),
    "tasting": ("実食", "食べたときの反応を見せる"),
    "testimonial": ("体験談", "本人の体験を言葉で伝える"),
    "voice": ("生声", "本人の自然な声を伝える"),
    "experience": ("原体験", "体験のきっかけや背景を伝える"),
    "comparison": ("比較", "違いを並べて見せる"),
    "problem": ("問題提起", "視聴者の悩みや課題を示す"),
    "surprise": ("意外な比較", "予想と異なる違いを見せる"),
    "price": ("価格明示", "購入判断に必要な価格を示す"),
    "drama": ("ドラマ風", "物語や会話で伝える"),
    "interview": ("街録風", "街頭インタビューの形式で伝える"),
    "save": ("保存", "後で見返すための保存を促す"),
    "follow": ("フォロー", "アカウントのフォローを促す"),
    "purchase": ("購入", "商品やサービスの購入を促す"),
    "cuts_first_3s": ("3秒で5〜8カット", "冒頭3秒に5〜8カットを配置する"),
    "loop": ("ループ設計", "末尾から冒頭へ自然につながる"),
    "telop_safe": ("テロップ枠内", "文字を画面の安全な領域に収める"),
}
ROLES = {"hook": "冒頭", "pain": "悩み", "demo": "実演", "cta": "行動喚起"}


class Segment(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    start: float = Field(ge=0, le=86400)
    end: float = Field(gt=0, le=86400)
    role: str = Field(max_length=100)
    telop: str = Field(default="", max_length=5000)
    speech: str = Field(default="", max_length=5000)
    visual: str = Field(default="", max_length=5000)

    @model_validator(mode="after")
    def ordered(self) -> Segment:
        if self.end <= self.start:
            raise ValueError("end must follow start")
        return self


class AnalysisPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    segments: list[Segment] = Field(default_factory=list, max_length=100)
    tags: dict[str, list[str]] = Field(default_factory=dict)
    standards: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ordered(self) -> AnalysisPayload:
        if any(a.end > b.start for a, b in zip(self.segments, self.segments[1:], strict=False)):
            raise ValueError("segments must be ordered and non-overlapping")
        if len(self.tags) > 20 or any(
            len(tags) > 30 or any(len(tag) > 200 for tag in tags) for tags in self.tags.values()
        ):
            raise ValueError("too many or oversized tags")
        if len(self.standards) > 30 or any(len(key) > 200 for key in self.standards):
            raise ValueError("too many or oversized standards")
        return self


EXAMPLE_PAYLOAD = {
    "segments": [
        {
            "start": 0,
            "end": 1.8,
            "role": "hook",
            "telop": "知ってた？",
            "speech": "こんな悩みはありませんか？",
            "visual": "商品を手に取り、カメラに見せる",
        },
        {
            "start": 1.8,
            "end": 6,
            "role": "pain",
            "telop": "毎日のひと手間",
            "speech": "準備に時間がかかる…",
            "visual": "困っている場面を見せる",
        },
        {
            "start": 6,
            "end": 14,
            "role": "demo",
            "telop": "使い方は簡単",
            "speech": "実際に試してみます",
            "visual": "手元の実演をアップで見せる",
        },
        {
            "start": 14,
            "end": 18,
            "role": "cta",
            "telop": "保存して試してね",
            "speech": "あとで見返してね",
            "visual": "完成した状態を見せる",
        },
    ],
    "tags": {
        "hook_type": ["question"],
        "presentation": ["demo"],
        "topic": ["problem"],
        "format": ["interview"],
        "pain": ["準備に時間がかかる"],
        "cta": ["save"],
    },
    "standards": {"cuts_first_3s": True, "loop": True, "telop_safe": True},
}


def render_analysis(payload: object = None) -> str:
    """Invalid/absent payloads show a clearly labelled, disabled example."""
    example = False
    try:
        analysis = AnalysisPayload.model_validate(payload)
        if not (analysis.segments or analysis.tags or analysis.standards):
            raise ValueError("empty")
    except (ValidationError, ValueError):
        analysis = AnalysisPayload.model_validate(EXAMPLE_PAYLOAD)
        example = True
    body = '<section class="analysis"><h2>構成</h2>'
    if example:
        body += (
            '<p><span class="badge">近日公開</span> <strong>見本</strong> · '
            "この動画の分析結果ではありません。</p>"
        )
    body += (
        '<p class="muted">区間を選ぶと、テロップ・発話・映像を確認できます。</p>'
        '<div class="timeline">'
    )
    for i, segment in enumerate(analysis.segments):
        # Numeric values have been validated; widths reflect actual durations (including gaps).
        gap = segment.start - (analysis.segments[i - 1].end if i else 0)
        if gap:
            body += (
                f'<style>#gap-{i}{{flex:{gap:g}}}</style><span id="gap-{i}" '
                f'class="timeline-gap" aria-label="未分析区間"></span>'
            )
        label = escape(ROLES.get(segment.role, segment.role))
        body += (
            f"<style>#segment-button-{i}{{flex:{segment.end - segment.start:g}}}</style>"
            f'<button type="button" id="segment-button-{i}" class="segment" '
            f'data-segment="segment-{i}" aria-controls="segment-{i}" aria-expanded="false">'
            f"<span>{segment.start:g}–{segment.end:g}s</span><strong>{label}</strong></button>"
        )
    body += "</div>"
    for i, segment in enumerate(analysis.segments):
        body += (
            f'<div class="segment-content" id="segment-{i}" hidden><h3>'
            f"{escape(ROLES.get(segment.role, segment.role))} · "
            f"{segment.start:g}–{segment.end:g}s</h3><dl>"
        )
        for label, value in (
            ("テロップ", segment.telop),
            ("発話", segment.speech),
            ("映像", segment.visual),
        ):
            body += f"<dt>{label}</dt><dd>{escape(value or '記録なし')}</dd>"
        body += "</dl></div>"
    body += '<div class="tag-groups">'
    groups = {**TAG_GROUPS, "standards": "社内の制作基準（加点要素）"}
    for group, heading in groups.items():
        tags = (
            [key for key, value in analysis.standards.items() if value]
            if group == "standards"
            else analysis.tags.get(group, [])
        )
        body += f'<div class="tag-group {group}"><h3>{heading}</h3><div class="chips">'
        for tag in tags:
            label, description = TAG_DESCRIPTIONS.get(tag, (tag, heading + "のタグ：" + tag))
            tip = f'<span class="tooltip" role="tooltip">{escape(description)}</span>'
            if example:
                body += (
                    f'<span class="tag" tabindex="0" aria-disabled="true">'
                    f"{escape(label)}{tip}</span>"
                )
            else:
                href = "/library/videos?" + urlencode({"tag_group": group, "tag": tag})
                body += f'<a class="tag" href="{escape(href, quote=True)}">{escape(label)}{tip}</a>'
        body += ('<span class="muted">未設定</span>' if not tags else "") + "</div></div>"
    return body + "</div></section>"
