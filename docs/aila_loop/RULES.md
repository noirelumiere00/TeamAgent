# RULES.md — AiLa Loop 行動規範（v1.1）

最終更新: 2026-06-16
対象: `/loop` `/schedule` で自律実行される全エージェント（Claude Code / Subagent / Workflow / Skill）
責任者: Shogo（Sprint 14 go-live 責任者）
本ファイルの位置づけ: loop は **各 iteration 冒頭で本ファイルを再読込し、SHA256 を期待値と照合してから動く**。読み込み失敗 / hash 不一致なら実行しない（PDCA_LOOP.md §9）。

---

## 0. 大原則（Prime Directives）

1. **本番は人間の明示承認なしに触らない。** AWS Account `718959508629`（東京）と `connect.vectorinc.co.jp` 配下の本番リソースは、ユーザーが当該セッションで明示的に「本番に適用してよい」と発話するまで **read-only ですら原則禁止**。
2. **疑わしいときは止まる。** ルール該当性が判定できない場合、loop は次のアクションを実行せず、ユーザー確認待ち（pause）に入る。
3. **冪等・可逆・小ロット。** 1 ループ 1 テーマ、差分は最小、git working tree は常に commit 可能な状態に保つ。force 系は禁止（§1）。
4. **計画は memory と plans に必ず残す。** 実行ログ・判断・次の Wave を `~/.claude/projects/-Users-s-komata/memory/` および `~/.claude/plans/abstract-zooming-raccoon.md` に書き込み、セッション断絶でも復元できるようにする。
5. **Sprint 14（2026-12-28）の go-live を毀損する変更は禁止。**
6. **subagent は role を超えない。** Maker は Check しない。Checker は Maker のメモリを引き継がない。Synthesizer は実装しない（PDCA_LOOP.md §4 三角分離）。

---

## 1. HARD BLOCKS（即停止：違反した瞬間に loop は abort）

以下に該当するアクションを生成・実行しようとした時点で loop は **即時停止**・スタックトレースを memory に記録・ユーザーへ通知する。再開には明示承認が必要。

### 1.1 データ破壊系
- 本番 RDS（pgvector・東京）に対する `DROP`, `TRUNCATE`, `DELETE FROM <table>`（WHERE 無し or 全件相当）、`ALTER ... DROP COLUMN`, `REVOKE ALL` の発行禁止。
- `pg_dump` 取得後の本番上書き restore 禁止。
- S3 本番バケット（ingest / artifacts / proposal_deck publish 先）への `--recursive` 削除、バージョン無効化、バケットポリシー上書き禁止。
- 本番 EC2 / Fargate / ECS タスクへの `terminate`, `stop-task`, `delete-service`, `update-service --force-new-deployment` の自動発行禁止。
- ローカルでも `rm -rf ~/`, `rm -rf /`, `rm -rf ~/.claude`, `rm -rf ~/Documents/TeamAgent*`, `rm -rf ~/Documents/AI-IA-UAE` は絶対禁止。

### 1.2 シークレット・トークン露出
- Bedrock API キー / Slack Bot Token (`xoxb-*`, `xoxa-*`, `xoxp-*`) / Google OAuth refresh_token / Gmail token / MCP bearer / RDS パスワード / SSM パラメータ値 / `.env*` の中身を Slack・Gmail 下書き・公開ログ・PR 本文・コミットメッセージ・MEMORY.md・スクショに **書き出さない**。
- Slack 投稿・Gmail 下書き・GitHub Issue / PR / Gist にトークン文字列が混入する出力を生成しない。事前 regex マスク必須:
  - `xox[abprs]-`, `AKIA[0-9A-Z]{16}`, `ASIA[0-9A-Z]{16}`, `ya29\.`, `eyJ[A-Za-z0-9_\-]+\.`, `-----BEGIN .* PRIVATE KEY-----`
- `git commit` 前に必ず `git diff --staged` で上記 regex を検査。1 件でもヒットしたら commit せず abort。
- `git push --force`, `git push --force-with-lease`, `git reset --hard origin/<protected>`, `git branch -D` は protected branch（`main`, `dev`, `release/*`, `feat/v3.1-monorepo`）に対して禁止。
- **Q5 対応**: Maker は AWS Secrets Manager / SSM / `gcloud auth` 系コマンドを呼べない（Bash 前置詞 denylist）。`aws secretsmanager get-secret-value`, `aws ssm get-parameter`, `aws ssm get-parameters`, `gcloud auth`, `cat ~/.aws/credentials`, `cat ~/.config/gcloud/**` は全部 abort。
- **Q5 対応**: pre-commit hook で `*.env`, `service-account.json`, `vertex_sa.json` のステージング自体を拒否。
- **Q5 対応**: PDCA loop の Maker/Checker worktree からは Sentry を disable（環境変数 `SENTRY_DSN=` 空で起動）。

