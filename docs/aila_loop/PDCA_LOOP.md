# PDCA_LOOP.md — AiLa Loop Engineering（v1.0 / 統合決定版）

最終更新: 2026-06-16
対象: `/schedule` で cron 起動される `AiLa-PDCA-daily` routine と、それを駆動する Skill `~/.claude/skills/aila-pdca`
参照: VISION.md（北極星）／ARCHITECTURE.md（現状）／RULES.md（規範）
本ファイルの位置づけ: loop の **6 構成要素**（Cadence / Phases / Agents / Memory / Stop / Parallelism）と **9 防壁**（Q1-Q30 の red-team を反映した実装契約）を 1 枚にまとめる。Skill の SCRIPT.md はこの仕様の実装。

---

## 0. なぜ Loop Engineering か

`abstract-zooming-raccoon.md` Wave2-4 と `not_done_priorities_2026-06-15.md` 76 項目は、Sprint 14（2026-12-28）まで **残り 135 営業日で全部畳む必要がある**。1 日 1 項目 × 135 日 ≒ 完走可能だが、loop 化しないと「Shogo が思い出した時だけ動く」依存になり、4-5 日滑ると Sprint 14 が崩れる。

→ **自走可能な PDCA を 1 サイクル / 1 営業日で回す**。Loop Engineer（= Shogo）はサイクル設計と Stop 判定だけに専念する。

---

## 1. Cadence — 毎営業日朝 08:00 JST

**実装**: `/schedule` で `AiLa-PDCA-daily` を `cron: "0 23 * * 0-4"`（UTC = JST 月-金 08:00）で登録。

**理由**:
- 残 135 営業日 ÷ 1 日 1 項目 ≒ Wave2-4 76 項目を 8 月末までに完走できるペース。週次だと27 サイクルしか取れず1 サイクル複雑度が爆発。
- 営業16名が触る前（始業前）に Plan→Do→Check を回し切れば、当日夕方の本番 deploy 窓に間に合う（deploy は Shogo の人間ゲート）。
- ingest 週次自動化と同じ systemd timer 文化に合わせ、運用リズムを一本化。
- 本番 deploy は人間ゲートなので夜間 cron は不要、朝で十分。

**Q20 対応（暦盲点）**:
- Skill 起動冒頭で日本の祝日（`jpholiday` lib）と `next_action.skip_until` を確認し、祝日 / skip_until 範囲なら静かに skip。
- 連休直前（金曜）のサイクルは Plan 段で `risk_flags` に「multi-day-no-review」を強制セットし、Maker の変更を read-only / docs-only に限定する保守化分岐へ。

---

## 2. Phases — 4 段（Plan → Do → Check → Improve）

各段は前段の memory append を読んでから動く。タイムボックスと subagent kind は厳格。

| 時刻 (JST) | フェーズ | 主担当 (agent_kind) | 上限 | 主出力 |
|---|---|---|---|---|
| 08:00 | Plan | `Explore` subagent | 15 分 | `cycle_plan.yaml` |
| 08:15 | Do | `general-purpose` subagent | 90 分 | worktree commit + ローカル test ログ |
| 09:45 | Check | `red-team-reviewer` subagent | 30 分 | `review_report.yaml` |
| 10:15 | Improve | Loop Engineer（Synthesizer = メインセッション） | 10 分 | state.md append + PR draft |
| 10:25 | Stop 判定 | Loop Engineer | 5 分 | next_action 確定 |

合計 約 2.5 時間。残り時間は Shogo が「Stop に引っかかった項目への対処」と「本番 deploy 承認」に使う。

### 2.1 Plan（Explore subagent）
- **入力**（Skill が前処理して渡す。Q12 対応）:
  - `abstract-zooming-raccoon.md` の **Wave2-4 未完了行のみ**（grep で抽出、数十行）
  - `not_done_priorities_2026-06-15.md` の **次の 3 項目候補だけ**（上位 N）
  - `state.md` **末尾 1 ブロックのみ**
  - VISION.md §3-5 と ARCHITECTURE.md §3-5 の該当箇所
  - **`--max-input-tokens 50000` で context window を物理 cap**
