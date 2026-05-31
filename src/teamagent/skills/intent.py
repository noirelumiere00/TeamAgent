"""メッセージ内容から起動すべき Skill を自動判定する (DB/LLM 非依存・純ロジック)。

ユーザーがスラッシュコマンドを使わずに @メンションするだけで、
「○○社のカルテ」→ clientkarte、「○○の提案作って」→ proposal_draft、
それ以外 → search、と自動振り分けするためのヒューリスティック。

曖昧なら search にフォールバック (search は RAG で大抵の質問に答えられる安全側)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# proposal_review 起動トリガー: 「レビュー/添削/診断/チェック」。draft より先に判定。
# 「フィードバック」は営業 FB と紛らわしいので採らない。
_REVIEW_RE = re.compile(r"レビュー|添削|診断|ブラッシュアップ|提案.{0,4}チェック|チェックして")

# proposal_draft 起動トリガー: 「提案」+ 作成系動詞、または「ドラフト/たたき台/骨子」
_DRAFT_RE = re.compile(
    r"提案.{0,6}(作|つく|ドラフト|たたき台|骨子|考え|練|まとめ)"
    r"|(作|つく|考え).{0,4}提案"
    r"|ドラフト|たたき台|提案骨子|どう提案"
)

# clientkarte 起動トリガー: 「カルテ」または「(の)状況/近況/温度感/履歴/どうなってる」等
_KARTE_TRIGGER = re.compile(r"カルテ|近況|温度感|どうなって|どんな感じ|今どう|状況|経緯|履歴")

# clientkarte のクライアント名抽出: トリガー語の手前の固有名詞っぽい部分を取る
_KARTE_EXTRACT = re.compile(
    r"(.+?)(?:の|は|って|が|に関して|について)?\s*"
    r"(?:カルテ|近況|温度感|どうなって|どんな感じ|今どう|状況|経緯|履歴)"
)

# 末尾に残りやすい助詞・記号 (抽出後のトリミング用)
_TRAILING = re.compile(r"[\s　]*(?:の|は|って|が|を|に|へ|、|。|！|!|？|\?)+$")


# 動画 URL (YouTube/Shorts/TikTok/Instagram)。検出したら video_analysis へ。
# Slack は <https://...> のように山括弧で包むことがあるため、それも許容して抽出する。
_VIDEO_URL_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(?:youtube\.com/\S+|youtu\.be/\S+|m\.youtube\.com/\S+"
    r"|tiktok\.com/\S+|instagram\.com/(?:reel|p)/\S+)"
)


# 一度に受け付ける動画 URL の上限 (コスト/レイテンシ防御)
MAX_VIDEO_URLS = 20


@dataclass(frozen=True)
class SkillIntent:
    """自動ルーティングの判定結果。"""

    skill: str  # search|clientkarte|proposal_draft|proposal_review|video_analysis
    client_name: str | None  # clientkarte のときのみ抽出
    reason: str
    video_url: str | None = None  # video_analysis 単一のときの先頭 URL (後方互換)
    video_urls: tuple[str, ...] = ()  # video_analysis の全 URL (複数一括対応)


def _clean_url(raw: str) -> str:
    """Slack の <url> / <url|label> 括りを剥がす。"""
    return raw.split("|", 1)[0].rstrip(">")


def extract_video_urls(message: str, *, limit: int = MAX_VIDEO_URLS) -> list[str]:
    """メッセージから動画 URL を全て抽出する (重複除去・登場順・上限 limit)。"""
    seen: list[str] = []
    for m in _VIDEO_URL_RE.findall(message):
        # findall はグループ無しパターンなので全文マッチが返る
        url = _clean_url(m if isinstance(m, str) else m[0])
        if url not in seen:
            seen.append(url)
        if len(seen) >= limit:
            break
    return seen


def extract_video_url(message: str) -> str | None:
    """メッセージから動画 URL を 1 つ (先頭) 抽出する。Slack の <...> 括りも剥がす。"""
    urls = extract_video_urls(message, limit=1)
    return urls[0] if urls else None


def _extract_client_name(message: str) -> str | None:
    m = _KARTE_EXTRACT.match(message.strip())
    if not m:
        return None
    name = _TRAILING.sub("", m.group(1).strip())
    # 1 文字や空は固有名詞として弱いので採用しない
    return name if len(name) >= 2 else None


def detect_skill(message: str) -> SkillIntent:
    """メッセージから起動 Skill を判定する。

    優先順位: proposal_draft (明確な作成意図) → clientkarte (カルテ/状況) → search (既定)。
    """
    text = message.strip()

    # 0. 動画 URL があれば最優先で動画分析 (URL は強い意図シグナル)。複数対応。
    video_urls = extract_video_urls(text)
    if video_urls:
        return SkillIntent(
            skill="video_analysis",
            client_name=None,
            reason=f"video url detected ({len(video_urls)})",
            video_url=video_urls[0],
            video_urls=tuple(video_urls),
        )

    # 1a. 提案レビュー意図 (レビュー/添削/診断)。draft より先に判定。
    if _REVIEW_RE.search(text):
        return SkillIntent(skill="proposal_review", client_name=None, reason="review trigger")

    # 1b. 提案ドラフト作成意図 (動詞が明確)
    if _DRAFT_RE.search(text):
        return SkillIntent(skill="proposal_draft", client_name=None, reason="draft trigger")

    # 2. クライアントカルテ (トリガー + クライアント名が抽出できたときのみ)
    if _KARTE_TRIGGER.search(text):
        client = _extract_client_name(text)
        if client:
            return SkillIntent(
                skill="clientkarte", client_name=client, reason="karte trigger + client"
            )

    # 3. 既定は横断検索
    return SkillIntent(skill="search", client_name=None, reason="default search")
