# 連携失敗の診断コード Runbook（`診断: CONNECT-…` を受け取ったら）

作成: 2026-09-03。対象: 利用者から転送された **`診断: CONNECT-<系統><番号> <時刻 JST> <識別子> [<request_id>]`** の 1 行から、
原因の特定 → ログの引き方 → 対処へ直行するための管理者向け手順。

単一情報源はコード側の `src/teamagent/connect_diagnostics.py`（`DIAG_SPECS`）。この runbook の表と食い違ったらコード側が正。

> 連携の経路（2 段）: ① Slack で「連携」→ mcp `oauth_connect`（`src/teamagent/skills/oauth_connect/skill.py`）が本人専用リンクを発行
> → ② 利用者が Google/Slack で許可 → connect-web の `/oauth2/callback` `/slack/oauth/callback`（`src/teamagent/connect_web/app.py`）が state 検証・token 交換・保存。
> ①の手前に mcp gateway の本人特定（`_resolve_metadata` / `_verify_caller`）がある。

## 診断行の読み方

```
診断: CONNECT-S01 2026-09-03 10:15 JST k***@vectorinc.co.jp 1-68b7c0de-0123456789abcdef01234567
      └─コード────┘ └─時刻(JST・分まで)─┘ └─識別子──────────┘ └─request_id（あれば）──────────────┘
```

| 部分 | 意味 | 出どころ |
|---|---|---|
| コード | 下表のどれか。**系統**が場所を表す（S/T = connect-web、I/L = mcp） | `ConnectDiag` |
| 時刻 | 失敗ページ/エラー文を組んだ時刻（JST）。ログ検索の窓は **±5 分** | サーバ時刻 |
| 識別子 | マスク済みメール（`k***@…`）／Slack user ID（`U…`）／`-`（不明） | 署名検証済みの値のみ。署名不一致(S01)では出さない |
| request_id | connect-web: ALB の `X-Amzn-Trace-Id` の `Root=` 値。mcp: `req-…`（SkillContext） | ALB アクセスログ / mcp の `request_id` フィールド |

秘匿値（state / code / token / secret）は**設計上含まれない**。転送文に URL が付いていたら利用者に削除を促す。

## コード表

| コード | 意味 | 場所 | ログ event | 利用者の対処 | 管理者の対処 |
|---|---|---|---|---|---|
| **S01** | state 署名不一致（リンクが途中で改変された。LLM の再タイプ・コピー欠け） | connect-web Google | `connect_callback_bad_state` (`state_reason=bad_signature\|malformed\|missing_params`) | 「連携」で新しいリンク | 再発なら `USE_OAUTH_START_LINKS`（PR #376・path 形式リンク）を ON に |
| **S02** | state 期限切れ（発行から 30 分超） | connect-web Google | `connect_callback_bad_state` (`state_reason=expired`) | 「連携」で新しいリンク | 発行→クリックの間隔を確認（DM を後で開いた等） |
| **S03** | 使用済みリンク | connect-web Google | `connect_callback_reused_state` | 「連携」で新しいリンク | 2 回目が来る原因（リンクスキャナ・ブラウザのプリフェッチ）を確認。1 回目が `connect_callback_ok` なら連携自体は済んでいる |
| **S04** | Google アカウント不一致 | connect-web Google | `connect_callback_account_mismatch` | 会社アカウントでログインし直して許可 | 個人 Gmail で許可していないか本人に確認 |
| **S05** | 許可画面で拒否（キャンセル） | connect-web 両方 | `connect_callback_user_denied` / `connect_slack_callback_user_denied` | もう一度「連携」→「許可」 | 権限説明が不安なら口頭で補足 |
| **S06** | サーバ側障害 | connect-web 両方 | `connect_callback_state_store_unconfigured` / `_state_consume_failed` / `_exchange_failed` / `_store_failed` / `_id_token_missing` / `_id_token_invalid` / `_client_id_missing`（Slack 版は `connect_slack_callback_*`） | 管理者へ | **下の「S06 の切り分け」** |
| **I01a** | 本人特定失敗: 署名済み Slack caller が無い | mcp gateway | `caller_claim_rejected` / `identity_spoof_rejected reason=missing_verified_caller` | 管理者へ | OpenClaw の caller-identity plugin / `_user_context` 欠落。Slack 以外の経路から呼んでいないか |
| **I01b** | 本人特定失敗: resolver でエラー | mcp gateway | `identity_spoof_rejected reason=resolver_error` | 管理者へ | Slack `users.info` の失敗（token 失効・rate limit）。mcp ログの直前の例外 |
| **I01c** | 本人特定失敗: Slack ユーザーを会社メンバーへ解決できない | mcp gateway | `identity_spoof_rejected reason=resolve_none` | 管理者へ | Slack プロフィールのメールが会社ドメイン外／未設定。ゲスト・外部 WS |
| **I02** | 本人メール未取得（fail-closed） | mcp `oauth_connect` | `oauth_connect_fail_closed reason=no_user_email` | Slack プロフィールのメールを確認・管理者へ | metadata に `user_email` が無い。I01 系と同根のことが多い |
| **I03** | Slack 再連携が必要 | mcp `oauth_connect` | `oauth_connect_slack_rebind_needed` (`reason=uid_mismatch\|stored_uid_missing`) | 案内文のリンクで Slack を連携し直す | 保存済み Slack ID と現在の ID が違う（アカウント作り直し等）。正常な自己復旧経路 |
| **L01** | 連携リンク生成失敗 | mcp `oauth_connect` | `oauth_connect_url_failed` / `oauth_connect_slack_url_failed` / `oauth_connect_slack_url_suppressed` | 管理者へ | OAuth 系 env（`OAUTH_REDIRECT_URI` / `CONNECT_GOOGLE_CLIENT_*` / `CONNECT_SLACK_CLIENT_ID` / `*_STATE_SECRET`）の欠落。`suppressed` は検証済み Slack ID が無い経路 |
| **T01** | Slack 側 state 不正/期限切れ/使用済み | connect-web Slack | `connect_slack_callback_bad_state` / `_reused_state` / `connect_slack_state_unbound_rejected` | 「連携」で新しいリンク | S01〜S03 と同じ切り分け（Slack 版は署名/期限を区別しない） |
| **T02** | Slack team 不一致 / 許可したアカウント不一致 / Slack ID 重複 | connect-web Slack | `connect_slack_callback_team_mismatch` / `_identity_mismatch` / `_identity_missing` / `slack_oauth_uid_collision` | 管理者へ | 別 WS・別アカウントで許可。`uid_collision` は同じ Slack ID が別メールに保存済み（DB 側の付け替えが要る） |

