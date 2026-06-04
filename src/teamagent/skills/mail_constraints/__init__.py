"""mail_constraints Skill: 本人受信箱から案件の制約（NG/予算/期限/関係性）を抽出する。

⚠️ PII 機微: 生メール本文は LLM/ログ/戻り値に入れない（DLP マスク後の構造化制約のみ返す）。
   死守ライン（本人受信箱限定 / 同意 / readonly / クエリ限定 / 注入対策 / 監査）は
   docs/poc/phase6_mail_drive_design.md §4 を参照。
"""

from teamagent.skills.mail_constraints.schema import (
    MailConstraint,
    MailConstraintsInput,
    MailConstraintsOutput,
)
from teamagent.skills.mail_constraints.skill import (
    ConsentStore,
    EmailSetConsentStore,
    MailConstraintsSkill,
)

__all__ = [
    "ConsentStore",
    "EmailSetConsentStore",
    "MailConstraint",
    "MailConstraintsInput",
    "MailConstraintsOutput",
    "MailConstraintsSkill",
]
