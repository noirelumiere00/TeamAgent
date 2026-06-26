#!/usr/bin/env python3
"""OpenClaw 設定（openclaw.config.json5）の不変条件を ship 前/起動時に fail-loud で検査する。

2026-06-22 の事故対策（柱1）：`dmPolicy:"open"` でも OpenClaw slack plugin は
allowFrom に "*" が無いと列挙者のみ通す allowlist として gating する。焼き込み config が
この矛盾を抱えても誰も検知せず、非管理者DMが無音 drop した。本スクリプトでその設定矛盾を
**CI（PRをマージ前に落とす）**と **entrypoint（注入後の実効configを検査して fail-loud）**の
両方で機械的に弾く。

依存ゼロ（標準ライブラリのみ）。json5 の full parse はせず、検査に必要な
channels.slack.{dmPolicy, groupPolicy, allowFrom} だけを寛容に抽出する
（config は人手レビュー対象＝この限定抽出で十分かつ堅牢）。

使い方:
    python scripts/check_openclaw_config.py [path]      # 既定 infra/openclaw/openclaw.config.json5
    違反があれば stderr に理由を列挙し exit 1。OK なら実効値を1行出して exit 0。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_DEFAULT_PATH = "infra/openclaw/openclaw.config.json5"

_VALID_DM_POLICY = frozenset({"open", "allowlist", "pairing"})
_VALID_GROUP_POLICY = frozenset({"open", "allowlist"})


def _strip_comments(text: str) -> str:
    """json5 のコメントを除去（`://` を壊さないよう `//` の前が `:` の場合は残す）。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)  # ブロックコメント
    text = re.sub(r"(?<!:)//[^\n]*", "", text)  # 行コメント（http:// は温存）
    return text


def parse_slack_policy(text: str) -> dict[str, object]:
    """raw config テキストから channels.slack のポリシー3項目を抽出する。

    返り値: {"dmPolicy": str|None, "groupPolicy": str|None, "allowFrom": list[str]|None}
    （allowFrom: None=記述なし / []=空配列 / [...]=要素あり）
    """
    clean = _strip_comments(text)
    dm_m = re.search(r"dmPolicy\s*:\s*[\"'](\w+)[\"']", clean)
    grp_m = re.search(r"groupPolicy\s*:\s*[\"'](\w+)[\"']", clean)
    af_m = re.search(r"allowFrom\s*:\s*\[(.*?)\]", clean, flags=re.DOTALL)
    allow_from: list[str] | None = None
    if af_m is not None:
        allow_from = re.findall(r"[\"']([^\"']*)[\"']", af_m.group(1))
    return {
        "dmPolicy": dm_m.group(1) if dm_m else None,
        "groupPolicy": grp_m.group(1) if grp_m else None,
        "allowFrom": allow_from,
    }


def check_slack_policy(policy: dict[str, object]) -> list[str]:
    """不変条件違反の理由リストを返す（空＝OK）。純関数・テスト容易。"""
    errors: list[str] = []
    dm = policy.get("dmPolicy")
    grp = policy.get("groupPolicy")
    allow_from = policy.get("allowFrom")
    allow_list: list[str] = allow_from if isinstance(allow_from, list) else []

    if dm is None:
        errors.append("channels.slack.dmPolicy が見つからない（抽出失敗 or 未設定）")
    elif dm not in _VALID_DM_POLICY:
        errors.append(f'dmPolicy 不正値: "{dm}"（許容: {sorted(_VALID_DM_POLICY)}）')

    if grp is not None and grp not in _VALID_GROUP_POLICY:
        errors.append(f'groupPolicy 不正値: "{grp}"（許容: {sorted(_VALID_GROUP_POLICY)}）')

    if isinstance(allow_from, list) and len(allow_from) == 0:
        errors.append('allowFrom が空配列 []＝全拒否（footgun・必ず値か "*" を入れる）')

    # 今回の事故の不変条件：open は allowFrom に "*" が無いと実は allowlist として gating する。
    if dm == "open" and "*" not in allow_list:
        errors.append(
            'dmPolicy:"open" なのに allowFrom に "*" が無い'
            "＝OpenClaw は列挙者のみ通す allowlist として無音 gating する"
            '（2026-06-22 の事故クラス）。全社内開放なら allowFrom=["*"] が必須。'
        )

    return errors


def main(path: str) -> int:
    p = Path(path)
    if not p.exists():
        print(f"[check_openclaw_config] config が見つからない: {path}", file=sys.stderr)
        return 2
    policy = parse_slack_policy(p.read_text(encoding="utf-8"))
    errors = check_slack_policy(policy)
    if errors:
        print(f"[check_openclaw_config] ❌ 不変条件違反 ({path}):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(
        "[check_openclaw_config] ✅ OK "
        f"dmPolicy={policy['dmPolicy']} groupPolicy={policy['groupPolicy']} "
        f"allowFrom={policy['allowFrom']}"
    )
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_PATH
    sys.exit(main(target))
