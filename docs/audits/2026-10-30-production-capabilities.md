# 本番の機能案内・登録条件の照合（2026-10-30）

読み取り専用の `live_config.json` 内の `teamagent-dev-mcp:202` / `teamagent-dev-openclaw:89` の env を、現 worktree の factory・各 description・SOUL と照合した。AWS・`.env`・secrets は参照していない。実際の公開範囲や認可は変更しない。

判定は **(a) 全員36本、(b) ツール全体としての一部対象0本、(c) OFF12本**。ただし `omiyage_report_submit` 内の **25P版は(b)一部対象1名**、標準版は(a)。本人確認・必要なGoogle/Slack連携・権限・入力・予算枠等の実行条件を満たす前提の「公開範囲」であり、実APIの成功を保証するものではない。

OpenClaw の `SLACK_DM_ALLOWLIST=*` は全員。36本すべてがリポジトリの `toolFilter.include` に含まれる。includeに名前があるだけではONではなく、factoryのフラグが先に効く。除外されている旧専用ツール群もすべて本番フラグは未設定＝OFF。

## 食い違い（修正前、重要度順）

| 重要度 | 対象 | 本番の実態 | 修正前の案内／影響 | 対応 |
|---|---|---|---|---|
| 高 | `digest_settings` | ON、全員 | 「通知設定の変更」をできない操作に列挙し、振り分けも無い。停止依頼を断る／口約束にし得る | 朝サマリー・直前通知の設定ルートを追加し、一般の通知設定と分離 |
| 高 | `tiktok_comment_mining` | `USE_TIKTOK_COMMENT_TOOLS=false` | PRリサーチ例で無条件に呼ぶ案内 | 実行例を削除し「準備中」の一文へ |
| 高 | `digest_ack` | `USE_DIGEST_ACK_TOOL` 未設定＝OFF | 確認済み・取り消しボタンを必ず呼ぶ詳細手順 | 詳細手順を削除し「準備中」の一文へ |
| 中 | `video_algorithm_status` / `proposal_builder_status` | ON、全員 | 汎用の「対応するstatus」のみで具体的な照会先が無い | 「まだ？」の照会先を明記 |
| 中 | `tiktok_search` | ON、全員 | 即時TikTok検索の専用ルートが無い | 動画保存・媒体比較と区別した例を追加 |
| 中 | お土産25P版 | フラグON、allowlistは1名のみ | SOULに標準版との対象差・対象外への返答が無い。descriptionには本人限定の記載あり | 一部対象と標準版への返し方を定義。版の判定はツール結果に委ねる |
| 中 | `video_analysis` の description → `video_approval` | 前者ON、後者OFF | 納品合否は別ツールへの参照だけで提供状況が不明 | SOULの優先する公開範囲に「納品動画の合否チェックは準備中」を明記 |

残りのOFF専用ツールは、SOULでの積極的な実行案内を確認していない。OFFであること自体を不具合とは数えず、準備中一覧で明確化した。メール要約＋社内検索を別々に使えることと、専用の自動突合がOFFであることは区別する。

## できること一覧（SOUL・description要旨）

「SOUL旧記載」は修正前のバッククォート付きツール名の有無。名前があっても専用ルートがあるとは限らない（上の食い違い表参照）。フラグ未設定はfactory既定のfalseとして判定した。

