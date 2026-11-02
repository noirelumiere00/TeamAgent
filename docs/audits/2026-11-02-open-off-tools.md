# A9: OFFツールの全員開放準備（2026-11-02）

小俣さんの「基本的には開いて大丈夫・全員に」に基づき、候補6本のうち **5本を全員向けに開放する想定**へSOUL・固定env・OpenClawの公開ゲートを更新した。`operation_log` は依頼者のSlack閲覧権限を検証しないため保留。公開対象は本人確認・連携・既存のRLSを通過した利用者であり、匿名公開ではない。

これは[10月30日の監査](2026-10-30-production-capabilities.md)を起点とした変更後の目標。実本番のenv・連携状態・予算残高を取得した記録ではない。env変更・デプロイはClaudeの別便。本変更を含むMCP/OCのリリースとenv反映が揃うまで本番開放は完了しない。

## 6本の確認結果

| ツール・判定 | 必要な連携・外部API | 費用・上限（コード上） | 不具合・制約・今回の処置 | 既存／追加テスト |
|---|---|---|---|---|
| `tiktok_comment_mining`：開放 | ローカルopt-in時はNode/ChromiumでTikTok公開コメント取得。本番coreは `TEAMAGENT_LOCAL_MEDIA_RUNTIME` 既定OFFなので通常はApify `clockworks/tiktok-comments-scraper`（`APIFY_API_TOKEN`）。分類はBedrock。HTML配信は既存の `VSEO_REPORT_BUCKET` 等、研究記録は `USE_RESEARCH_PERSIST` がONの場合のみ | 1〜3動画、各20〜500件（既定200）。Apifyはコード内概算$0.001/件、最大$1.50/依頼、既定3本なら$0.60。分類入力は各24,000 byte、出力3,072 token、最大3分類（クライアント内部の再試行は別）。Apify共有月次・個人月次枠は下記。Bedrock分はこの台帳の対象外で、USDの強制停止上限なし | CostGuardは未設定・台帳障害時に従来fail-openだった。有料経路に `require_cost_reservation=True` を渡し、全体月次枠の予約が未確認ならActor起動前に停止。全体月次上限未設定でも停止する。Chromium成功分はApify不要。coreへブラウザを追加せず既存のApify縮退を使う。取得0件は全コメント不在を意味しない。分類失敗は取得件数へ縮退。概算は実請求額の保証ではない | [コメント分析](../../tests/skills/tiktok_comment_mining/test_comment_mining.py)：取得経路、URL拒否、分類失敗、原文ID、rollout、今回追加の台帳なし／月次枠なし／台帳障害でHTTPを一度も呼ばない検証。[Apify](../../tests/adapters/test_apify_client.py)・[費用台帳](../../tests/adapters/test_cost_guard.py)にも予約・精算テストあり |
| `operation_log`：**保留** | Slack `SLACK_BOT_TOKEN` による `conversations.replies`、Bedrock。CRMへは書かず転記用本文を返す | 直接テキストは20,000文字、Slackは1ページ既定200メッセージ、出力900 token。Slack経由の総文字数制限・独立したUSD上限なし | [Skill](../../src/teamagent/skills/operation_log/skill.py)の `_resolve_conversation` は入力のchannel/threadを [Botクライアント](../../src/teamagent/adapters/slack_channel_ingest_client.py)へ直接渡す。依頼者の本人OAuth・チャンネル所属・出力先を照合しない。署名済み本人確認だけでは任意の入力先の閲覧権限は保証しない。Bot参加の非公開会話が他人へ漏れる可能性があり全員開放しない。本人トークンと出力面ガードに切り替えてから再評価 | [営業ログ](../../tests/skills/operation_log/test_operation_log_skill.py)は構造化・空入力・JSON失敗・Slack取得を検証するが、閲覧権限拒否の検証はない。今回実装は拡張しない |
| `mail_to_internal_context`：開放 | 本人Google OAuth・既存TokenStore（RDS/KMS）、Gmail APIメタデータ取得、共有SearchSkill/pgvector。OAuth要求は既存の `gmail.modify`、実クライアントはreadonly | 最大365日・メール30件（既定90日/10件）、社内参照20件（既定6）。factoryは要約OFF・Bedrock要約を注入しない。検索のembedding/rerankが有料構成なら既存検索と同じ課金、専用USD上限なし | 本文・件名・生Fromは返さず、DM限定を既存gatewayで強制。未連携は拒否。顧客名の断片・Gmail演算子混入をguard、負の検索スコアはclamp済み。社内資料は取込スナップショットで直近会話の未反映あり。要約スイッチを追加でONにしない | [メール突合](../../tests/skills/mail_to_internal_context/test_mail_to_internal_context.py)、[factory→MCPとRLS](../../tests/orchestrator/test_factory_mail_link.py)、[DMガード](../../tests/mcp_gateway/test_dm_only_tools.py) |
| `workspace_search`：開放 | 本人Google OAuth・TokenStore、Google Calendar API / People API。既存OAuth要求に `calendar.readonly`・`contacts.readonly` を含む。古い同意で不足なら再連携が必要 | 1回1サービス・最大50件（既定10）、検索語最大200文字。LLM・有料SaaS呼出なし、Google APIクォータに従う。予定と連絡先は別呼出 | 本人トークン・DLPは既存実装。予定の件名・連絡先の氏名は残るため、今回gatewayのDM限定一覧に追加。サービス入力も `Literal["calendar", "people"]` にして未対応サービスを入口で拒否 | [Workspace](../../tests/skills/workspace_search/test_workspace_search.py)：本人・未連携・正規化・DLP・サービス拒否。[DMガード](../../tests/mcp_gateway/test_dm_only_tools.py)：署名済み公開/非公開チャンネルではSkillを呼ばず、本人DMのみ実行 |
| `recommend`：開放 | factoryが既存SearchSkillを共有注入、pgvector・既存embedding/rerank。追加OAuthなし、既存RLSに本人contextを渡す | 概要2,000文字、候補最大30件（既定12）、3分類各3件。要約生成なし。検索のembedding/rerankが有料構成ならその分は課金、専用USD上限なし | 案件別名の統合は未実装。対象分類に入らない資料は出ない。出力は `source_uri` であり、開けるURLを保証しない。SOULでURLの創作を禁止。OpenClawのexcludeから外しinclude/公開台帳へ追加 | [推薦](../../tests/skills/recommend/test_recommend_skill.py)：分類・上限・空・条件・本人RLS context受渡し |
| `knowledge_search_url`：開放 | `CONNECT_BASE_URL` とconnect-webの検索/グラフ画面。閲覧時は本人Googleログインと既存認可 | URL組立のみ。外部API・LLM呼出・データI/Oなし | base未設定なら未公開案内となり、壊れた相対リンクを作らない。includeは既存だが公開台帳が `never` だったためenv gateへ変更。URLを配るだけで閲覧認可を追加・迂回しない | [URL生成](../../tests/test_knowledge_search_url_skill.py)：未設定・空・末尾スラッシュ・URL符号化 |

