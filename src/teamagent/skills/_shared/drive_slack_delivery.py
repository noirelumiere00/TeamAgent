"""Drive の実ファイルを Slack へ添付する共通部品（配信先の決定はここ 1 か所）。

knowledge_deliver（検索 → 実ファイル配信）と clientkarte（カルテ → 関連資料の同梱）が
**同じ流儀**で添付するために、元 knowledge_deliver/skill.py にあった部品をここへ移した。

配信先の決定ルール（勝手に広げない）:
  1. ``channel_id`` があればそのチャンネル / スレッドへ添付（聞かれた場所で完結）
  2. そこが 0 件 or ``channel_id`` 無しなら、依頼者本人の DM へフォールバック
  3. どちらも取れなければ添付しない（呼び出し側は本文だけを返す＝fail-open）

``SlackClient.upload_file`` は例外を握って ``False`` を返す（権限不足・チャンネル未参加・
サイズ超過が全部 False になる）ため、成功判定は必ず戻り値の bool を見る。
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

_DRIVE_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)")
# Drive の file_id そのものの形。``gdrive://`` 接頭辞の残りは正規表現抽出を経ないため、
# ここで明示的に強制する。強制しないと ``gdrive://../x`` のような source_uri が
# ``prepare_drive_files`` の ``Path(tmpdir)/file_id/filename`` でパス外へ出得る
# （実データは Drive の file_id なので現実には発生しないが、
# 「file_id は [A-Za-z0-9_-] のみでパスとして安全」というコメントの前提を実際に成立させる）。
_DRIVE_ID_ONLY_RE = re.compile(r"[A-Za-z0-9_-]+")
_DRIVE_QUERY_ID_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]+)")
# 解決済み URL から file_id を取るのは「アップロード実体ファイル」の形だけに限定する。
# ナレッジシート行の自リンク（docs.google.com/spreadsheets/...）や
# Google ネイティブ文書（docs.google.com/document|presentation/...）を誤って候補化しない
# （後者は download_file_bytes が get_media 専用で export 未対応のため配信不能）。
_DRIVE_BINARY_FILE_URL_RE = re.compile(r"https://drive\.google\.com/file/d/([A-Za-z0-9_-]+)")

# (file_id, local_path, filename)
PreparedFile = tuple[str, str, str]


def extract_drive_binary_file_id(url: str | None) -> str | None:
    """resolved URL（http(s)）からアップロード実体ファイルの file_id だけを取り出す。"""
    if not url:
        return None
    m = _DRIVE_BINARY_FILE_URL_RE.search(url.strip())
    return m.group(1) if m else None


def extract_drive_file_id(source_uri: str | None) -> str | None:
    """source_uri（`gdrive://FILE_ID` or Drive web リンク）から file_id を取り出す。"""
    if not source_uri:
        return None
    s = source_uri.strip()
    if s.startswith("gdrive://"):
        fid = s[len("gdrive://") :].strip().strip("/")
        return fid if fid and _DRIVE_ID_ONLY_RE.fullmatch(fid) else None
    m = _DRIVE_ID_RE.search(s)
    if m:
        return m.group(1)
    m2 = _DRIVE_QUERY_ID_RE.search(s)
    if m2:
        return m2.group(1)
    return None


def safe_filename(name: str | None, *, fallback: str = "document") -> str:
    """添付ファイル名として安全な形に落とす（パス区切り・制御文字を潰し 120 字で切る）。"""
    base = (name or "").strip() or fallback
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", base)
    return base[:120] or fallback


def prepare_drive_files(
    gdrive: Any,
    candidates: list[tuple[str, str]],
    *,
    request_id: str,
    log: Any,
    log_prefix: str,
    tmp_prefix: str,
    max_bytes: int | None = None,
) -> tuple[str | None, list[PreparedFile]]:
    """候補 ``(file_id, filename)`` を Drive から取得して一時ファイル化する。

    返り値は ``(tmpdir, prepared)``。1 件も候補が無ければ ``(None, [])``。
    個々の DL / 書き込み失敗はその 1 件だけを落として続行する（fail-open）。
    ``max_bytes`` を渡すとそれを超えるファイルは 1 件だけ落として続行する
    （既定 None＝上限なし＝従来挙動）。

    ``max_bytes`` は **``download_file_bytes`` へ素通しする**（2026-08-19 レビュー M5）。
    渡さないと adapter 既定の 256MB まで実際に受信してから捨てることになり、帯域・
    レイテンシ・メモリのどれも守れない（clientkarte は常時経路なので毎回 256MB を
    掴み得た）。素通しすれば ``_BoundedBytesIO`` が buffer 拡張前に中断する。
    受信後の ``len(data) > max_bytes`` 判定は、``max_bytes`` を解さない DI / 旧 adapter
    でも上限が効くようにするための二重化として残す。
    tmpdir の後始末は呼び出し側の責務（添付完了後に必ず ``shutil.rmtree`` する）。
    """
    if not candidates:
        return None, []
    tmpdir = tempfile.mkdtemp(prefix=tmp_prefix)
    prepared: list[PreparedFile] = []
    size_kwargs: dict[str, int] = {} if max_bytes is None else {"max_bytes": max_bytes}
    for file_id, filename in candidates:
        try:
            data = gdrive.download_file_bytes(file_id=file_id, request_id=request_id, **size_kwargs)
        except Exception as e:
            # 上限超過で adapter 側が中断した場合も 1 件落として続行（理由は残す）。
            log.warning(f"{log_prefix}_download_failed", file_id=file_id, error=type(e).__name__)
            continue
        if max_bytes is not None and len(data) > max_bytes:
            log.warning(
                f"{log_prefix}_too_large",
                file_id=file_id,
                size_bytes=len(data),
                max_bytes=max_bytes,
            )
            continue
        # sanitize 後に同名となる別ファイルが上書きし合わないよう file_id で区切る
        # （file_id は [A-Za-z0-9_-] のみでパスとして安全）。
        path = str(Path(tmpdir) / file_id / filename)
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(data)
        except Exception:
            log.warning(f"{log_prefix}_tmpwrite_failed", file_id=file_id)
            continue
        prepared.append((file_id, path, filename))
    return tmpdir, prepared


async def upload_all(
    slack: Any,
    channel: str,
    thread_ts: str | None,
    prepared: list[PreparedFile],
    comment: str | None,
    request_id: str,
) -> set[str]:
    """prepared を channel（任意で thread_ts）に添付。comment は最初の1件にだけ乗せる。"""
    delivered: set[str] = set()
    for i, (file_id, path, filename) in enumerate(prepared):
        ok = await slack.upload_file(
            channel,
            path,
            request_id,
            title=filename,
            initial_comment=comment if i == 0 else None,
            thread_ts=thread_ts,
        )
        if ok:
            delivered.add(file_id)
    return delivered


async def deliver_files(
    slack: Any,
    *,
    prepared: list[PreparedFile],
    comment: str | None,
    request_id: str,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    email: str | None = None,
    dm_channel: str | None = None,
) -> tuple[set[str], str]:
    """prepared を配信。返り値 (配信できた file_id 集合, 配信先種別 ``"thread"|"dm"|""``)。

    ``dm_channel`` に **解決済みの DM channel_id** を渡すと、ここでは
    ``users.lookupByEmail`` / ``conversations.open`` を叩かない。同じリクエストで
    テキストも DM へ投げる呼び出し元（clientkarte）が 1 度だけ解決した結果を共有し、
    往復を二重に撃たないため（2026-08-20 レビュー 要修正3(a)）。
    """
    if channel_id:
        delivered = await upload_all(slack, channel_id, thread_ts, prepared, comment, request_id)
        if delivered:
            return delivered, "thread"

    if dm_channel:
        delivered = await upload_all(slack, dm_channel, None, prepared, comment, request_id)
        if delivered:
            return delivered, "dm"
        return set(), ""

    if email:
        user_id = await slack.lookup_user_id_by_email(email, request_id)
        if user_id:
            dm = await slack.open_dm(user_id, request_id)
            if dm:
                delivered = await upload_all(slack, dm, None, prepared, comment, request_id)
                if delivered:
                    return delivered, "dm"
    return set(), ""


__all__ = [
    "PreparedFile",
    "deliver_files",
    "extract_drive_binary_file_id",
    "extract_drive_file_id",
    "prepare_drive_files",
    "safe_filename",
    "upload_all",
]
