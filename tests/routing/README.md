# カタログ第一弾ツールのルーティング検証（CLAUDE.md §10 E2）

OpenClaw(AiLa) の外側ルーター（Haiku 4.5）は **name + description だけ**でツールを選ぶ。
新6ツール（x_voice_search / x_needs_mining / x_buzz_measure(+status) / search_surface_check /
tiktok_comment_mining）が、既存の動画/TikTok系（tiktok_search / tiktok_acquire /
video_algorithm / video_analysis / video_approval）や X系どうしで**取り違えられないこと**を、
出荷前にルーティング・シミュで確認する。

## 成果物
- `catalog_routing_corpus.jsonl` — 期待ラベル付き発話コーパス（正例＋境界/敵対例＋新ツールに
  盗まれてはいけないネガ例）。`expect`=第一候補、`alt_ok`=許容される代替ツール。
- `../skills/test_routing_descriptions_catalog.py` — description のトリガー語・相互排他注記を
  固定する回帰テスト（description を将来いじっても棲み分けが壊れないよう pytest でガード）。

## シミュ手順（新規/変更時に手動で1回）
1. OC 可視ツール（openclaw.config.json5 の toolFilter.include）全部の name+description を集める。
2. Haiku モデルのサブエージェントを「name+description だけで1ツール選ぶルーター」に見立て、
   `catalog_routing_corpus.jsonl` の utterance をブラインドで流す（独立2本以上で多数決）。
3. `expect`（または `alt_ok`）と突き合わせ、誤選択＝混同ペアを description 修正で潰し、再実行。

## 最新結果（2026-07-13・3ラウンド反復で収束）
Haiku 独立シミュ2本 / OC可視28ツール。
- **R1**（硬化前・n=45）: `voice-01`（商材名＋「不満・欲求」）が x_needs_mining に誤流出
  （2本中1本が誤）。
- **R1硬化**: x_voice_search=「商材名が主語なら不満収集もここ」、x_needs_mining=「業界/テーマ
  全体（商材非特定）」を description に明記。既存 tiktok_search にも相互排他注記を追加。
- **R2**（硬化後・n=51、新変種 voice-05/06・needs-05 追加）: **2本とも全件一致・正解**。
- **R3**（未検証の敵対例14・過適合検出）: 13/14一致。`adv-07`（「TikTok検索して上位リストだけ
  先にちょうだい」）が tiktok_search / tiktok_acquire で割れた（「取得」語のトラップ）。
- **R3硬化**: tiktok_search に「今すぐ即時取得／本体DL・大量取得の非同期ジョブは tiktok_acquire」
  を追記。
- **R3b**（再確認）: adv-07 と同期/非同期の変種4件 → **2本とも全件一致・正解**。
- 残る許容ゆらぎ: `boundary-03`（「バズってるか件数で」）は x_buzz_measure/x_voice_search の
  どちらも許容（alt_ok）。実害なし。
- 結論: カタログP.9の全テンプレ＋境界/敵対例で、既存の動画/TikTok系・X系どうしの取り違えは
  解消済み。descriptionを将来いじる時は本 corpus で再シミュし、
  `test_routing_descriptions_catalog.py` の固定点を壊さないこと。