### 1.3 RLS / マルチテナント境界
- pgvector への SQL に `SET row_security = off` / `BYPASSRLS` / `FORCE ROW LEVEL SECURITY` / `SECURITY DEFINER` を含めない。これらを含む diff は Checker が **必ず verdict=block**（Q9 対応）。
- service role / superuser での本番接続を loop から開かない（SSM port-forward + アプリロール経由のみ許可）。
- `SELECT ... FROM documents` 等を **テナント絞り込み無しで** 発行しない（必ず `WHERE tenant_id = current_setting('app.tenant_id')` 相当を含める）。
- 検索結果を **テナント横断で** 1 人のユーザーに返さない（cross-tenant leakage は重大インシデント）。
- **Q9 対応**: PDCA loop 内では DB-gated test (28 件) を **実行禁止**（`RUN_DB_TESTS=0` 強制）。月次 RLS live 実行は Shogo が SSM トンネル経由で別途実施。
- **Q9 対応**: worktree の `.env*` は Skill 起動時に削除・作成禁止。本番 RDS 接続情報 (`teamagent/dev/database-url`) を Maker が解けないようにする。

### 1.4 IaC / デプロイ系
- `terraform apply` の plain 実行は禁止。必ず `terraform plan -target=...` → 人間レビュー → `terraform apply -target=...` の targeted only。
- `terraform destroy`, `terraform state rm`, `terraform state push` を loop から発行しない。
- 本番 ECS への `aws ecs update-service` / CodeBuild トリガー (`aws codebuild start-build`) は **soft_guardrail**（§2）。loop 内で自動起動しない。
- ECR への本番タグ（`prod`, `latest`, `release-*`）への `docker push` 禁止。
- **Q7 対応（dev も本番扱い）**: AWS Account `718959508629` 配下への deploy/build/update 系コマンド（`aws codebuild start-build`, `aws ecs update-service`, `aws ec2 send-command`, `aws ssm send-command`, `scripts/deploy_to_ec2.sh`, `scripts/deploy_to_ecs.sh`）は **dev／prod 問わず全部 Maker 禁止**。dev EC2 でも ingest 経由で本番ナレッジ生成パスに繋がる。
- **Q7 対応**: `scripts/deploy_to_ec2.sh` の冒頭に `if [ "$PDCA_LOOP_MODE" = "1" ]; then echo blocked; exit 1; fi` の物理ガード（実装責任は Shogo）。
- **Q19 対応（Bedrock model 切替）**: `openclaw.config.json5` / `variables_fargate.tf:openclaw_model_id` / MCP の `BEDROCK_MODEL_ID` 環境変数 / `jp.anthropic.claude-*` 文字列を含む diff は Checker 初期値=block。明示的に safe を論証できなければ block 維持。Maker の allowed_files から除外。
- **Q27 対応（Slack DM 暴発）**: `clawhub.disabled` の値を `true` から `false` に変える diff は Checker 初期値=block。OpenClaw の Slack 出力先 user ID／channel ID を環境変数化された allowlist 以外に変更する diff も block。

### 1.5 ネットワーク / SCP / IAM
- AWS Organizations SCP, IAM Policy, Trust Policy, Security Group の **本番側** 改変禁止（読み取りのみ可）。
- VPC Peering, Transit Gateway, Route53 本番ゾーン、ACM 本番証明書、CloudFront 本番ディストリビューションの変更禁止。
- MCP 金庫（streamable-http, port 8787, OpenClaw SG 限定）の SG 開放・public 化禁止。`0.0.0.0/0` の許可を含む変更は不可。
- 踏み台 SSM セッションを loop から張りっぱなしにしない（実行毎に close）。
- **Q8 対応（egress allowlist）**: Skill 起動時に egress allowlist を環境変数で固定。`HTTPS_ALLOW_HOSTS=api.anthropic.com,bedrock-runtime.ap-northeast-1.amazonaws.com,github.com,api.github.com,co.cohere.com,aiplatform.googleapis.com,oauth2.googleapis.com`（必要に応じて Shogo が追記）。それ以外の host への HTTP/HTTPS は Bash 前置詞 denylist（`curl https://` 系を一律 dry-run、Python `requests`/`httpx` も monkey-patch wrapper でフィルタ）。
- **Q8 対応**: Slack / Gmail / SES / Discord / Teams / SendGrid / Twilio / 任意の webhook 系 (`hooks.slack.com`, `discord.com/api/webhooks`, `*.webhook.office.com`) は hostname allowlist で物理拒否。
- **Q8 対応**: GitHub への push/PR 作成は `vectorinc/` org 配下のみ allow。それ以外の repo への `gh pr create` `gh issue create` は拒否。
- **Q8 対応**: 起動時に `ip route get 169.254.169.254` で IMDS の経路有無を確認し、loop は IAM 不要なので IMDS 経由の不意な IAM 借用が起きないよう `AWS_EC2_METADATA_DISABLED=true` を設定。