出典は各ツールの `src/teamagent/skills/<tool>/skill.py`・`schema.py`、[factory](../../src/teamagent/orchestrator/factory.py)、[MCP境界](../../src/teamagent/mcp_gateway/server.py)、[OAuth要求スコープ](../../src/teamagent/adapters/google_oauth_flow.py)。料金はコードの概算と設定値の確認であり、外部サービスの現在価格を照会していない。

## 別便で反映するenv

MCP側の変更（本人確認・RLSは既存どおり）：

```dotenv
USE_TIKTOK_COMMENT_TOOLS=true
USE_MAIL_LINK_TOOL=true
USE_WORKSPACE_TOOLS=true
USE_RECOMMEND_SKILL=true
USE_KNOWLEDGE_SEARCH_URL_TOOL=true
COMMENT_MINING_ALLOWED_EMAILS=
```

`COMMENT_MINING_ALLOWED_EMAILS` は空＝全員（`*` を入れない）。後半3ツールのフラグはTerraformに結線されていないため、MCP task envへの追加が必要。今回Terraformの既定値は変えない。既存のenv整合検査・runtime変更手順で反映し、通常のsyncで巻き戻さないこと。

MCP側で維持・確認する既存設定（新しい資格情報は不要、値の読み取り・転記は今回していない）：

- `COST_GUARD_TABLE`：既存の費用台帳テーブルとIAM権限が必要。空・障害時は新しい有料コメント取得を停止する。
- `COST_APIFY_MONTHLY_USD` / `COST_PER_USER_MONTHLY_USD`：[既存Terraform既定](../../infra/terraform/x_research.tf)はそれぞれ **50 / 15 USD**。全体50は他のApifyツールとも共有、個人15はprovider別の枠。本番値・残高は未確認なので、別便で既存値と有効な上限を確認し、勝手に増額しない。予約はコード内概算額であり実請求の厳密な上限ではない。
- `CONNECT_BASE_URL`：既存connect-webのbase URLを維持（Terraform既定は `https://connect.newstv.co.jp`）。未設定時はURL発行が未公開案内になる。
- 既存 `APIFY_API_TOKEN`、Bedrock権限、Google OAuthクライアント、`OAUTH_KMS_KEY_ID`・RDS、公開レポート配信設定を維持。トークン値は本書に記載しない。

