差し戻し（レビュー結果）。本物の 86 枚 FMT（SHA 07ea41700933…）で描くと、今回の判定は本番で一度も ready にならない。直してほしい。

本物の FMT のローカルコピー: ~/Documents/Claude/Artifacts/handoff-20261030/fmt86_07ea4170.pptx（読み取りのみ。コミットしない）
本文の見本: ~/Documents/Claude/Artifacts/20261022_BU2FMT/composer_{BU1,BU2}_{美容,食品}.json（合成の検証用）

## 1. 画像欄の検出が本物の FMT で 0 件（必須）
- `proposal_field_spec(Presentation(本物FMT))["images"]` も、worker の `_iter_proposal_image_slots` も 0 件。結果 `required_image_spec_missing` が毎回出て、本番は全件 draft になる。テスト雛形には魔法の座標の四角を置いているので、テストは本番を表していない。
- まず事実を確定する: 本物の FMT に「証拠画像を貼る枠」は本当に 0 か、検出漏れか。全 86 枚の画像系の枠（空の四角・「画像」ラベル・ピンクの仮置き四角など）を列挙して報告に書く。
  - 0 が正しいなら、0 は正当な欄仕様として扱い、`required_image_spec_missing` を出さない（期待 0・挿入 0 は不足ではない）。
  - 検出漏れなら、本物の FMT に合わせて検出を直す（座標の当て推量でなく、FMT の枠を特定できる根拠で）。
- 本物の FMT を使うテストを足す（ファイルが無い環境では skip。SHA を照合）。中身の揃った composer で、BU2 食品・BU1 美容など 2 種以上について `field_issues` に画像系の issue が出ないこと。

## 2. 補助欄の「要確認」行の置き換え（必須）
- `PB-ACCOUNTS` は上流でアカウント説明文の途中に「要確認（出典URL未取得）」が入って届くことがある。今の行単位置換だと、その行が丸ごと「条件未指定・要相談」になり意味が通らない（アカウントは「条件」ではない）。
- 「条件未指定・要相談」に置き換えるのは見積・試算・ヒアリング等の条件系の欄だけにする。PB-ACCOUNTS など条件系以外は、旧来どおり（要確認入りは表示しない）に戻し、issue としては残す。

## 3. 判断待ち（今は変えない）
依頼文に予算等が無いとき（plans=[]・simulation=null・hearing_items=[]）、PLAN/SIM/HEARING/ESTIMATE-NOTES が全て auxiliary_missing になり draft になる件は、仕様判断を待っている。今回は現状の挙動を変えない。ただし「この欄群だけで draft になっているか」が分かるよう、issue の接頭辞や並びで区別できるなら教えて（変更は不要）。

## 共通
- 先に失敗するテスト → 修正。`uv run --extra dev --extra mcp --extra media pytest -q -n auto tests/skills/proposal_deck tests/skills/proposal_builder tests/media`、ruff・mypy（変更ファイル）。infra テストの `ipaddr.js` 失敗が環境由来か本物かも一言。
- 禁止: git commit・push、AWS・Gemini・Bedrock の実呼び出し、.env・secrets の読み取り。顧客名を報告に書かない。差分は小さく。
- 最後に変更点（ファイル:行）・テスト結果・本物 FMT での画像枠の列挙結果・残る懸念を短く。
