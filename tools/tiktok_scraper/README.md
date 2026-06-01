# TikTok 検索スクレイパ

`tiktok_search` Skill が使う Node.js スクレイパ。Apify 等の課金 SaaS を使わず、
ローカルの Chrome を Puppeteer で headless 起動し、TikTok 検索ページの内部 API
レスポンスをネットワーク傍受してキーワード/ハッシュタグの上位動画メタを取得する。

元実装は EC2 で実証済みの `vseo-analytics-web/server/tiktokScraper.ts`
(`searchInIncognitoContext`) を Mac/CLI 向けに移植したもの。

## セットアップ

```bash
cd tools/tiktok_scraper
npm install            # puppeteer-core を入れる (node_modules は gitignore)
```

Chrome/Chromium が必要:
- macOS: `/Applications/Google Chrome.app` を自動検出
- Linux: `/usr/bin/google-chrome` 等を自動検出
- それ以外: 環境変数 `CHROMIUM_PATH` で明示指定

## 使い方 (CLI)

```bash
node search.mjs --query "新宿 ランチ" --type keyword --max 10
node search.mjs --query "新宿"        --type hashtag  --max 10   # タグ空振り時は keyword に自動フォールバック
node search.mjs --query "日焼け止め"  --max 5 --out /tmp/out.json # 結果をファイルにも書く
node search.mjs --query "新宿 カフェ" --headful                  # ブラウザを可視化 (デバッグ用)
```

- 標準出力: JSON のみ (`{ ok, query, type, count, videos: [...], error }`)
- 標準エラー: ブラウザの進捗ログ (Python 側は stdout だけ parse する)

## Python から

`src/teamagent/adapters/tiktok_scraper.py` が subprocess でこの CLI を呼ぶ。
Skill 層 (`skills/tiktok_search`) は adapter の `search_tiktok()` だけを使う。

## 環境変数 (任意)

| 変数 | 用途 |
| --- | --- |
| `CHROMIUM_PATH` | Chrome 実行ファイルの明示指定 |
| `TIKTOK_NODE_BIN` | node 実行ファイルの明示指定 (Python adapter 側) |
| `PROXY_SERVER` / `PROXY_USERNAME` / `PROXY_PASSWORD` | 住宅プロキシ経由にする (任意。大量取得・bot 検出回避用) |

## 注意 / 制約

- プロキシ無しのローカル IP でも検索 API は通る (実証済み) が、TikTok 側の
  bot 検出・地域制限・CAPTCHA により失敗することがある。失敗は JSON の
  `ok:false` + `error` で返る。
- 大量本数 (70本級) を安定取得するには住宅プロキシ + セッションローテーションが
  推奨 (元実装の `searchTikTokTriple` 相当)。本 CLI は単一セッションで上位 N 本を取る。
- 取得するのは公開動画の**メタデータ** (再生数/いいね/説明文/ハッシュタグ等)。
  動画ファイル自体はダウンロードしない。利用は ToS の範囲で。
