# カタログ第一弾ツールのルーティング検証（CLAUDE.md §10 E2）

OpenClaw(Aico) の外側ルーター（Haiku 4.5）は **name + description だけ**でツールを選ぶ。
新6ツール（x_voice_search / x_needs_mining / x_buzz_measure(+status) / search_surface_check /
tiktok_comment_mining）が、既存の動画/TikTok系（tiktok_search / tiktok_acquire /
video_algorithm / video_analysis / video_approval）や X系どうしで**取り違えられないこと**を、
出荷前にルーティング・シミュで確認する。

## 成果物
- `catalog_routing_corpus.jsonl` — 期待ラベル付き発話コーパス（正例＋境界/敵対例＋新ツールに
  盗まれてはいけないネガ例）。`expect`=第一候補、`alt_ok`=許容される代替ツール。
- `../skills/test_routing_descriptions_catalog.py` — description のトリガー語・相互排他注記を
  固定する回帰テスト（description を将来いじっても棲み分けが壊れないよう pytest でガード）。

## シミュ手順（新規/変更時に手動で1回）
1. OC 可視ツール（openclaw.config.json5 の toolFilter.include）全部の name+description を集める。
2. Haiku モデルのサブエージェントを「name+description だけで1ツール選ぶルーター」に見立て、
   `catalog_routing_corpus.jsonl` の utterance をブラインドで流す（独立2本以上で多数決）。
3. `expect`（または `alt_ok`）と突き合わせ、誤選択＝混同ペアを description 修正で潰し、再実行。

## 最新結果（2026-07-13・3ラウンド反復で収束）
Haiku 独立シミュ2本 / OC可視28ツール。
- **R1**（硬化前・n=45）: `voice-01`（商材名＋「不満・欲求」）が x_needs_mining に誤流出
  （2本中1本が誤）。
- **R1硬化**: x_voice_search=「商材名が主語なら不満収集もここ」、x_needs_mining=「業界/テーマ
  全体（商材非特定）」を description に明記。既存 tiktok_search にも相互排他注記を追加。
- **R2**（硬化後・n=51、新変種 voice-05/06・needs-05 追加）: **2本とも全件一致・正解**。
- **R3**（未検証の敵対例14・過適合検出）: 13/14一致。`adv-07`（「TikTok検索して上位リストだけ
  先にちょうだい」）が tiktok_search / tiktok_acquire で割れた（「取得」語のトラップ）。
- **R3硬化**: tiktok_search に「今すぐ即時取得／本体DL・大量取得の非同期ジョブは tiktok_acquire」
  を追記。
- **R3b**（再確認）: adv-07 と同期/非同期の変種4件 → **2本とも全件一致・正解**。
- 残る許容ゆらぎ: `boundary-03`（「バズってるか件数で」）は x_buzz_measure/x_voice_search の
  どちらも許容（alt_ok）。実害なし。
- 結論: カタログP.9の全テンプレ＋境界/敵対例で、既存の動画/TikTok系・X系どうしの取り違えは
  解消済み。descriptionを将来いじる時は本 corpus で再シミュし、
  `test_routing_descriptions_catalog.py` の固定点を壊さないこと。

---

## R4ラウンド（顧客名なし・一語入力クラス）— 2026-08-20 本番QA由来

### 何を足したのか
R1〜R3 のコーパスは **顧客名・商材名・URL が必ず入っている発話**しか持っていなかった。
本番QAで落ちたのは逆のクラス、つまり **利用者が固有名詞を何も言わない依頼**だった:

| 実発話 | 起きたこと |
|---|---|
| 今週の空いてる時間を教えて | `mail_followup(client_name="今週の空き時間")` → scanned=0 |
| 返信が必要なメールを教えて | `mail_summary(client_name="返信必要")` → scanned=0 |
| 今日届いたメールを要約して | `mail_summary(client_name="今日のメール")` → scanned=0 |
| 明日の予定を教えて | 予定一覧の受け口が無く空振り |
| 連携（一語） | トリガー語が description に無く未発火 |

ルーターは required フィールドを必ず埋めるので、値が発話に無ければ**依頼文の断片が詰められる**。
Gmail は完全一致フレーズ検索なので必ず 0 件になり、利用者からは「連携が壊れた」に見える。

