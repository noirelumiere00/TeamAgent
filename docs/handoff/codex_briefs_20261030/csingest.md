あなたは teamagent リポジトリの開発者。作業場所はこの worktree（origin/dev 起点）。日本語で報告する。
背景: 案件の状態を根拠付きで持たせる PR2（#752）は、取り込み（src/teamagent/ingest/pipeline.py の _differential_pipeline_config）で、CASE_STATE_EVIDENCE_ALLOWED_EMAILS が設定されると差分取り込みのキーに case_state_evidence の input_version を足す。このため、取り込み側でフラグを ON にした瞬間に、内容が変わっていない全文書で分類（LLM）・embedding・upsert がやり直しになり、費用がかかる。
やりたいこと: フラグを ON にしても、内容が変わっていない文書は分類・embedding をやり直さず、根拠束（case_state_evidence の metadata）だけを付け足す/更新する。
1. 差分取り込みのキーから case_state_evidence を外すか、別のキーに分け、内容不変の文書には metadata だけを更新する経路（既存の upsert で metadata のみ更新できるならそれ。無ければ最小の追加）を通す。
2. 根拠束の版（input_version）が変わったときは、metadata の更新だけが走ること。内容が変わった文書は従来どおり全処理。
3. テスト: フラグ OFF→ON で、内容不変の文書の分類・embedding が呼ばれない（フェイクの呼び出し回数 0）・metadata は更新される。内容が変わった文書は従来どおり。`uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/ingest tests/adapters` と ruff・mypy。
4. フラグを ON にしたときに走る処理量の見積り方（文書数×1 件あたりの処理）を報告に書く。
禁止: git commit・push、AWS・DB の実接続、.env・secrets の読み取り。差分は小さく。最後に変更点・テスト結果を短く。
