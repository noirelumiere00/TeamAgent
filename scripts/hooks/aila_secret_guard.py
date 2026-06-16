#!/usr/bin/env python3
"""AiLa pre-commit secret / forbidden-file ガード（RULES.md §1.2 Q5 / SHOGO_ACTIONS action 5）。

pre-commit からステージ済みファイル名を argv で受け取り、
  (1) ファイル名が *.env / service-account.json / vertex_sa.json → ステージ拒否（exit 1）
  (2) ファイル内容に secret regex（xox*/AKIA*/ASIA*/ya29.*/eyJ*.*.*/BEGIN PRIVATE KEY）→ 拒否
1 件でも該当したら exit 1。値そのものは出力しない（行番号と種別のみ）。

full-shape regex を使い、ドキュメント中の「パターン例」には誤反応しないようにする。
さらに docs/ と本フック自身・設定ファイルは内容スキャン対象から除外（gitleaks が別途カバー）。

stdlib のみ（CI/ローカルどちらでも追加依存なしで動く）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# full-shape（プレフィックスだけの doc 例にはマッチしない長さ要件つき）
SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"xox[abprs]-[0-9A-Za-z]{8,}-[0-9A-Za-z-]{8,}"),  # Slack token
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(r"ASIA[0-9A-Z]{16}"),  # AWS temp key
    re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),  # Google OAuth token
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # private key
]

FORBIDDEN_NAMES = {"service-account.json", "vertex_sa.json"}

# 内容スキャンを除外するパス接頭辞（doc の regex 例 / 設定 / フック自身）
SCAN_EXCLUDE_PREFIXES = (
    "docs/",
    ".pre-commit-config.yaml",
    ".gitleaks.toml",
    "scripts/hooks/",
)


def is_forbidden_name(path: str) -> bool:
    name = Path(path).name
    if name in FORBIDDEN_NAMES:
        return True
    # *.env / *.env.<suffix>（.env, .env.production など）
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


def should_scan(path: str) -> bool:
    return not any(path.startswith(p) for p in SCAN_EXCLUDE_PREFIXES)


def main(argv: list[str]) -> int:
    violations: list[str] = []
    for path in argv:
        if is_forbidden_name(path):
            violations.append(
                f"{path}: 禁止ファイル（*.env / service-account.json / vertex_sa.json）のステージ拒否"
            )
            continue
        if not should_scan(path):
            continue
        p = Path(path)
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for rx in SECRET_PATTERNS:
                if rx.search(line):
                    label = rx.pattern.split("[")[0].split("\\")[0][:14]
                    violations.append(
                        f"{path}:{lineno}: secret らしき文字列を検出（種別 ~ {label}…）"
                    )
                    break
    if violations:
        print("❌ AiLa secret guard: コミットを拒否しました（RULES.md §1.2）", file=sys.stderr)
        for v in violations:
            print(f"   - {v}", file=sys.stderr)
        print("   → 値を除去し、シークレットは AWS Secrets Manager に置くこと。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
