"""検索の二段返し（ヒット先出し → 回答後追い）の共通契約。

なぜ分離するか:
    ① この契約は **MCP ゲート層（mcp_gateway.server）と Skill 層（skills.search.skill）の両方**
       が参照する。skills.search.skill は boto3 / psycopg / LocalE5 を import する重いモジュール
       なので、ゲート層から import すると MCP プロセスの起動が重くなる。契約定数だけを
       依存ゼロのこのモジュールへ置き、両者が同じ文字列を見る（文字列の二重定義＝ドリフト防止）。
    ② 「どこへ投げてよいか」の判断（宛先ガード）を純関数にして単体テスト・変異テストの
       的にする。後追い投稿は **人に届く送信** なので、宛先の決定ロジックは検索本体の
       モックだらけのテストではなく、この純関数側で守る。

設計（USE_SEARCH_TWO_STAGE、既定 OFF）:
    - ON のとき search skill は「ヒット一覧＋続きを投げる旨の定型文」を即座にツール応答として
      返し、回答文（Bedrock 要約）はバックグラウンドで生成して **発信元スレッド**へ後追い投稿する。
    - 宛先は ctx.metadata の channel_id / thread_ts。無ければ依頼者本人の DM（user_email）。
      **どちらも無ければ後追いしない**（＝二段返し自体を諦めて従来どおり同期要約に落とす）。
      metadata に無いチャンネルへは絶対に投げない（既定チャンネルへのフォールバックを持たない）。
    - 適用面は MCP ゲート経由の search tool のみ。ゲートが ctx.metadata[TWO_STAGE_CTX_KEY] を
      立てた呼び出しだけが対象で、connect-web(/app) と runtime/slack_bot.py の直呼び、
      knowledge_deliver が内部で回す search は対象外（二重投稿・/app 破壊を構造的に防ぐ）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

# 二段返しの段階ゲート（既定 OFF）。イメージ配備 → env の順でのみ ON にする。
TWO_STAGE_ENV = "USE_SEARCH_TWO_STAGE"

# MCP ゲート層が「この呼び出しは Slack 面の search tool である」と印を付ける ctx.metadata キー。
# skill 側はこの印が無い呼び出し（/app・slack_bot 直呼び・knowledge_deliver 内部呼び）では
# 二段返しを行わない。
TWO_STAGE_CTX_KEY = "search_two_stage_allowed"

# ツール応答に載せる定型文。OpenClaw（@Aico）が「自分で回答を書き足す」のではなく
# 「ヒット一覧＋この一文をそのまま返す」よう SOUL 側にも同趣旨を書く
# （docs/INTEGRATION_search_speed.md の SOUL 文面案）。
TWO_STAGE_NOTICE = "🔎 該当資料を先にお出しします。詳細な考察を続けてこのスレッドに投稿します。"

# markdown リンク → Slack mrkdwn リンク。後追い投稿は OpenClaw を経由せず bot が直接
# chat.postMessage するため、`[ラベル](URL)` のままだとリテラル表示になる。
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def two_stage_enabled() -> bool:
    """USE_SEARCH_TWO_STAGE（既定 OFF）。

    呼び出し時に読む（__init__ 時ではない）。SearchSkill は常駐シングルトンで、
    env 切替のたびにプロセスを作り直せないため、_source_links_enabled と同じ流儀に揃える。
    """
    return os.environ.get(TWO_STAGE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class FollowupTarget:
    """後追い投稿の宛先。channel を優先し、失敗時のフォールバック用に email も運ぶ。"""

    channel_id: str | None
    thread_ts: str | None
    email: str | None

    @property
    def kind(self) -> str:
        """ "channel"（発信元スレッド）か "dm"（依頼者本人）か。ログ用の粒度。"""
        return "channel" if self.channel_id else "dm"


def _clean_str(value: Any) -> str | None:
    """metadata の値を「非空の文字列」だけに正規化する（それ以外は None）。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def resolve_followup_target(metadata: dict[str, Any] | None) -> FollowupTarget | None:
    """後追い投稿の宛先ガード。**ctx.metadata に在るものにしか投げない**。

    - channel_id があれば その チャンネル/スレッド（thread_ts があればスレッド返信）。
    - channel_id が無く user_email があれば 依頼者本人の DM。
    - どちらも無ければ **None**（＝後追いしない）。既定チャンネル・運用チャンネル等への
      フォールバックは設けない（宛先ミスは「他人の受信箱に社内資料の要約が届く」事故に直結する）。
    """
    meta = metadata or {}
    channel_id = _clean_str(meta.get("channel_id"))
    thread_ts = _clean_str(meta.get("thread_ts"))
    email = _clean_str(meta.get("user_email"))
    if not channel_id and not email:
        return None
    return FollowupTarget(
        channel_id=channel_id,
        thread_ts=thread_ts if channel_id else None,
        email=email,
    )


def to_slack_mrkdwn(text: str) -> str:
    """`[ラベル](URL)` を Slack mrkdwn の `<URL|ラベル>` に変換する（他は素通し）。"""
    if not text:
        return text
    return _MD_LINK_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", text)
