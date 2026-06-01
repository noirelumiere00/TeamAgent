"""ラッコキーワード 検索量スクレイパ (VSEO 自動化用)。

CLAUDE.md 6-bis Adapter 層。Skill からはこのモジュールの fetch_search_volumes() のみ使う。

実体は Node.js (puppeteer-core) スクリプト `tools/rakko_scraper/scrape.mjs`。
ラッコキーワード (月660円の有料サブスク) の関連KW結果から月間検索数/SEO難易度/CPC を取得する。
公式 API は高額プランのみのため、既存課金アカウントの**ログイン済みブラウザセッション**
(userDataDir に永続化した cookie) を再利用してスクレイピングする。

認証情報 (ID/パスワード) はこのコードでは一切扱わない。初回のみユーザーが
`node tools/rakko_scraper/scrape.mjs --login` で手動ログインし、以降はそのセッションを使う。

前提:
- `node` が PATH にあること (TIKTOK_NODE_BIN で上書き可、TikTok と共用)。
- `tools/rakko_scraper/` で `npm install` 済み。
- 初回ログイン済み (.userdata/ が存在)。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRAPER_DIR = _REPO_ROOT / "tools" / "rakko_scraper"
_SCRAPER_SCRIPT = _SCRAPER_DIR / "scrape.mjs"
_USERDATA_DIR = _SCRAPER_DIR / ".userdata"

_DEFAULT_TIMEOUT_S = 180  # 複数KW × 6秒待機 + ナビゲートで長めに


class RakkoScrapeError(RuntimeError):
    """ラッコスクレイピング失敗。呼び出し側がユーザー向け案内に変換する。"""


def _to_int(s: str) -> int | None:
    """「110,000」「9,900」「-」「」→ int or None。"""
    t = (s or "").replace(",", "").replace("位", "").strip()
    if not t or t in ("-", "—"):
        return None
    try:
        return int(float(t))
    except ValueError:
        return None


def _to_float(s: str) -> float | None:
    """「$0.24」「0.16」「-」→ float or None。"""
    t = (s or "").replace("$", "").replace(",", "").strip()
    if not t or t in ("-", "—"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


@dataclass(frozen=True)
class RakkoKeyword:
    """ラッコの関連KW 1 件。"""

    kw: str
    volume: int | None  # 月間検索数
    seo: int | None  # SEO 難易度
    cpc: float | None  # CPC ($)


@dataclass(frozen=True)
class RakkoResult:
    """fetch_search_volumes の返り値。"""

    # 検索KW → その関連KWリスト
    by_query: dict[str, list[RakkoKeyword]]

    @property
    def total_keywords(self) -> int:
        return sum(len(v) for v in self.by_query.values())


def _node_bin() -> str:
    explicit = os.environ.get("TIKTOK_NODE_BIN")  # TikTok と共用
    if explicit:
        return explicit
    found = shutil.which("node")
    if not found:
        raise RakkoScrapeError(
            "RAKKO_NODE_UNAVAILABLE: node が見つかりません。Node.js をインストールするか "
            "TIKTOK_NODE_BIN を設定してください"
        )
    return found


def is_logged_in() -> bool:
    """ログイン済みセッション (.userdata/) が存在するか。"""
    return _USERDATA_DIR.exists() and any(_USERDATA_DIR.iterdir())


def fetch_search_volumes(
    queries: list[str],
    *,
    limit: int = 30,
    request_id: str | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> RakkoResult:
    """複数KWの月間検索数/SEO難易度/CPC をラッコから取得する。

    Args:
        queries: 検索KWリスト。
        limit: 各KWで取得する関連KWの上限。
        request_id: トレース ID。
        timeout_s: subprocess の最大待ち秒。

    Raises:
        RakkoScrapeError: node 不在 / 未ログイン / タイムアウト / 取得失敗 等。
    """
    if not queries:
        raise RakkoScrapeError("RAKKO_EMPTY_QUERY: 検索KWが空です")
    if not _SCRAPER_SCRIPT.exists():
        raise RakkoScrapeError(
            f"RAKKO_SCRAPER_MISSING: {_SCRAPER_SCRIPT} がありません。"
            "tools/rakko_scraper で npm install を実行してください"
        )
    if not is_logged_in():
        raise RakkoScrapeError(
            "RAKKO_NOT_LOGGED_IN: 未ログインです。先に "
            "`node tools/rakko_scraper/scrape.mjs --login` で手動ログインしてください"
        )

    node = _node_bin()
    cmd = [
        node,
        str(_SCRAPER_SCRIPT),
        "--queries",
        ",".join(queries),
        "--limit",
        str(limit),
    ]
    logger.info("rakko_fetch_start", request_id=request_id, n_queries=len(queries), limit=limit)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_SCRAPER_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        logger.warning("rakko_timeout", request_id=request_id, timeout_s=timeout_s)
        raise RakkoScrapeError(
            f"RAKKO_TIMEOUT: 取得が {timeout_s}s 以内に終わりませんでした"
        ) from e

    stdout = (proc.stdout or "").strip()
    if not stdout:
        logger.warning(
            "rakko_no_output",
            request_id=request_id,
            returncode=proc.returncode,
            stderr_tail=(proc.stderr or "")[-300:],
        )
        raise RakkoScrapeError(
            f"RAKKO_NO_OUTPUT: スクレイパが結果を返しませんでした (exit={proc.returncode})"
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        logger.warning("rakko_bad_json", request_id=request_id, head=stdout[:200])
        raise RakkoScrapeError("RAKKO_BAD_JSON: スクレイパ出力を解析できませんでした") from e

    if not payload.get("ok"):
        err = payload.get("error") or "不明なエラー"
        logger.info("rakko_empty", request_id=request_id, error=err)
        raise RakkoScrapeError(f"RAKKO_EMPTY_RESULT: {err}")

    by_query: dict[str, list[RakkoKeyword]] = {}
    for q, rows in (payload.get("results") or {}).items():
        by_query[q] = [
            RakkoKeyword(
                kw=r.get("kw", ""),
                volume=_to_int(r.get("vol", "")),
                seo=_to_int(r.get("seo", "")),
                cpc=_to_float(r.get("cpc", "")),
            )
            for r in rows
        ]

    result = RakkoResult(by_query=by_query)
    logger.info("rakko_fetch_done", request_id=request_id, total_keywords=result.total_keywords)
    return result
