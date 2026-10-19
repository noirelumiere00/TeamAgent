# CLAUDE.md 簡素化（2026-10-19）— 落とした行と理由

基準は公式 memory ガイド「1ファイル 200 行未満・事実だけ書く・手順は skill や wiki へ・必ず守らせることは hook/permissions へ」。
判定は行ごとに「この1行が無いと Claude は間違えるか？」。原本は [`CLAUDE_md_before_simplify_2026-10-19.md`](CLAUDE_md_before_simplify_2026-10-19.md)。

| | 前 | 後 |
|---|---|---|
| 行数 | 210 | 120 |
| バイト | 23,063 | 12,284 |

残したもの: §2 変えない判断 1–5 / §3 Do 1–8・Don't / §4 B1–B10 / §5 C1–C5・既知 caveat / §6 D1–D5 / 4段ゲートの要点（§7）/ ローカル開発の最短コマンド（§8）/ 現役 runbook のリンク（§9）。
ラベル（B/C/D/E 番号・§1〜§4 の節番号）は openwiki が `CLAUDE.md §4 B8` のように引用しているので変えていない。

## 移した行

| 原本の行 | 内容 | 移動先 |
|---|---|---|
| 175–197（§10 E1〜E4 全文） | ツール追加の4段ゲート・description 棲み分け・チェックリスト・ブランチ衛生 | [`.claude/skills/aico-add-tool/SKILL.md`](../../.claude/skills/aico-add-tool/SKILL.md)。CLAUDE.md §7 には要点 3 行とリンクだけ残した |
| 197（E4 の「origin/dev 基点・何が本番か」） | ブランチ衛生の規則部分 | CLAUDE.md §1 に 1 行で統合 |
| 159–171（§9 困ったとき早見表） | 節番号への案内と wiki 入口 | 各節へのリンクは wiki quickstart の「やりたいこと別の入口」と重複。現役リンクだけ §9 リンク集に統合 |

## 落とした行