| ツール／descriptionの出典 | できること | factory登録条件（写しの値） | 利用者gate | 判定 | SOUL旧記載 |
|---|---|---|---|---|---|
| [`attachment_assist`](../../src/teamagent/skills/attachment_assist/skill.py) | PDF／Word／PPT／Excel／テキスト添付の要約・修正案・議事録・集計・英訳。投稿リンク先添付の読取・元ファイル再添付もON | `USE_ATTACHMENT_TOOLS=true` | 投稿リンク経路も `ATTACHMENT_PERMALINK_ENABLED=1`、`ALLOWED_EMAILS=*`（email_allowed） | (a) 全員ON | あり |
| [`calendar_event`](../../src/teamagent/skills/calendar_event/skill.py) | 本人カレンダーに予定を新規登録（ボタン／自由文、招待・変更・削除なし） | `USE_CALENDAR_EVENT_TOOL=true` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`calendar_freebusy`](../../src/teamagent/skills/calendar_freebusy/skill.py) | 本人の空き時間・予定一覧の読み取り | `USE_CALENDAR_FREEBUSY_TOOL=true` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`clientkarte`](../../src/teamagent/skills/clientkarte/skill.py) | 取引先の提案履歴・温度感・次アクションの時系列整理 | 常時登録 | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`digest_ack`](../../src/teamagent/skills/digest_ack/skill.py) | 朝サマリーの個別／一括確認済み・取り消し | `USE_DIGEST_ACK_TOOL=false` | 追加のツール別allowlistなし | (c) OFF | あり |
| [`digest_settings`](../../src/teamagent/skills/digest_settings/skill.py) | 本人の朝サマリー・直前通知の停止／休止／欄・件数等の設定（DM限定） | `USE_DIGEST_SETTINGS_TOOL=true` | 追加のツール別allowlistなし | (a) 全員ON | なし |
| [`knowledge_deliver`](../../src/teamagent/skills/knowledge_deliver/skill.py) | 検索した社内資料の実ファイルを本人DMへ配信 | `USE_KNOWLEDGE_DELIVER=true` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`knowledge_search_url`](../../src/teamagent/skills/knowledge_search_url/skill.py) | ブラウザの資料検索／グラフ閲覧URL発行 | `USE_KNOWLEDGE_SEARCH_URL_TOOL=false` | 追加のツール別allowlistなし | (c) OFF | なし |
| [`mail_constraints`](../../src/teamagent/skills/mail_constraints/skill.py) | 本人メールからNG手法・予算・期限等を抽出（別途同意制） | `USE_MAIL_TOOLS=false` | ON時もMAIL_CONSENT_EMAILSの明示同意が必要 | (c) OFF | なし |
| [`mail_draft`](../../src/teamagent/skills/mail_draft/skill.py) | 朝サマリーボタンまたは要返信一覧の選択から下書き保存（送信なし） | `USE_MAIL_DRAFT_TOOL=true` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`mail_followup`](../../src/teamagent/skills/mail_followup/skill.py) | 本人メールの要返信・放置候補の一覧／トリアージ | `USE_FOLLOWUP_TOOL=true` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`mail_reply`](../../src/teamagent/skills/mail_reply/skill.py) | 条件指定の返信下書き・対象選択・誤った下書きの作り直し（送信なし） | `USE_MAIL_REPLY_TOOL=true` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`mail_summary`](../../src/teamagent/skills/mail_summary/skill.py) | 本人メールの要約 | `USE_MAIL_SUMMARY_TOOL=true` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`mail_to_internal_context`](../../src/teamagent/skills/mail_to_internal_context/skill.py) | 本人メールと社内Slack／過去提案／営業FBの自動突合 | `USE_MAIL_LINK_TOOL=false` | 追加のツール別allowlistなし | (c) OFF | なし |
| [`media_case_search`](../../src/teamagent/skills/media_case_search/skill.py) | 登録済み自社トレンドメディアのキャンペーン事例・代表投稿の検索 | `USE_MEDIA_CASE_SEARCH=1` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`meeting_prep`](../../src/teamagent/skills/meeting_prep/skill.py) | 今日・次の社外商談の公開情報／過去案件／メール準備レポート（DM限定） | `USE_MEETING_PREP_TOOL=true` | `MEETING_PREP_ALLOWED_EMAILS` 空（rollout＝全員） | (a) 全員ON | あり |
| [`morning_digest`](../../src/teamagent/skills/morning_digest/skill.py) | この会話から朝サマリー生成（定期配信とは別） | `USE_MORNING_DIGEST_TOOL=false` | 追加のツール別allowlistなし | (c) OFF | なし |
| [`oauth_connect`](../../src/teamagent/skills/oauth_connect/skill.py) | 本人の未連携／追加権限が必要なGoogle・Slackの連携リンク発行 | `USE_OAUTH_CONNECT_TOOL=true` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`omiyage_report_status`](../../src/teamagent/skills/omiyage_report/skill.py) | お土産資料の進捗・取得状況・添付状態照会 | `USE_OMIYAGE_REPORT_TOOLS=1` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`omiyage_report_submit`](../../src/teamagent/skills/omiyage_report/skill.py) | TikTok検索実測→決定論集計→お土産PPTX生成・スレッド添付 | `USE_OMIYAGE_REPORT_TOOLS=1` | 標準版は全員。25P版のみ1名 | (a) 全員ON | あり |
| [`operation_log`](../../src/teamagent/skills/operation_log/skill.py) | 営業会話をCRM転記用ログ／フェーズ／BANTへ構造化 | `USE_OPERATION_LOG_TOOLS=false` | 追加のツール別allowlistなし | (c) OFF | なし |
| [`proposal_builder_status`](../../src/teamagent/skills/proposal_builder/skill.py) | 提案書の工程・成果物URL・添付済み状態の照会 | `USE_PROPOSAL_BUILDER_TOOLS=1` | 追加のツール別allowlistなし | (a) 全員ON | なし |
| [`proposal_builder_submit`](../../src/teamagent/skills/proposal_builder/skill.py) | 統合FMTの提案書PPTXを非同期生成（商材・与件、URL・開始日省略可） | `USE_PROPOSAL_BUILDER_TOOLS=1` | `PROPOSAL_BUILDER_ALLOWED_EMAILS=*`（email_allowed） | (a) 全員ON | あり |
| [`proposal_campaign`](../../src/teamagent/skills/proposal_campaign/skill.py) | KW群から提案用のTikTok上位サムネを収集 | `USE_PROPOSAL_CAMPAIGN_TOOLS=false` | 追加のツール別allowlistなし | (c) OFF | なし |
| [`proposal_deck`](../../src/teamagent/skills/proposal_deck/skill.py) | 旧FMT v2の95項目を埋めたPPTX生成 | `USE_PROPOSAL_DECK_TOOLS=false` | 追加のツール別allowlistなし | (c) OFF | なし |
| [`proposal_draft`](../../src/teamagent/skills/proposal/skill.py) | 類似の過去提案を使った文章の骨子作成 | 常時登録 | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`proposal_review`](../../src/teamagent/skills/proposal_review/skill.py) | 提案を勝ちパターン・失注理由と照合して診断 | 常時登録 | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`recommend`](../../src/teamagent/skills/recommend/skill.py) | 類似する提案書／議事録／営業FBの専用推薦 | `USE_RECOMMEND_SKILL=false` | 追加のツール別allowlistなし | (c) OFF | なし |
| [`schedule_propose`](../../src/teamagent/skills/schedule_propose/skill.py) | 朝サマリーのボタンから候補入り返信下書き＋透明仮予定（送信・招待なし） | `USE_SCHEDULE_PROPOSE_TOOL=true` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`search`](../../src/teamagent/skills/search/skill.py) | 提案書・議事録・メール・施策実績の検索／列挙 | 常時登録 | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`search_surface_check`](../../src/teamagent/skills/search_surface_check/skill.py) | TikTok／Instagram検索面・上位投稿構成・自社の在圏比較 | `USE_SEARCH_SURFACE_TOOL=1` | `SEARCH_SURFACE_ALLOWED_EMAILS` 空（rollout＝全員） | (a) 全員ON | あり |
| [`slack_search`](../../src/teamagent/skills/slack_search/skill.py) | 本人が見えるSlackのキーワード検索 | `USE_SLACK_SEARCH_TOOL=true` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`slack_summary`](../../src/teamagent/skills/slack_summary/skill.py) | 本人が見えるSlackスレッド／チャンネルの要約・名前／期間指定・集計（期間指定経路も本番ON） | `USE_SLACK_SUMMARY_TOOL=true` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`tiktok_acquire`](../../src/teamagent/skills/tiktok_acquire/skill.py) | KW群の上位投稿指標・サムネ・動画素材の非同期取得／保存 | `USE_TIKTOK_ACQUIRE=1` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`tiktok_acquire_status`](../../src/teamagent/skills/tiktok_acquire/skill.py) | 取得ジョブの進捗・投稿／サムネ／動画URL照会 | `USE_TIKTOK_ACQUIRE=1` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`tiktok_comment_mining`](../../src/teamagent/skills/tiktok_comment_mining/skill.py) | TikTok動画1〜3本のコメント分類・生活者の語彙レポート | `USE_TIKTOK_COMMENT_TOOLS=false` | `COMMENT_MINING_ALLOWED_EMAILS` 空（rollout＝全員） | (c) OFF | あり |
| [`tiktok_search`](../../src/teamagent/skills/tiktok_search/skill.py) | TikTok単体の即時検索・指標取得・横断分析 | `USE_TIKTOK_TOOLS=1` | 追加のツール別allowlistなし | (a) 全員ON | なし |
| [`video_algorithm`](../../src/teamagent/skills/video_algorithm/skill.py) | TikTok検索上位の新規取得・構造分析・レポート／スライド生成 | `USE_VIDEO_TOOLS=1` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`video_algorithm_status`](../../src/teamagent/skills/video_algorithm/skill.py) | 動画分析の実状態・本人の直近ジョブ照会 | `USE_VIDEO_TOOLS=1` | 追加のツール別allowlistなし | (a) 全員ON | なし |
| [`video_analysis`](../../src/teamagent/skills/video/skill.py) | YouTube等の外部動画の構成・フック・CTA分析（納品合否は対象外） | `USE_VIDEO_TOOLS=1` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`video_approval`](../../src/teamagent/skills/video_approval/skill.py) | 納品動画をオリエンと照合し合否・誤植・尺の一次FB | `USE_VIDEO_APPROVAL=false` | 追加のツール別allowlistなし | (c) OFF | なし |
| [`video_capture`](../../src/teamagent/skills/video_capture/skill.py) | TikTok／Instagram URL・会話添付動画から最大12時刻のJPEG切出し・配信 | `USE_VIDEO_CAPTURE_TOOL=true` | 追加のツール別allowlistなし | (a) 全員ON | あり |
| [`web_research`](../../src/teamagent/skills/web_research/skill.py) | 公開Webの市場規模・トレンド・製品仕様・ニュース・天気／イベント／祝日／為替・株価の現況等の一般質問（出典付き） | `USE_WEB_RESEARCH_TOOL=true` | `WEB_RESEARCH_ALLOWED_EMAILS` 空（rollout＝全員） | (a) 全員ON | あり |
| [`workspace_search`](../../src/teamagent/skills/workspace_search/skill.py) | 本人のWorkspaceの予定・連絡先の横断検索 | `USE_WORKSPACE_TOOLS=false` | 追加のツール別allowlistなし | (c) OFF | なし |
| [`x_buzz_measure`](../../src/teamagent/skills/x_research/skill.py) | 施策前後のX発話量・日別推移・上位投稿の非同期調査 | `USE_X_RESEARCH_TOOLS=1` | `X_RESEARCH_ALLOWED_EMAILS` 空（rollout＝全員） | (a) 全員ON | あり |
| [`x_buzz_measure_status`](../../src/teamagent/skills/x_research/skill.py) | X発話量調査の進捗・結果照会 | `USE_X_RESEARCH_TOOLS=1` | `X_RESEARCH_ALLOWED_EMAILS` 空（rollout＝全員） | (a) 全員ON | あり |
| [`x_needs_mining`](../../src/teamagent/skills/x_research/skill.py) | 業界／テーマの不満・欲求の発掘、分類・仮説レポート | `USE_X_RESEARCH_TOOLS=1` | `X_RESEARCH_ALLOWED_EMAILS` 空（rollout＝全員） | (a) 全員ON | あり |
| [`x_voice_search`](../../src/teamagent/skills/x_research/skill.py) | 特定商材の生活者の声を投稿全文・指標・URL付きで収集 | `USE_X_RESEARCH_TOOLS=1` | `X_RESEARCH_ALLOWED_EMAILS` 空（rollout＝全員） | (a) 全員ON | あり |

