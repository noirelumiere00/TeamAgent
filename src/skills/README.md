# skills/ — Skill 実装

TeamAgent v3.0 の **Skill Registry / Plugin パターン** に基づく Skill 実装。
新しい Skill は core を改修せず、ここに追加するだけで Registry に登録される。

## 初期 Skill セット（MVP）

| Skill | ディレクトリ | 役割 | Phase |
|---|---|---|---|
| 検索 Skill | `search/` | pgvector 横断検索（MVA 最初の Skill） | MVA P3 |
| Slack 自動サジェスト Skill | `suggest/` | 過去類似案件・社内ナレッジを引き出し | Phase 4-a |
| Mail ワークフロー Skill | `mail/` | 朝 push / 返信深掘り / 下書き生成 | Phase 4-b |
| 提案コンテンツ生成 Skill | `proposal/` | 5 フェーズの提案書ドラフト生成 | Phase 4-c |
| 動画ナレッジ分析 Skill | `video/` | Gemini 2.5 Flash + yt-dlp で構造分析 | Phase 4-d |
| 営業進捗サマリー Skill | `progress/` | マネージャー向け横串集約 | Phase 4-e |

## 拡張 Skill（順次追加可能）

- 高林プロンプト Skill（テンプレ穴埋め型）
- 議事録 Skill
- 商談ロープレ Skill
- Personal AI Coach Skill（機能⑥、Phase 5）
- 業務固有 Skill（営業 / 制作 / アカウント別）
- ユーザー定義 Skill（営業自身が登録可能）

## Skill の基本構造

```python
# src/teamagent/skills/base.py（実装予定）

from abc import ABC, abstractmethod
from typing import Any
from pydantic import BaseModel

class SkillSchema(BaseModel):
    name: str
    description: str
    input_schema: dict
    output_schema: dict
    permissions: list[str]
    tier_required: int  # 0-3

class BaseSkill(ABC):
    """すべての Skill が継承する基底クラス"""

    schema: SkillSchema

    @abstractmethod
    async def run(self, **kwargs) -> dict:
        """Skill のメインロジック。Pydantic で検証された入力を受ける"""
        ...

class SkillRegistry:
    """Skill のレジストリ。インポート時に自動登録される"""

    _skills: dict[str, type[BaseSkill]] = {}

    @classmethod
    def register(cls, skill: type[BaseSkill]) -> None:
        cls._skills[skill.schema.name] = skill

    @classmethod
    def get(cls, name: str) -> type[BaseSkill]:
        return cls._skills[name]

    @classmethod
    def list_all(cls) -> list[SkillSchema]:
        return [s.schema for s in cls._skills.values()]
```

詳細は `docs/v3.0/architecture_v3.0.html` の **Skill Registry 章**を参照。
