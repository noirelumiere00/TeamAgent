"""TeamAgent を spec-MCP サーバとして外部の自律オーケストレーター（OpenClaw 等）へ公開する境界層。

自律外殻はここ（MCP 境界）を越えて RDS / Secrets / Google に直接触れない。
RLS・per-user OAuth・fail-closed はすべて境界の内側（本パッケージ＋既存 adapters）で死守する。
"""
