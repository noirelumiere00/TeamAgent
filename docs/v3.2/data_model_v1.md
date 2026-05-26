# TeamAgent v3.2 データモデル v1

Sprint 3 / PR-1（migration 0001）で導入する **source 横断統合スキーマ + ACL + RLS** の設計。

最終更新: 2026-05-26

---

## 全体像

```
                       documents (source 横断メタ + ACL)
                       ┌──────────────────────────────┐
                       │ id UUID                       │
                       │ source_type ENUM              │
                       │ external_id TEXT              │ ◄── idempotency key
                       │ owner_email TEXT              │
                       │ acl_emails TEXT[]             │ ◄── RLS 評価対象
                       │ acl_groups TEXT[]             │
                       │ source_uri TEXT               │
                       │ modified_at TIMESTAMPTZ       │
                       └──────────────┬────────────────┘
                                      │ 1
                                      │
                                      │ N
                       ┌──────────────▼────────────────┐
                       │ chunks (検索対象本体)         │
                       │ id UUID                       │
                       │ document_id UUID (FK)         │
                       │ chunk_idx INT                 │
                       │ content TEXT                  │
                       │ contextualized TEXT           │
                       │ embedding vector(1024)        │
                       └───────────────────────────────┘
```

既存 `proposals_chunks` / `proposals_chunks_contextual` は **温存** し、
後方互換列（source_type / owner_email / acl_emails / external_id）を追加するだけ。
完全移行は Sprint 4 で別途 ETL を実施予定。

---

## source_type ENUM

| 値 | 由来 | external_id の例 |
|---|---|---|
| `pdf` | 既存 PDF 取り込み | `pdf:<sha1 of file>` |
| `gdrive` | Google Drive 経由 | Drive fileId (`1AbC...`) |
| `gmail` | Gmail メッセージ | Gmail messageId (`192a...`) |
| `slack` | Slack チャネル履歴 | `<channel_id>:<thread_ts>` |
| `other` | 将来拡張 | 任意 |

---

## ACL モデル（acl_emails TEXT[] + RLS）

### 設計判断
- **Drive `permissions.list` の結果を ingest 時に acl_emails へ写像**
- Workspace group は別カラム `acl_groups TEXT[]` で扱う
- RLS で SELECT 自動フィルタ → アプリ側で WHERE 句忘れの事故防止

### Slack ハンドラからの使い方

```python
# runtime/slack_bot.py の検索処理内（Sprint 3+ で追加予定）
async def run_search(query, request_id, user_email):
    with pgvector.connection() as conn:
        with conn.cursor() as cur:
            # ユーザー identity を session に注入
            cur.execute("SET LOCAL app.user_email = %s", (user_email,))
            cur.execute("SET LOCAL app.user_groups = %s", (",".join(user_groups),))
            cur.execute("SET LOCAL app.user_role = %s", ("member",))  # or "admin"
            # 通常の検索クエリ。RLS で自動的に ACL フィルタされる
            cur.execute("SELECT * FROM chunks WHERE embedding <=> %s LIMIT 5", (vec,))
```

### RLS の挙動表

| `app.user_email` | `app.user_role` | acl_emails に含む？ | 結果 |
|---|---|---|---|
| 未設定 | 未設定 | -- | **見えない (fail-safe)** |
| `taro@x.co` | `member` | ❌ | 見えない |
| `taro@x.co` | `member` | ✅ | 見える |
| `taro@x.co` | `member` | -- (owner=taro) | 見える |
| 任意 | `admin` | -- | 全件見える（管理者 bypass） |

### グループの動作

`app.user_groups` にカンマ区切りで設定：
```sql
SET LOCAL app.user_groups = 'sales@vectorinc.co.jp,managers@vectorinc.co.jp';
```
→ documents.acl_groups と交差判定（unnest + ANY）

---

## Idempotency

### 重複防止
`UNIQUE (source_type, external_id)` で同じ source から同じファイル/メッセージを
2 回投入できない構造。ingest パイプラインは：

