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
