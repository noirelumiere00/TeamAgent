# 有馬さん向け はじめの 1 枚（2026-11-10）

Aico（社内の Slack AI アシスタント）の開発・便（本番リリース）に入ってもらうための最短ルート。細かい話はリンク先へ。

## 1. いまの Aico

| 項目 | 状態（11/10） |
|---|---|
| 目標 | 全 DM の「良い」率を 12 月に 50%（今 25% 前後）。毎週月曜に全 DM を判定している |
| 本番 | mcp は r112（commit 5d93878a）、OpenClaw は TD 98 |
| 進行中の柱 | ① 便の前の答え試験（`python -m teamagent.eval.answer_gate`）② 入口の振り分けをコードで（#875/#876・今は持ち主だけ shadow）③ 答えの組み立てをコードで（まず事例一覧）④ 👎 の毎朝の仕分け |
| 今週の PR | #884 Sonnet 禁止・#885 画像だけの資料を Gemini で Markdown に |

構成の全体は `README.md`、AWS の資源と運用は `docs/handoff/aws_resources_and_ops_20261105.md`。

## 2. 守ること（ここだけは必ず）

- **Sonnet は使わない**（全世代。コスト方針 2026-11-10）。Bedrock は Haiku 4.5、画像・動画は Gemini。
- **terraform apply はしない**（正本が無い。tfvars 無しの apply で本番が全断した前歴あり）。
- **OpenClaw 便・buildspec 世代の公開・IAM の変更**は持ち主（小俣さん）だけ。
- **便は同時に 1 本**。撃つ前に kit の同時実行チェックが通ること（下の 5）。迷ったら小俣さんに一声。
- **社員の DM・本文・メールを外へ出さない**（Codex などの外部 AI に渡すときは一般化した例だけ）。ログにも本文を出さない。
- `.env`・`secrets/`・鍵ファイルは読まない・コミットしない。秘密は Secrets Manager か手元の `~/.claude/secrets/*.env`。
- Slack への送信は自分の DM でだけ試す。チャンネルや他人への送信はしない。

## 3. 手元の準備

```bash
git clone git@github.com:noirelumiere00/TeamAgent.git && cd TeamAgent
uv sync --extra dev --extra mcp --extra media
```

テストは CI と同じ依存で、変えた所だけ回す（全件は push して CI に任せる）:

```bash
uv run --extra dev --extra mcp --extra media pytest -q tests/<変えた所> -p no:cacheprovider
uv run --extra dev --extra mcp ruff check src tests scripts
uv run --extra dev --extra mcp ruff format --check src tests scripts
uv run --extra dev --extra mcp mypy src/teamagent
```

- dev を取り込んだ後は `ruff format` の漏れで CI が落ちやすい（11/10 に 2 回）。push 前に format を必ず。
- 収集エラーが出たら、まず extra の不足（`--extra media` など）を疑う。

## 4. AWS（CLI）

- ユーザー `engineer-arima`。権限はすべて **MFA 付きのセッションでだけ**効く。
- MFA は 6 桁の仮想デバイス `arn:aws:iam::718959508629:mfa/iCloudOTP`（パスキーは CLI では使えない）。

```bash
aws configure --profile engineer-arima   # 本人が発行したアクセスキー
export MFA_BASE_PROFILE=engineer-arima MFA_SERIAL=arn:aws:iam::718959508629:mfa/iCloudOTP
export RELEASE_PROFILE=arima-mfa AWS_PROFILE=arima-mfa
bash scripts/aws/release/mfa_refresh.sh  # 6 桁を入れる。36 時間のセッション
aws sts get-caller-identity
```

変数の意味は `scripts/aws/release/kit/README.md` の冒頭。

できること: 読み取り全般・タスク定義の登録・mcp/connect-web の差し替え・便の発射と承認（ロール経由）。
答え試験（`kit/run_answer_gate.sh`）も回せる（11/10 に試験専用のタスク定義 `teamagent-dev-answer-gate` だけ `ecs:RunTask` を付与）。

## 5. 便（mcp）の出し方

手順と道具は `scripts/aws/release/kit/README.md`。流れだけ:

1. PR を dev にマージ（CI 緑）
2. `mfa_refresh.sh` でセッションを作る
3. `kit/deploy_mcp_gate.sh <PR番号> -- rNNN "<説明>" <確認するファイル> <確認する文字列>`
   - 最初に同時実行チェック（ビルド中・差し替え中なら止まる）
   - 発射 → 答え試験（本番より悪くなった問題があれば止まる）→ 差し替え
4. 自分の DM で実機確認（変えた機能を普段の言い方で頼む）

便の番号は持ち主と共有。撃つ前に `releases/` の最新番号 +1 を使い、Slack で一声かける。

## 6. 最初の仕事の候補（どれから始めるかは小俣さんと決める）

| 候補 | 大きさ | 入口 |
|---|---|---|
| 事例一覧の見出しに残る日付・「御中」・会社名の重複を落とす | 小 | `src/teamagent/skills/search/case_list.py` の `_short_title` |
| 要点が 1 行しか出ない事例を 2 行に揃える | 小 | 同 `case_list.py`・`skills/search/case_points/system.md` |
| 答え試験のメール・予算の不合格（本物の LLM で落ちる）を調べる | 中 | `src/teamagent/eval/answer_gate/` |
| 入口の振り分けの shadow ログを集計し、全員 enforce の判断材料を作る | 中 | `infra/openclaw/caller-identity-plugin/dist/index.js` の `router route_version=1` ログ |

## 7. 困ったら

- 本番の様子: CloudWatch Logs `/teamagent/dev/teamagent-mcp`・`/teamagent/dev/openclaw`
- 便が止まった: `kit/README.md` の「止まったとき」
- それでも分からない: 小俣さんに Slack で（スクショとコマンドの出力を添えて）
