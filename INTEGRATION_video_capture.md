# video_capture 統合手順（共有ファイルへの追記指示）

このブランチ（`feat/video-capture-0817`）は **共有ファイルを編集していない**。
以下 4 ファイルは他作業と衝突するため、統合担当が本書のとおり反映する。
反映しないと `USE_VIDEO_CAPTURE_TOOL=true` にしても **OpenClaw の toolFilter で落ちて
エージェントからは見えない**（MCP 側だけ ON にしても解禁されない＝video_approval と同型）。

---

## 0. 前提（P0 スパイク実測・2026-08-17 本番 media worker）

| 入口 | 実測 | P1 の扱い |
| --- | --- | --- |
| TikTok URL | ✅ 成功（browser 経路・5.12MB・93.2s） | 対応 |
| Instagram Reel URL | ✅ 成功（yt-dlp・3.40MB・75.9s） | 対応 |
| YouTube 短尺 | ❌ `MEDIA_ACQUIRE_FAILED`（Sign in to confirm you're not a bot） | **未対応**（決定的メッセージで案内） |
| YouTube 長尺 | ❌ 同上 | **未対応** |
| Slack 添付動画 | 取得元に依存しない（ユーザー裁定で P1 に格上げ） | 対応 |

YouTube はスキル側で **入口で弾く**（`VIDEO_CAPTURE_ALLOW_YOUTUBE=1` で解禁可能だが、
cookie / PO_TOKEN 対応が入るまで通らない）。90 秒待たせて汎用エラーにしない設計。

切出し側の実測（同日・acquire 済み TikTok 動画に対して）:

| ジョブ | 結果 | 所要 |
| --- | --- | --- |
| frame（1.0 / 3.0 / 5.0 秒・width=480） | ✅ JPEG 3 枚（22.3KB / 39.5KB / 26.1KB） | 77.4s |
| frame（1.0 秒 + 3600 秒＝範囲外を1点混入） | ❌ **`MEDIA_PROCESS_FAILED`** | 68.9s |

> 🔴 **設計時の想定と実測が違う**: 範囲外 timecode の実コードは `MEDIA_FRAME_EMPTY` ではなく
> `MEDIA_PROCESS_FAILED`。worker ログ実物に
> `Nothing was written into output file, because at least one of its streams received no packets.`
> → `Conversion failed!` が出ており、ffmpeg が**非ゼロ終了**するため
> `operations._run` が `MEDIA_PROCESS_FAILED` を先に上げる。
> 両コードを同じ日本語文へ写像していないと、**本番で必ず起きるケースが汎用文に落ちる**。

**1 ジョブの往復は ECS 起動込みで 69〜93 秒**（acquire も frame も同程度）。
これがタイムアウト配分（acquire 240 + frame 180 = 420s ≦ 600s 天井の 70%）の根拠。

---

## 0-bis. 🔴 この単独ブランチでは 1 本のテストが必ず赤になる（設計どおり）

```
FAILED tests/scripts/test_tool_scope_registry_contract.py::
       test_scope_registry_and_factory_have_an_exact_classification
```

原因は欠陥ではなく **fail-closed 契約**である。同テストの不変量:

```python
assert registry_names - scope_names == DARK_SKILL_ALLOWLIST
```

＝「`@register` された skill は、effective-tool-scope.json に副作用分類つきで載るか、
明示的な dark 許容リストに載るかの**どちらかでなければならない**」。
新スキルを分類せずに factory の env flag だけ増やす変更を、この repo は構造的に拒否する。

本ブランチは共有ファイル（scope）を編集しない方針のため、**scope エントリが入るまでこの 1 本は赤**。
`§2` のエントリを追加すると緑になることは実測済み（追加→`2 passed`→ファイルは SHA256 一致で復元）。

そのまま貼れる JSON を `INTEGRATION_video_capture.scope.json` に置いた。

> ⚠️ 逆に **`DARK_SKILL_ALLOWLIST` に `video_capture` を足して緑にするのは誤り**。
> このツールは dark ではなく scope 掲載が正しい分類で、後で必ず剥がす必要が出る。

---

## 0-ter. テスト実測（2026-08-18・本ブランチ HEAD）

| 範囲 | 結果 |
| --- | --- |
| `tests/`（`tests/scripts` を除く全部） | **4459 passed / 25 skipped / 0 failed** |
| `tests/scripts`（`test_terraform_runtime_guard.py` 除く） | 983 passed / 2 skipped / **1 failed**＝上記 §0-bis の scope 契約のみ |
| `tests/scripts/test_terraform_runtime_guard.py` | 168 passed / 1 failed → **孤立再実行で 54 passed（当該パラメータ含む全件）** |
| ruff check / ruff format --check / mypy strict / import-linter | すべて緑 |
| `tests/skills/video_capture` + `tests/adapters/test_slack_file_bounded_download.py` | 71 passed |

