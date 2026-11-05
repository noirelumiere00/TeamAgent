あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
検収調査の報告書 /private/tmp/deck86_acceptance_report.md（読み取り専用）の「P1-4」「P1-3」「P1-5」を直す。対象は統合提案書（proposal_builder → proposal_deck → media worker の描画）の ready 判定。
前提の訂正: 報告書の P0-1（86 枚から BU・業種でページを除く）は仕様どおり。BU2 美容 72・BU2 非美容 68・BU1 美容 83・BU1 非美容 79 枚が正しい完成枚数。ページ除外は変えない。
- P1-4: 最終 PPTX の同じバイト列に inspect_pptx（L1）を、期待枚数＝上の仕様の枚数（BU と業種から決まる値。生成後の自分の枚数ではない）で実行し、error があれば ready にせず draft（要対応）にする。検査できなかったときもそう明示する。worker 側は metadata で返している結果を Builder まで届ける。既存の media/sales25_runner.py の L1 の使い方を参考にする。
- P1-3: 95 枠以外の補助欄（見積・試算・ヒアリング等）で、欠損を充足扱いにしない。数字を作らない。条件未指定は「条件未指定・要相談」など見える表示にし、意図して使わない欄（4 件入力時の 5/6 番枠など）とは区別する。
- P1-5: 必須画像の貼り先と期待数を欄の仕様として持ち、挿入できた実数と比べて足りなければ要対応にする（取得失敗を成功や空欄に吸収しない）。自動調査経路で画像が空になる件は、受け渡しを足すのが大きければ、要対応として見える化までに留め、報告に書く。
テストは先に失敗するものを書く。`uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/skills/proposal_deck tests/skills/proposal_builder tests/media` と ruff・mypy（変更ファイル）を通す。media イメージに入るファイルを変えたら `LC_ALL=C PYTHONPATH=src uv run --no-sync python infra/deploy/verify_generation_inputs.py --manifest infra/deploy/buildspec_generation_inputs.json --repo-root .` の結果を報告する（再導出は Claude がやる）。
禁止: git commit・push、AWS・Gemini・Bedrock の実呼び出し、.env・secrets の読み取り。顧客名を報告に書かない。差分は小さく。
最後に変更点（ファイル:行）・テスト結果・残る懸念を短くまとめる。
