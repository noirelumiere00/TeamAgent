"""mail_to_internal_context Skill の I/O スキーマ（Pydantic v2）。

⚠️ 戻り値に生メール本文・生件名・生 From・生 messageId を含めないこと（G3）。
メール側はメタデータ由来の「シグナル」（相手ドメイン・件数・最終日時）だけを返し、
社内側は社内ナレッジ（Slack/Drive/FB）の参照（タイトル＋リンク＋短い抜粋）だけを返す。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MailInternalContextInput(BaseModel):
    """メール×社内ナレッジ横断の入力。G5: client_name 必須（無差別走査を構造的に禁止）。"""

    client_name: str = Field(
        min_length=1,
        max_length=100,
        description="対象クライアント/案件名（メール検索と社内検索の核・必須）",
    )
    topic_hint: str | None = Field(
        default=None,
        max_length=200,
        description="案件テーマ（社内検索の文脈に足す任意キーワード）",
    )
    lookback_days: int = Field(
        default=90,
        ge=1,
        le=365,
        description="メール側で遡る期間（日）",
    )
    max_messages: int = Field(
        default=10,
        ge=1,
        le=30,
        description="メール側で走査する最大件数（メタデータのみ・コスト/レイテンシ上限）",
    )
    top_k_internal: int = Field(
        default=6,
        ge=1,
        le=20,
        description="社内ナレッジから突き合わせる参照の上限",
    )


class MailSignal(BaseModel):
    """メール側のシグナル（**メタデータ由来・マスク済み**。本文・生件名は含まない）。"""

    recent_count: int = Field(ge=0, description="対象期間で client にヒットした受信メール数")
    counterpart_domains: list[str] = Field(
        default_factory=list,
        description="やり取り相手のメールドメイン（ローカル部は含めない・最大数件）",
    )
    latest_at: str | None = Field(
        default=None,
        description="直近メールの日時（ISO 文字列・判明時のみ）",
    )


class InternalRef(BaseModel):
    """社内ナレッジ参照 1 件（Slack スレッド/Drive 資料/営業 FB 等。DLP マスク後）。"""

    kind: str = Field(description="参照種別: 'slack' | 'drive' | 'doc' | 'fb' | 'other'")
    title: str = Field(
        default="", max_length=200, description="表示用タイトル（チャネル名/ファイル名等）"
    )
    # slack://… の permalink 化は runtime 層の責務（3層分離: skill は raw を返す）。
    source_uri: str | None = Field(
        default=None,
        description="生の参照 URI（例 slack://CHANNEL/TS）。リンク化は runtime 層の責務",
    )
    drive_url: str | None = Field(default=None, description="Drive 直リンク（判明時）")
    snippet: str = Field(default="", max_length=240, description="抜粋（DLP マスク後・短縮）")
    score: float = Field(default=0.0, ge=0.0, le=1.0, description="関連度（cosine 類似度）")


class MailInternalContextOutput(BaseModel):
    """メール×社内ナレッジ横断の結果。生メール本文・生件名・生 From は一切含まない。"""

    client_name: str
    mail_signal: MailSignal
    internal_refs: list[InternalRef] = Field(default_factory=list)
    summary: str = Field(
        default="",
        description="社内状況の統合サマリ（USE_MAIL_LINK_SUMMARY=on の時のみ・本文は使わない）",
    )
    inbox_owner_masked: str = Field(
        default="", description="参照した受信箱（マスク）。本人性監査用"
    )
    note: str = Field(default="", description="鮮度などの但し書き")
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="この実行の概算コスト")