### 1.6 外部送信 / 公開
- Slack の **public channel** および **外部 workspace** への自動投稿禁止。
- Gmail / Outlook / SES 等からの **送信 (send)** 禁止。下書き作成 (`draft`) のみ許可（AiLa は denylist 物理封鎖を破らない）。
- 外部 API（OpenAI, Gemini, Anthropic 直, X/Twitter, TikTok 公式 API, ラッコ等）の **新規契約・課金プラン変更・キー発行** を loop が単独で行わない。
- GitHub Issue / PR の **public repo** への自動作成・コメント・close 禁止（vectorinc org private のみ）。

### 1.7 セッション・権限 / 改竄防止
- `sudo`, `chown root`, `chmod 777` を本番経路で発行しない。
- `~/.ssh/`, `~/.aws/credentials`, `~/.config/gcloud/`, `~/.claude.json` の **書き換え禁止**（読み取りすら明確な目的が必要）。
- 他ユーザー（営業16名）の OAuth refresh_token に loop から触らない。
- `git config --global` の書き換え禁止。
- **Q3 / Q16 / Q21 / Q29 対応（Maker 編集禁止リスト = allowed_files から除外）**:
  - `~/Documents/teamagent-orchestrator-poc/docs/aila_loop/VISION.md`
  - `~/Documents/teamagent-orchestrator-poc/docs/aila_loop/ARCHITECTURE.md`
  - `~/Documents/teamagent-orchestrator-poc/docs/aila_loop/RULES.md`（本ファイル）
  - `~/Documents/teamagent-orchestrator-poc/docs/aila_loop/PDCA_LOOP.md`
  - `~/Documents/teamagent-orchestrator-poc/docs/openclaw/deploy_runbook.md`
  - `~/Documents/teamagent-orchestrator-poc/docs/v3.2/slo_v1.md`
  - `~/.claude/plans/abstract-zooming-raccoon.md`
  - `~/.claude/plans/mossy-snacking-locket.md`
  - `~/.claude/projects/-Users-s-komata/memory/MEMORY.md`（**append-only でも Maker 禁止**。Synthesizer のみ初回 1 行追加可）
  - `~/.claude/projects/-Users-s-komata/memory/project_aila_pdca_state.md` の **Schema 定義部分**（`<!-- SCHEMA START -->` 〜 `<!-- SCHEMA END -->` ブロック内）
  - `~/.claude/skills/aila-pdca/**`（Skill self-modification 禁止）
  - `~/.claude/schedules.json`
  - `.github/CODEOWNERS`
  - `.git/hooks/**`, `.husky/**`, `.pre-commit-config.yaml`
  - 上記いずれかに対する変更を含む diff は Checker が **必ず verdict=block** とし、Improve は自動 revert + 人間エスカレーション。
- **Q3 対応**: `.github/CODEOWNERS` で本リスト全てを human-only に固定（実装責任は Shogo）。

### 1.8 ループ自身の暴走防止
- 同一 loop 内で同じ destructive コマンド（network call / write / shell）を **3 回連続失敗** したら abort。
- 1 loop iteration の実時間が **30 分** を超えたら abort。各フェーズには個別 wall-time（Plan 15min / Do 90min / Check 30min / Improve 10min）。フェーズ超過した時点で当該 subagent を Skill が kill（Q4 対応）。
- `/schedule` で cron 化する場合、本ルール（HARD BLOCKS 全条）を **冒頭で再読み込みしない実装は禁止**。冒頭で RULES.md の SHA256 が期待値と一致することを必ず検証（Q16 対応）。
- 子 Subagent / Workflow / Skill に **本ルールの遵守義務を継承させる**（prompt 先頭で RULES.md を必ず参照）。

