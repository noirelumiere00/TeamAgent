# 入力ルール第1版の実装・検証報告

作業ブランチは `feat/input-policy-v1`、HEAD は `8f243b8a`。現在のローカル `origin/dev` は `decb2578` だが、共通祖先は指定の `8f243b8a` であり、ブランチ・基点を移動していない。commit / push / ネットワーク / AWS / 本番 / Slack 操作は行っていない。

**サーバ側の実装と検証は完了。公開スキーマ・ツール説明の変更は未達であり、依頼全体の完了とは扱わない。** `tools/list` は身元を受け取らないため、許可外・身元不明のツール定義までバイト一致を守ることと、許可された人だけに新しいスキーマ・説明を配ることを同時には実現できない。ユーザーへこの境界を確認し、回答の無い現時点では厳しい互換条件を優先した。ON 時の共通定義変更が許容されれば、公開スキーマの任意化・状態の列挙値・ブランドだけの呼び出し例を追加する作業が残る。

## 1. 作ったもの・状態の置き場所

- `src/teamagent/skills/_shared/input_policy.py`: `FieldSpec`、4状態、純関数 `evaluate`、`proceed / ask_once / proceed_partial`、まとめた質問・番号つき選択肢、省略・既定値の1行。認証等の `confirm_side_effect` に既定値は代入しない。
- `mcp_gateway/server.py`: 本人解決・既存の権限ガードの後でだけ有効化。実際のPydantic `ValidationError` のうち、トップレベルの欠落だけをまとめて1回にする。その他の不正値・モデル検証・ツール例外は従来の返し方を維持する。
- `omiyage_report/skill.py`: 任意項目を省略、不要・お任せを反映、不適切な検索語・競合を点検して省く。受付・重複受付の最後に説明を付ける。既存の認証・宛先・アドミッション・重複防止を維持する。
- `omiyage_report/deck_plan.py / contract.py / compose.py / fmt/{contract,html,editable}.py`: 競合なしでは比較ページ・比較用のブランド・表紙の「×」を外す。一般KWなしでは検索軸・露出シェア・登場率を外す。要約も未取得や0件として誤記しない。ブランドだけの資料は実測TOPと総括等、作れる部分で構成する。従来のデッキビルダーの既定では競合必須を維持し、許可された実行だけ `allow_brand_only` を渡す。
- `search_surface_check/confirm.py`: ON・許可された本人の会話だけ、取得範囲の任意確認を省く。月間上限確認を行う後段には変更を加えていない。
- `infra/openclaw/SOUL.md`: ツールが省略・既定の説明や代わりの骨子を返した場合だけ、それを伝えて同じ質問を繰り返さない。説明文と過去の実測例を圧縮し、安全枠に収めた（27,988 UTF-16 / 28,000）。MCPだけでも判定が効く。
- 新規テスト3ファイル: `tests/skills/test_input_policy.py`、`tests/mcp_gateway/test_input_policy.py`、`tests/skills/omiyage_report/test_input_policy.py`。

台帳はロック付きプロセス内キャッシュ。TTLは1時間、状態・質問の各台帳は最大4,096件。鍵はメール・Slackチーム・チャンネル・スレッド・署名済みセッション・ツールのSHA-256。省略状態にはブランド・商材・調査対象を加える。質問回数は会話の鍵で数え、一部回答で対象が埋まっても質問を再開しない。保持するのはハッシュ・状態・期限・質問済みの印だけで、入力本文・値・メールの原文は保存・ログ出力しない。

再起動・複数タスク・TTL満了・容量による追い出しで状態を失う。任意項目は元から聞かないので、状態消失により再質問する可能性があるのは必須項目だけ。セッションの無い直呼びはチャンネル・スレッドとTTLが区切りであり、同じ会話の別依頼まで質問を抑える場合がある。別セッション・別人・別チャンネル・別スレッド・別ブランドはテストで分離を確認した。ブランド表記の別名・大文字小文字の統合は未実装。

## 2. 適用先と分類・第2版へ

