# ラッコキーワード 検索量スクレイパ

VSEO 提案書の検索量データ (月間検索数 / SEO 難易度 / CPC) を、ラッコキーワード
(月660円の有料サブスク) から取得する Node.js スクレイパ。公式 API は高額プランのみ
のため、**既存課金アカウントのログイン済みブラウザセッションを再利用**する方式。

## セキュリティ方針

- **ID/パスワードはコードで一切扱わない。** 初回にユーザーが手動でログインし、その
  セッション cookie を `.userdata/` に永続化 → 以降はそれを再利用する。
- `.userdata/` は cookie=認証情報を含むため **`.gitignore` 済み** (コミットされない)。

## セットアップ

```bash
cd tools/rakko_scraper
npm install            # puppeteer-core (node_modules は gitignore)
```

Chrome/Chromium が必要 (TikTok スクレイパと同様、自動検出 or `CHROMIUM_PATH`)。

## 使い方

### 1. 初回ログイン (手動・1回だけ)

```bash
node scrape.mjs --login
```

→ 画面付きで Chrome が開く → **自分の手でラッコキーワードにログイン** →
ログインできたらウィンドウを閉じる。セッションが `.userdata/` に保存される。

### 2. 検索量取得 (以降は自動・headless)

```bash
node scrape.mjs --query "新宿 ランチ" --out /tmp/rakko.json
node scrape.mjs --queries "新宿 ランチ,新宿 グルメ,新宿 ディナー" --limit 30 --out /tmp/rakko.json
```

- stdout: `{ ok, mode:"query", results: { "<KW>": [{kw,vol,seo,cpc}], ... }, error }`
- ログインセッションが切れたら `ok:false` で返る → 再度 `--login`。

## Python から

`src/teamagent/adapters/rakko_scraper.py` の `fetch_search_volumes(queries)` が
subprocess でこの CLI を呼び、`RakkoResult` (KW→検索量リスト) を返す。

## VSEO パイプラインでの位置づけ

VSEO 提案書の6フェーズのうち**フェーズ5 (検索量取得)** を担う。

```
[TikTok] tiktok_search → top10/multi_kw JSON  (フェーズ1-3, 自動化済)
[ラッコ] fetch_search_volumes → 検索量          (フェーズ5, このツール)
       ↓
VSEO スキルが検索量を6カテゴリに分類 (kw50_categorized.json) → PPTX
```

KW のカテゴリ分類 (指名/本軸/条件/探索/周辺/競合) は**クライアントのブランド文脈が
必要な判断**なので、このツールは生の検索量取得までを担い、分類は VSEO スキルの
LLM ステップ (ブランド情報を持つ) に委ねる。

## 注意

- ラッコの利用規約・レート制限を尊重すること。短時間の大量アクセスは避ける
  (本スクレイパは KW 間に 1.5-3 秒のランダム間隔を入れている)。
- 取得データは VSEO 提案の社内用途に限る。