> `test_runtime_attribute_regressions_fail_closed[lambda_source_reference]` の1件は
> **高負荷下のフレーク**。同ファイルを単独で回すと当該シナリオを含む 54 件が全部緑になる
> （本ブランチの変更は `core_from_snapshot` / `print_hcl_snapshot`＝snapshot 系のみで、
> このテストが叩く plan 系の lambda 属性検査とは経路が交わらない）。

---

## 1. `infra/openclaw/openclaw.config.json5`

`mcp.servers.teamagent.toolFilter.include` に 1 要素を追加する。

```json5
            // 動画キャプチャ: 指定時刻のフレームを JPEG にしてスレッド/DM へ添付。
            // URL(TikTok/Instagram) と会話の添付動画の 2 入口。MCP 側は USE_VIDEO_CAPTURE_TOOL=1。
            "video_capture",
```

配置位置の推奨: `"video_approval",` の直後（動画系ツールの並びを保つ）。

---

## 2. `infra/openclaw/effective-tool-scope.json`

`tools` 配列に 1 エントリを追加する（合計 31 → **32**）。

```json
{
  "name": "video_capture",
  "effect": "external-video-read-slack-file-delivery",
  "terraformGate": "use_video_capture_tool",
  "defaultEnabledByTerraform": false,
  "enabledBy": {
    "kind": "envAllTrue",
    "names": ["USE_VIDEO_CAPTURE_TOOL"]
  }
}
```

補足（scope 表記に載らない事実・レビュー時の確認事項）:

- 読むのは **外部公開 URL（TikTok / Instagram）と自ワークスペースの添付動画だけ**。
  社内ナレッジ・メール・カレンダーには一切触れない（依頼者の権限を超えた情報は構造的に返らない）。
- 配信先は **依頼スレッド or 依頼者本人 DM のみ**（`knowledge_deliver` と同型）。
- `user_email` は MCP 外殻が注入した値のみを信用し fail-closed。
  したがって **strict caller-claim 経路でのみ有効**。LEGACY モード（resolver 無）は
  `channel_id` が raw 採用（`mcp_gateway/server.py`）になるためテスト/PoC 専用と理解すること。
- `url_private` へは bot token が載るため、`*.slack.com` 以外へは絶対に出さない
  （adapter 側で allowlist + リダイレクト非追従を強制済み）。

---

## 3. `infra/openclaw/SOUL.md`

「PRリサーチ」節の `video_algorithm` トリガー行（`「この検索KWで勝ってる動画、なんで勝ってるのか
分析して」→ video_algorithm`）の直後に、以下の節を追加する。

```markdown
## 動画のシーン切出し（画像で欲しいとき）

- 「**このTikTokの0:05と0:12のシーン、画像で出して**」→ `video_capture`（url + timecodes）
- 「**さっき貼った動画の1:20あたり切り出して**」→ `video_capture`（url は空・`slack_file=true`）

**呼び分け**: 「なぜ勝ってるか分析して」＝`video_algorithm` /
「◯秒のシーンを切り出して・画像にして・サムネ用に出して」＝`video_capture`。

**厳守**:
- `timecodes` は**ユーザーが言った表記のまま**渡す（「0:05」「1:02:03」「5」いずれも可）。
  秒への換算はサーバ側が決定的に行うので、**自分で計算しない**（mm:ss の算術ミスがそのまま
  誤ったフレームになる）。最大12点。
- YouTube の URL は取得元にブロックされるため通らない。ツールが返す案内文をそのまま伝え、
  **動画ファイルをスレッドに添付してもらう**よう促す。
- 返ってきた `message` を**そのまま**返す（枚数・時刻を言い換えない・再計算しない）。
```

---

## 4. `tests/scripts/test_openclaw_runtime_contract.py`

`test_effective_tool_scope_matches_config_and_deployment_gates` の 2 箇所を更新する。

1. 総数を 31 → 32 にバンプ:
   ```python
   assert len(inventory_names) == len(set(inventory_names)) == 32
   ```
2. activation / gate の assert を追記（`video_analysis` の assert の並びに合わせる）:
   ```python
   assert activation_by_name["video_capture"] == {
       "kind": "envAllTrue",
       "names": ["USE_VIDEO_CAPTURE_TOOL"],
   }
   ```