R4 で追加した 18 行（`freebusy-01..03` / `agenda-01..03` / `mailnc-01..04` / `oauth-02..04` /
`r4neg-*` 5 行）は、この失敗クラスの**実発話そのもの**と、新語彙に盗ませてはいけない対照。

### コーパスの追加フィールド
既存の `id` / `utterance` / `expect` / `alt_ok` / `note` に 2 つ足した。

- `forbid`: **選んではいけない**ツール（例: 「明日の予定を教えて」で `calendar_event`＝登録に流す）。
- `arg_rules`: 引数の契約。`{"client_name": "must_be_absent"}` / `"must_be_present"` /
  `"must_equal:<値>"` の 3 述語。**選んだツールが合っていても引数を捏造したら不合格**。
- `expect: "__ask_back__"`: どのツールも呼ばず聞き返すのが正解、を表す sentinel
  （`SkillRegistry` に実在しないので `test_corpus_is_wellformed` が先頭 `__` を特別扱いする）。

`mailnc-*` は `expect="__ask_back__"` + `alt_ok=[mail_followup|mail_summary]` +
`arg_rules={"client_name":"must_be_absent"}`。**聞き返す**か、**client_name を空**で呼んで
サーバの決定論案内に落とすか、のどちらでも合格。`forbid` には `clientkarte` /
`mail_to_internal_context` / `mail_reply` を置く（この 3 本は `client_name` が required なので
選ばれた時点で捏造が強制される）。

### シミュ手順（R1〜R3 と同じ・採点だけ拡張）
1. OC 可視ツール（`openclaw.config.json5` の `toolFilter.include`）全部の name+description を集める。
2. Haiku サブエージェントを「name+description だけで 1 ツール選び、引数も出す」ルーターに見立て、
   `catalog_routing_corpus.jsonl` の utterance をブラインドで流す（独立 2 本以上で多数決）。
3. 採点は 3 段:
   - `expect` / `alt_ok` に一致するか
   - `forbid` を選んでいないか
   - `arg_rules` を満たすか（**R4 で追加。ここが今回の失敗クラスの本体**）

### R4 結果欄（手動シミュ・未実施）

| 項目 | 値 |
|---|---|
| 実施日 | **未実施** |
| モデル / 独立本数 | — |
| 可視ツール数 | — |
| n（コーパス行数） | 76 |
| ツール選択の一致 | — |
| `forbid` 違反 | — |
| `arg_rules` 違反 | — |
| 硬化した description | — |

> ⚠️ R4 の欄が「未実施」のままなのは、**LLM が実際に正しく振り分けるかは pytest では測れない**ため。
> P1-3 が自動化したのは以下の 4 点だけで、これはシミュの代わりにはならない。
>
> 1. 規約文（トリガー語・相互排他注記）が description に在るか
>    — `tests/skills/test_routing_descriptions_mail_calendar.py` / `..._catalog.py`
> 2. ルーティング指示の**指し先が実在し、本番タスクで登録されるか**
>    — `..._catalog.py::test_no_dangling_tool_references_in_descriptions_or_soul` ほか
> 3. 失敗例がコーパスに残っているか（R4 行の凍結）
>    — `..._catalog.py::test_r4_regression_rows_are_frozen`
> 4. 捏造された client_name をガードが弾くか
>    — `tests/skills/test_client_name_guard_contract.py`

### R4 で判明した既知の負債（シミュ前に把握しておくこと）
- `vapproval-01` / `vapproval-02` / `boundary-04` / `neg-ksurl-01` の 4 行は、期待ツール
  （`video_approval` / `knowledge_search_url`）が **本番タスクで登録されていない**
  （`effective-tool-scope.json` の `enabledBy.kind == "never"`）。可視ツール一覧に出ないので
  シミュでは必ず不合格になる。`CORPUS_ROWS_EXPECTING_UNWIRED_TOOLS` に明示済み。
- `mailnc-04`「放置してるメールある？」は、ルーターが `client_name="放置してるメール"` を作ると
  残差「してる」が 2 文字以上残り **ガードを素通りする**（`KNOWN_GAPS`）。ここは description と
  ルーター側の規律だけが防波堤。

---

