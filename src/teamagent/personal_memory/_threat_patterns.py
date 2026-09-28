"""personal_memory の脅威文字列を検出する正規表現。"""

from __future__ import annotations

import re
from typing import Final

# 呼び出し側で NFKC 正規化した文字列に対して使う。英語・ロールタグは、表記の
# 大文字小文字を変えた回避も同じ脅威として扱う。
INJECTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\bignore\s+(?:all\s+)?"
        r"(?:previous|prior|above)\s+(?:instructions|prompts)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bdisregard\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"\bact\s+as\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"\bdeveloper\s+mode\b", re.IGNORECASE),
    re.compile(r"\bjailbreak\b", re.IGNORECASE),
    re.compile(r"\bdo\s+anything\s+now\b", re.IGNORECASE),
    re.compile(
        r"(?:以前|前|上記|これまで|今まで)\s*の\s*"
        r"(?:指示|命令|設定|ルール)\s*を\s*(?:無視|忘れ)"
    ),
    re.compile(r"システムプロンプト"),
    re.compile(r"あなたは今から"),
    re.compile(r"として振る舞"),
    re.compile(r"開発者モード"),
    re.compile(r"制限を解除"),
    re.compile(r"(?:<system>|</system>|<assistant>)", re.IGNORECASE),
    re.compile(r"\[INST\]", re.IGNORECASE),
    re.compile(r"###\s*(?:system|instruction)\b", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
)

# 秘密情報は発行元が定める接頭辞の大小文字を維持して検出する。
SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"xox[abprs]-"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN"),
)

__all__ = ["INJECTION_PATTERNS", "SECRET_PATTERNS"]
