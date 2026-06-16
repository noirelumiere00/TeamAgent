"""HTML-first 資料生成の共通テーマ定数（html_first_proposal_strategy.md Phase I）。

重複していたフォントスタックと contenteditable 編集UX を 1 か所に集約する。視覚中立な定数で、
**新規コードが import して使う**前提（既存 slides.py / report.py は変えない＝採用は段階的）。
"""

from __future__ import annotations

# 日本語向け共通フォントスタック（apple-system → Hiragino → Meiryo → Noto → system）。
FONT_STACK_JP = (
    "-apple-system,'Hiragino Kaku Gothic ProN','Hiragino Sans',"
    "Meiryo,'Noto Sans JP',system-ui,sans-serif"
)

# contenteditable のホバー/フォーカス可視化（営業に「ここ編集できる」と伝える）。
# slides.py の編集UX と同等。--accent はホスト側 CSS 変数を流用（無ければフォールバック色）。
CONTENTEDITABLE_CSS = (
    "[contenteditable]:hover{outline:2px dashed #c7ccd3;outline-offset:3px;border-radius:3px}"
    "[contenteditable]:focus{outline:2px solid var(--accent,#e8362f);"
    "outline-offset:3px;border-radius:3px}"
)

# 編集ヒント（資料上部に出す一言・data-noexport で将来の PPTX 化時に除外可能）。
EDIT_TIP_HTML = (
    '<div class="edit-tip" data-noexport>✎ 文字をクリックして直接編集できます'
    "（保存はブラウザの印刷→PDF / または担当AIに「ここ直して」）</div>"
)

__all__ = ["CONTENTEDITABLE_CSS", "EDIT_TIP_HTML", "FONT_STACK_JP"]
