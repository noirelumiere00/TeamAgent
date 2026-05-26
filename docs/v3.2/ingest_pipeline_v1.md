# 取り込みパイプライン v1（Sprint 3 PR-6）

最終更新: 2026-05-27

`data/ingest_sources.yaml` で宣言した Slack/Drive/Sheets を、3 adapter 横断で
ディスパッチして documents/chunks テーブルに idempotent 投入するパイプライン。

---

## 構成

```
data/ingest_sources.yaml
        ↓
ingest/loader.py          → IngestSources (型安全 dataclass tuple)
        ↓
ingest/pipeline.py        → IngestRunner.run(kinds=['slack','gdrive','gsheets'])
        ↓ (kind ごとに dispatch)
_ingest_slack_channel / _ingest_gdrive_folder / _ingest_gsheet
        ↓ (DocumentUpsert + [ChunkUpsert])
ingest/repository.py      → IngestRepository.upsert_document_with_chunks()
        ↓ (psycopg + RLS app_role)
本番 RDS documents / chunks
```

---

## CLI 使い方

### 既定は dry-run（DB に書かない）

```bash
cd ~/Documents/TeamAgent
source .venv/bin/activate

# 1. SSM tunnel を別 Terminal で起動（docs/v3.2/ops/local_dev_with_tunnel.md 参照）

# 2. env をセット
set -a; source .env.local; set +a
source scripts/load_secrets.sh

# 3. dry-run（取り込み件数だけ集計）
python scripts/ingest_sources.py --sources slack

# 4. 本番投入
python scripts/ingest_sources.py --commit --sources slack \
    --owner-email shogo@vectorinc.co.jp
```

### オプション

| フラグ | 既定 | 説明 |
|---|---|---|
| `--yaml PATH` | `data/ingest_sources.yaml` | 設定ファイルパス |
| `--sources slack,gsheets` | `all` | カンマ区切りで kind 指定 |
| `--commit` | False（dry-run） | DB 投入する |
| `--owner-email` | `$INGEST_OWNER_EMAIL` or `noreply@vectorinc.co.jp` | documents.owner_email |
| `--app-role` | `teamagent_app` | SET ROLE 先、`none` で無効 |

### Exit code
- `0`: 全 source 成功
- `1`: 1 件以上のエラーあり（partial failure）
- `2`: 設定エラー（DATABASE_URL 未設定 / yaml 不在）

---

## プレースホルダ自動 skip

`channel_id` などに `REPLACE_WITH_...` / `__RDS_...` が残っている source は
自動 skip + WARN ログ。これで「設定未完成な状態でも残りの source は動く」。

```
2026-05-27 ... [warning] ingest_sources_skip_placeholder
    section=slack_channels channel_id=REPLACE_WITH_C_ID_FOR_proj-... name=#proj-ナレッジ共有
```

完全に動かしたい場合は yaml の `REPLACE_WITH_*` を実 ID に置換すること。
詳しい取得方法は `docs/v3.2/ingest_sources_v1.md` を参照。

---

## RLS との連携

`IngestRepository` は内部で：

```python
self._pgvector.connection(
    app_role=self._app_role,           # 既定 'teamagent_app'
    user_email=self._owner_email,      # WITH CHECK 用
    user_role="admin",                 # INSERT 用 bypass
)
```

を呼ぶので、**owner_email を持つ user のみが INSERT 可能**。
将来 ingest 用の専用 role を分けるなら、`app_role` を `teamagent_ingest` 等に。

---

## 投入される documents の例

| source_type | external_id | source_uri |
|---|---|---|
| `slack` | `C0XYZ:1700000001.000001` | `slack://C0XYZ/1700000001.000001` |
| `gdrive` | `1AbC...XyZ` | `https://drive.google.com/file/d/1AbC.../view` |
| `other` (sheets) | `1VukC:537831563:2` | `https://docs.google.com/spreadsheets/d/1VukC/edit?gid=...` |

> `gsheets` を `source_type` ENUM に追加するのは migration 0003 で予定。
> 暫定で `other` を使う。

---

## 現状の制限（Sprint 4 で対応予定）

| 項目 | 現状 | 解決 |
|---|---|---|
| Slack 増分取り込み | 1 ページ目のみ（最新 100） | cursor 永続化 + EventBridge cron 15min |
| Slack thread 参加者 → email | 解決せず、`extra_acl_emails` か owner_email のみ | users.info キャッシュ実装 |
| Drive ファイル本文抽出 | ファイル名のみ embed | pdfplumber / Docs API で本文取得 |
| Drive permissions → acl_emails | 写像せず | `list_permissions()` を呼んで反映 |
| Gmail 取り込み | pipeline 未組込 | S3-12 で組み込み |
| Sheets ENUM | `other` 暫定 | migration 0003 で `gsheets` 追加 |

---

## トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| `[ERROR] DATABASE_URL が未設定` | env 未読込 | `set -a; source .env.local; set +a` + `source scripts/load_secrets.sh` |
| `psycopg.OperationalError: failed to resolve host` | SSM tunnel 未起動 | `docs/v3.2/ops/local_dev_with_tunnel.md` 参照 |
| `permission denied for table documents` | `teamagent_app` role 未作成 | `python scripts/migrate.py` で 0002 適用 |
| `NotImplementedError: Gmail credentials が未設定` | OAuth 未取得 | `--sources slack,gsheets` で Gmail を除外、もしくは GCP S3-09 完了待ち |
| `gsheets` で 0 件 | sheet 共有不足 | Service Account / OAuth user に sheet 閲覧権限付与 |

---

## テスト

```bash
pytest tests/ingest -v
```

- `test_loader.py` 10 件: yaml パース / プレースホルダ検知 / 各 spec dataclass マッピング
- `test_pipeline.py` 7 件: orchestrator dispatch / partial failure / dry-run / 個別 handler

実 DB なしで CI 上でも全件動く（adapter は MagicMock / repository は fake）。
