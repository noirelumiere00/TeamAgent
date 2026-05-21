# src/ — 実装コード

TeamAgent v3.0 の Python 実装コードを配置するルート。

## ディレクトリ構成

```
src/
├── agent/         # Claude Agent SDK の Orchestrator ループ
├── connectors/    # Gmail / Drive / Slack 連携モジュール
├── pgvector/      # データ層（DB セッション / Embedding / 検索）
├── skills/        # Skill Registry と各 Skill 実装
│   ├── search/    # MVA で最初に実装する超軽量検索 Skill
│   └── ...        # Phase 4 以降の Skill が追加される
└── tools/         # MCP / Tools（外部 API 連携）
```

## パッケージ規約

- ライブラリの import は `pyproject.toml` に明記してから使う
- すべてのモジュールに type annotation を付ける（mypy strict）
- Skill は `skills/base.py` の `BaseSkill` を継承し、`SkillRegistry` に登録する形式

## Skill Registry の使い方（実装初期の方針）

```python
from teamagent.skills.base import BaseSkill, SkillRegistry

class SearchSkill(BaseSkill):
    name = "search"
    description = "pgvector から関連データを検索する"

    async def run(self, query: str) -> dict:
        # 実装
        ...

# 登録
SkillRegistry.register(SearchSkill)
```

詳細は `docs/v3.0/architecture_v3.0.html` の Skill Registry 章を参照。
