# 社内ナレッジ 定期 ingest（systemd timer）

`#proj-ナレッジ共有` / `#proj-ショート動画_営業フィードバック情報` の Slack、
ナレッジ共有・ショート動画の Google Drive フォルダ、ショート動画営業 FB スプレッドシートを、
**週次で自動的に pgvector(documents/chunks) へ取り込み続ける**ための仕組み。

OpenClaw / TeamAgent はこの pgvector を `teamagent-mcp` 経由で検索するため、
ここに入った瞬間に Aico から引き出せるようになる。月20件ペースで増えていく提案資料・
クライアント反応を、人手の再 ingest 無しで鮮度維持するのが狙い。

## 構成

| ファイル | 役割 |
|---|---|
| `scripts/run_ingest.sh` | env/secrets を Bot と同一手順でロードして `ingest_sources.py --commit` を実行するラッパ |
| `deploy/systemd/teamagent-ingest.service` | oneshot ユニット（タイマーから起動） |
| `deploy/systemd/teamagent-ingest.timer` | 週次スケジュール（毎週月 18:00 UTC = 火 03:00 JST） |

取り込み対象は `data/ingest_sources.yaml` に定義済み（Slack 2ch / Drive 2フォルダ / Sheets 2件）。
ソースの追加・除外は **このファイルを編集するだけ**でよく、ユニットの変更は不要。

### 冪等性（重複が溜まらない理由）
`repository.upsert_document_with_chunks` が `ON CONFLICT (source_type, external_id) DO UPDATE`
＋ チャンク全削除→再投入で動くため、毎回フル走査しても
**新規は追加・更新は差し替え・不変は上書きのみ**。何度回しても安全。

## worker EC2 へのインストール

前提:
- アプリは `/opt/teamagent/app`、venv は `/opt/teamagent/app/.venv`、
  env は `/opt/teamagent/teamagent.env.base`（= `teamagent-bot.service` と同じ配置）。
- 最新コードは通常デプロイ（`/opt/teamagent/deploy.sh` / `scripts/deploy_to_ec2.sh`）で
  `/opt/teamagent/app` に入っている前提。
- **埋め込みはローカル E5（`LocalE5Embedder`）を使うため `sentence-transformers` が venv に必要。**
  `scripts/deploy_to_ec2.sh` が導入済み（外部の埋め込み API は呼ばない＝embedding に課金は発生しない）。

```bash
# 1) ユニットを配置
sudo cp deploy/systemd/teamagent-ingest.service /etc/systemd/system/
sudo cp deploy/systemd/teamagent-ingest.timer   /etc/systemd/system/

# 2) 反映してタイマー有効化
sudo systemctl daemon-reload
sudo systemctl enable --now teamagent-ingest.timer

# 3) 次回発火予定を確認
systemctl list-timers teamagent-ingest.timer
```

## 動作確認

```bash
# タイマーを待たず即時に1回流す（実 DB に書き込む）
sudo systemctl start teamagent-ingest.service

# ログ追跡
journalctl -u teamagent-ingest.service -f

# まず dry-run で疎通だけ見たい（DB 非書き込み）
cd /opt/teamagent/app && sudo INGEST_DRY_RUN=1 -E bash -lc 'scripts/run_ingest.sh'
```

成功時は `[run_ingest] ... done exit=0` で終わり、`ingest_sources.py` が
`documents_upserted` / `skipped` の集計を出力する。

## 運用つまみ

`run_ingest.sh` は環境変数で挙動を上書きできる（ユニットの `Environment=` か drop-in で指定）。

| 変数 | 既定 | 用途 |
|---|---|---|
| `INGEST_SOURCES` | `slack,gdrive,gsheets` | 取り込むソースの絞り込み |
| `INGEST_OWNER_EMAIL` | `shogo@vectorinc.co.jp` | documents.owner_email |
| `INGEST_DRY_RUN` | `0` | `1` で `--commit` を外し dry-run |
| `TEAMAGENT_ENV_BASE` | `/opt/teamagent/teamagent.env.base` | env ファイルのパス |

### 頻度を変える（週次 → 日次）
`teamagent-ingest.timer` の `OnCalendar` を編集する（再 `daemon-reload` 必須）：

```ini
# 日次（毎日 18:00 UTC = 03:00 JST）
OnCalendar=*-*-* 18:00:00
```

```bash
sudo cp deploy/systemd/teamagent-ingest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart teamagent-ingest.timer
```

> 埋め込みはローカル計算なので **API 課金は増えない**。日次化で増えるのは worker の
> CPU 時間と実行時間だけ。月20件ペースなら週次で取りこぼしは最大7日。FB（最重要・
> priority:high）の鮮度を毎朝にしたい場合は日次にしてよい（コスト面の障壁は無い）。

## 無効化 / アンインストール

```bash
sudo systemctl disable --now teamagent-ingest.timer
sudo rm /etc/systemd/system/teamagent-ingest.{service,timer}
sudo systemctl daemon-reload
```

## ローカル / SSM tunnel で手動実行

worker を使わず手元から流す場合（RDS への SSM tunnel が前提）：

```bash
# 別ターミナルで tunnel を張ったうえで
set -a; source .env.local; set +a
scripts/run_ingest.sh                 # 本投入
INGEST_DRY_RUN=1 scripts/run_ingest.sh  # dry-run
```