- **出力** `cycle_plan.yaml`（YAML frontmatter で構造化）:
  ```yaml
  selected_item: "Wave2 #X — office本文抽出 (docx/pptx/xlsx ingest)"
  wave: Wave2
  why_now: "..."
  target_repo: "teamagent-orchestrator-poc"   # 単一 repo 必須
  target_files: ["ingest/office_extract.py", "tests/test_office_extract.py"]
  expected_diff_size: 200  # ≤500
  worktree_name: "feat/pdca-001-office-extract"
  worktree_base_tag: "pdca-base-001"  # dev HEAD~0 の snapshot tag を新規切る
  success_criteria:
    - "pytest tests/test_office_extract.py が緑"
    - "office_extract.extract_text(docx) が日本語本文 ≥1000 字を抽出"
  risk_flags:  # 固定 enum から複数選択必須（Q30 対応）
    - touches_db_schema      # migration を触る
    - touches_iam            # IAM policy / role を触る
    - touches_secrets        # secrets 経路を触る
    - touches_skills         # ~/.claude/skills/* を触る
    - touches_protected_files  # §1.7 allowed_files 外を触る
    - cross_repo             # 複数 repo にまたがる
    - new_dependency         # pyproject.toml / package.json に追加
    - bedrock_model_change   # model_id を触る
    - slack_egress           # Slack 出力経路を触る
    - multi_day_no_review    # 連休前で人間レビューが遅れる
    - none
  ```
- **gate**: (i) Wave2-4 のいずれかに属する、(ii) PR diff 推定 ≤500 行、(iii) 本番影響なしでローカル検証可能（terraform plain apply 不要・production EC2/Fargate に触らない）。3 項目連続で満たせない場合は Stop して人間判断。
- **Q12 対応**: Planner は **次サイクルへの示唆を残さない**（memory 汚染防止）。それは Improve の Synthesizer の責務。

### 2.2 Do（general-purpose subagent）
- **入力**: `cycle_plan.yaml` + 作業 worktree のパス。**本番 secret / IAM / DB には触れない**。
- **作業 worktree の作り方**（Q1 対応）:
  ```
  dev の HEAD で snapshot tag を新規作成: pdca-base-NN
  git worktree add ~/Documents/teamagent-poc-cycle-NN pdca-base-NN
  cd ~/Documents/teamagent-poc-cycle-NN
  git switch -c feat/pdca-NN-<slug>
  git config --local push.default nothing       # Q1: 誤 push 物理遮断
  git config --local advice.detachedHead false
  ```
  これで Maker は `git push` を打っても何も起きない。Improve だけが明示形 push を発行する。
- **出力**:
  - (i) コード変更（allowed_files の範囲内）
  - (ii) `uv run pytest`（`RUN_DB_TESTS=0` 強制）緑のログ
  - (iii) `ruff format --check` / `mypy --strict` 緑のログ
  - (iv) 自己評価メモ（なぜこの実装か / 残課題）— **Checker には渡さない**（Q13 バイアス排除）
  - **git commit はするが push しない**（push は Improve のみ）
- **gate**: ローカルで pytest / ruff format / mypy が全部緑。緑にならない場合は Do 内で修復試行（上限 90 分）。タイムアウト時は `status=stalled` で次フェーズへ渡し Improve で繰越し。
- **絶対禁止**（RULES.md §1.4 / §1.7 を Skill prompt に hard rule で埋込）:
  - `git push`, `gh pr create`, `gh pr merge` — 違反したら abort
  - `aws secretsmanager`, `aws ssm get-parameter`, `gcloud auth`, `cat ~/.aws/credentials`
  - `aws codebuild start-build`, `aws ecs update-service`, `aws ec2 send-command`, `scripts/deploy_to_ec2.sh`
  - allowed_files 外への Write/Edit
  - **Q5 対応**: `SENTRY_DSN=`（空）強制で Sentry traces 出力なし

### 2.3 Check（red-team-reviewer subagent — 別 agent 種・別ターン）
- **入力**: `git diff main...feat/pdca-NN-<slug>` + `cycle_plan.yaml` の `success_criteria` と `risk_flags` のみ。**Maker のセルフ評価メモは渡さない**（Q13 対応）。
- **追加入力**（Q13 対応）: `git log --oneline pdca-base-NN..HEAD` でコミット粒度確認。
- **必須事前マスク**（Q5 対応）: Checker への diff 入力前に secret regex を Skill が自動マスク（`xox*`, `AKIA*`, `ya29.*`, `eyJ.*`, BEGIN PRIVATE KEY）。
- **出力** `review_report.yaml`:
  ```yaml
  verdict: pass | fail | block
  findings: [...]
  security_concerns: [...]
  suggested_followups: [...]
  confidence: 0.0 - 1.0
  ```
