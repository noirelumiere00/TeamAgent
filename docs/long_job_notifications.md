# 長いジョブの終了通知

対象は、署名検証済みの依頼元を持つ MCP の非同期ジョブと切り離しジョブ。
通知先は検証済みの会話、投稿が拒否された場合は依頼者本人の DM。
既存の `ProposalJobStore`・通知 outbox・`dedup_notice_*` を使用する。

## 終了状態と通知経路（修正後）

P = status 照会 → `publish_notice`。D = 実行完了 callback → 同じ `publish_notice`。
R = 永続 outbox から P を再開。S = heartbeat/実行期限を CAS で failed に変更 → P。
D と P/R は同じ terminal の dedup lock を共有する。進捗通知は終了通知とは別。

| ジョブ種別 | done | failed | incomplete | timeout | heartbeat 切れ | 再起動・孤立 |
|---|---|---|---|---|---|---|
| 提案書 `proposal_builder_submit` | P（添付/URL） | P | draft の結果説明を P | failed の理由を P | S（PROPOSAL の閾値） | R → S |
| お土産資料・通常版 | P（添付） | P | done 内の report_status を P | failed の理由を P | S（OMIYAGE の閾値） | R → S |
| お土産資料・sales25 | P（添付） | P | 保存済みの未完成理由を P | 未完成結果を P | checkpoint から実行再開 | R → 再開／queued／開始失敗を P |
| 切り抜き提案 `clip_proposal_submit` | P（添付） | P | partial の結果説明を P | failed の理由を P | S（PROPOSAL の閾値） | R → S |
| TikTok取得（複数 job_id を含む） | 全件を集約して P | 全件を集約して P | 未完了の説明を P | canonical request の期限 → S | 実行期限 → S | R → P/S |
| 動画分析 `video_algorithm` | D/P | D/P | 結果説明を D/P | 登録簿の45分上限 → D/P | 生存不明は unknown、孤立は S | 終了 hook → D、強制終了は R → S |
| 検索上位チェック・二段目動画分析 | D/P | D/P | 結果説明を D/P | 登録簿の45分上限 → D/P | S（PROPOSAL の閾値） | 終了 hook → D、強制終了は R → S |
| Web調査 `web_research` | D/P | D/P | 結果説明を D/P | 失敗結果／45分上限 → D/P | S（PROPOSAL の閾値） | 終了 hook → D、強制終了は R → S |
| X単発調査 `x_voice_search` | D/P | D/P | partial の結果説明を D/P | 失敗結果／45分上限 → D/P | S（PROPOSAL の閾値） | 終了 hook → D、強制終了は R → S |
| Xニーズ調査 `x_needs_mining` | D/P | D/P | partial の結果説明を D/P | 失敗結果／45分上限 → D/P | S（PROPOSAL の閾値） | 終了 hook → D、強制終了は R → S |
| X発話量 `x_buzz_measure` | P | P | 欠測日を明示して P | failed の理由を P | 600秒無更新 → S | R → P/S（外部 worker は継続可能） |
| 事例取り込み `media_case_search` | P | P | 保存済み範囲と失敗理由を P | failed の理由を P | S（PROPOSAL の閾値） | worker が再開できれば継続、孤立は R → S |

同期の短い完了は従来の tool 応答で返す。依頼者・署名付き宛先のない管理者 CLI の初回 ingest、
cron の ingest、内部の子工程は本人への通知先を持たない。子工程の結果は依頼元の親ジョブに集約する。

## 発見した欠落箇所（修正前 HEAD の行番号）

