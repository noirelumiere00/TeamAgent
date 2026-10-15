# Aico Vault 施策動画・案件リンク（2026-10-15）

## 変更

- `scripts/export_vault.py`: 施策は160字抜粋をやめ、取込済み本文の上位投稿を既存の `_video_lines` で読む。順位・再生数・保存数・投稿アカウント・投稿文の冒頭・動画リンクを出す。
- `campaign_files.py`: アカウントを含めるオプションのみ追加。既定の検索回答の表示は維持。
- 取引先は `client_match.names_overlap` と `aliases` で照合。同じ取引先のカルテ・案件決定、および広告主と施策名が一致する提案書／報告書へ双方向の wikilink を追加する。施策名の照合は `campaign_files.campaign_similarity` と既存閾値を使う。検索回答の関連資料2件上限はVaultには掛けない。
- 案件決定は `topic=案件決定` で判別。分類メタが無い定型投稿も、本文の「クライアント名／商材名」欄で照合する。代理店欄だけでは結ばない。
- カルテ末尾に「この取引先の施策」（施策名・投稿本数・合計再生・最終投稿日・リンク）を追加。リンク先が一つも無い施策は `docs/紐付け先の無い施策.md` に残す。架空の取引先カルテは作らない。
- 完全exportは従来の最新100資料制限を外し、取引先分類の有無によらず全施策・案件決定も取得する。共有ACL・suppressed・staleの条件は共通。`--client` / `--limit` の部分exportは従来の制限を維持する。
- #537 のgsheets／研究ノートの安定ID由来16桁discriminator、同一URLでも異なるsource identityを別noteにする規則を維持。同名のSlack案件決定にも同方式を適用し、`_2` のchunk結合による欠落を防ぐ。それ以外の既存資料のファイル名は変更しない。
- `scripts/build_app_html.py`: 施策・未紐付け一覧・逆リンク付き資料は900字で切らず表示。カルテは従来の2600字＋施策一覧。グラフ生成は既存のwikilink抽出をそのまま使う。

取込元の `campaign_aggregate` は1文書1チャンクで、上位5本だけを保持している。この変更はその全行を表示する。全投稿のURLを新規取得する変更ではない。

## 検証

```bash
.venv/bin/python -m pytest -q -p no:warnings \
  tests/scripts/test_export_vault.py tests/scripts/test_build_app_html.py \
  tests/skills/search/test_campaign_files.py tests/skills/search/test_client_match.py \
  tests/ingest/test_campaign_aggregate.py tests/scripts/test_refresh_app_html.py
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/ruff format --check src/ tests/ scripts/
.venv/bin/mypy --follow-imports=silent scripts/export_vault.py src/teamagent/skills/search/campaign_files.py
```

関連テスト378件成功。実際に生成したHTMLのpayloadで5動画のURL・アカウント、カルテ／案件決定／資料との双方向リンク、グラフの辺を確認。法人格と別名、分類の無い同題案件、代理店だけの誤一致除外、未紐付け一覧、本文の表示上限、note件数と安定ファイル名、無関係な既存noteの本文不変も検証した。

全体の `.venv/bin/python -m pytest -q -p no:warnings` は `PIL` / `pptx` 不在により6ファイルの収集で停止。全体の `.venv/bin/mypy src/teamagent scripts/export_vault.py` は変更外の `media/deck_qa.py` と `skills/proposal_deck/renderer.py` のPIL関連3エラー。依存追加・ネットワーク利用はしていない。完全な失敗出力は [validation-failures.txt](validation-failures.txt)。ruff checkとformatは全体で成功、変更した型付き2ファイルのmypyは成功。

変異は実装を1箇所ずつ一時変更してpytestの失敗を確認し、毎回復元した。動画・アカウント・aliases・逆リンク・施策名照合・未紐付け一覧・SQL全文取得・全施策回収・ファイル名discriminator・HTML本文短縮・構造化クライアント欄の11変異すべて赤。結果は [mutation-summary.txt](mutation-summary.txt)。当初のHTML短縮変異はURL確認だけでは見逃したため、本文長の境界テストで検出するよう確認対象を修正した。

## 再生成と予約タスク

DBへ接続できる通常の運用環境で、共有ドメインと `DATABASE_URL` をこれまでどおり設定して実行する。今回DB接続・公開は実行していない。ここでの `--commit` はexporterのローカル書き出しフラグ。

```bash
.venv/bin/python scripts/export_vault.py \
  --out /tmp/aico-vault-videos --shared-group vectorinc.co.jp --commit --prune
.venv/bin/python scripts/build_app_html.py \
  --vault /tmp/aico-vault-videos --out /tmp/aico-vault-videos.html
```

再実行時も完全exportを使う（`--client` / `--limit` を付けない）。`--prune` は古い生成ファイル名の移行も行う。初回の空出力先では既存ファイルは無い。HTMLと隣のstats.jsonで件数・容量、施策末尾の5動画リンク、関係図、未紐付け一覧を確認する。

`~/Documents/Claude/Scheduled/aico-vault-monthly/build_app.sh` と `publish_app.sh` はいずれも `REPO=$HOME/dev/worktrees/teamagent-build` を参照していた。このworktreeの変更だけでは次の月次実行に反映されない。通常の反映手順で参照先へこの変更を取り込んだ後は、export→build→既存公開処理を引数変更なしで使える。予約タスク・別worktree・S3には手を加えていない。

## 容量の見積り

実データはネットワーク禁止のため未取得。40施策の合成データをHEAD版と変更後それぞれのexport→HTML buildへ通して比較した。各施策5動画、提案書1・報告書1・案件決定1、FBなし。旧版はSQL相当の160字抜粋を入力し、変更後だけ施策全文を入力。通常資料は両版160字。出力note件数は両版で同一。フォント等の固定部分を含むHTML全体の差を779/40倍した。数値は圧縮前のUTF-8 bytesで、詳細は [size-estimate.json](size-estimate.json)。

| 条件 | 変更前HTML | 変更後HTML | 779施策の追加推計 | 現7.3MBからの推計 |
|---|---:|---:|---:|---:|
| 取引先ごと1施策・1案件決定（40取引先） | 623,815 B | 743,719 B | 約2.34 MB | 約9.64 MB |
| 取引先ごと5施策・5案件決定（8取引先） | 604,032 B | 1,058,723 B | 約8.86 MB | 約16.16 MB |

動画だけでなく、双方向リンクの行コンテキスト・グラフ情報もHTMLへ入るため、同じ取引先の案件数で増分が大きく変わる。この範囲は上限保証ではない。100件制限撤廃で新たに入る既存資料・未分類案件の量は推計に含まないので、運用環境で再生成したstats.jsonを最終値とする。

commit / push は実行していない。