## ログの引き方（CloudWatch Logs Insights）

時刻は診断行の JST を UTC に直して（−9h）±5 分で窓を切る。

### mcp（gateway / oauth_connect）— `event` フィールドで引く

ロググループ: mcp サービスのもの（`/ecs/teamagent-dev-mcp` 系）。structlog の JSON なので `event` で絞れる。

```
fields @timestamp, event, reason, diag, tool, request_id, user_email_masked, error
| filter event in ["identity_spoof_rejected", "caller_claim_rejected",
                   "oauth_connect_fail_closed", "oauth_connect_url_failed",
                   "oauth_connect_slack_url_failed", "oauth_connect_slack_url_suppressed",
                   "oauth_connect_slack_rebind_needed", "oauth_connect_url_issued"]
| sort @timestamp desc
| limit 50
```

- 診断行に `req-…` があれば `| filter request_id = "req-…"` で 1 発。
- `diag` フィールド（本 PR で追加）に `CONNECT-…` が入るので `| filter diag = "CONNECT-L01"` でも引ける。
- 直前の `oauth_connect_url_issued`（`user_email_masked`）で「リンクは発行できていたか」が分かる。

### connect-web（callback）— `@message` を parse して引く

ロググループ: connect-web サービス（`/ecs/teamagent-dev-connect-web` 系）。structlog の console 出力なので event 名を正規表現で抜く。

```
fields @timestamp, @message
| parse @message /(?<ev>connect_(slack_)?callback_[a-z_]+|connect_slack_state_unbound_rejected|slack_oauth_uid_collision)/
| parse @message /diag=(?<diag>CONNECT-[A-Z]\d\d[a-c]?)/
| parse @message /state_reason=(?<state_reason>[a-z_]+)/
| filter ispresent(ev)
| sort @timestamp desc
| limit 50
```

- `diag=` と `state_reason=` は本 PR で warning ログに付けた。**S01 なら `state_reason=bad_signature`（転記事故）か `malformed`（切れた/欠けた）か**まで分かる。
- request_id は ALB の trace（`Root=1-…`）。ALB アクセスログ（S3）を同じ値で grep すると、UA・送信元 IP・実際のリクエスト URL 長が分かる（＝スキャナ/プリフェッチの判定に使う）。

### 受信 state の復号診断（利用者が URL そのものを送ってきた場合）

state は `base64url(email|issued|nonce|hmac)` で秘匿ではない（署名鍵が秘密）。本番の `OAUTH_STATE_SECRET` は扱わず、**署名を検証せずに構造だけ**見る:

```bash
python3 - <<'EOF'
import base64, datetime, sys
state = sys.argv[1] if len(sys.argv) > 1 else input("state: ").strip()
pad = "=" * (-len(state) % 4)
try:
    raw = base64.urlsafe_b64decode(state + pad).decode("utf-8")
except Exception as e:
    print("malformed:", type(e).__name__); raise SystemExit(1)
parts = raw.split("|")
print("fields:", len(parts), "(expect 4)")
if len(parts) == 4:
    email, issued, nonce, sig = parts
    print("email :", email)
    print("issued:", datetime.datetime.fromtimestamp(int(issued), datetime.timezone(datetime.timedelta(hours=9))))
    print("sig   :", len(sig), "hex chars (expect 64)")
EOF
```

