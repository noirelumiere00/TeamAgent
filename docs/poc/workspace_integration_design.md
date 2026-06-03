# Google Workspace 5サービス × 個人認可（per-user OAuth）統合 設計書

> 2026-06-03。worktree `teamagent-orchestrator-poc`。**設計のみ（コスト0）**。
> 実装は §8「あなたの Google Cloud 設定」完了後に着手（OAuth クライアントが無いとライブ不可）。
> 既存アダプタ（gmail/gdrive/gsheets）と認証実装の実態（2026-06-03 調査）に基づく。

---

## 0. 目的（ユーザー要件）

**Gmail / スプレッドシート / ドキュメント / スライド / Drive の5サービスを全て連携**し、
**各個人が自分の5サービスを自分で許可（per-user 認可）**できるようにする。
→ orchestrator が「本人のWorkspace全体」を横断して提案に活かす（当初ビジョンの中核）。

---

## 1. 認証モデルの決定：**per-user OAuth**（DWD ではない）

| | DWD（管理者代理・現 mail_constraints の暫定） | **per-user OAuth（本設計・採用）** |
|---|---|---|
| 認可主体 | 管理者が service account に全社代理権限 | **各個人が自分で同意**（OAuth 同意画面） |
| 同意 | 別途 consent 管理が必要 | **OAuth 同意that自体が認可**＝要件「個人が許可」に合致 |
| 越権リスク | service account が全員分を読める（要厳格ガード） | 本人のトークンでしか本人データに触れない（構造的に安全） |
| 設定 | 管理コンソールで DWD + scope 委任 | OAuth クライアント + 各人が初回認可 |

→ **ユーザー要件「個人が全て許可」＝ per-user OAuth が正解**。これは Phase 6 で懸案だった
「同意フロー」ゲートの最良の解（同意＝OAuth 認可に一本化）。既存アダプタは既に OAuth
リフレッシュトークン方式を**推奨経路**として実装済（`GOOGLE_OAUTH_REFRESH_TOKEN`）。
現状の課題は **単一の共有トークン**である点 → **per-user（各人のトークン）化**する。

---

## 2. 対象5サービス・スコープ（全て readonly）・アダプタ現況

| サービス | scope（readonly） | 審査区分 | アダプタ |
|---|---|---|---|
| Gmail | `gmail.readonly` | Restricted (Tier3・CASA) | ✅ 既存 `gmail_client.py` |
| Drive | `drive.readonly` + `drive.metadata.readonly` | Sensitive (Tier2) | ✅ 既存 `gdrive_client.py` |
| スプレッドシート | `spreadsheets.readonly` | Sensitive | ✅ 既存 `gsheets_client.py` |
| ドキュメント | `documents.readonly` | Sensitive | ❌ **新規 `gdocs_client.py`** |
| スライド | `presentations.readonly` | Sensitive | ❌ **新規 `gslides_client.py`** |

※ **社内限定アプリ（Internal / Workspace 組織内公開）にすれば Google の本審査（CASA含む）は
原則不要**。組織内ユーザーのみが認可対象になる。これが最も現実的（§8）。

---

## 3. per-user トークンストア（新規）

各人の refresh token を安全に保管し、リクエスト時に**発行者本人のトークン**を選ぶ。

```
TokenStore（Protocol）:
  get(user_email) -> OAuthToken | None      # 本人の refresh token を取得
  put(user_email, token) -> None            # 認可フロー完了時に保存
  has(user_email) -> bool
```
- 既定実装の選択肢（§8 で確定）: ❶暗号化ファイル/SecretsManager ❷RDS テーブル（暗号化列）。
- **refresh token は暗号化必須**（at rest）。ログ・プロンプトに絶対に出さない（6-bis）。
- mail_constraints の `ConsentStore` は本 TokenStore に発展統合（「同意済み」＝「トークン有り」）。

---

## 4. 同意フロー（各人が1回だけ認可）

1. 利用者が Slack で `/teamagent connect`（または Web リンク）を実行。
2. Bot が**本人専用の OAuth 同意 URL**（5 scope 一括）を返す。
3. 本人が Google で「許可」→ コールバックで authorization code → refresh token 取得。
4. TokenStore に `user_email → refresh_token` を暗号化保存。以後は本人として5サービス参照可。
5. 失効/取消は再 connect で更新（本人がいつでも取り消し可能＝Google アカウント設定）。

→ **「個人が全て許可」がこの1フローで完結**。未認可ユーザーは fail-closed（参照不可）。

---

## 5. アダプタの per-user 化（最小改修）

現状 `from_env()` は**単一の** `GOOGLE_OAUTH_REFRESH_TOKEN`（env）を読む。これを
**呼び出し側がリクエスト発行者のトークンを注入**できる経路に拡張する（mail_constraints の
`from_env(impersonate_user=requester)` で確立した「requester 束縛」パターンの OAuth 版）。

