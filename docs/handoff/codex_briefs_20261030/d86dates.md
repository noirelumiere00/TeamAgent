あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
検収調査の報告書 /private/tmp/deck86_acceptance_report.md（読み取り専用）の「P2-6」と「P1-2」を直す。対象は統合提案書（proposal_builder → proposal_deck → 描画）の、投稿開始日が NULL（未定）のときの扱い。
- P2-6: 最初の ComposerOutput の model_validate より前に posting_start_date を入力値で上書きし、モデルが「未定」や空文字を返しても失敗しないようにする。補助枠・profile など、モデルに委ねない値の初回検証順も点検する。
- P1-2: NULL のとき、投稿・工程・フェーズなど「計画の日付」に当たる欄の自由記述に残った絶対日付（例: 投稿開始 2050/12/31）を「未定」にする。実績・出典の発行日・提案日・利用者が指定した期間は消さない。全文の年月日を一括で消さない。固定雛形の計画日付はトークンに寄せられるなら寄せる。local と worker の両 renderer で同じ回帰テストを足す。
テストは先に失敗するものを書く。`uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/skills/proposal_deck tests/skills/proposal_builder tests/media` と ruff・mypy（変更ファイル）を通す。media イメージに入るファイルを変えたら `LC_ALL=C PYTHONPATH=src uv run --no-sync python infra/deploy/verify_generation_inputs.py --manifest infra/deploy/buildspec_generation_inputs.json --repo-root .` の結果を報告する（再導出は Claude がやる）。
禁止: git commit・push、AWS・Gemini・Bedrock の実呼び出し、.env・secrets の読み取り。顧客名を報告に書かない。差分は小さく。
最後に変更点（ファイル:行）・テスト結果・残る懸念を短くまとめる。
