"""Authenticated URL intake and durable batch results; never fetch public pages here."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from teamagent.connect_web.library import _e, _select
from teamagent.ingest.video_library_url_import import submit_import
from teamagent.ingest.video_library_url_models import URLImportError, inspect_input
from teamagent.ingest.video_library_url_repository import RETRYABLE, URLImportRepository
from teamagent.ingest.video_library_url_worker import settings

STATUS = {
    "queued": "受付",
    "processing": "処理中",
    "succeeded": "成功",
    "partial": "一部取得",
    "failed": "失敗",
    "duplicate": "重複",
    "invalid": "不正",
    "unsupported": "対象外",
    "deferred": "翌日以降の処理待ち",
    "classification_pending": "分類保留",
}
ERRORS = {
    "empty_input": "URLを入力してください。",
    "too_many_lines": "1回100本までです。",
    "invalid_url": "URLの形式が不正です。",
    "unsupported_host": "TikTokのURLだけが対象です。",
    "unsupported_path": "動画URLまたは対応する短縮URLを入力してください。",
    "daily_limit": "日次上限に達しました。翌日以降に処理します。",
    "http_429": "取得先の制限により停止しています。",
    "http_403": "取得先へのアクセスが制限されています。",
    "challenge": "取得先の認証確認により停止しています。",
    "consecutive_failures": "連続失敗のため停止しています。",
    "network_error": "通信に失敗しました。",
    "page_data_missing": "公開ページの投稿情報を取得できませんでした。",
    "caption_missing": "投稿文が取得できず分類を保留しています。",
    "video_id_mismatch": "投稿IDが一致しません。",
    "storage_error": "保存に失敗しました。",
    "campaign_unavailable": "選択した案件は利用できません。",
    "idempotency_conflict": "受付内容が変わっています。入力画面から確認し直してください。",
    "retry_changed": "結果が変わりました。結果画面を更新してください。",
    "batch_unavailable": "受付が見つかりません。",
    "invalid_request_identity": "受付キーが不正です。",
    "fetch_paused": "取得を停止しています。",
    "processing_recovered": "処理を再開します。",
}


def reason(code: str | None) -> str:
    return ERRORS.get(code or "", "取得できませんでした。" if code else "")


def batch_state(rows: list[dict[str, Any]], *, paused: bool, enabled: bool) -> tuple[str, bool]:
    pending = any(r["status"] in {"queued", "processing", "deferred"} for r in rows)
    if pending:
        if paused or not enabled or any(r["status"] == "deferred" for r in rows):
            return "停止中", False
        return ("処理中" if any(r["status"] == "processing" for r in rows) else "受付"), True
    failed = any(r["status"] in RETRYABLE | {"invalid", "unsupported"} for r in rows)
    return ("一部失敗" if failed else "完了"), False


def register_import_routes(
    router: APIRouter,
    *,
    identity: Callable[..., str],
    connection: Callable[..., Any],
    get_pg: Callable[..., Any],
    page: Callable[..., HTMLResponse],
    form_data: Callable[..., Any],
    is_admin: Callable[[str], bool],
) -> None:
    def selected(form: dict[str, str], choices: list[Any]) -> tuple[str | None, str | None]:
        if form.get("requested_origin", "external") not in {"own", "external"}:
            raise HTTPException(400, "自社・他社の指定が不正です")
        if form.get("requested_origin", "external") != "own":
            return None, None
        # The submitted value is a pair, never an index into a potentially changed list.
        value = form.get("case", "")
        if not value:
            return None, None
        for row in choices:
            if value == json.dumps([row["advertiser"], row["campaign"]], ensure_ascii=False):
                return row["advertiser"], row["campaign"]
        raise HTTPException(400, reason("campaign_unavailable"))

    def screen(
        email: str, form: dict[str, str], *, preview: bool = False, notice: str = ""
    ) -> HTMLResponse:
        text = form.get("urls", "")
        inspected = inspect_input(text) if preview else []
        with connection(email) as conn:
            choices = URLImportRepository.campaigns(conn)
            selected(form, choices)
            existing = {
                r["video_id"]
                for r in conn.execute(
                    """SELECT video_id FROM vl_videos_effective WHERE platform='tiktok'
                   AND video_id=ANY(%s) AND (%s OR NOT is_private)""",
                    ([r.video_id for r in inspected if r.video_id], is_admin(email)),
                ).fetchall()
            }
            recent = conn.execute(
                """SELECT batch_id,created_at FROM vl_url_import_batches WHERE requested_by=%s
                   ORDER BY created_at DESC LIMIT 20""",
                (email,),
            ).fetchall()
            control = conn.execute(
                "SELECT paused,pause_reason,paused_at FROM vl_public_fetch_control "
                "WHERE platform='tiktok'"
            ).fetchone()
        options = [("", "案件を選択しない")] + [
            (
                json.dumps([r["advertiser"], r["campaign"]], ensure_ascii=False),
                r["advertiser"] + " / " + r["campaign"],
            )
            for r in choices
        ]
        body = '<div class="import-workspace">'
        if control["paused"]:
            body += (
                '<section role="status"><h2>取得停止中</h2>'
                f"<p>{_e(reason(control['pause_reason']))}</p>"
                f"<p>停止した時刻: {_e(control['paused_at'] or '不明')}</p>"
            )
            if is_admin(email):
                body += (
                    '<form method="post" action="/library/import/resume">'
                    "<button>取得を再開</button></form>"
                )
            else:
                body += "<p>管理者に再開を依頼してください</p>"
            body += "</section>"
        if notice:
            body += f'<p role="status">{_e(notice)}</p>'
        body += (
            "<p>社内の閲覧者全員が利用できます。TikTokの動画URL・短縮URLに対応しています。</p>"
            '<form method="post" action="/library/import">'
            '<label for="import-urls">URL（1行1本・100本まで）</label>'
            f'<textarea id="import-urls" name="urls" rows="9" required>{_e(text)}</textarea>'
            '<input type="hidden" name="idempotency_key" '
            f'value="{_e(form.get("idempotency_key") or str(uuid.uuid4()))}">'
            "<label>投稿の区分"
            + _select(
                "requested_origin",
                [
                    ("external", "他社・世の中の投稿"),
                    ("own", "自社の案件（KOL・インフルエンサーへの依頼も含む）"),
                ],
                form.get("requested_origin", "external"),
            )
            + "</label>"
            '<p class="muted">シート掲載・自社アカウントの動画は、'
            "選択にかかわらず自社になります。</p>"
            "<div data-own-campaign"
            + (" hidden" if form.get("requested_origin", "external") != "own" else "")
            + "><label>広告主・案件（任意）"
            + _select("case", options, form.get("case", ""))
            + "</label>"
            '<p class="muted">選択した案件は新規の #PR 付き動画にだけ適用します。'
            "登録済みの動画は数値を更新し、既存の案件を維持します。</p></div>"
            '<button name="action" value="check" class="primary">確認</button>'
        )
        if preview:
            body += (
                "<h2>行ごとの検査結果</h2>"
                "<p>外部通信はまだ行っていません。</p>"
                '<div class="import-table">'
                "<table>"
                "<thead>"
                "<tr>"
                "<th>行</th>"
                "<th>URL</th>"
                "<th>結果・理由</th>"
                "</tr>"
                "</thead>"
                "<tbody>"
            )
            for row in inspected:
                status = (
                    "登録済み（統合済み）"
                    if row.status == "queued" and row.video_id in existing
                    else "OK"
                    if row.status == "queued"
                    else STATUS[row.status]
                )
                detail = reason(row.error_code)
                if row.duplicate_of:
                    detail = f"{row.duplicate_of}行目と同じ動画です。"
                elif row.status == "queued" and not row.video_id:
                    detail = "短縮URLは処理時に展開します。"
                body += (
                    f"<tr>"
                    f"<td>{row.line_no}</td>"
                    f"<td>{_e(row.input_url)}</td>"
                    f"<td>{status} {_e(detail)}</td>"
                    f"</tr>"
                )
            body += "</tbody></table></div>"
            if any(r.status == "queued" for r in inspected):
                body += '<button name="action" value="submit" class="primary">取り込む</button>'
        body += (
            "</form>"
            "<details data-own-campaign"
            + (" hidden" if form.get("requested_origin", "external") != "own" else "")
            + ">"
            "<summary>新しい広告主・案件を追加</summary>"
            "<p>表記揺れに注意してください。上の既存候補を確認し、同じ組がある場合はそちらを選んでください。</p>"
        )
        body += '<form method="post" action="/library/import/campaign" data-import-campaign>'
        for name in ("urls", "case", "idempotency_key", "requested_origin"):
            body += f'<input type="hidden" name="{name}" value="{_e(form.get(name, ""))}">'
        body += (
            '<label>広告主<input name="advertiser" required maxlength="500">'
            "</label>"
            '<label>案件<input name="campaign" required maxlength="500">'
            "</label>"
            "<button>追加して選択</button>"
            "</form>"
            "</details>"
        )
        body += (
            f'<p class="muted">全員合算の日次上限は {settings()[0]:,} 回のページ取得です。'
            "超過分は翌日以降の処理待ちになります。</p><h2>自分の取り込み</h2><ul>"
        )
        body += "".join(
            f'<li><a href="/library/import/{_e(r["batch_id"])}">'
            f"{_e(r['created_at'])} の受付</a></li>"
            for r in recent
        )
        body += "</ul></div>"
        return page("URL取り込み", body, email, "import")

    def wake(request: Request) -> None:
        worker = getattr(request.app.state, "video_library_import_worker", None)
        if worker:
            worker.wake()

    @router.get("/import")
    def intake(email: str = Depends(identity)) -> HTMLResponse:
        return screen(email, {})

    @router.post("/import/resume")
    async def resume(request: Request, email: str = Depends(identity)) -> RedirectResponse:
        if not is_admin(email):
            raise HTTPException(403)
        await form_data(request, import_form=True)
        with connection(email) as conn:
            resumed = URLImportRepository.resume_fetches(conn, resumed_by=email)
        if resumed:
            wake(request)
        return RedirectResponse("/library/import", 303)

    @router.post("/import")
    async def confirm(request: Request, email: str = Depends(identity)) -> Any:
        form = await form_data(request, import_form=True)
        try:
            inspect_input(form.get("urls", ""))
            if form.get("action") == "check":
                return screen(email, form, preview=True)
            if form.get("action") != "submit":
                raise HTTPException(400)
            with connection(email) as conn:
                advertiser, campaign = selected(form, URLImportRepository.campaigns(conn))
            batch = submit_import(
                form.get("urls", ""),
                URLImportRepository(get_pg()),
                requested_by=email,
                idempotency_key=form.get("idempotency_key", ""),
                requested_origin=form.get("requested_origin", "external"),
                advertiser=advertiser,
                campaign=campaign,
                wake=lambda: wake(request),
            )
        except URLImportError as exc:
            raise HTTPException(
                409 if str(exc) == "idempotency_conflict" else 400, reason(str(exc))
            ) from None
        return RedirectResponse(f"/library/import/{batch}", 303)

    @router.post("/import/campaign")
    async def add_campaign(request: Request, email: str = Depends(identity)) -> HTMLResponse:
        form = await form_data(request, import_form=True)
        advertiser, campaign = (form.get(k, "").strip() for k in ("advertiser", "campaign"))
        if any(
            not v or len(v) > 500 or any(ord(c) < 32 for c in v) for v in (advertiser, campaign)
        ):
            raise HTTPException(400, "広告主・案件は1〜500文字で入力してください")
        with connection(email) as conn:
            found = conn.execute(
                "SELECT 1 FROM vl_campaigns WHERE advertiser=%s AND campaign=%s",
                (advertiser, campaign),
            ).fetchone()
            if found and not URLImportRepository.campaign_visible(conn, advertiser, campaign):
                raise HTTPException(400, reason("campaign_unavailable"))
            conn.execute(
                """INSERT INTO vl_campaigns(advertiser,campaign,created_by,created_at,created_via)
                   VALUES (%s,%s,%s,now(),'url_import') ON CONFLICT DO NOTHING""",
                (advertiser, campaign, email),
            )
        form["requested_origin"] = "own"
        form["case"] = json.dumps([advertiser, campaign], ensure_ascii=False)
        return screen(
            email, form, notice="広告主・案件を選択しました。表記を確認してから取り込んでください。"
        )

    def load(email: str, batch_id: uuid.UUID) -> dict[str, Any]:
        with connection(email) as conn:
            batch = URLImportRepository.batch(conn, str(batch_id), email, admin=is_admin(email))
            if batch is None:
                raise HTTPException(404)
            batch["control"] = conn.execute(
                "SELECT paused,pause_reason FROM vl_public_fetch_control WHERE platform='tiktok'"
            ).fetchone()
            # A successful result must not expose a now-private video's link.
            batch["visible"] = {
                r["video_id"]
                for r in conn.execute(
                    """SELECT video_id FROM vl_videos_effective WHERE platform='tiktok'
                   AND video_id=ANY(%s) AND (%s OR NOT is_private)""",
                    ([r["video_id"] for r in batch["rows"] if r["video_id"]], is_admin(email)),
                ).fetchall()
            }
        return batch

    @router.get("/import/{batch_id}")
    def result(
        request: Request, batch_id: uuid.UUID, email: str = Depends(identity)
    ) -> HTMLResponse:
        batch = load(email, batch_id)
        control = batch["control"]
        enabled = getattr(request.app.state, "video_library_import_worker", None) is not None
        state, poll = batch_state(batch["rows"], paused=control["paused"], enabled=enabled)
        if (
            enabled
            and control["paused"]
            and control["pause_reason"] == "http_429"
            and any(r["status"] in {"queued", "processing"} for r in batch["rows"])
        ):
            poll = True
        counts = Counter(STATUS[r["status"]] for r in batch["rows"])
        body = (
            f'<div class="import-workspace" data-import-poll="{str(poll).lower()}">'
            '<p><a href="/library/import">URL取り込みに戻る</a></p>'
            # role on the h2 itself would drop its heading role; keep the live region around it.
            f'<div role="status"><h2>{state}</h2></div>'
        )
        body += "<p>" + _e(" / ".join(f"{k} {v}件" for k, v in counts.items())) + "</p>"
        if not enabled:
            body += (
                "<p>取り込みworkerが無効です。受付は保存され、workerの有効化後に処理します。</p>"
            )
        if control["paused"]:
            body += (
                f"<p>{_e(reason(control['pause_reason']))} "
                "共通の停止は再実行では解除されません。</p>"
            )
        body += (
            '<div class="import-table">'
            "<table>"
            "<thead>"
            "<tr>"
            "<th>行</th>"
            "<th>URL</th>"
            "<th>結果</th>"
            "</tr>"
            "</thead>"
            "<tbody>"
        )
        for row in batch["rows"]:
            label = row.get("registration_result") or STATUS[row["status"]]
            link = (
                f'<a href="/library/v/tiktok/{_e(row["video_id"])}">{_e(label)}</a>'
                if row["status"] in {"succeeded", "partial"} and row["video_id"] in batch["visible"]
                else _e(label)
            )
            detail = reason(row.get("error_code"))
            if row["status"] == "duplicate":
                detail = f"{row['duplicate_of']}行目と重複"
            warnings = (row.get("observation") or {}).get("warnings", [])
            if warnings:
                detail += " 一部の公開情報が欠損しています。"
            body += (
                f"<tr>"
                f"<td>{row['line_no']}</td>"
                f"<td>{_e(row['input_url'])}</td>"
                # The status is shown once; the link label repeats it only when it adds a result.
                f"<td>{'' if label == STATUS[row['status']] else STATUS[row['status']] + ' '}"
                f"{link} {_e(detail)}</td>"
                f"</tr>"
            )
        body += "</tbody></table></div>"
        if any(r["status"] in RETRYABLE for r in batch["rows"]) and (
            batch["requested_by"] == email or is_admin(email)
        ):
            body += (
                '<form method="post" action="'
                + str(batch_id)
                + '/retry"><input type="hidden" name="idempotency_key" value="'
                + str(uuid.uuid4())
                + '"><p>成功分は再取得しません。一部取得・分類保留も再実行に含み、'
                "日次枠を消費します。</p><button>失敗分を再実行</button></form>"
            )
        return page("取り込み結果", body + "</div>", email, "import")

    @router.post("/import/{batch_id}/retry")
    async def retry(
        request: Request, batch_id: uuid.UUID, email: str = Depends(identity)
    ) -> RedirectResponse:
        form = await form_data(request, import_form=True)
        batch = load(email, batch_id)
        text = "\n".join(r["input_url"] for r in batch["rows"] if r["status"] in RETRYABLE)
        try:
            child = submit_import(
                text,
                URLImportRepository(get_pg()),
                requested_by=email,
                idempotency_key=form.get("idempotency_key", ""),
                requested_origin=batch["requested_origin"],
                advertiser=batch["advertiser"],
                campaign=batch["campaign"],
                parent_batch_id=str(batch_id),
                admin=is_admin(email),
                wake=lambda: wake(request),
            )
        except URLImportError as exc:
            raise HTTPException(400, reason(str(exc))) from None
        return RedirectResponse(f"/library/import/{child}", 303)
