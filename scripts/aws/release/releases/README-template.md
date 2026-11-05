# 便の共通型と便データ

MCP は `fire_and_authorize.sh`、OC は `driver_oc.sh` を共通の型として使います。次の便では `releases/` の JSON だけを追加します。元の `fire_and_authorize_r76.sh` と `driver_1026_oc34.sh` は比較用に残しています。

このフォルダの型は承認に回すための成果物です。この作業ではリリーススクリプトを起動せず、AWS・本番・Slack へ書き込んでいません。実環境への配置・改名による承認も行っていません。

## 承認と配置

1. 型2本を既存の運用先の `drafts/` に置き、内容を確認します。MCP の型は既存の `apkwatch/` と `purge_candidate_tags.sh` と同じ位置関係になるように配置します。
2. 小俣さんが `drafts/` から正式な場所へ改名して、型をそれぞれ一度承認します。
3. 以後、便ごとの JSON を確認して追加します。JSON の追加のために型を複製・改名する必要はありません。
4. **型を変更した場合は、再び `drafts/` → 内容確認 → 改名による承認が必要です。** JSON 検証器は型の中に含めています。別の可変な実行ヘルパーはありません。

このフォルダでのファイル名は納品名であり、実環境での改名承認が済んだことを意味しません。

## 使い方（承認・配置後）

既存の MFA セッション、発射台、AWS 権限、補助スクリプト、雛形 manifest、免除 gate など、見本と同じ前提が必要です。Python 3.9 以降、Bash、Git、grep と既存の運用ツールを使います。新しいライブラリのインストールは不要です。

```bash
# MCP: データファイルを先頭に追加し、従来の実行モードを続ける
bash fire_and_authorize.sh releases/r76.json --dry-only
bash fire_and_authorize.sh releases/r76.json "APPROVED: <理由>"

# OC: 見本と同様、検査の後に build → authorize → TD 差し替えへ進む
bash driver_oc.sh releases/oc34.json
```

上記は実運用時の例です。今回の作業環境では実行しません。

MCP の `--dry-only` は見本と同じく STEP 4 の後で停止します。**STEP 1 の fetch／checkout／早送り、STEP 3 の候補タグ削除も含まれます。** オフラインの構文検査ではありません。OC に `--dry-only` や新しい承認文字列は追加していません。

便データは最初に全件検証し、検証済みの内容をメモリに固定します。実行途中で JSON が編集されても検査対象は変わりません。MCP は発射台の `HEAD`、OC は見本と同じく、dev の早送り・clean 確認後の作業ツリーを検査します。

## 次の便の追加

1. 直前の `releases/rNN.json` または `releases/ocNN.json` を新しい便名でコピーします。
2. `name` と `description` を更新します。MCP の `OLD_CORE` は直前に本番へ入った core の digest（`sha256:` と小文字16進数64桁）に更新します。
3. 積み荷の確認を `checks` に追加します。既存の確認は引き継ぎ、置き換える必要がある版指定だけを更新します。OC の `phase` は後述の位置を保ちます。
4. JSON の差分で label・kind・path・pattern と版指定を確認します。コマンドや AWS 操作を記述する欄はありません。
5. 下記の構文検査とローカルテストを行います。新しい便の実コードへの適合確認は、承認された実運用手順で行います。このテストの固定比較は r76／oc34 が対象です。

MCP データの構造:

```json
{
  "name": "r77",
  "description": "次の便の説明",
  "OLD_CORE": "sha256:ここを直前のcoreの小文字16進数64桁に置換",
  "checks": [
    {
      "label": "#706 原本リンク",
      "kind": "grep_fixed",
      "path": "src/teamagent/skills/search/skill.py",
      "pattern": "from teamagent.mcp_gateway.document_followup import reauthorize"
    }
  ]
}
```

上記 digest は説明用のプレースホルダーです。そのままでは検証で拒否されます。OC データは `name: "ocNN"`、`description`、`checks` の3欄で、`OLD_CORE` を持ちません。