OC側は新規env不要。既存の全員DM受付を維持する：

```dotenv
SLACK_DM_ALLOWLIST=*
```

ただし **envだけでは不十分**。この差分のSOUL・`openclaw.config.json5`・`effective-tool-scope.json`を含むOCリリースが必要（推薦/Workspaceのinclude追加・exclude削除、URL発行の公開gate変更）。コメント分析の費用予約強制・WorkspaceのDMガードを含むMCPリリースも同じ便で先に揃える。

## OFFのまま残すもの

```dotenv
USE_OPERATION_LOG_TOOLS=false
USE_PROPOSAL_CAMPAIGN_TOOLS=false
USE_PROPOSAL_DECK_TOOLS=false
USE_MORNING_DIGEST_TOOL=false
USE_DIGEST_ACK_TOOL=false
USE_VIDEO_APPROVAL=false
USE_MAIL_TOOLS=false
```

- `operation_log`：上記のSlack閲覧権限の欠落。
- `proposal_campaign`・`proposal_deck`：資料生成休止中（この2本の判断。他の既存資料ツールの公開範囲は変更しない）。
- `morning_digest`・`digest_ack`：別AIの担当。定期配信タスク全体を停止する指示ではない。
- `video_approval`：動画チェックツールと重複。
- `mail_constraints`：本人同意の設計が先。`USE_MAIL_TOOLS` をONにしない。

## 検証

固定envは実本番のドリフトを取得せず、変更後想定とfactory・SOUL・OC include/exclude・公開gateを外部接続なしで照合する。後二者の照合も今回 `test_soul_capabilities` に追加した。

- 指定の `uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/infra tests/skills tests/orchestrator` は通常キャッシュの権限制限で起動不可。`UV_CACHE_DIR=/private/tmp/teamagent-a9-uv-cache` で再試行したが、Pillow 13.0.0の取得先DNS制限でテスト起動前に失敗（完全ログ：`/private/tmp/teamagent-a9-pytest.log`）。
- 代替として既存Python 3.13.13環境に `PYTHONPATH=src` を指定し、同じ `pytest -q -n auto tests/infra tests/skills tests/orchestrator` を実行：**8,288 passed / 100 skipped / 4 warnings**。skipは外部FMTキット・ブラウザ起動不可等の既存条件。完全ログ：`/private/tmp/teamagent-a9-suite-final.log`。
- 関連契約・境界・費用台帳の追加範囲（`tests/mcp_gateway/test_dm_only_tools.py`、`tests/test_knowledge_search_url_skill.py`、`tests/adapters/test_apify_client.py`、`tests/adapters/test_cost_guard.py`、`tests/scripts/test_openclaw_runtime_contract.py`、SOUL関連）：**515 passed / 3 skipped**。OC配布物がローカルにない3件はskipで、実イメージbuild時のconfig validateは別便の門。完全ログ：`/private/tmp/teamagent-a9-contracts.log`。
- SOUL・公開範囲・ルーティングを最終状態で個別再検証：**233 passed**。`test_soul_capabilities` 成功。SOULは **27,985 UTF-16ユニット / 上限28,000**。ログ：`/private/tmp/teamagent-a9-routing-final.log`。
- plugin：`node --test infra/openclaw/caller-identity-plugin/test/*.test.mjs` → **721 passed / 1 skipped / 0 failed**。ログ：`/private/tmp/teamagent-a9-node.log`。
- `ruff check src/ tests/ scripts/`、`ruff format --check src/ tests/ scripts/` → 成功（1,466ファイル）、`git diff --check` → 成功。
- 既存ローカルの `ipaddr.js` 2.2.0をignoredな `node_modules` から参照してNode依存を補完。依存定義・lockfileは変更なし。初回検証の失敗出力も `/private/tmp/teamagent-a9-focused.log`・`/private/tmp/teamagent-a9-suite.log` に保存（字数超過とURL発行の古いOFF前提を修正後、上記範囲は成功）。

本番env・AWS・外部APIによる実機疎通、OCイメージbuild、デプロイは未実施。`.env`・secretsは読まず、git commit・pushもしていない。実本番の予算値・残高・Google同意済みスコープは別便で確認する。特にBedrockと検索基盤には今回専用USD上限を追加していない。