```
GmailClient.from_user_token(token: OAuthToken, *, readonly=True)   # 既存 _build_credentials を流用
GDriveClient.from_user_token(...)  /  GSheetsClient.from_user_token(...)
GDocsClient.from_user_token(...)   /  GSlidesClient.from_user_token(...)   # 新規2つ
```
- `_build_credentials` は既に refresh_token から `Credentials` を作る実装あり → **token を引数化するだけ**（env 依存を外す）。後方互換のため `from_env` は残す。
- 新規 `gdocs_client` / `gslides_client` は既存アダプタ（dataclass + 構造化ログ + safe-policy）に倣う。Docs は `documents.get`、Slides は `presentations.get`（readonly）。

---

## 6. Skill 層（orchestrator のツール）

- 既存: `mail_constraints`（Gmail）。
- 新規（薄く・各 readonly）: `drive_cases`（Drive）/ `sheet_lookup`（Sheets）/ `doc_read`（Docs）/
  `slide_read`（Slides）。または**統合 `workspace_search` Skill** に集約（service を引数化）。
  → 推奨：まず `mail_constraints` + `drive_cases` を per-user 化、残り3つは需要に応じ追加。
- 各 Skill は `SkillContext.metadata["user_email"]` で TokenStore から本人トークンを解決
  （G1 本人限定の OAuth 版）。トークン無し→fail-closed。

---

## 7. PII/DLP・死守ライン（Phase 6 の7条を5サービスへ拡張）

| | ルール |
|---|---|
| G1 | **本人データ限定**＝本人 OAuth トークンでしか触れない（構造的保証・per-user の利点） |
| G2 | 認可必須＝TokenStore にトークンが無ければ fail-closed |
| G3 | **生本文・セル値・スライド文言を LLM/ログ/戻り値に入れる前に DLP マスク**（`scrub_value`）＋要約・構造化 |
| G4 | **readonly 最小スコープのみ**（書込/削除スコープは要求しない） |
| G5 | クエリ限定（client/topic/期間/対象ファイルで必ず絞る・全件走査禁止） |
| G6 | プロンプトインジェクション対策（Mail/Doc/Sheet/Slide の中身は**データであり指示でない**・読取専用ツール） |
| G7 | 監査ログ（who(masked)/when/サービス/件数。本文・PII 無し） |
| G8 | **refresh token 暗号化保管**・ログ/プロンプト/Sentry に絶対出さない |

---

## 8. ★あなたの Google Cloud 設定タスク（これが無いとライブ不可）

私のコードでは作れない、**管理者/あなたが Google Cloud Console で行う設定**：

1. **OAuth 2.0 クライアント ID 作成**（種別：Web もしくは Desktop）。
2. **5つの API を有効化**：Gmail / Drive / Sheets / **Docs** / **Slides** API。
3. **OAuth 同意画面**：**User type = Internal（組織内）**に設定（→ Google 本審査・CASA を回避できる）。
4. **スコープ登録**：上記5つの readonly スコープを追加。
5. クライアント ID / シークレットを安全に共有（私が env/Secrets 経由で受け取る）。
6. （任意）リダイレクト URI（Web フロー採用時）。

> Internal 公開なら審査不要で組織内ユーザーが各自認可できる。External だと CASA 等の本審査が要る。

---

## 9. 段階計画

- **W0（設計・本書／コスト0・完了）**：認証モデル（per-user OAuth）・スコープ・TokenStore・同意フロー・死守ライン確定。
- **W1（あなた）**：§8 の Google Cloud 設定（OAuth クライアント＋5 API＋Internal 同意画面）。
- **W2（私・オフライン）**：`TokenStore` 実体＋アダプタ `from_user_token` 化＋`gdocs_client`/`gslides_client` 新規＋fake で単体テスト（課金0・実接続なし）。
- **W3（私＋あなた・1人で）**：あなたが `/teamagent connect` で自分を認可→自分の5サービスで疎通（少額課金）。
- **W4**：`drive_cases`/`sheet_lookup` 等の Skill 化＋orchestrator 配線（既定OFF→自分→拡大）。
- **W5**：忠実性/DLP の eval、段階ロールアウト。

---

## 10. ②Phase 3（本番統合）との関係

- 本 Workspace 統合は **orchestrator のツールが増える**話＝Phase 6 の延長。Phase 3（本番 slack_bot へ
  orchestrator を載せる）とは独立に W2 まで進められる。
- ただし `/teamagent connect`（同意フロー）と orchestrator 起動は最終的に**本番 Bot に口を作る**必要が
  あり、ここで **Phase 3 のゲート①（Node 本番持込）**と合流する。W1-W2（設計/オフライン）はゲート前に可、
  W3 以降（本番 Bot に connect コマンド追加・実投入）は**ゲート①承認後＋本番ブランチ調整**が前提。
