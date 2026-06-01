"""VideoApproval Skill (動画一次FB審査) の入出力スキーマ。

編集者が納品した動画を、案件のオリエンシート (必須シーン/必須テロップ/NG事項/
尺・仕様) と照合して、一次フィードバックを自動生成する。仕様: ベクトル社の
動画制作公募フロー (オリエン → 編集者納品 → 一次FB を AI 化)。

4 観点で審査する (ユーザー指定):
1. 必須要素の有無 (必須シーン・必須テロップが含まれるか)
2. NG事項の混入 (入れてはいけない表現・シーンが無いか)
3. テロップ誤植・訴求誤り (誤字脱字・事実誤認・メインメッセージのズレ)
4. 尺・構成・仕様 (指定尺・縦型等フォーマット・ハッシュタグ/メンション指定)
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class OrientationBrief(BaseModel):
    """オリエンシートから抽出した審査の「正解条件」。

    シートの自由記述を構造化したもの。各項目は空でも可 (記載が無い観点は審査スキップ)。
    """

    product_name: str | None = Field(default=None, description="商材・商品名")
    target: str | None = Field(default=None, description="ターゲット (例: 20-30代女性)")
    main_message: str | None = Field(default=None, description="メインメッセージ・訴求点")
    required_scenes: list[str] = Field(default_factory=list, description="必須シーン")
    required_telops: list[str] = Field(
        default_factory=list, description="必須テロップ (焼き込み文字)"
    )
    ng_items: list[str] = Field(
        default_factory=list, description="NG事項 (入れてはいけない表現/シーン)"
    )
    duration_spec: str | None = Field(default=None, description="尺の指定 (例: 30秒以内)")
    format_spec: str | None = Field(default=None, description="フォーマット指定 (例: 縦型9:16)")
    hashtags: list[str] = Field(default_factory=list, description="指定ハッシュタグ")
    mentions: list[str] = Field(default_factory=list, description="指定メンション")
    notes: str | None = Field(default=None, description="その他の指示・注意事項")

    def to_prompt_block(self) -> str:
        """オリエンを Gemini プロンプト用のテキストに整形する。"""
        lines: list[str] = []

        def add(label: str, val: str | None) -> None:
            if val:
                lines.append(f"- {label}: {val}")

        def add_list(label: str, vals: list[str]) -> None:
            if vals:
                lines.append(f"- {label}:")
                lines.extend(f"    - {v}" for v in vals)

        add("商材", self.product_name)
        add("ターゲット", self.target)
        add("メインメッセージ", self.main_message)
        add_list("必須シーン", self.required_scenes)
        add_list("必須テロップ", self.required_telops)
        add_list("NG事項", self.ng_items)
        add("尺の指定", self.duration_spec)
        add("フォーマット指定", self.format_spec)
        add_list("指定ハッシュタグ", self.hashtags)
        add_list("指定メンション", self.mentions)
        add("その他注意", self.notes)
        return "\n".join(lines) if lines else "(オリエン情報なし)"


class VideoApprovalInput(BaseModel):
    """VideoApproval Skill の入力。

    動画は url (Drive/YouTube 等) か、bytes を直接渡す経路の両対応。
    オリエンは構造化済み OrientationBrief を渡す (シート読取は呼び出し側で実施)。
    """

    orientation: OrientationBrief = Field(description="案件のオリエン (審査の正解条件)")
    video_url: str | None = Field(default=None, description="動画 URL (Drive/YouTube 等)")
    editor_name: str | None = Field(default=None, description="編集者名 (FB の宛先表示用)")


class ApprovalIssue(BaseModel):
    """1 件の指摘。"""

    category: str = Field(description="観点: 必須要素/NG事項/テロップ/尺・構成・仕様")
    severity: str = Field(description="重大度: must_fix (要修正) | suggestion (任意) ")
    timecode: str | None = Field(default=None, description="該当タイムコード (MM:SS、分かる場合)")
    detail: str = Field(description="何が問題か")
    fix: str | None = Field(default=None, description="どう直すか (修正指示)")


class VideoApprovalOutput(BaseModel):
    """VideoApproval Skill の出力 (一次FB)。"""

    verdict: str = Field(description="総合判定: OK | 要修正 | 確認要")
    summary: str = Field(description="一行サマリ (編集者向け)")
    issues: list[ApprovalIssue] = Field(default_factory=list, description="観点別の指摘リスト")
    feedback_text: str = Field(description="そのまま Slack/シートに貼れる FB 本文")
    model_id: str | None = None
    total_cost_usd: float = Field(default=0.0, ge=0.0)