| 適用先 | required | nice_to_have | confirm_side_effect / 維持する門 | 選定理由 |
|---|---|---|---|---|
| お土産資料 | 対象ブランド | 競合・一般KW・公式アカウント（カテゴリ・先行調査も元から任意） | 本人・宛先・ジョブ上限等の既存処理 | 実測の繰り返し質問と、資料生成側の競合必須を同時に解消できる |
| 検索上位チェック | 検索語 | 分析本数=5、競合なし、取得範囲の事前確認 | 本人所有ジョブ・月間動画上限・権限 | 対象者には毎回発生していた任意の確認を省ける |
| Xの生活者の声検索 | 商材名 | 検索語=[商材名]、それ以外は既存の既定 | 既存の公開範囲・権限・費用等 | 商材名があるのに別途検索語を必須としていた入力をMCP側で補える |
| MCP共通の検証エラー | 各ツールの欠落した入力 | 自動補完は上記適用先だけ | 不正値・入れ子検証・認証・費用等の例外は従来どおり | 内部英語の欠落エラーを利用者向けの質問と骨子へ変える |

状態の現在の受け方は、既存スキーマに収まる `competitors: ["不要"]` / `keywords: ["お任せ"]` 等。配列の未指定は元から任意で、未指定・お任せの競合/一般KWは省略する。新たな「省いてよいか」の確認は作らない。`不要` を受けた検索語で検索を実行しない。

公開スキーマは維持したため、OpenClawが呼び出し前に必須欠落を拒否する経路には、今回のMCP側補完・質問抑止が届かない。特にX検索の `queries` を完全に省略する呼び出し、および他ツールの required 欠落はこの制約が残る。スキーマの `Optional` / 列挙値としての状態・tool descriptionのブランドだけの例は追加していない。ブランドだけのバックエンド実行はテスト済みだが、Haikuが旧説明に従って呼ぶ前に聞く挙動の解消は未検証。

第2版へ: `tiktok_acquire`、動画分析、`x_needs_mining`、`x_buzz_measure`、83枚の提案書、`mail_summary`、`mail_reply`、`mail_draft`、`meeting_prep`、`calendar_event`、`slack_summary`、`calendar_freebusy`、素材前提の3ツール、未公開のクリップ提案。自由文検索系と `mail_followup / mail_to_internal_context / clientkarte` は元から聞かない部分を維持。X検索0件の自動再検索、モデル検証エラーの日本語化・再質問抑止も次版に残した。

## 3. OFFで変わらない根拠

- `test_off_outside_unknown_preserve_preflight_bytes`: OFF、"true"、空許可リスト、許可外、未確認、メール不明、会話不明の7分岐で、既存preflightの文面とのバイト一致と status / missing を固定。
- `test_condition_outside_validation_bytes`: MCPの実際のPydantic欠落エラーをOFF/許可外/身元不明/空許可リストでバイト一致。
- `test_tool_defs_unchanged_even_when_enabled`: tools/list の定義はONでもOFFとバイト一致。
- `test_bad_values_keep_original_validation`: 不正な上限値も元の検証エラーとバイト一致。
- `test_auth_and_expensive_confirmation_keep_original_error`: 権限失敗・月間動画費用の確認という既存失敗形を使い、ON/OFF/再呼び出しでerrorを一致させる。宛先・認証・費用を埋めて実行する処理はない。

回帰の呼び出し元は `orchestrator/factory.py:379,768,808`、`connect_web/app.py:4829`、`runtime/slack_bot.py` のSkillContext生成、`scripts/run_morning_digest_fargate.py:2247,2738` を確認。対象3ツールの直接生成はfactoryに集約されている。connect-web、Slack直呼び、スケジュール経路には本人確認と会話の組が無いので、新しい判定は有効にならない。既存公開入力モデルを変更していないため、直呼びのPydantic検証も維持する。

## 4. テスト・lint・型

完全な出力は [pytest.txt](pytest.txt)、[checks.txt](checks.txt)。下に最終結果をそのまま追記する。

ローカルの `.venv` には当初 `python-pptx` が無く、指定の全体テストは収集時に4エラーで止まった。ネットワークを使わずローカルキャッシュから `python-pptx 1.0.2 / Pillow 12.3.0 / XlsxWriter 3.2.9` を `.venv` に補った。リポジトリの依存宣言は変更していない。

最終チェックの初回には既存の `test_capacity_full_queues_and_returns_within_detach_after_s` が1件失敗。完了通知・usageが揃った直後に registry の `finally: release` がまだ走っていない場合がある競合で、単独再実行は成功した。失敗全文を [pytest-failed.txt](pytest-failed.txt)、単独再確認を [detach-recheck.txt](detach-recheck.txt) に保存。検証条件・assertion・対象実装は変更せず、指定の全体テストを再実行した。

