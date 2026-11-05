あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。

本番の事故: 営業向け 25 枚資料（sales25）の生成で、段 "media"（src/teamagent/skills/omiyage_report/sales25.py の STAGES）が次の例外で incomplete になり、資料が出ない。
  sales25_stage_incomplete stage=media error_type=ValueError error_message="JSON object required"
"JSON object required" は src/teamagent/skills/first_visit/sales25_analysis.py の `_json` だけが出す。前の PR #739 で、画像確認の答え（analyze_image_bytes / analyze_images_bytes）には `_model_json`（```json の囲みや 1 要素の配列を許す）と `_ask_again`（答えは request_id でキャッシュされるので、壊れた答えは "-again" を付けた別 id で 1 回だけ聞き直す）を入れた。

やること:
1. 段 "media" の実行経路をたどり、`_json` / `_response` を通る箇所を全部列挙する。モデルの答え（Gemini の response.text）を読む箇所と、自分たちが作った JSON（media-manifest.json 等の正規の成果物）を読む箇所を分ける。
2. モデルの答えを読む箇所（例: 表紙の照合 `_response(cover_response, ...)`、音声 `_response(response, {...segments})`、要素 `_response(response, {"elements"})` など、media 段で通るもの）すべてに、#739 と同じ「`_model_json` で読む＋形が壊れていたら `_ask_again` で別 id で 1 回だけ聞き直す」を当てる。正規の成果物（manifest）は今の厳密な `_json` のまま変えない。
3. それでも 1 本の投稿で答えが壊れ続けた場合に、段全体を incomplete にせず、その投稿だけを不足（既存の partial / media_missing の仕組みがあればそれ）にして先へ進めるか確認し、仕組みがあるなら使う。新しい仕組みは作らない。
4. テスト: 各箇所について「1 回目は配列や ``` 付き・2 回目で正しい」「2 回とも壊れる」のフェイクで、聞き直しの request_id が変わること・最終的な扱いを確かめる。既存の tests/skills/first_visit/test_sales25_analysis.py の流儀に合わせる。
5. `uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/skills/first_visit tests/skills/omiyage_report` と ruff・mypy（変更ファイル）を通す。lambda を _ask_again に渡すと mypy が型を推論できないので、型付きの内側関数にする。
6. 生成入力（media イメージに入るファイル）に当たる変更なら、`LC_ALL=C PYTHONPATH=src uv run --no-sync python infra/deploy/verify_generation_inputs.py --manifest infra/deploy/buildspec_generation_inputs.json --repo-root .` の結果を報告に書く（再導出は Claude がやる）。

禁止: git commit・push、AWS・Gemini の実呼び出し、.env・secrets の読み取り。差分は小さく保つ。
最後に、直した箇所の一覧（ファイル:行）・テスト結果・残った懸念を短くまとめる。
