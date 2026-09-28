"""clip_proposal（2秒で切り抜くん）— ビデオリリース本編から切り抜き提案 PPTX を組む。

計画 §2-2 / §3 C-10。本 PR はテンプレ差し替えの手前まで（入力スキーマ・解析パイプライン・
非同期ジョブ・テンプレ差し替え層・注意文・上限）を入れる。MCP へのツール登録と
OC の 4 点セットは便C、切り抜き MP4 の埋め込みは便D。
"""

from __future__ import annotations

__all__ = ["analysis", "inventory", "limits", "notices", "schema", "skill", "template_fill"]