- **gate**:
  - `verdict=pass` → Improve へ。
  - `verdict=fail` かつ 1 サイクル目: 同サイクル中に Do に戻して **1 度だけ** 修正試行（90 分上限 → 失敗なら fail 確定で memory へ繰越し）。
  - `verdict=block` → 即 Stop → 人間判断。
- **block 初期値の自動セット**（Q9 / Q13 / Q17 / Q19 / Q27 / Q29 対応）:
  以下のパターンが diff に含まれていたら verdict 初期値 = block。Checker は safe と論証できなければ block 維持。
  - SQL: `SET row_security`, `BYPASSRLS`, `SECURITY DEFINER`, `FORCE ROW LEVEL SECURITY`
  - Alembic migration / `0011_*.sql` / proposal_deck schema 変更
  - `.github/workflows/ci.yml` 変更
  - `RULES.md` / `VISION.md` / `ARCHITECTURE.md` / `PDCA_LOOP.md` / `deploy_runbook.md` / `slo_v1.md` 変更
  - `~/.claude/skills/aila-pdca/**` / `~/.claude/schedules.json` / `.github/CODEOWNERS` 変更（self-modification guard）
  - `pyproject.toml` の dependencies/optional-dependencies 増加で `ci.yml` の手動列挙に対応行が無い（Q17 対応）
  - `openclaw.config.json5` / `variables_fargate.tf:openclaw_model_id` / `BEDROCK_MODEL_ID` / `jp.anthropic.claude-*` 文字列（Q19 対応）
  - `clawhub.disabled = true → false` への変更（Q27 対応）
  - Slack 出力先 user_id / channel_id を allowlist 外に変更（Q27 対応）
- **risk_flags 連動の追加検査**（Q30 対応）:
  - `touches_db_schema` → migration の rollback SQL が同 commit にあるか確認、無ければ fail
  - `touches_iam` → IAM policy diff を読み、最小権限から外れた付与があれば fail
  - `touches_secrets` → 絶対 block 初期値
  - `new_dependency` → ci.yml 手動列挙の対応追記を確認、無ければ fail
- **Checker と Maker の agent_kind 検証**（Q2 対応）: Skill が起動レスポンスから `metadata.agent_kind` を抽出して memory に記録。Improve の冒頭で `assert plan_agent_kind != do_agent_kind != check_agent_kind` を機械検証。`red-team-reviewer` 不在の場合は **フォールバック禁止**＝ Check 不実行で `status=stalled` 終了。

