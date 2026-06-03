# W1 セットアップ手順：Google Cloud で per-user OAuth クライアントを作る

> あなた（管理者）が Google Cloud Console で行う作業。所要 15〜30分。
> これが完了すると、各メンバーが `/teamagent connect` で自分の5サービスを認可できるようになる。
> 設計：`docs/poc/workspace_integration_design.md`。

---

## 0. 前提
- vectorinc の **Google Workspace 管理者権限**（または OAuth 設定権限）のあるアカウントで作業。
- 「**Internal（組織内）**」公開にするのが肝（→ Google の本審査・CASA が**不要**になる）。

---

## 1. プロジェクトを用意
- Google Cloud Console: <https://console.cloud.google.com/>
- 既存の TeamAgent 用プロジェクトがあればそれを選択。無ければ「新しいプロジェクト」を作成。

## 2. 5つの API を有効化
「APIとサービス」→「ライブラリ」 <https://console.cloud.google.com/apis/library> で以下を検索→**有効にする**：
1. **Gmail API**
2. **Google Drive API**
3. **Google Sheets API**
4. **Google Docs API**
5. **Google Slides API**

（直リンク例：Gmail <https://console.cloud.google.com/apis/library/gmail.googleapis.com> ／ Docs <https://console.cloud.google.com/apis/library/docs.googleapis.com> ／ Slides <https://console.cloud.google.com/apis/library/slides.googleapis.com>）

## 3. OAuth 同意画面を「Internal」で設定 ★最重要
- <https://console.cloud.google.com/apis/credentials/consent>
- **User Type =「内部（Internal）」を選択**（組織内ユーザーのみ＝Google 本審査・CASA 不要）。
- アプリ名（例「TeamAgent」）・サポートメール等を入力して保存。

## 4. スコープを追加（すべて **readonly**）
同意画面の「スコープを追加または削除」で、以下5つを手入力で追加：
```
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/drive.readonly
https://www.googleapis.com/auth/spreadsheets.readonly
https://www.googleapis.com/auth/documents.readonly
https://www.googleapis.com/auth/presentations.readonly
```
（Drive メタデータも使う場合は `https://www.googleapis.com/auth/drive.metadata.readonly` も追加）

## 5. OAuth クライアント ID を作成
- 「APIとサービス」→「認証情報」 <https://console.cloud.google.com/apis/credentials>
- 「認証情報を作成」→「**OAuth クライアント ID**」
- アプリケーションの種類：
  - 手早く各自認可するなら **「デスクトップアプリ」**（リダイレクト不要・コピペ式）が簡単。
  - 将来 Slack からワンクリックにするなら **「ウェブアプリケーション」**（リダイレクト URI 必要）。
  - → **まずは「デスクトップアプリ」推奨**（W3 で自分1人を認可する検証が最速）。
- 作成すると **クライアント ID** と **クライアント シークレット** が表示される。

## 6. 私に安全に共有してもらうもの
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
（共有方法は Secrets/環境変数経由。**Slack 平文やコミットには貼らない**でください。）

---

## 完了後の流れ（私の W2/W3）
- W2（私・並行作業中）：Docs/Slides アダプタ・per-user トークンストア・`from_user_token`・`connect` 同意フローの骨組みをオフラインで実装（コスト0・実接続なし）。
- W3（あなたのクライアント情報受領後）：あなた1人が consent フローで自分を認可 → 自分の5サービスで疎通確認（少額課金）。
- W4：drive/sheets/docs/slides を orchestrator のツール化 → 既定OFF→自分→段階拡大。

## トラブル時
- 「Internal を選べない」→ Workspace 組織アカウントでない可能性（個人 Gmail だと Internal 不可）。組織アカウントで作業を。
- 「スコープが見つからない」→ 手入力欄に上記 URL をそのまま貼る。
- 詰まったら该当画面のスクショを共有いただければ個別に補助します。
