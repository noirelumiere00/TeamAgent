# Obsidian 完全再現 — フェーズ2（上級機能）実装仕様

§1-4（シェル / グラフ設定パネル / タグ・関連 / 0件UX）が入った後に、本家 Obsidian の体験を「完全再現」に近づけるための上級機能。すべて connect_web の `app.py`（フロント vanilla JS・依存ゼロ）＋ごく一部の backend で実装。読み取り専用ナレッジ文脈にマップする。

## P2-1. クイックスイッチャー（Cmd/Ctrl+O）  ★must
- 本家：`Cmd+O` でモーダル → ファイル名を fuzzy 検索 → Enter で即ジャンプ。
- 我々：`Cmd/Ctrl+O`（または `o`）でモーダル。`/api/v1/graph` の nodes（全資料タイトル）を fuzzy フィルタ（部分一致＋簡易スコア）。↑↓で選択、Enter＝グラフモードならその node にフォーカス／リストモードなら検索に流す、`Cmd+Enter`＝出典を開く。textContent 描画・依存ゼロ。

## P2-2. コマンドパレット（Cmd/Ctrl+P）  ★should
- 本家：`Cmd+P` で全コマンドを fuzzy 実行。
- 我々：登録コマンド＝「グラフ表示/ノート表示の切替」「全体表示(zoom-to-fit)」「ビューをリセット」「左/右サイドバー開閉」「色分け: 資料タイプ/出典/業界/案件」「孤立ノード表示切替」「シミュレーション停止/再開」。fuzzy 選択 → 実行（既存の setMode/zoomToFit/recolor 等を関数として呼ぶ）。

## P2-3. ホバープレビュー  ★should（backend 1点要）
- 本家：リンクにホバー → ノート本文のポップアッププレビュー。
- 我々：結果カード／グラフ node にホバー → タイトル＋全タグ＋**抜粋**＋接続数のポップオーバー。
- **backend 追加**：`list_documents_for_graph`（pgvector）に各 doc の先頭チャンク抜粋を LATERAL JOIN で1つ付与 → `build_graph` の node に `excerpt`（~120字）を additive 追加。RLS は同 conn なので維持。§3 の関連/プレビューパネルでも使える。

## P2-4. キーボードナビゲーション  ★must
- `g`=グラフ / `l`=リスト切替、`f`=全体表示、`r`=リセット、`/`=検索ボックスにフォーカス、`Esc`=フォーカス解除/モーダル閉じ、`Cmd/Ctrl+O`=スイッチャー、`Cmd/Ctrl+P`=パレット。`?` でショートカット一覧オーバーレイ。input にフォーカス中は無効化。

## P2-5. 設定の永続化  ★should
- グラフ設定パネル（フォース4値・色分け・表示スライダー・孤立トグル・ローカル深さ）と、シェルの `{mode,leftOpen,rightOpen}` を localStorage に保存→次回復元（本家の graph settings 記憶に相当）。

## P2-6. 検索オペレータ構文  ★should
- 本家：`tag:`, `path:`, `file:` 等。
- 我々：検索ボックスで `tag:食品` `source:gdrive` `client:ニチレイ` `type:提案書` を解釈 → 該当を filter_industry / metadata_filters / クエリに変換（既存 api_search の filter_industry＋クエリ整形で対応、必要なら軽い前処理）。プレーンテキストは従来通り。

## P2-7. 仕上げ（テーマ/状態）  ★nice
- ローディング/空/エラーの各状態を本家風に（スケルトン・優しい空表示）。
- ノードカード/パネルのトランジション（fade/slide 140ms）。
- ハイコントラスト・フォーカスリング（アクセシビリティ）。

## 実装順（パリティ/工数比）
1. P2-4 キーボード（軽・体験大）→ 2. P2-1 クイックスイッチャー → 3. P2-5 永続化 → 4. P2-3 ホバープレビュー（backend 抜粋付与込み）→ 5. P2-2 コマンドパレット → 6. P2-6 検索オペレータ → 7. P2-7 仕上げ。

## 制約
- 依存ゼロ vanilla JS（社内プロキシでCDN不可）・textContent で XSS 安全・単一 canvas・RLS 不変・既存の滑らか sim とエンジンを壊さない・ruff(E501)/mypy/pytest 緑。