| 原本の行 | 落とした内容 | 理由 |
|---|---|---|
| 3–4 | 「このファイルは起動時に読み込まれます」「…を集約した運用マニュアルです」 | 自己紹介。無くても判断は変わらない |
| 7–9 | 2026-05 構築日記の退避案内（引用ブロック） | 経緯。リンクは §9「構築当時の経緯」に 1 行で残した |
| 11・201–202 | 「最終更新：2026-06-25」「最終更新：2026-09-30（…）」「前回：2026-06-25（…）」 | 更新履歴は git log が持つ |
| 19 | 構成（Slack → OpenClaw → MCP gateway → …）の説明 | 仕組みは wiki `architecture/overview.md`。リンクのみ残した |
| 25 の一部 | 「気づいたら別ブランチ/別ディレクトリ事故が頻発するので」 | 経緯。規則（現在地確認）は残した |
| 26 の一部 | 「`adapters/bedrock_client.py`。暗黙の Sonnet 課金を防ぐため」のファイル名 | 既定値の事実は残し、場所は wiki `integrations/bedrock-gemini-and-retry.md` |
| 27 | 「3層分離は §3 のルールに従う」 | §3 と重複 |
| 33 の一部 | 「理由：2026/4 のサブスク制限事件以降、政策変動を Bedrock で遮断。実装は region-aware」 | 経緯。規則（Bedrock 経由・直叩き禁止）は残した |
| 36・38 | 「⚠️ 現状（2026-09-30 確認）: sdk_runner は temperature / caching を使っていない」 | 現状のコードとの差は wiki `architecture/orchestrator.md` 93 行目付近が記録している。CLAUDE.md には「差は wiki に記録・新規はこの方針に合わせる」の 1 行だけ残した |
| 45 の一部 | 「新しい Skill / タスク / バッチを書くときは以下を必ず守る」 | 見出しで十分 |
| 48 の一部 | 「`pyproject.toml [tool.importlinter]` で CI fail」 | D3 と重複 |
| 49 | 「⚠️ CI が強制しているのは 2 本だけ…`skills/knowledge_deliver` が `orchestrator.factory` を import している」 | 現状の説明は wiki `architecture/layering-and-skill-contract.md`「層と依存方向」にある。規則（一方向）は残した |
| 52 の一部 | 「（CloudWatch Insights が JSON 前提＝§7）」 | 理由説明。JSON ログの規則は残した |
| 59 の一部 | 「— PII/機密漏洩」 | 理由説明。禁止事項は残した |
| 65–66 | コードレビュー定型チェック（7 項目） | Do 1–8 と D3 の言い換え。重複 |
| 72 | 「デプロイは CodeBuild → ECR → tfvars 反映 → terraform apply → run-task 検証が基本フロー。turnkey 手順は §7 の bundled_deploy_* / deploy_log.md」 | 経路の説明は wiki `operations/release-gates-and-deploy.md`。リンクに置き換え |
| 74 の一部 | 「（過去の CLI `-var` 渡しの名残）」 | 経緯 |
| 75 の一部 | 「root が build した `/app/.venv` を非 root が `uv run` で再生成 →」の長い説明 | 原因は 1 句に圧縮。規則（`uv run` 禁止）は残した |
| 77 の一部 | 「『apply したら env が勝手に sync』は誤り」 | 規則本文と同じ意味 |
| 78 の一部 | 「（`variables_fargate.tf` の `slack_bot/app_token_secret_name`）」「（2026-06-25 に誤 `.env.production` でこの事故が発生）」 | 変数名の場所は wiki `architecture/openclaw-gateway.md`。事故の日付は経緯 |
| 79 の一部 | 「（管理者限定 allowlist 扱い）。2026-06-22 の無音 DM drop 事故の真因」 | 経緯。規則と CI 検出は残した |
| 80 の一部 | 「per-user token refresh に」「errors=2」「（2026-06-25 回帰）」 | 経緯と細部。症状（全件ゼロ）は残した |
| 81 の一部 | 「（2026-07-10 裁定・#151）」「（tf 実値 5・コード上限 10）」「#154 の」 | 裁定の日付と PR 番号は git log。実値は tfvars/コードが真実源で CLAUDE.md に写すと古くなる |
| 94 の一部 | 「（table owner）で接続すると」の説明の一部 | 圧縮。規則は残した |
| 98 | C5「2026-07-10 検証済み・①は事実だった」「fail-open する」「`slack_team_check_disabled` の WARN」 | **訂正**。現行 `adapters/slack_client.py` 410–417 行は `SLACK_TEAM_ID` 未設定・形式不正で `None`（fail-closed・`missing_expected_team`）を返す。wiki `integrations/slack-identity-and-oauth.md` も同じ。原本の「skip（fail-open）」は古い記述で、地雷は逆（未設定だと全員拒否で bot が無応答）。C5 を書き直した |
| 99 | 「未検証メモ ② `connect_google_client_id` 変数が `variables*.tf` に未定義で terraform error の疑い」 | 解消済。`infra/terraform/connect_web.tf` 78 行目に `variable "connect_google_client_id"` が定義されている |
| 105 の一部 | 「（依存リーク）」 | 理由説明 |
| 107 の一部 | 「（§3）」 | 参照の重複 |
| 108 の一部 | 「（B6 の `dmPolicy:open ⇒ allowFrom:["*"]` 等）」 | B6 参照に圧縮 |
| 109 の一部 | 「（2026-07-10 追加）」「`--ignore-unfixed` 運用＝修正版が出た時点で赤くなる」「ID グローバル ignore は将来の新規検出まで素通りさせるため禁止・expired_at 超過で自動的に再検出＝赤」「image スキャンは CI 対象外（E5 2.2GB 焼き込みで PR 毎 build 不可）＝ECR scan_on_push＋別チケット」「（D1-D4 も同条件）」 | 日付・理由・背景。規則（バンプで直す／ignore は paths+statement+expired_at のみ／赤なら merge しない）は残した。image スキャンの扱いは wiki `operations/container-images-and-build.md` |
| 110 | D6「CI トリガーは `branches: [main, dev]`（2026-07-10 修正）。かつて `[main]` のみで dev PR は CI 未発火だった」 | 修正済の経緯。いまは dev PR も CI が走るので、守る規則ではない。「PR は dev 宛て」は §1 に残した |
| 114 | 「（いずれも実在・現役）」 | 注記。今回リンク存在を再確認済 |
| 120 | EC2 worker 退役の長い経緯（2026-08-03 停止・2026-09-28 撤去・`worker.tf` 削除・HMAC 経路・便δ…） | 経緯。「撤去済・runbook は歴史文書」の 1 行に圧縮 |
| 127–129 | 検索 Skill 設計 / Sprint 14 全タスク / オーケストレータ PoC 判断根拠 のリンク | 設計時の歴史文書。現役の規則・運用には使わない（必要なら `docs/` を直接探す） |
| 136–149 の一部 | コードブロック内の説明コメント（「3 コンテナ」「dev だけだと…収集で落ちる」「push 前に全部緑にする」） | 圧縮。コマンドと extras の規則は残した |
| 154 | 「ローカル DB：localhost:5432（teamagent/teamagent）/ Adminer / MinIO のポート」 | `infra/docker/docker-compose` が真実源 |
| 155 の一部 | 「（踏み台 EC2 → `localhost:15432`）」の説明の一部 | 圧縮。ポート・手順リンク・psql hang は残した |
| 164・170・171 | 早見表の「いま何が本番で動いてるか」「AWS リソース ID / モデル ID」「ツールを足したい」 | §1 と §7 に統合 |
| 177 の一部 | 「（実際 ③`video_approval` は skill 完成済みなのに OC 未露出で長く埋もれていた）」 | 実例は skill 側に残し、CLAUDE.md からは落とした |
| 190 の一部 | 「実績：32発話で正答率97%」 | 実績値は skill 側に残した |

## 訂正した行（落としたのではなく直した）

| 原本の行 | 前 | 後 | 根拠 |
|---|---|---|---|
| 53 | prompt の置き場 `src/prompts/<skill>/v1/system.md` | `src/teamagent/prompts/<skill>/<version>/<name>.md` | `src/prompts` は存在せず `src/teamagent/prompts/` が実体。wiki `architecture/layering-and-skill-contract.md` も同じ |
| 98 | C5: `SLACK_TEAM_ID` 未設定は fail-open（team 検証 skip） | 未設定・形式不正は fail-closed（全員拒否） | `src/teamagent/adapters/slack_client.py` 410–417 行 |

## 今回やらなかったこと（提案）

- 「必ず守らせることは hook/permissions へ」のうち、`terraform destroy` 禁止や `secrets/` 読取禁止はローカルの `.claude/settings.json`（未コミット）にだけある。repo に入れると全員の挙動が変わるので、この PR では触らず別 PR 候補とした。
- openwiki の各ページは `CLAUDE.md §4 B8` のように原本の節番号を引用している。番号は維持したが、次回の wiki 再生成（`openwiki-update.yml`）で参照が自然に更新される。