## allowlist方式と部分公開

| 対象 | 実装 | 写しの値 | 結論 |
|---|---|---|---|
| 提案書生成 | `submit_allowed` → `email_allowed`。空＝誰も、`*`＝本人確認済み全員 | `PROPOSAL_BUILDER_ALLOWED_EMAILS=*` | 全員。古いコードコメントの「本番は空」では判定しない |
| 投稿リンク先の添付 | `_permalink_allowed` → `email_allowed`。同じ方式 | enabled=1、allowlist=`*` | 全員（本人確認が必要） |
| Web・商談準備・X・検索面 | `rollout_allowed`。空＝全員、個別メール＝その人。`*`はワイルドカードにならない | すべて空 | 全員。空をOFFと誤認しない |
| コメント分析 | `rollout_allowed`でallowlist空＝全員でも、登録フラグがOFF | フラグfalse、allowlist空 | OFFが優先 |
| お土産25P版 | `sales25.enabled`。フラグON＋検証済みSlack本人＋非空allowlist、`*`またはrolloutの個別一致 | `USE_OMIYAGE_SALES25=1`、1名 | 一部対象。空ならこの機能は誰も通らない。対象外は標準版へ |

実装: [email_allowed](../../src/teamagent/mcp_gateway/allowlist.py)、[rollout_allowed](../../src/teamagent/skills/_shared/rollout.py)、[sales25.enabled](../../src/teamagent/skills/omiyage_report/sales25.py)。本番の個人メールは転載せず、フィクスチャでは `pilot@example.test` に置換した。