### 1.9 コスト hard cap（Q4 対応）
- Skill 起動時に **当日 24h の Bedrock コスト** を `aws ce get-cost-and-usage` で事前確認。**$10/日（≈ 1500 円）超** なら即 abort。
- 月次 Bedrock コストが **$200（Budget $250 の 80%）** 到達で PDCA loop 自動 pause。再開は Shogo の明示承認。
- subagent 1 起動の wall-time タイマー（Plan 15min / Do 90min / Check 30min / Improve 10min）を Skill 側で実装し、超過した時点で当該 subagent を kill。
- 同一 `next_action` が **3 サイクル連続** で選び直された時点で「無限ループ検知」として人間エスカレーション（status=fail 3 連続ではなく、項目同一性で検知）。

---

## 2. SOFT GUARDRAILS（人間承認が必要：loop は提案・diff・dry-run まで）

以下は loop が **準備** はできるが、**実行はユーザー明示承認待ち** で停止する。承認は同一セッション内のテキスト発話で得る（"approve", "yes apply", "本番適用 OK" 等）。承認は **アクション単位** で消費し、次の同種アクションには再承認が必要。

### 2.1 本番デプロイ
- CodeBuild → ECS 本番デプロイ（runbook: `docs/openclaw/deploy_runbook.md`）。
- `terraform apply -target=...` 本番反映。
- RDS migration 本番適用（Alembic upgrade head）。
- MCP 金庫の bearer rotation 反映。
- `mcp:5` worker / orchestrator イメージ差し替え。

### 2.2 シークレット投入・更新
- AWS Secrets Manager / SSM Parameter Store への put（特に `/aila/prod/*`, `/teamagent/prod/*`）。
- Slack App credentials, Bedrock model access, Google OAuth client_secret の更新。
- `connect.vectorinc.co.jp` の OAuth client 設定変更。

### 2.3 16 名展開 / ユーザー影響
- 営業16名への Slack DM・チャンネル招待・機能ロールアウト。
- 朝メール秘書（AiLa）の朝配信スケジュール変更。
- 営業ログ化 / 提案ドラフト機能の本番 enable / disable。
- 機能の `feature_flag` を本番 `on` にする。
- **Q27 対応**: Wave4 サイクル全般（aiia-mcp ↔ OpenClaw 結線、connect.vectorinc.co.jp 配線、朝配信先 user_id allowlist の変更）は **Plan 後に status=stopped-by-human で必ず止まる**。
- **Q27 対応**: DM 送信先 Slack User ID の allowlist を Skill 環境変数で固定（パイロット中は Shogo の Slack ID のみ）。それ以外への送信は abort。

### 2.4 外部契約・課金
- Bedrock cross-region / 他リージョン model 有効化（コスト発生）。
- Cohere Rerank v3.5 / Gemini API / OpenAI 等の **新規 API キー発行・上位プラン契約**。
- AWS サービスクォータ引き上げ申請。
- 新規 SaaS / MCP connector の契約・有効化。

### 2.5 トークン rotation / 鍵管理
- Bedrock IAM ロールのポリシー更新。
- MCP bearer / Slack bot token / Google OAuth refresh_token の rotation。
- pgvector アプリロールの password rotation。
- TLS 証明書の差し替え（ACM, connect.vectorinc.co.jp）。

### 2.6 大型変更
- monorepo 構造変更、`feat/v3.1-monorepo` 等の大型ブランチの `main` への merge。
- proposal_deck Skill の本番配線（Wave3）。
- AiLa ↔ OpenClaw 結線（§Q-Q3, Wave4）。
- スキーマ大改修（pgvector index 再構築、テナント分割変更）。

### 2.7 運用イベント
- 負荷試験の本番（または本番影響のあるステージング）実施。
- DR / フェイルオーバー訓練（RDS / ECS / Route53）。
- 障害訓練（chaos / kill task）。

### 2.8 自動化の新規 cron 化
- `/schedule` でクラウド cron を作成する場合、対象が **本番に書き込みうる** なら承認必須。
- `systemd timer` / EventBridge Rule の本番側新設は承認必須。
- **Q14 対応**: PDCA loop の **並列実行** 解禁（Plan/Do/Check の並列 worktree 運用）は soft_guardrail。default は 1 サイクル/日の単線運用。
- **Q18 対応**: 1 サイクルで **複数 repo** をまたぐ（`teamagent-orchestrator-poc` と `AI-IA-UAE` の両方を編集する）作業は soft_guardrail。default は `cycle_plan.target_repo` 単一。

---

## 3. ALLOWED（loop が単独で進めてよい範囲）

