# TeamAgent 取り込みソース管理（Ingest Sources v1）

最終更新: 2026-05-26

Sprint 3 で複数の異種ソース（Slack ch / Drive folder / Google Sheet）を統合管理するための設計。
ユーザー提供の貴重情報源（ナレッジ共有 / ショート動画営業 FB）を確実に DB 化する。

---

## 全体像

```
                    data/ingest_sources.yaml
                    （宣言的に対象を管理）
                              │
       ┌──────────────────────┼──────────────────────┐
       ▼                      ▼                      ▼
slack_channels         gdrive_folders          gsheets
       │                      │                      │
slack_channel_ingest_  gdrive_client.py       gsheets_client.py
client.py                                     (PR-5 で実装)
       │                      │                      │
       └──────────────────────┼──────────────────────┘
                              ▼
                       documents
                  (source_type ENUM 分類)
                              │
                              ▼
                          chunks
                  (RLS で ACL 自動フィルタ)
```

---

## 取り込み対象（2026-05-26 確定）

### 1. ナレッジ共有
| ソース | id / 場所 | 用途 |
|---|---|---|
| Slack ch | `#proj-ナレッジ共有` | 提案ナレッジの議論・共有 |
| Drive folder | `12FMLe9XG24wlPrBCHOQ_vcr4uELtMN1E` | 提案資料 PDF / 添付 |
| Google Sheet | `1jRmoUPo0kAhOGA6secGcwGHILH5LHt7lYvEuxJ5uupo` (gid=278789217) | フォーム回答（業界 / 商材 / 結果） |

### 2. ショート動画営業フィードバック（**最重要**）
| ソース | id / 場所 | 用途 |
|---|---|---|
| Slack ch | `#proj-ショート動画_営業フィードバック情報` | 商談 FB・クライアント温度感 |
| Google Sheet | `1VukC1Qv0MRqxSvgxuSqDwzpPsM_K1FJNTpTXs10KQhY` (gid=537831563) | FB フォーム回答 |

> ユーザー曰く「**貴重な情報源、DB にしたい**」（2026-05-26）。Sprint 3 着手の最大の動機。

---

## 設計判断

### 取り込み単位
| ソース | 単位 | external_id 構成 |
|---|---|---|
| Slack ch | 1 スレッド = 1 document（thread_ts 単位）<br>単発投稿 = 1 document（ts 単位） | `<channel_id>:<thread_ts or ts>` |
| Drive folder | 1 ファイル = 1 document | `<file_id>` |
| Google Sheet | 1 行 = 1 document（フォーム回答想定）<br>または 1 タブ = 1 document（自由記述） | `<sheet_id>:<gid>:<row_idx or 'all'>` |

### ACL 写像

| ソース | acl_emails 抽出元 |
|---|---|
| Slack ch | `conversations.members` → `users.info` の email |
| Drive folder | `permissions.list` の user type emails |
| Google Sheet | `permissions.list` の user type emails（Drive API 経由） |

**Public ソース** の扱い：
- 全営業 OK なら `acl_groups: ["sales@vectorinc.co.jp"]` で workspace group 全員許可
- 特定メンバーのみなら `acl_emails: [taro@…, jiro@…]` で個別指定

### 取り込み頻度
| ソース | 頻度 | 方法 |
|---|---|---|
| Slack | 15 分 | 前回 ts 以降の `conversations.history`（EventBridge cron） |
| Drive | 15 分 | `changes.list`（folder_id 配下の変更検知） |
| Sheets | 5 分 | フォーム回答は即時取り込みが価値 |

### idempotency 戦略
全ソースで `UNIQUE (source_type, external_id)` を満たすので、ON CONFLICT で重複防止：

```sql
INSERT INTO documents (source_type, external_id, owner_email, acl_emails, ...)
VALUES (...) ON CONFLICT (source_type, external_id)
DO UPDATE SET modified_at = EXCLUDED.modified_at, acl_emails = EXCLUDED.acl_emails;
```

---

## 設定ファイルフォーマット

`data/ingest_sources.yaml`:

