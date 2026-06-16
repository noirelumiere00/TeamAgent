# Shogo が踏む 8 つのアクション（loop 起動準備）

## 1

1. Skill 配置: ~/.claude/skills/aila-pdca/ ディレクトリを新規作成し、SKILL.md（メタデータ）と SCRIPT.md（PDCA_LOOP.md §1-§11 を Skill 起動指示として実装）を配置する。SCRIPT.md は cron_routine.prompt と同じ 4 phase 仕様で書き、jpholiday・aws ce get-cost-and-usage・fcntl.flock・atomic write・SHA256 検証・agent_kind 機械検証・egress allowlist・osascript 通知の各 Python ヘルパを同梱する。

---

## 2

2. SHA256 期待値の初回投入: 以下 6 ファイルの SHA256 を取り、~/.claude/skills/aila-pdca/expected_hashes.yaml に記入する。`sha256sum ~/Documents/teamagent-orchestrator-poc/docs/aila_loop/{VISION,ARCHITECTURE,RULES,PDCA_LOOP}.md ~/.claude/skills/aila-pdca/SCRIPT.md ~/.claude/schedules.json`。改訂時は必ず同時更新（更新を忘れると翌朝 Skill が起動しない儀式化）。

---

## 3

3. MEMORY.md にインデックス 1 行を手動追加（loop 自身は §1.7 で書込禁止）: `- [AiLa PDCA Loop 自走状態](project_aila_pdca_state.md) — 毎営業日朝のサイクル状態。最新サイクル末尾を読めば別セッションが続行可能。Sprint 14 (2026-12-28) go-live まで毎営業日朝 08:00 JST に Plan→Do→Check→Improve を1 回。`

---

## 4

4. /schedule 登録: `/schedule` コマンドで AiLa-PDCA-daily routine を cron `0 23 * * 0-4`（UTC = JST 月-金 08:00）で作成し、prompt 欄に本 workflow の cron_routine.prompt を貼り付ける。登録後に SHA256 期待値表へ schedules.json の hash を追加する。

---

## 5

5. CODEOWNERS / pre-commit hook の整備（~/Documents/teamagent-orchestrator-poc 側）: `.github/CODEOWNERS` に RULES.md §1.7 の編集禁止ファイル群を human-only として登録。`.pre-commit-config.yaml` に secret regex 検査（xox*/AKIA*/ya29.*/eyJ.*/BEGIN PRIVATE KEY）と `*.env`/`service-account.json`/`vertex_sa.json` ステージング拒否を追加。`scripts/deploy_to_ec2.sh` 冒頭に `if [ "$PDCA_LOOP_MODE" = "1" ]; then echo blocked; exit 1; fi` の物理ガードを追記。

---

## 6

6. AWS Budgets の更新: `TeamAgent-Bedrock-Monthly` の閾値を現状 $50 → $250 に更新し、80%（$200）通知を有効化。loop の自動 pause トリガーと整合させる。

---

## 7

7. 初回 bootstrap キック: Skill 配置と上記 1-6 完了後、Shogo がインタラクティブセッションから `/loop aila-pdca bootstrap` を 1 回手動起動し、4 phase / agent_kind 検証 / SHA256 / lockfile / コスト確認が機能することをコード変更なしで確認。state.md 末尾の bootstrap ブロックの human_gate.blocked_on を null に更新し、next_action.anchor に最初の Wave2 項目（abstract-zooming-raccoon.md の番号で1つ）を Shogo が明示投入する。これが完了した翌営業日朝 08:00 JST から自動サイクル開始。

---

## 8

8. （任意・推奨）Wave4 関連の特別承認フロー: `~/Documents/AI-IA-UAE` 側の repo に対して PDCA loop が触り始めるのは Wave4 から。それまでに target_repo allowlist に `AI-IA-UAE` を追加し、Wave4 サイクルが Plan 後に必ず stopped-by-human で止まることを bootstrap サイクルで動作確認しておく。

---

## まとめ

VISION.md（北極星 v1.0）/ ARCHITECTURE.md（現状 v3.2.1）/ RULES.md（行動規範 v1.1）/ PDCA_LOOP.md（loop 仕様 v1.0）の 4 ファイルを `~/Documents/teamagent-orchestrator-poc/docs/aila_loop/` に配置し、state.md（append-only）を `~/.claude/projects/-Users-s-komata/memory/` に作成しました。cron routine `AiLa-PDCA-daily`（JST 月-金 08:00）を登録すれば、毎営業日朝に Explore→general-purpose→red-team-reviewer→Synthesizer の三角分離で 1 サイクルが自走し、Sprint 14（2026-12-28）の go-live まで Wave2-4 を 1 日 1 項目で削っていきます。red-team 30 件は RULES.md §1.4/§1.7/§1.9 と PDCA_LOOP.md §3/§4/§5/§9 に全て反映済み（SHA256 改竄検知・コスト hard cap $10/日 & $200/月・dev も本番扱い・Wave4 全件 stopped-by-human・agent_kind 機械検証・atomic write + flock・macOS osascript 通知・egress allowlist）。残りは Shogo の 8 アクション（Skill 配置 / SHA256 投入 / MEMORY.md 1 行 / schedule 登録 / CODEOWNERS+hook / Budget 更新 / bootstrap キック / Wave4 target_repo 設定）で起動可能。
