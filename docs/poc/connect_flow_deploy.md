# 営業の Google 連携 ＆ 管理画面 — 運用手順書（リンク配布方式・常時稼働）

営業に配るのは **リンク1本だけ**。営業は `salesperson_connect_guide.pptx` のとおり
**「届いたリンクを開く → 自分のGoogleでログイン → 許可」** の3ステップで完了（コマンド/ターミナル不要）。
管理画面は常時稼働させ、ブラウザのリンクでいつでも確認する。

```
[管理者] 全員分のリンクを生成 ──Slack/メールで配布──▶ [営業] リンクを開く→ログイン→許可
                                                              │
                                          Google が connect_web /oauth2/callback にリダイレクト
                                                              ▼
                                   state検証 → token を KMS暗号化して RDS 保存 → 「✅連携完了」
                                                              │
                                      [管理者] 管理画面の「Workspace連携状況」が +1名（実データ）
```

---

## A. 一回だけの準備（管理者）

### A-1. Web 型 OAuth クライアントを用意（最重要・ここが入口）
Google Cloud Console → APIとサービス → 認証情報:
- **OAuth クライアント（種類: ウェブ アプリケーション）** を作成（既存は Desktop 型＝loopback専用で**使えない**）。
- **承認済みリダイレクト URI** に connect_web の公開 callback を登録:
  - 本番: `https://<連携サーバのドメイン>/oauth2/callback`
  - ローカルテスト: `http://localhost:8788/oauth2/callback`
- 同意画面 = Internal（社内のみ）。スコープは 7サービス readonly（コードで固定）。
- 発行された **Client ID / Client Secret** を控える。

### A-2. 共有 env（connect_web と Slack Bot で同じ値に）
| env | 用途 |
|-----|------|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | A-1 の Web 型クライアント |
| `OAUTH_STATE_SECRET` | CSRF state の署名鍵。`openssl rand -hex 32` 等で生成し**全所で同一値** |
| `OAUTH_REDIRECT_URI` | A-1 で登録した callback URL と**完全一致** |
| `OAUTH_KMS_KEY_ID` | `alias/teamagent-oauth-tokens`（connect_web のみ） |
| `DATABASE_URL` | RDS（connect_web / 管理画面のみ。EC2同一VPCなら直結、ローカルはSSMトンネル） |

---

## B. サーバを常時稼働させる

> **本番（推奨・真の常時稼働）= RDS と同じ VPC の EC2 にデプロイ**。トンネル不要・実HTTPS・24/365。
> 営業全員がいつでもリンクを踏め、管理画面もいつでも見られる。ノートPC運用は sleep/トンネル切れで
> 落ちるので**ロールアウトには EC2 を強く推奨**。

### B-1. connect_web（連携の受け口）を起動
```bash
cd ~/Documents/teamagent-orchestrator-poc
# A-2 の env を export 済みの前提
PYTHONPATH=src .venv/bin/python -m teamagent.connect_web   # 既定 127.0.0.1:8788
```
本番は systemd（EC2）でサービス化し、ALB/HTTPS の背後に置く。`CONNECT_WEB_HOST=0.0.0.0` で待受。

### B-2. 管理画面を常時稼働
- **ローカル簡易**: `bash scripts/run_dashboard.sh`（SSMトンネル自動＋起動）。ターミナルを閉じても
  動かすには `nohup bash scripts/run_dashboard.sh >/tmp/dash.log 2>&1 &`、または
  `caffeinate -s bash scripts/run_dashboard.sh`（Mac をスリープさせず常駐）。
- **本番（推奨）**: EC2 に systemd サービスとして常駐。`http://<host>:8787`（or ALB/HTTPS）で常時アクセス。
  同一 VPC なら `DATABASE_URL` は RDS 直結（SSMトンネル不要）。

> ローカルで「自動起動」したい場合は launchd LaunchAgent 化も可能（必要なら言ってください、plistを用意します）。

---

## C. 営業へリンクを配る（管理者）

### C-1. 全員分のリンクを生成
```bash
cd ~/Documents/teamagent-orchestrator-poc
# A-2 の env（GOOGLE_CLIENT_ID/SECRET・OAUTH_STATE_SECRET・OAUTH_REDIRECT_URI）を export 済み
PYTHONPATH=src .venv/bin/python scripts/make_connect_links.py \
    taro@vectorinc.co.jp hanako@vectorinc.co.jp
# または名簿ファイルから: ... scripts/make_connect_links.py --file emails.txt
```
→ `email <TAB> リンク` が1人1行で出力される。

### C-2. 配布
- 各リンクを **本人だけに** Slack DM / メールで送る（`salesperson_connect_guide.pptx` を添えると親切）。
- 営業は **リンクを開く → 自分の会社Googleでログイン → 「許可」** で完了（3ステップ）。

> 代替: Slack に `/teamagent_connect` コマンドを登録すれば、営業が自分で打って自分のリンクを取得も可能
> （Bot 稼働＋A-2 env が必要）。配布方式（C）と併用できる。

---

## D. 連携できたか確認（管理者）
管理画面（`http://localhost:8787` など）の **「Workspace連携状況」** が **+1名** になればOK。
表示は email・scope数・連携日のみ（トークン本体＝暗号化は画面・DBロールとも復号・取得しない）。

---

## E. セキュリティ & 運用注意
- **正しい人に正しいリンクを**: リンクは「その email 用」に署名(state)されている。本人は**自分の会社
  アカウント**でログイン＆許可すること（Internal アプリ＋会社ドメインで社外は弾かれる）。別アカウントで
  許可すると意図しない紐付けになり得るので、配布時に取り違えない。
  - さらに堅くするなら「同意したGoogleアカウントの email が state と一致するか」を callback で検証する
    強化が可能（openid/email スコープ追加が必要）。必要なら実装します。
- connect_web は **トークン書き込み（KMS暗号化 + RDS）= teamagent_app 級**。read-only 管理ダッシュボード
  （teamagent_dashboard・復号不可）とは**別アプリ・別権限**に分離済み。
- state は HMAC 署名でCSRF/改竄検証。同意ページ/ログに refresh token は出さない（G8）。

---

## F. すぐ試す（1人・ローカル最短）
1. A-1 で Web 型クライアント作成 → リダイレクト URI に `http://localhost:8788/oauth2/callback` 登録。
2. A-2 の env を export（`OAUTH_REDIRECT_URI=http://localhost:8788/oauth2/callback`）＋ SSMトンネル(15433)。
3. `PYTHONPATH=src .venv/bin/python -m teamagent.connect_web`（callback 起動）。
4. `PYTHONPATH=src .venv/bin/python scripts/make_connect_links.py あなた@vectorinc.co.jp` → 出たリンクを
   この Mac のブラウザで開く → 許可 → 「✅連携完了」。
5. 管理画面（localhost:8787）の連携状況が +1名 になれば成功。

_作成: 2026-06-04 / PoC branch poc/multiskill-orchestrator。リンク配布方式・常時稼働対応版。_
