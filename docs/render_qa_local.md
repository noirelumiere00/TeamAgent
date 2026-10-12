# PPTX 描画 QA（手元・CI、P1）

`render_qa.py` は消毒・色付け・文字座標判定、`render_qa_child.py` は隔離実行、
`scripts/deck_render_qa.py` は手元の Docker を呼ぶ入口です。
本番の操作型・dispatcher・release 契約には接続していません。

## 代用書体のビルド

Microsoft の書体・数値表は入力しない。font-builder 段で fonttools
（`render-qa-font-build` グループ）を使い、Noto Sans CJK の Regular/Bold TTC
から JP face を選ぶ。Noto 自身の GPOS `palt` の XPlacement を字形の平行移動と
hmtx の lsb に、XAdvance を hmtx の advance に加算して焼き込む。
YPlacement/YAdvance は使わず、hhea・OS/2 の縦値（行の高さ）は Noto のまま保持する。

FeatureList の palt のうち DFLT/hani/kana の JAN LangSys を優先し、なければ
既定 LangSys、さらに該当がなければ FeatureList の palt を使う。
lookup type 1（SinglePos、Format 1/2）と type 9（Extension）内の SinglePos に対応する。
同一 lookup 内は最初に一致した subtable、別 lookup の値は加算する。
焼き込み後は palt feature を除去して LangSys の feature index を再割当てし、
他 feature が共有し得る lookup は保持する。palt が空・未検出、または未対応の
lookup 形式ならビルドを失敗させる。素の Noto へ黙って切り替えない。

family は `TA Palt Gothic`、style は Regular/Bold、PS 名は
`TAPaltGothic-Regular` / `TAPaltGothic-Bold`。OFL の予約名を新しい名前に使わず、
Noto のライセンス表示を保持する。両 style の成功後だけ `palt.available` を作る。
BuildKit secret は使わない。

## ビルドと起動（この作業では実行しない）

apk lock の TODO を正確な版に置き換え、ベースと uv の digest を固定する。
依存は `uv.lock` と `--frozen` を使う。TODO が残るとビルドは停止する。

```sh
docker build -f infra/docker/Dockerfile.teamagent-render-qa \
  --build-arg ALPINE_DIGEST=@sha256:<固定したdigest> \
  --build-arg UV_IMAGE=ghcr.io/astral-sh/uv:<固定した版>@sha256:<固定したdigest> \
  -t teamagent-render-qa:local .

.venv/bin/python scripts/deck_render_qa.py /path/to/deck.pptx \
  --profile proposal_builder_v1 --json --out .local_out/render-qa/run-001
```

Yu Gothic UI / Yu Gothic / 游ゴシック / 游ゴシック体を TA Palt Gothic に置換し、
なければ fontconfig で Noto Sans CJK JP にフォールバックする。
Arial / Helvetica Neue は Liberation Sans に置換する。
実行時は marker の有無で `font_substitute: palt` / `plain_noto` を返す。
plain_noto は warn 境界を 0.2 行から 0.4 行に広げ、error 境界は 0.5 行を維持する。
plain_noto の結果は `ok` にしない。書体一式の SHA は実行時に算出する。
PowerPoint とのずれは P2 で実測し、warn の幅を校正する。

CLI は既存イメージのみ使用し、pull/build はしない。
ネットワークなし・rootfs 読取専用・cap 全除去・新規権限禁止・4GB/256 PID。
書込みは `/tmp` と、その下に mount する専用一時出力だけ。
出力先には `render_qa.json`、`contact_sheet.png`、最大10枚の `slide-NNN.png` を出す。
出力先は実行ごとに分ける。終了コードは error=1、unavailable=2、それ以外=0。

元の PDF とは **消毒後・色付け前** の PPTX の描画結果を指す。
外部参照、OLE、ActiveX、マクロ、埋め込み書体、音声・動画、PNG/JPEG 以外の画像を除去し、
残る bitmap はメタデータなしで再生成する。
PDF、色付け PPTX、本文を含む抽出データは一時ジョブ内だけに置く。
結果 JSON は本文や文字列を含まず、図形名は40字以内、数値座標と種類だけを返す。

## 校正・残る制限

- `render_qa_baseline_proposal_builder_v1.json` は未校正の空一覧。
  本物のテンプレを PowerPoint と LibreOffice で比較してから、数値と shape key の
  exact match に限って除外項目を追加する。図形名だけの一括除外はしない。
- LibreOffice の設定キー、書体置換の動作、実 PDF の色保持は実イメージで検証が必要。
  Java は導入しない。マクロ禁止・リンク更新禁止の設定は XCU にある。
- 回転は対象外として件数のみ。画像との意図した重なりも warn になる。
  literal の白四角と欠落字形、改行・禁則・カーニングの差は校正で確認する。
- baseline は左上原点の pt に正規化。最終行 +0.25em を下端とし、
  行数は baseline の y を束ねて数える。色の許容差は各チャネル3、間隔は8。
- Mac に直接 LibreOffice を導入して実行する用途ではなく、Linux コンテナ用。
  macOS は有限の RLIMIT_AS を拒否し、NPROC はホスト全体の利用者プロセス数に影響する。
  テストでは両者の実適用を Mac だけ分離し、Linux 用の全設定値を別に検証する。
- 変換の180秒上限は元・probe の2ファイルを一緒に変換する1呼出しに適用する。
  全体480秒、CPU300秒、AS3GiB、file1GiB、FD1024、NPROC256、core dump 禁止。

通常 CI に pdfminer を導入しないため、抽出器の合成 PDF テストだけは未導入時に skip。
純粋判定・消毒・色付け・サムネ・Docker 契約は常時検証する。
合成デッキ・座標・PDF・書体のみで CI テストを行う。本物の資料は repo に置かない。
