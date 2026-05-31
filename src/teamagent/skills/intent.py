"""メッセージ内容から起動すべき Skill を自動判定する (DB/LLM 非依存・純ロジック)。

ユーザーがスラッシュコマンドを使わずに @メンションするだけで、
「○○社のカルテ」→ clientkarte、「○○の提案作って」→ proposal_draft、
それ以外 → search、と自動振り分けするためのヒューリスティック。

曖昧なら search にフォールバック (search は RAG で大抵の質問に答えられる安全側)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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


@dataclass(frozen=True)
class SkillIntent:
    """自動ルーティングの判定結果。"""

    skill: str  # "search" | "clientkarte" | "proposal_draft"
    client_name: str | None  # clientkarte のときのみ抽出
    reason: str


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

    # 1. 提案ドラフト作成意図 (動詞が明確なので最優先)
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
