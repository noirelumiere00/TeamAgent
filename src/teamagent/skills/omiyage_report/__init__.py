"""お土産資料（TikTok検索データ確認資料）便1。

営業の「◯◯のお土産資料つくって。競合は△△」を受け、AWS内 tiktok_search の実測
（一般KW・ブランド名・競合名の各検索軸・深掘り120本/軸）と動画解析（DL→フレーム→
視覚AIで界隈クラスタ分類+テロップ読取）から決定論集計を行い、FMT契約
（assets/deck_specs/omiyage_fmt_v1.json の input_contract）準拠の計測JSON
（deck_meta + slide_plan）を出力・S3保存し、レンダラ経由のPPTXを依頼スレッドへ配信する。

正本はローカルSkill tiktok-competitive-analysis の定義（指標定義v2・受付の型・
資料規律）+ 2026-08-24 FMT化ユーザー裁定（U1〜U3）。登場率は caption/hashtag/telop
の3経路で計測し、voice のみ **未計測**（0件ではない）として必ず開示する。
"""
