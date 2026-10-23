"""レポートの PowerPoint（DeckSpec）を組む共通部品（mcp 側・python-pptx は使わない）。

- ``layouts``: 寸法・色・書体・字の大きさの唯一の正本（aico_report_v1）。
- ``spec``: DeckSpec の型（``teamagent.media.deck_contracts``）と、ページを組む道具。
- ``text``: 文の切れ目で切る・絵文字を落とす・言い方の検査（お土産 FMT と共用）。

描画は media worker 側の ``teamagent.media.deck_render`` が行う。
"""
