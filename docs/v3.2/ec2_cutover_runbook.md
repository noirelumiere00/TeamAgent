# EC2 worker への Bot 切替ランブック（v3.2）

Mac で手動稼働している TeamAgent Bot を、常駐 EC2 worker（`i-0feaa3c103ab6ef91`）へ移す手順。
狙いは **24時間稼働** と **会社プロキシ外での SSL 根治**（TikTok CDN DL が証明書エラーゼロ → VSEO が backfill 不要で素の 10/10）。

> 実証済み: EC2 上 yt-dlp で TikTok CDN 4.51MiB DL 成功・`CERTIFICATE_VERIFY_FAILED` ゼロ（2026-06-03）。

---

## ⚠️ 最重要: 二重起動を避ける
Bot は Slack Socket Mode。**Mac と EC2 が同時接続するとイベントが不定に振り分けられる**（16名のライブ環境）。
必ず **Mac 側 Bot を停止してから** EC2 を起動すること。切替は「Mac停止 → EC2起動」の順で、同時に動かさない。

---

## 0. 一回だけ: Vertex SA を Secrets Manager に投入
Gemini 動画分析は Vertex SA JSON が要る。Mac はローカルファイル（`secrets/vertex-sa.json`）だが、EC2 は
Secrets Manager から `load_secrets.sh` が materialize する設計。SA JSON を**一度だけ**登録する（**チャットに値を貼らない**）:

```bash
aws secretsmanager create-secret --region ap-northeast-1 \
  --name teamagent/dev/vertex_sa \
  --secret-string file:///Users/s-komata/Documents/TeamAgent/secrets/vertex-sa.json
# 既にあれば: aws secretsmanager put-secret-value --secret-id teamagent/dev/vertex_sa --secret-string file://...
```

worker の IAM ロールは `teamagent/dev/*` 読取を許可済み。`infra/deploy/ec2.overrides.env` の
`VERTEX_SA_SECRET_NAME=teamagent/dev/vertex_sa` を `load_secrets.sh` が見てファイル化する（umask 077）。

---

## 1. Mac 側 Bot を停止
Mac でフォアグラウンド起動なら Ctrl-C。バックグラウンドなら:
```bash
pkill -f "teamagent.runtime.slack_bot" || true
# 念のため確認
pgrep -fl "teamagent.runtime.slack_bot" || echo "Mac bot 停止済"
```

## 2. EC2 worker を起動
```bash
aws ec2 start-instances --region ap-northeast-1 --instance-ids i-0feaa3c103ab6ef91
# SSM Online になるまで待つ（30-60s）
aws ssm describe-instance-information --region ap-northeast-1 \
  --filters "Key=InstanceIds,Values=i-0feaa3c103ab6ef91" \
  --query "InstanceInformationList[].PingStatus" --output text   # → Online
```

## 3. デプロイ（コード + env.base → S3 → SSM 展開・起動）
ローカル（Mac, `.env.production` がある場所）で:
```bash
# まず DRY-RUN: tarball/env.base を生成し秘密混入が無いか検証（S3/SSM は触らない）
scripts/deploy_to_ec2.sh

# 問題なければ実デプロイ
scripts/deploy_to_ec2.sh --go
```
`--go` は内部で: S3 へ tarball + `teamagent.env.base` を置き、SSM で
**venv作成 → `pip install -e .` → スクレイパ `npm ci` → Chrome 導入 → `CHROMIUM_PATH` 解決 → `systemctl enable/restart teamagent-bot`** を実行し、`systemctl status` を返す。

### ⚠️ 初回デプロイで時間/容量がかかる点（想定内）
- `pip install -e .` は **torch + sentence-transformers(e5)** を含み arm64 で数分・数百MB。30GB gp3 で足りる想定。
- 検索 Skill が使う **e5 モデル**は初回 embedding 時にDL（VSEO 単体では不要だが Bot は全 Skill ロード）。最初の検索系応答が遅いことがある。
- スクレイパ `npm ci` で Puppeteer 取得 + `@puppeteer/browsers` で Chrome 本体導入。`CHROMIUM_PATH` は deploy が `find` して env.base に追記。

## 4. 検証チェックリスト
```bash
# (a) systemd 稼働
aws ssm start-session --target i-0feaa3c103ab6ef91 --region ap-northeast-1
#   → sudo systemctl status teamagent-bot   (active running)
#   → sudo journalctl -u teamagent-bot -n 50 --no-pager
#     期待ログ: slack_bot_start mode=socket / load_secrets OK / Vertex SA materialized
```
- [ ] systemd active (running)、journal に `slack_bot_start mode=socket`
- [ ] `load_secrets` が DB/Slack/Google/Vertex を OK（`MODE: direct` で RDS 直結）
- [ ] Slack で `@TeamAgent <何か>` に応答（Socket 接続OK）
- [ ] **VSEO: `@TeamAgent VSEO分析 新宿 ランチ`** → S3 URL 返却。ログで `failed=0` 近く（SSL 根治で backfill 不要）
- [ ] 既存 Skill（検索/カルテ等）が応答（DB 直結 + Bedrock OK）

## 5. ロールバック（問題時）
```bash
# EC2 の bot を止めて
aws ssm send-command --region ap-northeast-1 --instance-ids i-0feaa3c103ab6ef91 \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["systemctl stop teamagent-bot; systemctl disable teamagent-bot"]'
# EC2 自体を停止（課金最小化）
aws ec2 stop-instances --region ap-northeast-1 --instance-ids i-0feaa3c103ab6ef91
# Mac 側 Bot を再起動（従来運用に戻す）
#   cd ~/Documents/TeamAgent && set -a; source .env.production; set +a; source scripts/load_secrets.sh; \
#     ./.venv/bin/python -m teamagent.runtime.slack_bot
```

## コスト
- 稼働中: t4g.medium ≈ $29/mo + EBS。停止中: EBS 30GB ≈ $2.4/mo のみ。
- 完全ゼロ化: `terraform destroy -target=aws_instance.worker`（再構築は worker.tf で targeted apply・5分）。

## 関連
- IaC: `infra/terraform/worker.tf`（PR #107）/ EC2上書き: `infra/deploy/ec2.overrides.env`
- デプロイ: `scripts/deploy_to_ec2.sh` / 秘密展開: `scripts/load_secrets.sh`
- AWS リソース詳細: メモリ `reference_teamagent_aws_resources.md`