## 5. 変異

15件すべて、該当実装だけを一時的に壊し、対象テストが assertion の失敗で赤になることを確認して復元した。収集エラーを「赤」とは数えていない。対応一覧は [mutation-summary.txt](mutation-summary.txt)、失敗全文は [mutations.txt](mutations.txt)。

対象はフラグ、空許可リスト、本人確認、任意分類、不要の記憶、本人・会話の鍵、質問上限、不要な必須項目、一部回答後の質問上限、省略文、確認への既定値禁止、登場率ページ、比較ページ、共通検証の門、任意の事前確認。

## 6. 自己審査

- 回帰: OFF時にもセッション情報をmetadataへ足していた箇所を、ON・本人確認済みのときだけに直した。デッキの競合必須を無条件に外した箇所は、既定で従来の門を維持するよう直した。公開入力モデル・tools/listはそのまま。
- 体験: 必須を不要と言った場合は初回でも聞かず骨子にする。一部の回答で質問の鍵が変わって2回目を聞く問題を、会話単位の質問台帳で直した。配列の既定値をPython表記で出していた説明は日本語の並びへ直した。指定済みの分析本数にも既定5本と説明していた文面は、判定結果から作る形へ直した。
- 品質: 任意扱いを理由に競合の品質点検まで飛ばしていた箇所を直し、不適切な入力だけを除いて進める。比較・登場率のページと配信要約を同じ省略条件へ合わせた。
- 安全: 新しい判定で認証・権限・送信・費用の既定値を作らない。既存のDM限定ガードや身元解決より前には動かさない。台帳の本文保存・ログ追加は無い。状態の保持と質問の判定をロック内で行う。

残りは、冒頭の公開スキーマ/説明の配布条件、揮発する台帳、同一会話内の新しい依頼を完全には区別できない点、ブランド別名・表記揺れの統合、短い英字ブランドで商材カテゴリが無いときの検索曖昧性。商材カテゴリの安全な推定元が無いため、今回カテゴリを創作して埋めていない。既存の関連性フィルタと、材料不足時に空ページを作らず失敗を明示する処理を維持した。既存の切り離しテストの終了待ちの競合は入力ルールの差分と別であり、失敗記録を残している。

## 最終検証出力（原文）

指定コマンド:

```text
.venv/bin/python -m pytest -q -p no:warnings tests/skills tests/mcp_gateway tests/prompts tests/infra/test_soul_contract.py
```

```text
=========================== short test summary info ============================
SKIPPED [8] tests/skills/clip_proposal/test_write_blockade.py:100: SlackClient を掴んでいない
SKIPPED [1] tests/skills/video_algorithm/chromium.py:34: could not import 'playwright.sync_api': No module named 'playwright'
SKIPPED [1] tests/skills/video_algorithm/test_report_dads.py:263: could not import 'playwright.sync_api': No module named 'playwright'
SKIPPED [8] tests/skills/video_algorithm/test_slides_layout.py:182: could not import 'playwright.sync_api': No module named 'playwright'
SKIPPED [1] tests/skills/video_algorithm/test_slides_layout.py:195: could not import 'playwright.sync_api': No module named 'playwright'
6337 passed, 19 skipped in 79.25s (0:01:19)

```