### 2.4 Improve（Loop Engineer = Synthesizer）
- **入力**: Plan / Do / Check の 3 アウトプット + state.md 末尾。
- **出力**:
  - (i) `state.md` に新ブロック append（atomic write — Q6 対応）
  - (ii) MEMORY.md は **初回 1 行追加のみ**（Q21 対応。Maker / 通常サイクルでは触らない）
  - (iii) 次サイクルの種 = `next_action` を具体的に書き残す（abstract-zooming-raccoon の anchor 番号で 1 項目に限定／曖昧文禁止 — Q12 対応）
  - (iv) `verdict=pass` なら **明示形 push のみ** 発行（Q1 / Q15 対応）:
    ```
    git push origin HEAD:refs/heads/feat/pdca-NN-<slug>
    ```
    `--force*` を含む引数を pre-flight で grep して 1 件でもヒットしたら abort。
  - (v) PR draft 作成（Q15 対応）:
    - `gh pr create --draft --base dev --head feat/pdca-NN-<slug> --label do-not-auto-merge`
    - 本文は **Skill 固定テンプレ**（Maker のセルフ評価は input せず cycle_id と次の 3 行のみ）
    - base が `dev` 以外の指定は abort（main / release/* / feat/v3.1-monorepo への PR 禁止）
    - 既存の auto-merge ラベルが付くリポなら衝突検出して push せず stalled
  - (vi) 学びがあれば `feedback_*.md` 新設提案（新規作成は人間承認後）
- **gate**: memory append が成功した時点で当サイクル終了。append できなければ Stop（Q6）。翌朝の Planner が末尾を読めば自走再開可能であることが必須要件。
- **Q24 対応（Checker verdict を機械的に尊重）**: Synthesizer は status を改変できない。
  - `verdict=pass` → `status=pass` 固定、次 next_action は新項目
  - `verdict=fail` → `status=fail` 固定、次 next_action は **同項目を翌サイクルで再試行**（自由文での「次に進めよう」判断は不可）
  - `verdict=block` → `status=block` 固定、cron 自動 disable、Shogo 通知

---

## 3. Agent Roles — maker ≠ checker の三角分離

| 役割 | agent_kind | 責務 | 禁止事項 |
|---|---|---|---|
| Planner | `Explore` subagent | 次の 1 項目を選ぶ・読み取り専用 | 実装・状態変更・次サイクル示唆 |
| Maker | `general-purpose` subagent | worktree 内で実装・ローカル品質ゲート通過 | 自分の PR を Check・push・PR 作成・deploy・secrets read・allowed_files 外編集 |
| Checker | `red-team-reviewer` subagent | diff と success_criteria だけ見て pass/fail/block を判定 | Maker のセルフ評価を参照・フォールバックで `general-purpose` 起動 |
| Synthesizer | Loop Engineer（メインセッション） | 3 出力統合・memory append・PR draft 作成・次サイクル種付け | 実装・Check・status 改変・allowed_files 編集 |

**原則**:
1. Maker と Checker は **絶対に同一 agent 種にしない**（自己レビュー禁止）。
2. Synthesizer は実装も Check もしない（役割の三角分離）。
3. Planner は次サイクルへの示唆を残さない（memory 汚染防止）。
4. 4 役割すべての出力は Improve でまとめて 1 つの memory ブロックに集約（サイクル境界の単一真実）。

**Q2 対応の機械検証**:
- Skill が各 subagent 起動時に `agent_kind` を必須引数化し、起動レスポンスの `metadata.agent_kind` を memory に記録。
- Improve 開始時に `assert plan_agent_kind == "Explore"`, `do_agent_kind == "general-purpose"`, `check_agent_kind == "red-team-reviewer"`, かつ 3 つが互いに異なることを assert。
- どれか NG なら `status=block` で Stop（フォールバック禁止）。

---

## 4. Memory Schema — append-only state file

### 4.1 配置

- **メイン**: `~/.claude/projects/-Users-s-komata/memory/project_aila_pdca_state.md`
- **アーカイブ**: `~/.claude/projects/-Users-s-komata/memory/archive/project_aila_pdca_archive_YYYYMM.md`
- **インシデント**: `~/.claude/projects/-Users-s-komata/memory/incidents/<UTC>.md`
- **サイクル詳細ログ**（Q28 対応）: `~/.claude/projects/-Users-s-komata/memory/cycles/<cycle_id>/{plan,do,check,improve}.json`
  - 各 phase の input/output の SHA256 + truncated 先頭 1KB を保存
  - フル prompt/response は別ディレクトリに gzip で 30 日保持
- **MEMORY.md インデックス**: 初回 1 行のみ追加（Q21 対応）。以後は触らない。

### 4.2 1 ブロック形式（append-only）

```markdown
<!-- SCHEMA START — このブロック内の Schema 定義部分は Maker 編集禁止 -->
## cycle_id: 2026-06-17-001
```yaml
started: 2026-06-17T08:00:00+09:00
ended: 2026-06-17T10:25:00+09:00
wave: Wave2
item: "office本文抽出 (docx/pptx/xlsx ingest/office_extract.py 新設)"
worktree: ~/Documents/teamagent-poc-cycle-001
branch: feat/pdca-001-office-extract
base_tag: pdca-base-001
status: pass | fail | block | stalled | stopped-by-human
agents:
  planner:      { kind: Explore,            session_id: "..." }
  maker:        { kind: general-purpose,    session_id: "..." }
  checker:      { kind: red-team-reviewer,  session_id: "..." }
  synthesizer:  { kind: main }
confidence: 0.0 - 1.0   # Checker の confidence をそのまま転記
risk_flags: [...]
cost:
  bedrock_usd_today_pre:  0.00
  bedrock_usd_today_post: 0.00
  bedrock_usd_delta:      0.00
next_action:
  anchor: "abstract-zooming-raccoon.md:Wave2#5"   # 必須・1 項目限定
  summary: "..."                                  # 1-2 行
  skip_until: null  # 連休スキップ等の延期日時
human_gate:
  needs_production_deploy:  false
  needs_secret_rotation:    false
  blocked_on: null
artifacts:
  pr_url:      "https://github.com/vectorinc/.../pull/NNN"  # push 済みなら
  commit_hash: "abc1234"
  test_log:    "~/.claude/projects/-Users-s-komata/memory/cycles/2026-06-17-001/do.json"
```

### plan_summary
（Explore が選んだ理由 1-3 行）

### do_summary
（変更ファイル一覧 + テスト結果 + diff サイズ）

### check_summary
（verdict + 主要 findings 上位 3 つ + confidence）

### findings
- security: ...
- correctness: ...
- followup: ...
<!-- SCHEMA END -->
```

### 4.3 atomic write & lock（Q6 / Q10 対応）

- 書き込みは **tmp file + `pathlib.Path.replace`** で atomic。
- 書き込み前後に末尾 YAML frontmatter を `pyyaml.safe_load` で parse 成功確認。失敗なら Skill abort。
- 起動時に末尾 10 行が valid Markdown でなければ Skill abort + Shogo 通知（macOS `osascript`）。
- ファイル `fcntl.flock` 排他で同時書き込みを物理遮断。
- archive 移送は git commit でスナップショット化（state.md の git history がバックアップ）。

### 4.4 セッション lock（Q10 対応）

- Skill 起動冒頭で `~/.claude/projects/-Users-s-komata/memory/.pdca.lock` を `O_EXCL` で作成。
- 存在すれば即 abort（メッセージ「他セッションが PDCA を実行中」）。
- lockfile に PID + 起動時刻 + cycle_id を書き、stale（> 2.5h）なら自動削除。
- `status=block` / `stopped-by-human` のサイクル直後は `state.md` 末尾の `resume_after` を尊重し、再開可能日時前なら Skill abort。
- worktree ベース dir は cycle_id ごとに必ず新規作成。既存 worktree は再利用しない。

### 4.5 圧縮ルール

- 30 サイクル（≒ 6 週）経過したブロックは status=pass のみ要約して archive へ移送。state.md は **直近 30 サイクルだけ保持**して読み込みコストを抑える。

---

## 5. Stop Conditions — 8 つ（基本姿勢: 迷ったら止める）

1. **本番影響あり**: terraform plain apply / 本番 ECS taskdef 直更新 / 本番 RDS DML / 本番 IAM 変更 / dev EC2 への deploy（Q7 — dev も本番扱い）。
2. **RULES 違反**: HARD BLOCKS のいずれかを破る選択肢しか残らない。
3. **予算超過**（Q4 対応）:
   - 当日 Bedrock コスト $10 超（`aws ce get-cost-and-usage` で事前確認）
   - 月次 Bedrock コスト $200 超（Budget $250 の 80%）— PDCA loop 自動 pause
   - subagent 個別 wall-time タイマー超過 → 当該 subagent kill
   - 同一 `next_action` が 3 サイクル連続で再選定 → 無限ループ検知
4. **status 連続不良**（Q11 対応）:
   - `status ∈ {fail, block, stalled}` が **3 連続**
   - 同一 `next_action.anchor` が **3 サイクル連続**で選び直し
   - `confidence < 0.5` の `pass` が **5 連続**
   - 上記いずれかで次サイクル cron 自動 disable + Shogo 通知
5. **人間判断要**: AiLa OpenClaw 結線（connect.vectorinc.co.jp DNS / Plan C 連携 URL）など外部依存待ち、トークン rotation 本番投入、RLS 設計変更、新規 IAM policy 付与、Slack App スコープ追加、Bedrock モデル ID 変更、proposal_deck ブランチのマージ判断（cherry-pick `58ec55d` の方針）など、ビジネス影響が出る決定。
6. **Checker が `verdict=block`**（即 Stop — Q24 対応で自由判断不可）。
7. **Maker と Checker が同一 subagent 種で動いた痕跡が検出**（Q2 対応：`agent_kind` assert 失敗 → 当サイクル invalidate）。
8. **memory append 失敗 / lockfile 衝突 / SHA256 不一致**（Q6 / Q10 / Q16 対応）。

---

## 6. Wave 別の特別運用

| Wave | デフォルト挙動 |
|---|---|
| **Wave2** office 本文抽出 / 増分同期 / SLO 文書 / token rotation | 通常 PDCA。ingest 専用なので本番 MCP 無停止。|
| **Wave3** proposal_deck マージ + 本番配線 / PDF 変換 | `USE_PROPOSAL_DECK_TOOLS=1` env を **付けない限り無害**。env 投入は人間ゲート。Maker は env 値の変更を含む diff を出せない（allowed_files から除外）。|
| **Wave4** AiLa OpenClaw 結線（§Q-Q3）/ 朝配信 / 10名展開 | **Q27 対応: Plan 後に必ず `status=stopped-by-human` で停止**。Maker は draft のみ作成、deploy / DNS / Slack 配信先変更は全部 Shogo の人間ゲート。|

---

## 7. Parallelism — worktree isolation（default は単線）

- **default**: 1 サイクル / 日の単線（Plan も Do も Check も Improve も逐次）。
- 並列解禁は soft_guardrail（Shogo の明示承認、RULES.md §2.8）。解禁する場合の制約:
  - worktree base が cycle_id ごとに独立
  - worktree ごとに独立 venv（`uv venv .venv-cycle-NN`）を強制
  - git 操作は worktree ごとに `GIT_INDEX_FILE` 分離
  - Q14 対応: `pip` / `uv install` が共有 venv に走らないよう strict

---

## 8. /schedule 登録

```yaml
routine_name: AiLa-PDCA-daily
schedule:     "0 23 * * 0-4"        # UTC = JST 月-金 08:00
prompt:       (cron_routine.prompt を参照 — 本ファイル末尾相当)
```

Skill 側 `~/.claude/skills/aila-pdca/SCRIPT.md` がこの仕様の実装。Skill 起動冒頭で:
1. RULES.md / VISION.md / ARCHITECTURE.md / PDCA_LOOP.md / Skill 自身の **SHA256 を期待値と照合**（Q16 対応）
2. 祝日 / `next_action.skip_until` チェック（Q20 対応）
3. `aws ce get-cost-and-usage` で当日コスト確認（Q4 対応）
4. lockfile O_EXCL 取得（Q10 対応）
5. egress allowlist の設定（`HTTPS_ALLOW_HOSTS`, `AWS_EC2_METADATA_DISABLED=true`、Q8 対応）
6. `RUN_DB_TESTS=0` / `SENTRY_DSN=` / `PDCA_LOOP_MODE=1` の env を設定（Q5 / Q9 対応）
7. 4 phase を順に起動
8. 終了後に lockfile 解除

---

## 9. 防壁マトリクス — SHA256 / allowed_files / 自己改竄禁止

### 9.1 SHA256 期待値表（Q16 対応）

Skill 起動冒頭で以下のファイルを `sha256sum` し、期待値と一致しなければ Skill abort。期待値は **改訂のたびに Shogo が手動更新**（儀式化）。

```yaml
# /Users/s-komata/.claude/skills/aila-pdca/expected_hashes.yaml
files:
  - path: "/Users/s-komata/Documents/teamagent-orchestrator-poc/docs/aila_loop/RULES.md"
    sha256: "<RULES_MD_SHA256 — RULES.md 改訂時に更新>"
  - path: "/Users/s-komata/Documents/teamagent-orchestrator-poc/docs/aila_loop/VISION.md"
    sha256: "<VISION_MD_SHA256>"
  - path: "/Users/s-komata/Documents/teamagent-orchestrator-poc/docs/aila_loop/ARCHITECTURE.md"
    sha256: "<ARCHITECTURE_MD_SHA256>"
  - path: "/Users/s-komata/Documents/teamagent-orchestrator-poc/docs/aila_loop/PDCA_LOOP.md"
    sha256: "<PDCA_LOOP_MD_SHA256>"
  - path: "/Users/s-komata/.claude/skills/aila-pdca/SCRIPT.md"
    sha256: "<SKILL_SCRIPT_MD_SHA256>"
  - path: "/Users/s-komata/.claude/schedules.json"
    sha256: "<SCHEDULES_JSON_SHA256>"
```

### 9.2 allowed_files（Maker が編集できる範囲のホワイトリスト）

- `~/Documents/teamagent-orchestrator-poc/**`（ただし `docs/aila_loop/**`, `docs/openclaw/deploy_runbook.md`, `docs/v3.2/slo_v1.md`, `.github/CODEOWNERS`, `.github/workflows/ci.yml` を除く）
- `~/Documents/AI-IA-UAE/**`（cycle_plan.target_repo が `AI-IA-UAE` の場合のみ）
- `~/.claude/projects/-Users-s-komata/memory/cycles/**`（サイクル詳細ログのみ、Schema 定義部分は禁止）

**編集禁止**（RULES.md §1.7 と同期、Q3 / Q21 / Q29 対応）:
- `~/Documents/teamagent-orchestrator-poc/docs/aila_loop/**`
- `~/Documents/teamagent-orchestrator-poc/docs/openclaw/deploy_runbook.md`
- `~/Documents/teamagent-orchestrator-poc/docs/v3.2/slo_v1.md`
- `~/.claude/plans/abstract-zooming-raccoon.md`（Synthesizer のみ追記可）
- `~/.claude/plans/mossy-snacking-locket.md`
- `~/.claude/projects/-Users-s-komata/memory/MEMORY.md`（Synthesizer の初回 1 行のみ）
- `~/.claude/projects/-Users-s-komata/memory/project_aila_pdca_state.md` の Schema 定義部分
- `~/.claude/skills/aila-pdca/**`
- `~/.claude/schedules.json`
- `.github/CODEOWNERS`, `.github/workflows/ci.yml`
- `.git/hooks/**`, `.husky/**`, `.pre-commit-config.yaml`

### 9.3 self-modification guard（Q16 / Q29 対応）

- Skill 自身 (`~/.claude/skills/aila-pdca/**`) を PDCA loop から編集することは **完全に禁止**。
- VISION.md / ARCHITECTURE.md / RULES.md / PDCA_LOOP.md の改訂は別 routine（`/vision-review` — 人間が手動起動）でのみ可能。
- 改訂時は同時に §9.1 の SHA256 期待値を更新する儀式を Shogo が実施。

### 9.4 cross_repo 越境（Q18 対応）

- `cycle_plan.target_repo` は **単一 repo 必須**（`teamagent-orchestrator-poc` or `AI-IA-UAE`）。
- 複数 repo にまたがる作業は `risk_flags: cross_repo` を立て、Plan 後に Stop して人間判断。
- Maker subagent の cwd を worktree dir に固定し、`cd` で外に出るコマンドを Skill が監視して abort。

---

## 10. 初回起動の安全網（Q25 対応）

- Skill 初回起動時は **`bootstrap` cycle** を実行:
  - 4 phase が機能していることを **コード変更なし**で確認（read-only Plan / no-op Do / no-op Check / state.md に bootstrap ブロックを 1 つ append）
  - 各 phase の subagent kind が正しく取れるか検証
  - SHA256 / lockfile / コスト確認が動くか検証
- bootstrap 成功後、Shogo が `next_action.anchor` を明示的に投入（手動で 1 行追加）して本番サイクル開始。

---

## 11. 通知と可視化（Q23 対応）

- Slack / Gmail / SES 等の外部送信は禁止（RULES.md §1.6）。
- 通知経路は **macOS ローカルのみ**:
  - `osascript -e 'display notification "..." with title "AiLa PDCA"'`
  - `~/Desktop/aila_pdca_status.md` への symlink で常時可視化（state.md 末尾を表示）
  - 通知ラッパは Skill が実装、Bash tool の denylist には抵触しない
- 連休中の Stop は連休明け朝の最初の cron で `osascript` 通知 + `~/Desktop/aila_pdca_status.md` 確認を Shogo に促す。

---

## 12. なぜこれが go-live に効くか

- **残 135 営業日 ÷ 1 サイクル 1 項目 = Wave2-4 全 76 項目を 8 月末までに畳めるペース**（バッファ 2.5 ヶ月）
- バッファを Sprint 13（負荷試験 / DR 訓練）に充てる
- Shogo が Loop Engineer に徹することで、毎朝の「今日何やる？」の判断コストが消える
- maker ≠ checker でレビューの目が常に入る = 本番事故率を下げる（go-live 後の運用にも転用可）

---

## 13. 退役条件

- 2026-12-28 営業16名 go-live 完了 → このループは **週次 health-check ループに格下げ**（Improve 中心）
- それまでは毎営業日朝の routine を回し続ける

---

## 14. 変更履歴

| 日付 | バージョン | 内容 |
|---|---|---|
| 2026-06-16 | v1.0 | 初版。4 案統合 + red-team 30 件（Q1-Q30）反映。SHA256 改竄検知 / 並列単線 default / Wave4 stopped-by-human / cost hard cap / agent_kind 機械検証 / atomic write + flock / macOS osascript 通知 を全て実装契約として組込み。|