- 4 要素・64 hex なのに S01 → **署名不一致**＝どこかの 1 文字が変わっている（LLM の再タイプ）。`/oauth2/start/{state}`（PR #376）への切替が根治。
- 4 要素にならない／末尾が欠ける → **コピー欠け**（Slack の URL 折り返し・スマホのコピー）。
- `issued` が 30 分より前 → S02。

署名の検証が必要なときは、本番 secret を手元に持ってこず、`connect-web` タスクへ `aws ecs execute-command` で入って `inspect_state()` を呼ぶ（`OAUTH_STATE_SECRET` は環境に入っている）。

### S06 の切り分け

| ログ event | 典型原因 | 対処 |
|---|---|---|
| `*_state_store_unconfigured` | `HMAC_STATE_TABLE` / `HMAC_STATE_SCOPE` env 欠落（2026-08 実害・8 連打） | tfvars の env を確認して再デプロイ。利用者の操作では直らない |
| `*_state_consume_failed` | DynamoDB スロットリング・IAM・ネットワーク | 時間をおいて再試行。続くならタスクロールの DynamoDB 権限 |
| `*_exchange_failed` | Google/Slack token endpoint 失敗（client secret 失効・redirect_uri 不一致） | `detail=` の例外型。`invalid_client` なら secret、`redirect_uri_mismatch` なら OAuth クライアント設定 |
| `connect_callback_id_token_missing` / `_invalid` | `openid` scope 欠落・時計ズレ・client_id 不一致 | mcp と connect-web の `CONNECT_GOOGLE_CLIENT_ID` が同じか |
| `connect_callback_client_id_missing` | connect-web に client_id env が無い | tfvars |
| `*_store_failed` | RDS/KMS（`OAUTH_KMS_KEY_ID`・RLS ロール） | `detail=` の例外型 |

## 2026-09-03 の実例（本 PR の動機）

| 誰 | 起きたこと | 本 PR 後に出るコード | 当時どう見えたか |
|---|---|---|---|
| kyokav-sato / w-tokuno | @Aico が返した約 600 字の認可 URL を LLM が再タイプして state が 1 文字ズレ → callback で HMAC 不一致。1〜2 分後に再発行したリンクは通った（鍵ではなく転記の事故） | **S01**（`state_reason=bad_signature`） | 「検証に失敗しました。リンクが古いか不正です」だけ。利用者は「古い」と読んで何度も取り直した |
| （複数） | Slack のリンクスキャナ／ブラウザのプリフェッチが callback を先に踏み、本人が開いた時には消費済み | **S03**（1 回目が `connect_callback_ok` でないなら S01/S06 も疑う） | 「リンクが古いか使用済みです」。本人は 1 回しか開いていないので混乱 |
| n-watanabe | callback は届いたがサーバ側で失敗（state 保管先/交換/保存のどれか＝`connect_callback_*_failed`） | **S06** | 「連携に失敗しました。時間をおいて…」で利用者は再試行し続けた。実際は利用者側で直らない |
| mx-ebata | Google は完了、Slack の許可画面まで進まず（Slack 連携未完了のまま）。`oauth_connect` の Slack リンクが出ていなかった可能性 | **L01**（`slack_url_suppressed` / `slack_url_failed`）か、リンクは出ていて未クリック | 「Google 連携が完了しました」で終わったと思っていた |

## 問い合わせ対応の流れ（テンプレ）

1. 利用者から `診断:` 行を受け取る（無ければ「失敗画面の一番下の 1 行を送ってください」）。
2. 上のコード表で **系統**を見る: S/T → connect-web のクエリ、I/L → mcp のクエリ。
3. `S01/S02/S03/S05/T01` は利用者が「連携」でやり直せば直る。2 回以上続くなら根治側（`USE_OAUTH_START_LINKS`）を検討。
4. `S04` は Google のアカウント選択。`I03` はリンクをそのまま押してもらう。
5. `S06/I01*/I02/L01/T02` は利用者では直らない。ログを引いて env / Slack プロフィール / DB を直す。
6. 直したら利用者に「連携」をもう一度送ってもらい、`connect_callback_ok` / `connect_slack_callback_ok` を確認して閉じる。

## 関連

- コード: `src/teamagent/connect_diagnostics.py`（コード表の正）、`src/teamagent/connect_web/app.py`（`_connect_failure`）、`src/teamagent/mcp_gateway/server.py`（`_identity_rejected`）、`src/teamagent/skills/oauth_connect/skill.py`（`_diag`）
- OpenClaw 指示: `infra/openclaw/SOUL.md`「連携の失敗は『診断:』行をそのまま出す」（OC 再ビルドは別便）
- 根治側: PR #376 `/oauth2/start/{state}`（path 形式リンク・`USE_OAUTH_START_LINKS` 既定 OFF）