## R5ラウンド（YouTube の「切り出し不可」を動画全般へ広げた）— 2026-09-24 本番由来

### 何が起きたか
本番（mcp:115 / openclaw:48）で「この動画を分析して <YouTube URL>」に対し、Aico がツールを一度も
呼ばず（`tool_calls=0`）「YouTube は取得元にブロックされるため分析できません」と返した。
`video_capture` の説明文と SOUL の注意書き（切り出しは DL が要るので YouTube 不可）を、動画分析
全般に広げて読んだため。`video_analysis` は Gemini に file_uri で URL を渡す経路（DL 不要）で
YouTube を分析できる（同日 12 秒で成功）。SOUL 側は PR #441、説明文側が本ラウンド。

### 足した行
| id | 発話の型 | expect | forbid |
|---|---|---|---|
| `vanalysis-yt-01`（PR #441） | 本番の発話そのもの | `video_analysis` | `video_capture` |
| `vanalysis-yt-02` / `-03` | 秒数・時刻の言及がある「分析して」（敵対） | `video_analysis` | `video_capture` |
| `vcapture-01` / `-02` | 切り出しの正例（既存コーパスに `video_capture` の行が無かった） | `video_capture` | `video_analysis` |
| `vcapture-yt-01` | YouTube の切り出し。呼べばサーバが決定論の案内（添付のお願い）を返す。**呼ばずに断るのは不合格** | `video_capture` | `video_analysis` |

### 結果（2026-09-24・Haiku 独立 2 本 × 4 条件）
可視ツール 35（`effective-tool-scope.json` の `enabledBy` が never 以外）。動画系 18 行
（上の 6 行＋既存の algo / comment / tsearch / acquire / boundary-02 / adv-06 / adv-07 /
vanalysis-01 / -02。`video_approval` 期待の未配線 3 行は除外）。

| 条件 | `video_capture` の説明文 | 一致（2 本） | `forbid` 違反 | `arg_rules` 違反 | `vcapture-yt-01` で呼んだ本数 |
|---|---|---|---|---|---|
| 変更前 | origin/dev（「YouTube は取得元にブロックされるため、ファイル添付でお願いする」） | 14 / 13 | 0 | 0 | 0 / 2 |
| R5 | 「この制限は切り出しだけ・YouTube 動画の分析は video_analysis で可能」を追記 | 12 / 17 | 0 | 0 | 0 / 2 |
| R5b | description に「断らずにそのまま呼べばサーバが添付のお願いを返す」 | 14 / 14 | 0 | 0 | 1 / 2 |
| R5c | url 引数の説明にも「YouTube の URL もそのまま入れて呼べばサーバが添付の案内を返す」 | 17 / 15 | 0 | 0 | **2 / 2** |

- `vanalysis-yt-01` / `-02` / `-03` は**全 8 本で `video_analysis`**。name+description だけの
  ルーターは変更前から YouTube の分析を取り違えていなかった＝本番の事故は SOUL 側の誤汎化が主因（#441）。
  説明文の但し書きは同じ読み違いの再発防止（二重化）。
- 説明文を変えても、分析の依頼が切り出しへ流れる誤り（`forbid`）は全条件でゼロ。
- **新たに見つかった穴**: YouTube の切り出しは、変更前の説明文だと 4 本中 4 本がツールを呼ばずに
  自分で断った（SOUL の「断る前にツールを呼ぶ」と食い違う）。R5b → R5c で 2 本中 2 本が呼ぶようになった。
  固定点は `tests/skills/video_capture/test_video_capture.py` の
  `test_youtube_capture_is_called_not_refused_by_the_router`。
- 条件によらず残る外れ（シミュの作り由来）: `algo-01` / `algo-03` / `acquire-01`（「この5つの検索KW」
  「この検索KW」の中身が発話に無い）と `vanalysis-02`（`<url>×3`）は、単発のブラインドでは中身が
  決まらず聞き返しに倒れる。`vcapture-02`（「さっき貼った動画」）も 8 本中 2 本が文脈不足として
  聞き返した（変更前 0/2・変更後 2/6。n が小さく、説明文の変更との因果は判定できない）。
- R4 の 18 行（mail / calendar 系）は本ラウンドでは流していない（R4 結果欄は未実施のまま）。