- ローカル worktree（`~/Documents/teamagent-orchestrator-poc` の feature branch、`~/Documents/AI-IA-UAE` の feature branch）の編集・commit（allowed_files の範囲内）。
- ローカル `pytest`, `ruff`, `mypy`, `npm test` の実行（`RUN_DB_TESTS=0` 強制）。
- `git fetch`, `git status`, `git diff`, `git log`, `git show`（read-only git 操作）。
- `gh pr list`, `gh issue list`, `gh pr view`（private repo, vectorinc org のみ）。
- `~/.claude/plans/abstract-zooming-raccoon.md` への進捗追記（**Maker は読み取りのみ**、Synthesizer のみ追記可）。
- `~/.claude/projects/-Users-s-komata/memory/project_aila_pdca_state.md` の **末尾 append**（Schema ブロック外）。Synthesizer のみ。
- `docs/v3.2/` 配下の設計文書の更新（spec_vs_current_full_matrix, not_done_priorities 等）。
- PR の **draft** 作成（private repo のみ、base=`dev` 固定、`do-not-auto-merge` label 自動付与、本文に regex マスク済み）。
- ステージング / `dev` 環境への **read-only 確認**（`aws logs tail`, `aws ecs describe-services`, `aws logs get-query-results` 等の get/describe のみ）。
- Skill / Subagent の追加・改善（本ルールを継承させる。ただし `~/.claude/skills/aila-pdca/**` 自身の改変は禁止）。

---

## 4. 違反検知時のプロトコル

1. **STOP**: 実行中の tool call を即中断。後続の queue を破棄。
2. **RECORD**: 違反内容・該当ルール番号・直前の context を `~/.claude/projects/-Users-s-komata/memory/incidents/<UTC>.md` に追記。
3. **NOTIFY**: ユーザーへ「RULES.md §X.Y に該当したため停止しました。再開には明示承認が必要です。」と伝える。Slack / メール送信はしない（§1.6）。代わりに macOS ローカル通知を Skill ラッパが `osascript -e 'display notification'` で起動（Q23 対応）。
4. **WAIT**: 明示承認（"override approved", "再開してよい" 等）が来るまで pause。承認は当該違反 1 件にのみ有効。
5. **POSTMORTEM**: 再開後、同種違反の再発防止策（lint, pre-commit hook, prompt 改修）を提案する。

---

## 5. ルール改訂

- 本 RULES.md の改訂は **Shogo（ユーザー本人）の明示承認** が必要。loop が単独で本ファイルを編集してはならない（§1.7 allowed_files から除外）。
- 改訂時は version を `vX.Y` で増分し、変更履歴を末尾に追記。
- **改訂と同時に PDCA_LOOP.md §9 の `RULES_MD_SHA256` 期待値を更新する** こと（更新を忘れると翌朝 Skill が SHA256 不一致で起動しない設計＝Q16 対応）。
- Sprint 14 go-live（2026-12-28）以降は SLO 章を追加予定（負荷試験・DR 訓練の正式手順）。

---

## 6. 適用範囲確認チェックリスト（loop 実行前に必ず通す）

各 iteration の冒頭で以下を YES/NO で自己判定。1 つでも NO なら HARD BLOCK 扱いで停止。

- [ ] RULES.md の SHA256 を期待値と照合し一致した
- [ ] このアクションは §1（HARD BLOCKS）のいずれにも該当しない
- [ ] §2（SOFT GUARDRAILS）に該当する場合、ユーザーの明示承認を当該セッションで得ている
- [ ] working tree は clean か、または意図した未コミット差分のみ
- [ ] 触っているブランチは protected branch ではない（または承認済み）
- [ ] 出力（commit msg / PR body / Slack draft / log）にシークレット regex が含まれない
- [ ] 本番 AWS / 本番 DB / 本番 Slack に書き込まない（dev EC2/Fargate 含む — §1.4 Q7 対応）
- [ ] egress 先が allowlist 内
- [ ] 編集対象が allowed_files 内（§1.7 リストの編集禁止ファイルに触れていない）
- [ ] 当日 Bedrock コストが $10 未満、月次 $200 未満
- [ ] 失敗時に巻き戻し手順がある（rollback plan）
- [ ] 進捗を memory / plans に追記する準備がある

---

## 7. 変更履歴

| 日付 | バージョン | 内容 |
|---|---|---|
| 2026-06-16 | v1.0 | 初版（4 案統合直前）。HARD BLOCKS 8 軸 + SOFT GUARDRAILS 8 軸 + ALLOWED + プロトコル + チェックリスト。|
| 2026-06-16 | v1.1 | red-team 30 件統合。Q3/Q5/Q7/Q8/Q9/Q14/Q16/Q18/Q19/Q23/Q27/Q29 反映。§1.4 に dev も本番扱い・§1.7 に Maker 編集禁止リスト・§1.9 にコスト hard cap・§2.3 に Wave4 全件 stopped-by-human・チェックリストに SHA256 / allowed_files / コスト確認を追加。|
