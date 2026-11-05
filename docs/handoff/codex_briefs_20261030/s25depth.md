あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。

対象: 営業向け 25 枚資料（sales25）の生成ジョブ（src/teamagent/skills/omiyage_report/sales25.py ほか）。本番で 2 つの問題が出た。

問題 1: 検索 6 語すべてで「50 本目標 → 12〜14 本」しか取れない。検索は自社回線のブラウザが 1 ページ目以降を拒否されるので Apify（actor clockworks~tiktok-scraper、src/teamagent/adapters/apify_client.py）へ回している。ログでは 1 回目 13 件、やり直し（:retry）は 0 件で abort、費用 $0.2。毎回ほぼ 12 件なので、actor への入力（検索の件数指定・ページ送り・resultsPerPage / maxItems / searchSection などの名前）が 50 件を取る形になっていない疑い。
- コードと既存テスト・フィクスチャ（Apify の入力例・README の写しがあれば）から、渡している入力を確かめ、50 件取るための正しい入力を突き止める。actor の入力仕様はリポジトリ内の記録だけで判断し、分からない点は推測で決めず報告に「要確認」と書く。
- 直す場合は、件数指定の修正と、その入力が組み立てられることのテストを足す。やり直しが 0 件で abort になる理由（待ち時間・上限・同じ入力の繰り返し）も調べて報告する。

問題 2: ジョブが incomplete（作れなかった）で終わったのに、利用者の DM に失敗の知らせが届いていない（result_json は slack_delivered=false, delivery_target="none", delivery_status="not_ready"）。incomplete で終わったときにも、依頼した本人へ「作れなかった・理由・次にどうするか」の 1 通が届くようにする。既存の完了通知（長いジョブの通知の仕組み）の流れを使い、新しい仕組みは作らない。完了時の通知が届く経路と、incomplete 時に届かない分岐をファイル:行で示してから直す。

テストは先に失敗するものを書く。`uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/skills/omiyage_report tests/skills/first_visit tests/adapters` と ruff・mypy（変更ファイル）を通す。media イメージに入るファイルを変えたら `LC_ALL=C PYTHONPATH=src uv run --no-sync python infra/deploy/verify_generation_inputs.py --manifest infra/deploy/buildspec_generation_inputs.json --repo-root .` の結果を報告する。
禁止: git commit・push、AWS・Apify・Gemini の実呼び出し、.env・secrets の読み取り。顧客名を報告に書かない。差分は小さく。
最後に変更点（ファイル:行）・テスト結果・要確認の点を短くまとめる。
