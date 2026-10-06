# ファイル

- [ルーティング検証と評価](routing-and-eval.md) - OpenClaw の外側ルーターが name+description だけでツールを選ぶ前提での棲み分け検証（tests/routing のコーパスと手動シミュ、description・台帳を固定する pytest）、オーケストレーション評価（eval.py の決定的採点と課金ありの eval_orchestration.py）、検索精度評価（run_eval.py の gold set と compare_retrieval.py）の使い方と限界。
- [テストの走らせ方と CI](running-tests.md) - GitHub Actions の CI（lint-and-test・uv.lock 固定版 pytest・activation freeze・gitleaks・trivy・terraform validate）の中身と、ローカルで CI と同じ extras（dev/mcp/media）・Node 依存・使い捨て PostgreSQL（TEAMAGENT_TEST_DB_DSN と teamagent_app ロール）でテストを走らせる方法、tests/ の配置。