### 確認の書式

共通の必須欄は `label`・`kind`・`path` です。`file_exists` 以外は空でない1行の `pattern` も必須です。

| kind | 内容 | 追加の欄 |
| --- | --- | --- |
| `grep_fixed` | 文字列をそのまま検索 | `absent`、`whole_line`、`count` |
| `grep_regex` | 正規表現で検索。既定は見本の通常の grep と同じ基本正規表現 | `dialect: "extended"` で `grep -E`。ほかは上と同じ |
| `file_exists` | ファイルの存在確認 | `absent: true` で存在しないことを確認。`pattern` は不要・指定不可 |
| `lock_version` | 下記の固定された版・整合性検査 | `format` と、その形式で必要な欄 |

- `path` はリポジトリ内の相対パスです。grep では配列も指定でき、複数パスのいずれかで一致するという見本の意味を保ちます。
- `absent: true` は一致・存在しないことの確認です。grep の読取エラーや不正な正規表現は成功扱いにしません。
- `whole_line: true` は行全体の一致、`count: 2` は一致する行がちょうど2行あることの確認です。どちらも単一ファイルを指定します。件数指定と `absent` は併用できません。
- OC の各確認には `phase` が必須です。`before_mcp` は現行 MCP の順序ゲートの前、`after_mcp` は後です。配列内はこの順に並べ、新しい通常の確認は既存の位置に合わせて追加します。
- `label` は見本の失敗時の説明を引き継いでいます。`CHECK OK` はその検査が通ったことを示し、失敗時はその説明を `FATAL` として表示します。

`lock_version` の形式（任意の jq 式・Python・シェル式は指定できません）:

| format | 必要な追加欄 | pattern の意味 |
| --- | --- | --- |
| `uv_exact` | `package` | uv.lock の該当パッケージが指定の版1件だけであること |
| `uv_adjacent` | `package`、任意の `whole_line` | 見本の `grep -A1` と同様、パッケージ名の直後の行で版を確認 |
| `apk_arg` | `package`、`arg_path` | Dockerfile の ARG 名。値が空でなく、lock に `package-値` があること |
| `json_value` | `field`（キーの配列） | JSON の指定値と一致する文字列 |
| `json_array_length` | `field`（キーの配列） | JSON 配列の件数。OC の例外0件の確認用 |
| `json_no_package_prefix` | なし | JSON の `exceptions` の package に、この接頭辞がないこと |

未知の kind・欄・形式、空の pattern、不正な正規表現、絶対パス、`..` を含むパス要素、特殊なパス、形式の違う OLD_CORE は、AWS 呼び出しや運用先への書き込みより先に拒否します。作業ツリー外へ出る symlink も拒否します。JSON の値は `source`／`eval`／`shell=True` に渡しません。

## ローカル検証

```bash
bash -n fire_and_authorize.sh
bash -n driver_oc.sh
# インストールされている場合
shellcheck fire_and_authorize.sh driver_oc.sh
python3 -m unittest discover -s tests -v
```

テストは見本を**テキストとして**読み、確認内容と件数を比較します。r76 は **199件**、oc34 は **71件**です。数え方は検査単位です。r76 の4パッケージをまとめた版検査は4件、ARG と lock の対応確認は1件、`&&` で連なる別の検索は個別に数えます。

一時ディレクトリの Git リポジトリで全検査の成功・失敗を確認し、不正データの拒否も検証します。発射前の部分だけを型から取り出したローカルのテスト用ハーネスでは、AWS をシェル関数に置き換え、補助スクリプトの起動も遮断します。元スクリプト・型全体・実際の build／authorize／TD 差し替えは起動しません。積み荷以外の後半の手順は見本とのテキスト一致で保護しています。

実AWS、実際の依存スクリプト、実機DMでのQAはこのテストの対象外です。証跡ファイルの既存の置き場所・名前、OC の雛形置換用 `OLD_OC`、既存の運用手順は見本から引き継いでいるため、並行実行は避けてください。
