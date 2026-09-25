"""Aico の DM 本人メモ v1 で使う Hermes「覚える係」ランナー。

設計: docs/architecture/hermes_migration_design.md §10b。

MCP からだけ呼ばれ、MCP・RDS・Slack・既存の鍵には一切触れない。
Hermes 本体は子プロセスの中でだけ import する。
"""