`default_enabled` 集合は **変更不要**（`video_capture` は `defaultEnabledByTerraform=false`）。

---

## 5. terraform / tfvars（本ブランチで実装済みの部分と、apply 時に人がやる部分）

本ブランチで反映済み:

| ファイル | 内容 |
| --- | --- |
| `infra/terraform/variables_fargate.tf` | `variable "use_video_capture_tool"`（default `false`） |
| `infra/terraform/fargate.tf` | mcp task env に `USE_VIDEO_CAPTURE_TOOL` |
| `infra/terraform/runtime_guard.tf` | `runtime_guard_live` object 型に `use_video_capture_tool` + parity 条件 |
| `infra/deploy/terraform_runtime_guard.sh` | live task def → tfvars 生成に `use_video_capture_tool` を追加（**2箇所**） |

> ⚠️ 4 番目を忘れると、生成される `runtime_guard_live` に属性が足りず
> **object 型不一致で plan が落ちる**（parity は fail-closed）。

apply 時に人がやること（git 管理外なので PR に含められない）:

1. `terraform.tfvars`（build worktree のもの）の `runtime_guard_live` に
   `use_video_capture_tool = false` を **先に**追記する（初回は現況＝false と一致させる）。
2. `-var-file` 付きで plan/apply（tfvars 無し apply は禁止）。
3. ON にするときは `use_video_capture_tool = true` と `runtime_guard_live.use_video_capture_tool = true`
   を **同時に**書き換える（片方だけだと parity で落ちる）。

---

## 6. リリース順序（依存関係）

```
dev merge → CI 緑
  → MCP イメージ焼き（video_capture が MCP 側に出る）
  → OpenClaw イメージ再ビルド（toolFilter / SOUL / scope は OC イメージに焼き込まれる）
  → 署名リリース（ユーザー MFA 1回）
  → tfvars で use_video_capture_tool=true → apply
  → 本番実 Slack で 1 往復検証
```

**MCP 側の env を ON にするだけでは解禁されない。** OpenClaw イメージの再ビルドが必須
（`video_approval` が「scope enabledBy=never のまま」で解禁できていない前例と同型）。

実機検証（health check はすり抜けるので**必ず実 Slack で 1 往復**・テスト配信は小俣さん本人のみ）:

1. TikTok URL + 「0:01と0:03を画像にして」→ スレッドに JPEG 2 枚
2. 動画ファイルをスレッドに添付 + 「1:20切り出して」→ JPEG 1 枚
3. YouTube URL → 90 秒待たずに案内文が返る
4. 動画長を超える時刻 → 「指定時刻が動画の長さを超えています」

切り戻し: `use_video_capture_tool = false` → apply のみ（コード撤去不要）。

---

## 7. 運用ノブ（env・すべて任意）

| env | 既定 | 意味 |
| --- | --- | --- |
| `USE_VIDEO_CAPTURE_TOOL` | false | ツール自体の解禁 |
| `VIDEO_CAPTURE_ALLOW_YOUTUBE` | false | YouTube URL を受けるか（実測でブロック中） |
| `VIDEO_CAPTURE_SLACK_MAX_MB` | 100 | 添付動画の上限（1〜128） |
| `VIDEO_CAPTURE_MAX_CONCURRENCY` | 2 | 同時切出し数（3GB 共有コンテナの総量規制） |

---

## 8. 添付経路が必要とする Slack bot scope（未確認・要点検）

`slack_file=true` の経路は bot token で会話履歴を読み、`url_private` を取得する。

| API | 必要 scope | 用途 |
| --- | --- | --- |
| `conversations.replies` | `channels:history` / `groups:history` / `im:history` | スレッドの添付を探す |
| `conversations.history` | 同上 | スレッド外／DM の直近30件を探す |
| `files.slack.com` の GET | `files:read` | 添付実体の取得 |
| `files.upload v2` | `files:write` | JPEG の添付返信 |

`files:read` / `files:write` は EC2 bot 経路（`runtime/slack_bot.py` の動画添付分析・
`knowledge_deliver` の資料配信）で実績があるが、**mcp コンテナの bot token で
`im:history` があるかは未確認**。

⚠️ scope 不足は「動画が見つかりません」に化けない設計にしてある
（`conversation_read_failed`＝「この会話の履歴を読めませんでした（動画の有無を確認できていません）」）。
本番検証で **DM で `slack_file=true` を1回試し**、このメッセージが出たら scope を追加する。
