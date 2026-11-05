あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
背景: 全 DM 判定で「できる機能を断る・完了の約束を守らない」が 36 件。案内（infra/openclaw/SOUL.md の振り分け・できること一覧、ツールの description）と、本番で実際に ON のツール（環境変数）が食い違うと、社員に「その機能はありません」と言ったり、無い機能を約束したりする。
本番の env（読み取り専用の写し）: ~/Documents/Claude/Artifacts/tfvars-rebuild-20261030/live_config.json の teamagent-dev-mcp と teamagent-dev-openclaw の env（ALLOWED_EMAILS は空＝誰も、*＝全員の email_allowed 方式と、空＝全員の rollout_allowed 方式が混在する点に注意: src/teamagent/_shared または skills/_shared/rollout.py を確認）。
やること:
1. SOUL.md と各ツールの description に書かれた「できること」を列挙し、本番の env で各ツールが (a) 全員に ON (b) 一部の人だけ (c) OFF のどれかを、factory.py の登録条件と allowlist の方式から判定して表にする。
2. 食い違い（SOUL は案内しているのに OFF／一部だけ、または ON なのに SOUL が「できない」と言う・振り分けが無い）を重要度順に列挙。
3. 直せるものは直す: SOUL の文を本番の実態に合わせる（OFF の機能は「準備中」と 1 文で・一部だけの機能は対象外の人への返し方を決める）。さらに、SOUL の機能案内と factory の登録条件の食い違いを CI で検出するテスト（env の想定値を固定したフィクスチャで）を 1 本足す。
4. SOUL の字数上限など既存の契約テストを通す。`uv run --extra dev --extra mcp pytest -q -n auto tests/orchestrator tests/infra tests/openclaw` 相当と ruff。
禁止: git commit・push、AWS の実行、.env・secrets の読み取り。最後に食い違いの表・直した点・残した点を短くまとめる。
