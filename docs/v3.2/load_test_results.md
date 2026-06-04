# VSEO 負荷/堅牢性テスト 結果（マルチエージェント・全モック）

> 実施: 2026-06-04 / 方式: 5シナリオ + 敵対1 を**並列に実装＋実走**（Gemini/スクレイパ/ffmpeg I/O は全モック＝**GCP/ネットワーク課金ゼロ**）。
> 結論: **失敗ゼロ化の設計は全シナリオで健全**。敵対テストが発見した poller の潜在 race を1件修正済み。

## 結果サマリ

| シナリオ | 結果 | 主要メトリクス |
|---|---|---|
| **10本完璧** | ✅ | analyzed **10/10**・レポートに個別pane×10・統計 n=10 反映・5連続runすべて成功・peak mem 1.2MB・~0.5s/run |
| **並行20件** | ✅ | 20/20完了・例外0・デッドロック0・**19.75x speedup**(直列69s→並列3.5s)・20レポートユニーク・peak 12.3MB |
| **DL失敗50%嵐** | ✅(graceful) | over-fetch 14本中8本失敗→DL可6本→analyzed=6で**捏造せず正直に**(『成功6本』明示)・backfill=1・クラッシュ0・5run決定的 |
| **enumドリフト嵐** | ✅ | 40/40動画 生存・131フィールドを既定値で救済・parse_recovered 40回ログ・None カスケード0・クラッシュ0 |
| **poller 1000件** | ✅ | 冪等性保持(各1回)・二重処理0・初回baseline既読化・性能良好 |
| **敵対: poller並行race** | ⚠️→修正 | 並行 poll_once で**二重処理しうる**race を発見→**claim-before-await で修正**（回帰テスト追加） |

## 検証された堅牢性
- **10本完璧**: 要求10→分析10、レポート(pane/統計付録 n=10)に正しく反映。over-fetch + ThreadPool(3並列) でも整合。
- **並行**: 20件同時でもネスト ThreadPool がデッドロック・スレッド枯渇・レポート競合を起こさない。
- **DL失敗**: 失敗を隠さず正直に少なく出す（生存者バイアス回避）。backfill も機能。
- **enumドリフト**: 寛容パースが enum外/範囲外/型ズレを外科的に既定化し、兄弟フィールドは無傷・全動画生存。

## 敵対テストが見つけた1件（修正済み）
- **症状**: `poll_once` が `await run_one()` の**後**に `store.mark()` していたため、2つの poll_once が同時に走ると同一行を二重処理しうる。
- **本番影響**: poller は単一 `poll_loop` で逐次実行のため**現状は顕在化しない**が、将来の多重起動で危険な潜在バグ。
- **修正**: **claim-before-await** — `await` の前に `mark()` で所有権を確定（asyncio は await 間が非分割なので seen→mark はアトミック）。失敗時は `unmark()` で次ティックへ戻す。回帰テスト `test_concurrent_poll_once_no_double_processing` で並行2本でも各行1回のみを担保。

## 再現
負荷ハーネス: `/tmp/lt_10perfect.py` / `lt_concurrency.py` / `lt_dl_failure_storm.py` / `lt_enum_drift_storm.py` / `lt_poller-load.py` / `lt_concurrent_poller_race.py`。いずれも skill の注入口（gemini/searcher/downloader/proxy）をモックして外部I/Oゼロで実走。