| 欠落・誤表示 | 根拠 |
|---|---|
| 切り抜き提案・X発話量の全終了状態が共通監視外 | `src/teamagent/mcp_gateway/server.py:99`、復旧対象の固定列挙 `async_job_notify.py:430` |
| submit が failed/incomplete/timeout を返した場合は監視登録しない | `src/teamagent/mcp_gateway/server.py:527` |
| incomplete/timeout/stale/interrupted が終端として扱われない | `src/teamagent/mcp_gateway/async_job_notify.py:287`、集約側 `server.py:537` |
| 期限超過の未通知 outbox が知らせず削除される | `src/teamagent/mcp_gateway/async_job_notify.py:375`、`:420` |
| 動画分析の古い登録を外すだけで失敗通知しない | `src/teamagent/mcp_gateway/detached_jobs.py:798` |
| 二段目動画分析はメモリ登録簿＋直接投稿だけで強制終了後の復旧なし | `src/teamagent/mcp_gateway/surface_video_followup.py:679`、`:499` |
| Web・単発X調査は切り離し対象外 | `src/teamagent/mcp_gateway/detached_jobs.py:43` |
| 切り抜き提案には heartbeat/stale 判定がなく queued にも「作成中」 | `src/teamagent/skills/clip_proposal/skill.py:614`、`:733`、`:749` |
| X発話量の queued/running は時間経過を考慮しない | `src/teamagent/adapters/x_task_store.py:109`、`skills/x_research/skill.py:941` |
| X結果の読取失敗を空の完了結果として扱う | `src/teamagent/skills/x_research/skill.py:956` |
| TikTok媒体台帳の照会が worker 消失後も queued/running を返す | `src/teamagent/adapters/media_job.py:639` |
| 古い動画分析で生存確認ができなくても running を返す | `src/teamagent/skills/video_algorithm/skill.py:2350` |
| 取り込み照会は古い行でも running を返す | `src/teamagent/skills/media_case_search/skill.py:333` |
| sales25 の再開枠を取れない／thread 開始失敗でも古い running を返す | `src/teamagent/skills/omiyage_report/skill.py:1716` |

## 配信の境界

確定した投稿失敗は錠を返却して再試行し、未通知 outbox を残す。
期限超過でも保存済み結果を一度確認し、終了通知の取得後だけ outbox を削除する。
再起動で監視だけが止まる場合は途中の中断通知を送らず、復旧後の終了通知に集約する。

Slack の応答 timeout は到達が不明なため、既存方針どおり自動再送しない。
錠の取得と Slack 投稿は別システムなので、その間の強制終了も含む厳密な exactly-once 到達保証はできない。
外部通信はフェイクで検証し、実際の AWS/Slack の可用性・配信は今回の検証対象外。

## 検証結果

- `uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/mcp_gateway tests/skills tests/adapters` 相当を実行。既存の検証用 venv を `--no-sync` で使用し、最終実行は4並列。
- 最終の全体実行: **9571 passed / 4 failed / 110 skipped**。4件はいずれも Node の `puppeteer-core` 未導入で、変更箇所へ到達する前の import 失敗。lockfile と一致する既存のローカル Node 依存を参照し、対象 `tests/adapters/test_tiktok_sales25_contract.py` は **4 passed**。
- 上記のほか、`tests/media/test_adapter.py` の canonical deadline 超過・同時完了との CAS 競合もフェイクで検証。通知・復旧・添付・sales25 を含む関連実行は **274 passed**、追加の回帰確認は **158 passed**、切り抜き提案の保存後1回添付は **1 passed**。
- 変更Pythonファイルの ruff: 成功。変更ソース14ファイルの mypy: 成功。
- 変更テストファイルも mypy を実行。既存の型エラー **37件**が残るが、修正前 HEAD を別ディレクトリで同じ条件で検査し、ファイル名・エラー本文・件数の差分が **0件**であることを確認した。
- スキップは実DB・ローカルテンプレート等の未設定条件による。AWS・実Slackへの配信、実ブラウザでの取得は実行していない。
- 再起動後の復旧には既存の `PROPOSAL_JOBS_TABLE` による永続化が必要。未設定のメモリ動作では、プロセスを越えた復旧はできない。

失敗出力を含む検証ログは `/private/tmp/teamagent-a8jobs-*.log` に保存。
