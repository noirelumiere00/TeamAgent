# 追加依頼: python 3.15 core（B1）の残りのテスト失敗 14 件を直す

作業場所: ~/dev/worktrees/teamagent-py315b（branch chore/python315-core-20261105）。前回の衝突解消は Claude がマージコミット b452857d にした。

## 状況
pytest の事前確認（ps）は Claude が済ませた。手元で走る pytest はこの 1 本だけなので、そのまま実行してよい。
`uv run --extra dev --extra mcp pytest -q -n 4 tests/infra tests/codebuild tests/scripts tests/runtime` の結果、次の 14 件が赤:
- tests/codebuild/test_teamagent_bundle_provenance.py の 13 件:
  - test_pair_rejects_legacy_argument_reintroduction[WOLFI_PYTHON_VERSION]
  - test_pair_rejects_missing_media_package_assertion
  - test_pair_rejects_mutable_external_copy_image
  - test_pair_rejects_mutable_external_image
  - test_pair_rejects_removed_sales25_pin_override_guard[×4]
  - test_pair_rejects_self_or_invalid_numeric_copy_stage[4/99/final]
  - test_pair_rejects_unknown_teamagent_dockerfile_label
  - test_release_contract_is_statically_satisfiable_without_embedded_approval
- tests/scripts/test_release_preflight_vuln.py::test_core_build_reuses_real_replacements_without_app_stages

（tests/infra/test_dockerfile_teamagent_media_worker.py::test_dns_pinned_proxy_rebinding… は、未変更の dev でも落ちる手元の環境の都合なので対象外。）

## やること
- 失敗の原因を、3.15 の変更（Dockerfile・契約 JSON・provenance）とテストの期待値の食い違いとして特定する。**検証を弱めずに**、契約側かテストの期待値のどちらかを正しく揃える。
- 来歴・供給網の検証（拒否すべきものを拒否するテスト）は、拒否の性質を保つこと。期待値を「通るように」緩めない。
- 直したら、上の 4 ディレクトリの pytest を全部緑にする（DNS の 1 件を除く）。ruff check / format（変更ファイル）も緑に。
- 世代の入力が変わったら再導出して fresh を保つ（verify_generation_inputs.py）。

## やらないこと
- git add・commit・push、AWS、docker build、.env・secrets の読み取り。

最後に: 原因（1〜2 行ずつ）・変更点・テスト結果を短く。