## 修正と検証

- SOULに優先する公開範囲・対象外への返し方を追加。OFF機能の積極的な案内を除去し、ON機能の振り分けと照会先を追加。字数枠のため重複説明・表示例を短縮し、既存契約は維持。
- [固定フィクスチャ](../../tests/infra/fixtures/production_capabilities.json) と [契約テスト1本](../../tests/infra/test_soul_capabilities.py) を追加。実factoryの本番想定登録集合、全フラグON、各フラグを個別OFFにした登録集合、SOULの準備中一覧／ON案内、OpenClaw include、実allowlist関数と25P版の対象差を検証する。外部接続するsearch／token storeは差し替える。
- 新規テストは既存CIの `tests/` 収集対象。CI定義の変更や追加依存は不要。
- `pytest -q -n auto tests/orchestrator tests/infra`: **705 passed**（既存DeprecationWarning 1件）。`tests/openclaw` はこのworktreeには存在せず、OpenClawの契約は `tests/infra` に含まれる。
- `ruff check src/ tests/ scripts/` と `ruff format --check src/ tests/ scripts/`: ともに成功（1,446ファイル）。SOULは **27,953 UTF-16ユニット / 安全枠28,000**。
- 指定の `uv run --extra dev --extra mcp` は、通常キャッシュへの権限制限、書込可能キャッシュに切り替えた後の依存取得先DNS制限で起動できなかった。代わりに既存のPython 3.13.13環境のpytest／xdist／mcp／ruffを用い、`PYTHONPATH=src` でこのworktreeを検証した。
- 初回の全範囲検証はNodeの `ipaddr.js` 不足で1件失敗。同じロック版2.2.0の既存ローカルインストールをignoredな `node_modules` に補って再実行し、全件成功。依存定義・ロックファイルの変更なし。初回の完全な失敗出力は `/private/tmp/teamagent-capsync-suite.log`、最終結果は `/private/tmp/teamagent-capsync-suite-2.log` に保存。

## 残した点・確認範囲

- env・factory・各ツールの公開範囲は変更しない。descriptionは汎用の機能説明として維持し、SOULの公開範囲を優先する。
- 朝サマリーの会話からの生成がOFFでも、別の定期配信タスクまでOFFとは断定しない。今回指定された2サービス以外の定期配信側 `MORNING_DIGEST_PREFERENCES` 等は未確認。
- `answer_feedback_record` はONだがモデルに出さないボタン専用経路。`run_agent`・`personal_memory_*` もfactory外の経路であり、今回の48本に含めない。旧同期 `proposal_builder` や `chitchat` はOpenClawの明示exclude。
- テストは固定想定とソースのずれを検出する。将来の実本番envドリフトを取得するテストではないため、公開範囲変更時はフィクスチャとSOULを同時更新する。自然言語のあらゆる矛盾や実Slack応答の品質を証明するものではない。
- AWS・外部APIの実行、デプロイ、commit・pushはしていない。