```yaml
version: 1

slack_channels:
  - channel_id: "C0XYZ"           # ★ 後で取得して埋める（conversations.list で確認）
    channel_name: "#proj-ナレッジ共有"
    description: "..."
    include_files: true
    oldest_days: 180
    extra_acl_emails: []
    extra_metadata: {topic: "提案ナレッジ"}

gdrive_folders:
  - folder_id: "12FMLe9XG24wlPrBCHOQ_vcr4uELtMN1E"
    folder_name: "ナレッジ共有 - 添付ファイル"
    ...

gsheets:
  - sheet_id: "1VukC1Qv0MRqxSvgxuSqDwzpPsM_K1FJNTpTXs10KQhY"
    sheet_name: "ショート動画営業 FB"
    tabs:
      - {gid: 537831563, tab_name: "フォーム回答 1"}
    row_unit: true
    extra_metadata: {priority: "high"}
```

---

## 実装フェーズ

### Sprint 3 PR-4（今日）← この PR
- ✅ `slack_channel_ingest_client.py` 雛形 + 16 テスト
- ✅ `data/ingest_sources.yaml` 雛形（ユーザー提供 2 ソース反映、channel_id は REPLACE_WITH_… プレースホルダ）
- ✅ 本ドキュメント

### Sprint 3 PR-5（後日、要 GCP OAuth）
- `gsheets_client.py` 雛形（Sheets API v4 / `spreadsheets.values.get`）
- 1 行 → 1 document の chunking 設計
- 既存 `gdrive_client.py` の permissions.list 流用（Sheet も Drive permissions）

### Sprint 3 PR-6（後日、要 channel_id 取得）
- `scripts/ingest_sources.py`: yaml を読んで各 adapter にディスパッチ
- ON CONFLICT で idempotent INSERT
- ACL 写像（Slack members → email、Drive permissions → emails）
- EventBridge cron 化（Sprint 4）

### Sprint 4
- 既存 `proposals_chunks` → `documents` + `chunks` への ETL
- SearchSkill を新スキーマに切替（RLS 経由）
- `proposals_chunks` 系の DROP（旧コードの clean up）

---

## チャネル ID の取得方法（ユーザー作業 / 5 分）

`REPLACE_WITH_C_ID_FOR_*` を実値に置き換えるには：

```bash
# slack_sdk で channels:read OAuth 必要（既存 App に付与済）
python3 -c "
from slack_sdk import WebClient
import os
c = WebClient(token=os.environ['SLACK_BOT_TOKEN'])
for name in ['proj-ナレッジ共有', 'proj-ショート動画_営業フィードバック情報']:
    r = c.conversations_list(types='public_channel,private_channel', limit=1000)
    for ch in r['channels']:
        if ch['name'] == name:
            print(f'{name}: {ch[\"id\"]}')
"
```

または Slack で対象 ch を右クリック → "リンクをコピー" → URL 末尾の `/C0XXX` 部分。

---

## セキュリティ留意点

1. **ナレッジ共有 ch / FB ch は private** の場合、Bot を **invite してから** ingest 開始
2. Bot が invite された channel に限り `conversations.history` で読める
3. `acl_emails` は ingest 時の channel members 状態を写すスナップショット。
   メンバー変更時の差分反映は **15 分 cron で再取得**（Sprint 4 で実装）
4. **削除されたメッセージ** は `conversations.history` から消えるが、
   既存 documents には残る → Sprint 4 で `conversations.history` の差分 ts と
   照合して論理削除する仕組みを追加
5. **添付 PDF の機密性**：Slack に貼られた PDF は `include_files=true` 時に
   pgvector に取り込む。社外秘 PDF は `acl_emails` で確実に制限

---

## 関連ドキュメント

- `data/ingest_sources.yaml` - 取り込み対象宣言
- `src/teamagent/adapters/slack_channel_ingest_client.py` - Slack 取り込み実装
- `src/teamagent/adapters/gdrive_client.py` - Drive 取り込み実装（PR #36）
- `infra/migrations/0001_unified_documents.sql` - documents/chunks スキーマ
- `infra/migrations/0002_app_role_separation.sql` - RLS 用ロール分離
- `docs/v3.2/data_model_v1.md` - スキーマ詳細 + RLS 使い方
