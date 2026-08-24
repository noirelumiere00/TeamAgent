# FMT デッキ用フォント資産（お土産資料 便1）

deck spec `assets/deck_specs/omiyage_fmt_v1.json` の
`tokens.typography.font_embedding` が前提とするフル書体。レンダラ
（`teamagent.skills.omiyage_report.fmt.fonts`）がデッキごとに
fonttools で woff2 サブセットを生成し data:URI で HTML へ埋め込む。

| ファイル | 書体 / weight | ライセンス |
|---|---|---|
| ShipporiMinchoB1-SemiBold/-Bold/-ExtraBold.ttf | Shippori Mincho B1 600/700/800 | OFL (OFL-ShipporiMinchoB1.txt) |
| ZenKakuGothicNew-Regular/-Medium/-Bold/-Black.ttf | Zen Kaku Gothic New 400/500/700/900 | OFL (OFL-ZenKakuGothicNew.txt) |
| InstrumentSans-Variable.ttf | Instrument Sans 可変 (wdth,wght) | OFL (OFL-InstrumentSans.txt) |

取得元: github.com/google/fonts main ブランチ ofl/（2026-08-24 取得）。
