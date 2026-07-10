"""メール下書きの「全返信宛先・スレッド文脈・署名」を組み立てる共有ヘルパー（純粋・テスト容易）。

morning_digest（朝の自動下書き）と mail_reply（メンション時の返信下書き）の両方から再利用する。
全て副作用なし（I/O は呼び出し側で済ませ、ここは整形のみ）。DLP は `scrub_value` を必ず通す。

死守ライン:
  - 全返信(Cc)に本人・主宛先・重複を入れない。Bcc は引き継がない（情報漏れ防止）。
  - スレッド履歴は LLM への「資料」として境界トークンで囲み、本文は scrub 済み。
  - 署名は LLM 生成物に「機械連結」する（LLM に書かせて改変されるのを防ぐ）。
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from teamagent.adapters.gmail_client import extract_plain_text, extract_thread_participants
from teamagent.identity import normalize_email
from teamagent.observability import scrub_value

logger = structlog.get_logger(__name__)


# ── env フラグ（既定挙動の kill-switch 用）─────────────────────────────────


def env_bool(name: str, default: bool) -> bool:
    """環境変数を bool として読む。未設定は default。"""
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    """環境変数を int として読む。未設定・不正は default。"""
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    try:
        return int(v.strip())
    except ValueError:
        return default


def env_str(name: str, default: str) -> str:
    """環境変数を str として読む。未設定・空は default。"""
    v = os.environ.get(name)
    return v if (v is not None and v.strip()) else default


# ── A. 全返信(Reply-All)の Cc 組み立て ─────────────────────────────────────


def build_cc(
    headers: dict[str, str],
    requester: str,
    to_addr: str,
    *,
    internal_only_cc: bool = False,
    company_domains: frozenset[str] | None = None,
    max_cc: int = 20,
) -> str | None:
    """元メールの From/To/Cc から「全返信の Cc」を組み立てる。

    - 本人(requester)・主宛先(to_addr)・重複(大小無視)は除外。
    - Bcc は引き継がない（自分宛 Bcc を burn しない）。
    - internal_only_cc=True かつ company_domains 既知なら、社外ドメインを Cc から除外。
    - 件数が max_cc を超えたら暴発防止で None（人手で追加する方が安全）。
    - 結果が空なら None（create_draft の cc=None 分岐に合わせる）。
    """
    req = normalize_email(requester) or (requester or "").strip().lower()
    to_norm = (to_addr or "").strip().lower()
    # From/To/Cc のみ（Bcc を除く）から participant を抽出（順序保持 dedup 済み）。
    participants = extract_thread_participants(
        {k: headers.get(k, "") for k in ("From", "To", "Cc")}
    )
    cc: list[str] = []
    for e in participants:
        ne = normalize_email(e)
        if ne is None or ne == req or ne == to_norm:
            continue
        if internal_only_cc and company_domains is not None:
            domain = ne.rsplit("@", 1)[-1]
            if domain not in company_domains:
                continue
        cc.append(e.strip())
    if not cc:
        return None
    if len(cc) > max_cc:
        logger.info("mail_cc_truncated", cc_total=len(cc), max_cc=max_cc)
        return None
    return ", ".join(cc)


# ── B1. スレッド履歴（これまでの経緯）の整形 ───────────────────────────────


_THREAD_MAX_MSGS_CEIL = 60
_THREAD_MAX_CHARS_CEIL = 40000
_THREAD_PER_MSG_CEIL = 4000


def build_thread_history(
    messages: list[Any],
    *,
    exclude_id: str | None,
    requester: str,
    max_msgs: int = 6,
    max_chars: int = 4000,
    per_msg_chars: int = 800,
) -> str:
    """スレッドの過去メッセージ群を「これまでの経緯」テキストに整形する。

    messages: GmailMessage 風（.id / .headers / .payload / .internal_date_ms）の list。
    返信対象(exclude_id)自身は除外。時系列（古→新）。直近 max_msgs 通・合計 max_chars 字まで。
    各メッセージは scrub 済み本文を境界で囲む（差出人は本人/先方のみ・生アドレス非表示）。
    max_msgs/max_chars/per_msg_chars は env ノブ由来でも暴走しないよう上限で丸める。
    """
    # コード側ハードキャップ（env 誤設定でのコスト暴発を防ぐ）。
    max_msgs = max(1, min(max_msgs, _THREAD_MAX_MSGS_CEIL))
    max_chars = max(1, min(max_chars, _THREAD_MAX_CHARS_CEIL))
    per_msg_chars = max(1, min(per_msg_chars, _THREAD_PER_MSG_CEIL))
    req = (requester or "").strip().lower()
    ordered = sorted(messages, key=lambda m: getattr(m, "internal_date_ms", 0) or 0)
    prior = [m for m in ordered if getattr(m, "id", None) != exclude_id]
    prior = prior[-max_msgs:]

    rendered: list[str] = []
    for m in prior:
        headers = getattr(m, "headers", {}) or {}
        frm = str(headers.get("From", "")).lower()
        who = "自分(営業)" if req and req in frm else "先方"
        body = extract_plain_text(getattr(m, "payload", {}) or {})
        # G6: scrub に加え境界トークン(<<< / >>>)を無害化し、本文が枠から脱出して
        # 指示位置に注入されるのを防ぐ（攻撃者制御テキストのため）。
        masked = (
            str(scrub_value(body))
            .strip()[:per_msg_chars]
            .replace("<<<", "‹‹‹")
            .replace(">>>", "›››")
        )
        if not masked:
            continue
        rendered.append(f"<<<HISTORY from={who}>>>\n{masked}\n<<<END>>>")
    if not rendered:
        return ""

    # 合計文字数の上限を「新しい方を優先」で適用（古いものから落とす）。
    kept: list[str] = []
    total = 0
    for block in reversed(rendered):
        if kept and total + len(block) > max_chars:
            break
        kept.append(block)
        total += len(block)
    kept.reverse()
    return "\n\n".join(kept)


# ── 一斉送信/自動配信の判定（個人返信不要 → 下書き対象外）──────────────────

_BULK_SALUTATIONS = (
    "各位",
    "ご担当者",
    "担当者様",
    "関係者各位",
    "お客様各位",
    "会員各位",
    "みなさま",
    "みなさん",
    "皆様",
    "皆さま",
    "皆さん",
    "ご利用者",
)


def is_mass_or_impersonal(headers: dict[str, str], body: str) -> bool:
    """個人宛でない一斉送信/自動配信メールか判定（True なら下書き対象外）。

    いずれか該当で True:
      - 一括配信ヘッダ（List-Unsubscribe / List-Id / Precedence: bulk|list / Auto-Submitted）
      - no-reply 系の差出人
      - 本文冒頭が一般宛名（各位 / ご担当者様 / みなさま 等）＝特定個人宛でない
    """
    if headers.get("List-Unsubscribe") or headers.get("List-Id"):
        return True
    if (headers.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk"):
        return True
    auto = (headers.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return True
    frm = (headers.get("From") or "").lower()
    if any(k in frm for k in ("noreply", "no-reply", "donotreply", "do-not-reply", "no_reply")):
        return True
    # 本文冒頭の一般宛名を検出。固定 120 字窓だと空行/画像/前置きが先頭にあると
    # 「各位」等が窓外に出て取りこぼす（＝マスメールを下書きしてしまう）。
    # 空行を除いた実コンテンツの先頭 6 行（最大 400 字）で判定する。
    meaningful = [ln for ln in (body or "").splitlines() if ln.strip()]
    head = "\n".join(meaningful[:6])[:400]
    return any(s in head for s in _BULK_SALUTATIONS)
