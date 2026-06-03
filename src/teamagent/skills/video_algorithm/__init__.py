"""VSEO 動画アルゴリズム読み解き分析 Skill。

検索KW1つ → 検索上位N本 → 各動画を Gemini で時刻付き構造分析（テロップ/ブランド認識/
フック/CTA/音声/構成）→ 5本横断で「なぜ上位か」を読み解き、HTML タイムラインレポートと
Slack 要約を生成する。設計: docs/v3.2/vseo_video_algorithm_analysis.md
"""
