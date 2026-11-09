# Routing v2：現行規則のローカル基準値

2026-11-09 / Node v25.9.0 / 分類器：`origin/dev` の `9d9a9be0`。plugin の変更なし。
**eval 40件の分野一致は60.0%。初週 enforce 候補の分類合格は0件。**

再現：`node scripts/routing_eval/classify_eval.mjs --split eval > /tmp/routing-v2-eval.json`
（JSON は stdout、テキストは stderr。`--split tune|all` も利用可。）

分野24/40（60.0%、目標90%に未達）、rule_id 25/40（62.5%）、根拠要否31/40（77.5%）、出力34/40（85.0%）。
退避は予測22/40（55.0%）・正解25/40（62.5%）、一致37/40（92.5%）。誤った直接候補はeval-032・033・034。
分類時間はp50 0.016ms／p95 0.658ms（暫定予算20ms以内）。

下表のrule一致は正解rule別の一致数／母数。直接候補適合は予測rule別の直接候補のうち、分野・rule・根拠要否・出力・退避が全一致した数／母数。初週候補3種のみ、両指標95%以上・各N≥5で分類合格（N=0は未達）。

| rule_id | rule一致 n/N | 直接候補適合 n/N | 初週 enforce 候補の分類判定 |
|---|---:|---:|---|
| case_list | 5/5 | 5/6 | 不合格：適合83.3% |
| internal_status | 4/5 | 4/4 | 不合格：一致80%・直接候補N<5 |
| internal_metrics | 4/5 | 4/4 | 不合格：一致80%・直接候補N<5 |
| absent | 1/1 | 0/0 | 対象外 |
| chat | 0/0 | 0/0 | 対象外・評価例なし |
| document_delivery | 1/1 | 0/0 | 対象外 |
| existing_operation | 0/9 | 0/0 | 対象外 |
| fresh_video | 1/1 | 0/0 | 対象外 |
| general | 1/2 | 0/0 | 対象外 |
| generation | 3/3 | 0/0 | 対象外 |
| internal_comparison | 1/1 | 0/1 | 対象外 |
| internal_term | 1/1 | 0/1 | 対象外 |
| internal_topic | 0/0 | 0/2 | 対象外・正解例なし |
| involvement | 0/0 | 0/0 | 対象外・評価例なし |
| negated_search | 1/1 | 0/0 | 対象外 |
| source | 0/1 | 0/0 | 対象外 |
| unknown | 2/4 | 0/0 | 対象外 |

正解は希望動作を手で付与し、未対応のSlack等も既存rule_idの `existing_operation` を使う。expected_toolは従来経路を含む期待先（複合・一般会話・原文不明はnull）で、ツール選択は採点しない。退避falseは明確な初週候補3種の読取のみ。

分野はintentの固定射影：generation→document_creation、cases/status/metrics/comparison→cases、document/internal_term/internal_topic→internal_knowledge、external_research→public_research、existing_operation→integration、general/chat→general、unknown→unknown。document_deliveryのみintegration。追加の発話規則はなく、未対応分野と射影の粗さも誤分類に含む。

調整80件は新規制作・過去ログ・単純集計、固定評価40件は改訂・現在スレッド・条件付き目録・正式状態・内訳を中心に分離。`notes`のfamilyはsplit間で共有せず、意味の近さは手で確認。evalは資料作成5・Slack6・案件15件と境界例を含む。

主な不足はSlack 0/6、出典→一覧の誤流入1件、失注確定・受注金額/粗利率→internal_topicの2件、作成否定・状態照会・複合→generationの3件。退避予測は現行hookの原文条件（根拠不要・file・secondary_intentなら退避）だけで、本人確認・添付メタデータ・実行許可を再現しない。時間はimport除外の冷間1回・nearest-rankで環境依存。

未実施：2名レビュー、本人/route/media gateのhook試験、回答・配送・本番評価。分類合格だけでenforceしない。AWS・DB・Slack・LLM接続なし。CIは構造・実行可能性を検査し成績は強制しない。

## R2 後

2026-11-09。出典を一覧より優先し、失注・受注金額・粗利率の語彙を追加。生成は肯定の依頼動詞で判定し、作成否定・生成状況照会・複合依頼を分離した。Slack・資料レビュー・カルテ・メール・予定は既存の `existing_operation`、複合は `unknown` を使用する。新規intentは評価器の固定射影にも追加した。`requested_count`・`industry` は分類属性のみで、実行には使わない。

**tune の初週候補3種は各4/4（適合率100%）だが、各N≥5を満たせず、R2の合格条件は未達。** tuneの正解が各4件しかないため、誤分類を増やさず各N≥5にすることは不可能。母数・閾値を変更せず、分類ゲートはHOLDのままとした。解消にはtuneへ各1件以上を追加する別作業が必要で、今回の正解表は変更していない。

