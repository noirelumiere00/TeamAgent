"""お土産資料 submit の二重受付をはじく（同じ利用者・同じ内容の依頼は 1 本にまとめる）。

背景（2026-10-06 本番）: 利用者の 1 回の依頼に対し、モデルが 1 回の返事の中で
omiyage_report_submit を 2 回呼び（1 秒差・別 request_id）、同じ内容のジョブが 2 本
走って PPTX が 2 通届いた（約 40 分×2 の費用）。

仕組み:
  - 判定キー = 利用者 + 正規化した入力（ブランド・競合の集合・一般KWの集合・検索軸の集合）。
    NFKC・空白圧縮・大文字小文字の同一視・ソートで、並び順や全角半角の差では別物にしない。
    ハッシュ化して錠の ID にする（台帳に入力の平文を増やさない）。
  - 錠は ProposalJobStore の条件付き書込（DynamoDB の条件付き PutItem）で取る。
    同時に 2 本来ても、同じキーへの条件付き書込は直列に評価されるので勝つのは 1 本だけ。
    負けた側は錠を読み直して勝った側の job_id を返す。
  - 錠が指すジョブを再利用する条件:
      queued / running（心拍が新しい）… 受付からの経過に関係なく再利用（まだ終わっていない）
      done … 受付から N 分以内なら再利用
      failed・心拍切れ・見つからない（受付直後の書込中を除く）… 再利用しない（錠を奪って新規）
  - 判定や錠の読み書きで予期しない例外が出たら、重複判定をあきらめて通常どおり受け付ける
    （二重受付の防止より、受付そのものを止めないことを優先する）。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from teamagent.adapters.proposal_job_store import DEDUP_LOCK_PREFIX, ProposalJobStore

_KEY_VERSION = 1
_LOCK_ID_PREFIX = f"{DEDUP_LOCK_PREFIX}omy_"
# 錠を取ってからジョブ行を書くまでの間（ミリ秒〜秒）に来た 2 本目は、ジョブ行がまだ無い。
# この猶予の間は「受付中」とみなして再利用する。過ぎても無ければ書込に失敗した錠として奪う。
_MISSING_JOB_GRACE_SECONDS = 120
# 錠の取り合いに負け続けたときの読み直し回数（越えたら重複判定をあきらめて通す）。
_MAX_CLAIM_ATTEMPTS = 4


def normalize_text(value: str) -> str:
    """判定キー用の正規化（NFKC・空白圧縮・大文字小文字の同一視）。"""

    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def _normalized_set(values: Iterable[str]) -> list[str]:
    return sorted({text for text in (normalize_text(v) for v in values) if text})


def dedup_lock_id(
    *,
    user_id: str,
    brand: str,
    competitors: Iterable[str],
    keywords: Iterable[str],
    axes: Iterable[tuple[str, str]],
) -> str:
    """利用者＋正規化した入力から錠の ID を作る（入力の平文は残さない）。"""

    payload = {
        "v": _KEY_VERSION,
        "user": user_id,
        "brand": normalize_text(brand),
        "competitors": _normalized_set(competitors),
        "keywords": _normalized_set(keywords),
        "axes": sorted(
            {(role, normalize_text(query)) for role, query in axes if normalize_text(query)}
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{_LOCK_ID_PREFIX}{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class DedupOutcome:
    """錠の取得結果。

    ``duplicate_of`` が入っていれば重複（新しいジョブを作らずその job_id を返す）。
    ``lock_id`` が入っていれば錠を取れた（ジョブを渡せなかったら ``release`` で返す）。
    どちらも空なら重複判定をしなかった（無効・利用者不明・判定失敗）。
    """

    lock_id: str = ""
    duplicate_of: str = ""
    duplicate_status: str = ""


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class OmiyageSubmitDedup:
    """同じ依頼の二重受付をはじく錠の取得・判定・返却。"""

    def __init__(
        self,
        store: ProposalJobStore,
        *,
        window_seconds: int,
        stale_seconds: int,
        job_kind: str,
        clock: Callable[[], datetime],
        missing_job_grace_seconds: int = _MISSING_JOB_GRACE_SECONDS,
    ) -> None:
        self._store = store
        self._window_seconds = max(0, window_seconds)
        self._stale_seconds = max(1, stale_seconds)
        self._job_kind = job_kind
        self._clock = clock
        self._missing_job_grace_seconds = max(0, missing_job_grace_seconds)

    @property
    def enabled(self) -> bool:
        return self._window_seconds > 0

    def claim(self, lock_id: str, new_job_id: str, log: Any) -> DedupOutcome:
        """錠を取る。重複なら既存の job_id を返す。判定できなければ素通し（例外を出さない）。"""

        if not self.enabled:
            return DedupOutcome()
        try:
            for _ in range(_MAX_CLAIM_ATTEMPTS):
                lock = self._store.get_dedup_lock(lock_id)
                expected: str | None = None
                if lock is not None:
                    target = str(lock.get("target_job_id") or "")
                    status = self._reusable_status(lock, target)
                    if status:
                        return DedupOutcome(duplicate_of=target, duplicate_status=status)
                    expected = target
                if self._store.put_dedup_lock(lock_id, new_job_id, expected_target=expected):
                    return DedupOutcome(lock_id=lock_id)
                # 取り合いに負けた（同時に来た別の依頼が先に書いた）→ 読み直して判定し直す。
            log.warning("omiyage_report_dedup_contended", attempts=_MAX_CLAIM_ATTEMPTS)
        except Exception as exc:
            log.warning("omiyage_report_dedup_failed", error_type=type(exc).__name__)
        return DedupOutcome()

    def release(self, lock_id: str, job_id: str, log: Any) -> None:
        """ジョブを渡せなかった錠を返す（その job を指しているときだけ・例外を出さない）。"""

        if not lock_id:
            return
        try:
            self._store.put_dedup_lock(lock_id, "", expected_target=job_id)
        except Exception as exc:
            log.warning("omiyage_report_dedup_release_failed", error_type=type(exc).__name__)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(UTC)

    def _reusable_status(self, lock: dict[str, Any], target: str) -> str:
        """錠が指すジョブを再利用してよければその status、だめなら ""。"""

        if not target:
            return ""  # 返却済みの錠
        now = self._now()
        claimed_at = _parse_timestamp(lock.get("claimed_at"))
        age = (now - claimed_at).total_seconds() if claimed_at is not None else None
        row = self._store.get_job(target)
        if row is None:
            # 錠を取った直後でジョブ行の書込がまだ（同時に来た 2 本目）なら受付中とみなす。
            if age is not None and age <= self._missing_job_grace_seconds:
                return "queued"
            return ""
        if not self._is_own_kind(row):
            return ""
        status = row.get("status")
        if status in ("queued", "running"):
            updated_at = _parse_timestamp(row.get("updated_at"))
            if updated_at is None or (now - updated_at).total_seconds() > self._stale_seconds:
                return ""  # 心拍切れ（再起動で止まったジョブ）は待たせない
            return str(status)
        if status == "done" and age is not None and age <= self._window_seconds:
            return "done"
        return ""

    def _is_own_kind(self, row: dict[str, Any]) -> bool:
        raw = row.get("request_summary")
        if not isinstance(raw, str):
            return False
        try:
            summary = json.loads(raw)
        except ValueError:
            return False
        return isinstance(summary, dict) and summary.get("kind") == self._job_kind


__all__ = ["DedupOutcome", "OmiyageSubmitDedup", "dedup_lock_id", "normalize_text"]