```sql
INSERT INTO documents (source_type, external_id, owner_email, acl_emails, title, source_uri)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (source_type, external_id)
DO UPDATE SET
    modified_at = EXCLUDED.modified_at,
    acl_emails  = EXCLUDED.acl_emails,
    title       = EXCLUDED.title
RETURNING id;
```

### Chunks の冪等性
`UNIQUE (document_id, chunk_idx)` で同じドキュメントの同じ位置に 2 つ chunk を作れない。
再 ingest 時は `DELETE FROM chunks WHERE document_id = %s` → INSERT で OK。

---

## migration の運用

### ローカル開発
```bash
docker compose up -d
DATABASE_URL=postgresql://teamagent:teamagent@localhost:5432/teamagent \
    python scripts/migrate.py
```

### 本番 RDS（踏み台 EC2 経由）
```bash
# Mac から SSM tunnel を張る（別 Terminal で）
aws ssm start-session --target i-04fd1f367b454f641 \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters '{"host":["teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com"],"portNumber":["5432"],"localPortNumber":["15432"]}' \
    --region ap-northeast-1

# Bot ディレクトリで
set -a; source .env.local; set +a
source scripts/load_secrets.sh
python scripts/migrate.py
```

### migration の追加方法

1. `infra/migrations/NNNN_description.sql` で次の番号を採番（4 桁ゼロ詰め）
2. 全 SQL は **idempotent** に：`CREATE ... IF NOT EXISTS`, `DROP POLICY IF EXISTS` 等
3. ENUM / TYPE は `DO $$ BEGIN IF NOT EXISTS (...) THEN ... END IF; END $$;` で wrap
4. `python scripts/migrate.py --dry-run` で順序確認
5. `python scripts/migrate.py` で実適用
6. `schema_migrations` テーブルに記録され、二度目以降は SKIP

### 改竄検知

`schema_migrations.checksum_sha` に適用時の SHA-256 を記録。**ファイルを後から書き換えると WARN が出る**：
```
[WARN] 0001_unified_documents.sql は適用済だが内容が変わっています (stored=abc12345…, current=def98765…).
       `--rerun 0001` で再適用するか、新 version で追加してください。
```

開発中の頻繁な書き換えは `--rerun NNNN` で強制再適用、リリース後の変更は **必ず新規 migration として追加**。

---

## 既存 `proposals_chunks` との関係

### 追加列（後方互換、既存データは default）
| 列名 | 型 | default | 用途 |
|---|---|---|---|
| `source_type` | document_source_type | `'pdf'` | 既存 PDF として固定 |
| `owner_email` | TEXT | NULL | 未設定（後で backfill） |
| `acl_emails` | TEXT[] | `'{}'` | 空 = RLS で誰も見えない |
| `external_id` | TEXT | NULL | 後で backfill |

### 注意
**`proposals_chunks` には RLS を有効化していない**（既存 SearchSkill が壊れるため）。
Sprint 4 の移行 PR で：
1. `proposals_chunks` → `documents` + `chunks` への ETL
2. 旧テーブル DROP
3. RLS 一斉適用

を行う予定。それまでは旧テーブルへの検索は **ACL チェックなし** で動く。

---

## 次の Sprint との接続

- **PR-2 (gdrive_client.py)**: `documents.source_type='gdrive'`, `external_id=Drive fileId`, `acl_emails` は permissions.list 結果
- **PR-3 (gmail_client.py)**: `documents.source_type='gmail'`, `external_id=Gmail msgId`, `acl_emails` は thread 参加者 + `gmail.modify` で隠しラベル管理
- **S3-06 Drive 差分監視**: `documents.modified_at` で changes.list の since として利用
- **S3-13 Slack 取り込み**: `documents.source_type='slack'`, `external_id='<channel>:<ts>'`

---

## 関連ドキュメント

- `infra/migrations/0001_unified_documents.sql` — 実体 SQL
- `scripts/migrate.py` — runner
- `tests/adapters/test_pgvector_schema.py` — contract test
- `docs/v3.2/ops/local_dev_with_tunnel.md` — ローカル開発の SSM tunnel 手順
- `docs/v3.2/ops/observability_and_security.md` — Sentry / CloudWatch / セキュリティ全般