比較前のtuneは変更前に実行した `node scripts/routing_eval/classify_eval.mjs --split tune`、比較前のevalは上記の既存基準値を使用（再実行なし）。調整はtuneのみ。規則と回帰テストを確定してから `node scripts/routing_eval/classify_eval.mjs --split eval` を1回実行し、その後は分類規則・評価器を変更していない。既存のPython構造・JSON出力テストも内部でeval/allを呼ぶが、その出力を調整には使っていない。

| 指標 | tune R2前（80件） | tune R2後（80件） | eval R2前（40件） | eval R2後（40件） |
|---|---:|---:|---:|---:|
| 分野一致 | 49/80（61.3%） | 80/80（100.0%） | 24/40（60.0%） | 36/40（90.0%） |
| rule_id一致 | 51/80（63.7%） | 80/80（100.0%） | 25/40（62.5%） | 37/40（92.5%） |
| 根拠要否一致 | 60/80（75.0%） | 80/80（100.0%） | 31/40（77.5%） | 38/40（95.0%） |
| 出力形式一致 | 69/80（86.3%） | 80/80（100.0%） | 34/40（85.0%） | 36/40（90.0%） |
| 退避一致 | 60/80（75.0%） | 80/80（100.0%） | 37/40（92.5%） | 40/40（100.0%） |
| 誤った直接候補 | 20件 | 0件 | 3件 | 0件 |
| Slack分野一致 | 0/16 | 16/16 | 0/6 | 6/6 |
| case_list：rule一致 | 4/4（100.0%） | 4/4（100.0%） | 5/5（100.0%） | 5/5（100.0%） |
| case_list：直接候補適合 | 4/4（100.0%） | 4/4（100.0%） | 5/6（83.3%） | 5/5（100.0%） |
| internal_status：rule一致 | 4/4（100.0%） | 4/4（100.0%） | 4/5（80.0%） | 5/5（100.0%） |
| internal_status：直接候補適合 | 4/4（100.0%） | 4/4（100.0%） | 4/4（100.0%） | 5/5（100.0%） |
| internal_metrics：rule一致 | 4/4（100.0%） | 4/4（100.0%） | 4/5（80.0%） | 5/5（100.0%） |
| internal_metrics：直接候補適合 | 4/4（100.0%） | 4/4（100.0%） | 4/4（100.0%） | 5/5（100.0%） |
| 初週候補の分類合格 | 0/3種 | 0/3種（各N=4） | 0/3種 | 3/3種 |
| 分類時間p95 | 0.130ms | 0.126ms | 0.658ms | 0.788ms |

R2後の退避は分類器が返す `fallback` を採点する。根拠不要・複合依頼・初週3種以外は既定で退避する。R2前は従来hookの原文条件から算出していたため、退避関連は定義変更を含む比較であり、実行gate改善の測定ではない。`prepareInboundRoute` のenforce条件・allowlistは変更していない。既存の社内用語等の直接検索挙動は維持し、新規の根拠付きoperationは既存の `secondary_intent` で退避する。分類上の候補を初週3種へ絞る実行gateの接続はR3。

evalの分野未一致は `eval-007`（社内実績確認＋資料生成→generation）、`eval-014`（未提供の別会話との複合→slack）、`eval-015`（引用文の読点修正→unknown）、`eval-034`（社内規定比較→casesへの固定射影）の4件。その他の項目不一致は `eval-003`・`eval-004`（生成状況照会のanswer→tool）、`eval-008`（添付返却の根拠不要→要）、`eval-011`（スレッド要約のanswer→list）、`eval-040`（予定登録のtool→list）。5項目全一致は31/40。これらは保留とし、今回のevalに合わせた規則追加はしていない。正解表の誤りと判断した行はなし。

確認：`node --test infra/openclaw/caller-identity-plugin/test/*.test.mjs` は743 passed・1 skipped（上流実行物未指定）。`UV_CACHE_DIR=/tmp/routing-r2-uv-cache uv run --offline --extra dev --extra mcp --extra media pytest -q tests/scripts/test_openclaw_runtime_contract.py tests/scripts/test_routing_v2_corpus.py` は237 passed・3 skipped（`OPENCLAW_DIST_DIR` 未指定）。既存期待値は変更せず、否定・状態照会・複合・新分野の退避・件数/業種属性の回帰テストを追加。`ps` はsandboxで拒否され、他pytest本数は確認不能。pytestは並列オプションなしで指定2ファイルのみ実行した。

未実施：外部サービス接続、実際のツール実行・回答・配送・本番評価。Terraform・環境変数定義・SOUL・正解表は変更なし。commitなし。
