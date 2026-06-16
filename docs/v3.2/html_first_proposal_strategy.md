# HTML-first 資料生成 統合戦略（v0.1 / 2026-06-16）

## 北極星
**すべての営業向け資料（VSEO 分析・提案書・界隈マップ等）を「URL で配布 → ブラウザでノーコード編集 → 将来 PPTX 変換」の一本の規格に統一する。**

営業は「探す・テンプレを開く・PowerPoint で整える」をやめ、**署名URLを1クリックで開き、文字をその場で直して送る**だけにする。AI が下書きを置いた HTML が出発点。

## なぜ HTML-first か
- **配布が一瞬**: 非公開S3の署名URL（7日）を Slack に貼るだけ。ログイン不要・社外秘リスク最小。
- **ノーコード編集が無料で付く**: `contenteditable` の素HTMLなら、営業がブラウザで文字をクリック→直接編集できる（専用エディタ不要）。
- **PPTX は HTML から焼ける**: 16:9 固定スライドHTML → playwright スクショ → python-pptx で 1:1 変換可能（将来）。
- **1規格で全資料**: VSEO・提案書・界隈を別フォーマットで作らない。

## 現状資産（2026-06-16）
| 資産 | 場所 | HTML-first 度 |
|---|---|---|
| **編集可スライドHTML** | `src/teamagent/skills/video_algorithm/slides.py`（全テキスト `contenteditable`・16:9・1280×720） | ◎ 唯一の完成形 |
| **分析レポートHTML** | `src/teamagent/skills/video_algorithm/report.py`（縦長ダッシュボード・読取） | ○ |
| **汎用 publish** | `src/teamagent/adapters/report_publish.py`（`publish_file`/html/pptx/pdf・S3署名URL 7日） | ◎ 配布の共通基盤 |
| **proposal_deck** | `src/teamagent/skills/proposal_deck/`（python-pptx テンプレ直・PDF は weasyprint コンパニオン） | △ HTML 経由せず |
| **界隈 / tiktok-vseo（ローカルSkill）** | `~/.claude/skills/{kaiwai-community-proposal,tiktok-vseo-proposal}`（pptxgenjs） | ✗ JS 直生成・本番と規格別 |

## 段階計画
### Phase I — 共通基盤（無停止・本ラウンドで着手）
- 本戦略doc（これ）。
- **配布の統一**: `report_publish.publish_artifact(path, kind=...)`（kind→content_type/ext/prefix を1か所に集約）。video / proposal_deck が同一経路で配布できる足場。既存 `publish_*` は据置（後方互換）。
- **共通 HTML テーマ**: `src/teamagent/skills/_html/theme.py` に、重複しているフォントスタックと `contenteditable` 編集UX（hover/focus アウトライン・編集ヒント）を視覚中立な定数で集約。新規コードが import する形にし、**既存 slides/report の見た目は変えない**（採用は段階的）。

### Phase II — 編集→保存の往復（将来）
- `contenteditable` 編集は現状ブラウザ内のみ（永続化なし。保存は印刷→PDF か「AIに直して」）。
- 「編集後HTMLを受け取って S3 に保存し直す」エンドポイント（S3 presigned PUT or 小Lambda）を置き、**編集→保存→（将来）PPTX 焼き直し**の往復を成立させる。これが PPTX 統合の前提。

### Phase III — 各スキルを HTML-first へ寄せる（大規模・別計画）
- `proposal_deck`: python-pptx テンプレ → HTML 中間生成を経由（編集可HTML＋HTML→PPTX）へ。
- 界隈 / tiktok-vseo（pptxgenjs）: HTML 生成 → playwright → PPTX に置換。デザイン再合意が必要。
- 利用者フィードバック後に判断。

## スコープ外（今は触らない）
- PPTX 変換そのもの（HTML→PPTX 往復）＝Phase II/III。
- 非同期配信・恒久URL（CloudFront）＝必要時に別途。

## 関連
- `src/teamagent/skills/video_algorithm/{slides,report}.py` / `adapters/report_publish.py`
- `docs/aila_loop/VISION.md`（北極星：「書く前に書かれている」）
- 本ラウンドの変更計画: `~/.claude/plans/ok-abc-stateful-eclipse.md`