```text
$ .venv/bin/ruff check src/teamagent/mcp_gateway/server.py src/teamagent/skills/_shared/input_policy.py src/teamagent/skills/omiyage_report/compose.py src/teamagent/skills/omiyage_report/contract.py src/teamagent/skills/omiyage_report/deck_plan.py src/teamagent/skills/omiyage_report/fmt/contract.py src/teamagent/skills/omiyage_report/fmt/editable.py src/teamagent/skills/omiyage_report/fmt/html.py src/teamagent/skills/omiyage_report/skill.py src/teamagent/skills/search_surface_check/confirm.py tests/mcp_gateway/test_input_policy.py tests/skills/omiyage_report/test_input_policy.py tests/skills/test_input_policy.py
All checks passed!
exit_code=0

$ .venv/bin/ruff format --check src/teamagent/mcp_gateway/server.py src/teamagent/skills/_shared/input_policy.py src/teamagent/skills/omiyage_report/compose.py src/teamagent/skills/omiyage_report/contract.py src/teamagent/skills/omiyage_report/deck_plan.py src/teamagent/skills/omiyage_report/fmt/contract.py src/teamagent/skills/omiyage_report/fmt/editable.py src/teamagent/skills/omiyage_report/fmt/html.py src/teamagent/skills/omiyage_report/skill.py src/teamagent/skills/search_surface_check/confirm.py tests/mcp_gateway/test_input_policy.py tests/skills/omiyage_report/test_input_policy.py tests/skills/test_input_policy.py
13 files already formatted
exit_code=0

$ .venv/bin/mypy src/teamagent/mcp_gateway/server.py src/teamagent/skills/_shared/input_policy.py src/teamagent/skills/omiyage_report/compose.py src/teamagent/skills/omiyage_report/contract.py src/teamagent/skills/omiyage_report/deck_plan.py src/teamagent/skills/omiyage_report/fmt/contract.py src/teamagent/skills/omiyage_report/fmt/editable.py src/teamagent/skills/omiyage_report/fmt/html.py src/teamagent/skills/omiyage_report/skill.py src/teamagent/skills/search_surface_check/confirm.py tests/mcp_gateway/test_input_policy.py tests/skills/omiyage_report/test_input_policy.py tests/skills/test_input_policy.py
pyproject.toml: note: unused section(s): module = ['docx', 'docx.*', 'fontTools', 'fontTools.*', 'google_auth_oauthlib.*', 'googleapiclient.*', 'openpyxl', 'openpyxl.*', 'playwright', 'playwright.*', 'pptx', 'pptx.*', 'pypdf', 'pypdf.*', 'sentence_transformers.*', 'teamagent.adapters.gcalendar_client', 'teamagent.adapters.gdocs_client', 'teamagent.adapters.gdrive_client', 'teamagent.adapters.gmail_client', 'teamagent.adapters.google_auth', 'teamagent.adapters.google_oauth_flow', 'teamagent.adapters.gpeople_client', 'teamagent.adapters.gsheets_client', 'teamagent.adapters.gslides_client', 'weasyprint', 'weasyprint.*', 'yt_dlp']
Success: no issues found in 13 source files
exit_code=0
```

```text
$ git status --short
 M infra/openclaw/SOUL.md
 M src/teamagent/mcp_gateway/server.py
 M src/teamagent/skills/omiyage_report/compose.py
 M src/teamagent/skills/omiyage_report/contract.py
 M src/teamagent/skills/omiyage_report/deck_plan.py
 M src/teamagent/skills/omiyage_report/fmt/contract.py
 M src/teamagent/skills/omiyage_report/fmt/editable.py
 M src/teamagent/skills/omiyage_report/fmt/html.py
 M src/teamagent/skills/omiyage_report/skill.py
 M src/teamagent/skills/search_surface_check/confirm.py
?? docs/reports/
?? src/teamagent/skills/_shared/input_policy.py
?? tests/mcp_gateway/test_input_policy.py
?? tests/skills/omiyage_report/test_input_policy.py
?? tests/skills/test_input_policy.py

$ git diff --stat
 infra/openclaw/SOUL.md                             |  6 +-
 src/teamagent/mcp_gateway/server.py                | 74 ++++++++++++++++++++++
 src/teamagent/skills/omiyage_report/compose.py     | 13 +++-
 src/teamagent/skills/omiyage_report/contract.py    |  2 +-
 src/teamagent/skills/omiyage_report/deck_plan.py   | 25 ++++++--
 .../skills/omiyage_report/fmt/contract.py          |  4 +-
 .../skills/omiyage_report/fmt/editable.py          |  8 ++-
 src/teamagent/skills/omiyage_report/fmt/html.py    | 11 +++-
 src/teamagent/skills/omiyage_report/skill.py       | 54 ++++++++++++++--
 .../skills/search_surface_check/confirm.py         |  5 ++
 10 files changed, 177 insertions(+), 25 deletions(-)
```

新規ファイルは未追跡なので、`git diff --stat` の数には入らない。収集時の失敗全文は [collection-failed.txt](collection-failed.txt)。19件のskipは既存のSlack接続未使用とPlaywright未導入によるものであり、入力ルールのテストは全件実行した。
