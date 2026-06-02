"""動画一次FB(AIチェック結果)を、見やすい形で書き出す（シート列 / Slack）。

ユーザー要望(2026-06-01): AIのチェック結果を案件シートに**1列追加して記入**する。
**既存データは絶対に削除しない**こと。

「削除しない」を担保する設計（このモジュール + gsheets_client.update_single_cell）:
- 書き込みは **1 セルずつ**（AI列の該当行のみ）。範囲書き込み・行列削除は使わない。
- 書き込み先の AI 列は **既存データの無い右端の空き列**を選ぶ（既存セルに構造上当たらない）。
  既にこのツールが作った AI 列があればそれを再利用（ヘッダ名で検出）。
- 列の物理挿入(insertDimension)はしない＝他列をズラさない・数式参照を壊さない。

書き込み内容は「**判定＋指摘要点**」（ユーザー選択）。フル本文ではなく要約。

現状はテスト段階のため、出力は Slack（テストサーバー）を主とし、シート書込は
spreadsheets スコープの再認証が済んだら解禁する（gsheets は from_env(write=True)）。
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from teamagent.adapters.gsheets_client import GSheetsClient
from teamagent.skills.video_approval.schema import ApprovalIssue, VideoApprovalOutput
from teamagent.skills.video_approval.sheet_orientation import (
    ITOEN_NTV_LAYOUT,
    SheetLayout,
    _norm,
    band_header_index,
    build_header_index,
    col_letter,
    find_col,
    find_header_row,
    resolve_tab_title,
)

logger = structlog.get_logger(__name__)

DEFAULT_AI_HEADER = "AI一次チェック"
_AI_DISCLAIMER = "※AIの暫定一次FB・最終は人の確認をお願いします"
_MAX_ISSUES_IN_CELL = 8


# -----------------------------------------------------------
# 表示整形（シートセル / Slack）
# -----------------------------------------------------------
def _issue_line(issue: ApprovalIssue, bullet: str) -> str:
    tc = f"({issue.timecode})" if issue.timecode else ""
    detail = issue.detail.strip()
    return f"{bullet}{issue.category}: {detail}{tc}".rstrip()


def _ordered_issues(output: VideoApprovalOutput) -> list[ApprovalIssue]:
    """must_fix を先に、その後 suggestion。"""
    must = [i for i in output.issues if i.severity == "must_fix"]
    sugg = [i for i in output.issues if i.severity != "must_fix"]
    return must + sugg


def format_check_cell(output: VideoApprovalOutput, *, stamp: str | None = None) -> str:
    """シートの AI 列 1 セルに入れる「判定＋指摘要点」テキスト（改行区切り）。"""
    lines: list[str] = [f"【AI一次】{output.verdict}"]
    if output.summary:
        lines.append(output.summary.strip())
    issues = _ordered_issues(output)
    for issue in issues[:_MAX_ISSUES_IN_CELL]:
        lines.append(_issue_line(issue, "・"))
    extra = len(issues) - _MAX_ISSUES_IN_CELL
    if extra > 0:
        lines.append(f"・…他 {extra} 件")
    footer = _AI_DISCLAIMER if not stamp else f"{_AI_DISCLAIMER}（{stamp}）"
    lines.append(footer)
    return "\n".join(lines)


def format_verdict(output: VideoApprovalOutput) -> str:
    """AIチェック列に入れる短い判定（OK / 要修正 / 確認要）。"""
    return output.verdict


def format_fb_body(output: VideoApprovalOutput, *, stamp: str | None = None) -> str:
    """AI FB内容列に入れる本文（判定の見出しは付けない＝判定は別列にある前提）。"""
    lines: list[str] = []
    if output.summary:
        lines.append(output.summary.strip())
    issues = _ordered_issues(output)
    for issue in issues[:_MAX_ISSUES_IN_CELL]:
        lines.append(_issue_line(issue, "・"))
    extra = len(issues) - _MAX_ISSUES_IN_CELL
    if extra > 0:
        lines.append(f"・…他 {extra} 件")
    footer = _AI_DISCLAIMER if not stamp else f"{_AI_DISCLAIMER}（{stamp}）"
    lines.append(footer)
    return "\n".join(lines)


def format_check_slack(
    output: VideoApprovalOutput,
    *,
    management_no: str | None = None,
    creative_name: str | None = None,
    video_url: str | None = None,
) -> str:
    """Slack 投稿用 mrkdwn（テスト段階の主出力）。"""
    head = ":clapperboard: *AI一次チェック*"
    if management_no:
        head += f" — `{management_no}`"
    if creative_name:
        head += f" {creative_name}"
    lines: list[str] = [head, f"判定: *{output.verdict}*"]
    if output.summary:
        lines.append(output.summary.strip())
    for issue in _ordered_issues(output):
        sev = "🔴" if issue.severity == "must_fix" else "🟡"
        tc = f" ({issue.timecode})" if issue.timecode else ""
        # 体裁: 🔴 *必須要素*: 詳細 (0:05) — カテゴリだけ太字
        lines.append(f"{sev} *{issue.category}*: {issue.detail.strip()}{tc}")
    if video_url:
        lines.append(f"<{video_url}|納品動画>")
    lines.append(f"_{_AI_DISCLAIMER}_")
    return "\n".join(lines)


# -----------------------------------------------------------
# シート書込（削除ゼロ保証）
# -----------------------------------------------------------
@dataclass(frozen=True)
class AiWriteResult:
    management_no: str
    tab_name: str
    ai_column: str  # 列名（A1, 例 "AB"）
    header_row: int  # 1 始まり
    data_row: int  # 1 始まり
    created_column: bool  # 新規に AI 列を作ったか


def write_ai_check(
    client: GSheetsClient,
    *,
    sheet_id: str,
    management_no: str,
    cell_text: str,
    layout: SheetLayout = ITOEN_NTV_LAYOUT,
    header_label: str = DEFAULT_AI_HEADER,
    request_id: str = "aiwrite",
) -> AiWriteResult:
    """投稿管理シートの当該クリエイティブ行の AI 列に cell_text を書く。

    **既存セルには絶対に触れない**: AI 列は既存データの無い右端の空き列を使い
    （既存の AI 列があれば再利用）、書き込みは update_single_cell（単一セル限定）のみ。
    """
    title = resolve_tab_title(client, sheet_id, layout.posting.name_keyword, request_id)
    if title is None:
        raise ValueError(f"投稿管理タブが見つかりません（keyword={layout.posting.name_keyword}）")

    tr = client.get_tab_rows(sheet_id=sheet_id, tab_name=title, request_id=request_id)
    all_rows: list[list[str]] = [list(tr.headers)] + [list(r) for r in tr.rows]
    h_idx = find_header_row(all_rows, layout.posting.header_tokens)
    header_index = build_header_index(all_rows[h_idx])

    c_mgmt = find_col(header_index, *layout.join_posting_key)
    if c_mgmt is None:
        raise ValueError("管理番号列が見つかりません")
    target = _norm(management_no)
    data_idx: int | None = None
    for i in range(h_idx + 1, len(all_rows)):
        cell = all_rows[i][c_mgmt] if c_mgmt < len(all_rows[i]) else ""
        if _norm(cell) == target:
            data_idx = i
            break
    if data_idx is None:
        raise ValueError(f"管理番号 {management_no} の行が投稿管理シートに見つかりません")

    # AI 列の決定: 既存ヘッダがあれば再利用、無ければ「全データの右端の次」＝空き列
    ai_col_idx = find_col(header_index, header_label)
    created = ai_col_idx is None
    if ai_col_idx is None:
        ai_col_idx = max((len(r) for r in all_rows), default=0)

    ai_col = col_letter(ai_col_idx)
    header_row_1 = h_idx + 1
    data_row_1 = data_idx + 1

    if created:
        client.update_single_cell(
            sheet_id=sheet_id,
            tab_name=title,
            cell=f"{ai_col}{header_row_1}",
            value=header_label,
            request_id=request_id,
        )
    client.update_single_cell(
        sheet_id=sheet_id,
        tab_name=title,
        cell=f"{ai_col}{data_row_1}",
        value=cell_text,
        request_id=request_id,
    )
    logger.info(
        "ai_check_written",
        management_no=management_no,
        tab=title,
        ai_column=ai_col,
        data_row=data_row_1,
        created_column=created,
    )
    return AiWriteResult(
        management_no=management_no,
        tab_name=title,
        ai_column=ai_col,
        header_row=header_row_1,
        data_row=data_row_1,
        created_column=created,
    )


# 列が無いときに新設する際の見出し（ユーザー設定の「AIチェック / AI FB内容」に揃える）
AI_VERDICT_HEADER = "AIチェック"
AI_FB_HEADER = "AI FB内容"


@dataclass(frozen=True)
class AiReviewWriteResult:
    """指定 2 列（判定・FB本文）への書込結果。"""

    management_no: str
    tab_name: str
    verdict_cell: str  # 例 "R12"
    fb_cell: str  # 例 "S12"
    created_columns: tuple[str, ...] = ()  # 新設した列名（例 ("AA","AB")）


def write_ai_review(
    client: GSheetsClient,
    *,
    sheet_id: str,
    management_no: str,
    verdict: str,
    fb_body: str,
    layout: SheetLayout = ITOEN_NTV_LAYOUT,
    create_if_missing: bool = True,
    request_id: str = "aireview",
) -> AiReviewWriteResult:
    """「AIチェック(判定)」「AI FB内容」列に書き込む。無ければ右端に新設して書く。

    ユーザー方針(2026-06-02): 今後も同構造のシートが来る。AI 列が無ければ
    「セルを追加し R・S の項目(AIチェック/AI FB内容)を足して対応」する。

    既存データは消さない:
      - 書込は update_single_cell（単一セル）のみ。clear/delete/insert 系は使わない。
      - 新設列は**既存データの右端より外**に置く（既存セルに構造上当たらない）。
      - 既存の「内部C / 投稿ステ」等を上書きしない（見出し名で判別し、別物には書かない）。
    見出しが複数行に跨る実シートに対応するため band_header_index で解決する。
    """
    title = resolve_tab_title(client, sheet_id, layout.posting.name_keyword, request_id)
    if title is None:
        raise ValueError(f"投稿管理タブが見つかりません（keyword={layout.posting.name_keyword}）")

    tr = client.get_tab_rows(sheet_id=sheet_id, tab_name=title, request_id=request_id)
    all_rows: list[list[str]] = [list(tr.headers)] + [list(r) for r in tr.rows]
    h_idx = find_header_row(all_rows, layout.posting.header_tokens)
    idx = band_header_index(all_rows, h_idx, lookback=2)

    c_mgmt = find_col(idx, *layout.join_posting_key)
    if c_mgmt is None:
        raise ValueError("管理番号列が見つかりません")

    # 対象クリエイティブの物理行を先に特定（行が無ければ列も作らない）
    target = _norm(management_no)
    data_idx: int | None = None
    for i in range(h_idx + 1, len(all_rows)):
        cell = all_rows[i][c_mgmt] if c_mgmt < len(all_rows[i]) else ""
        if _norm(cell) == target:
            data_idx = i
            break
    if data_idx is None:
        raise ValueError(f"管理番号 {management_no} の行が見つかりません")

    c_verdict = find_col(idx, *layout.col_ai_verdict)
    c_fb = find_col(idx, *layout.col_ai_fb)

    created: list[str] = []
    new_headers: list[tuple[int, str]] = []  # (col_idx, header) を後でヘッダ行に書く
    if c_verdict is None or c_fb is None:
        if not create_if_missing:
            raise ValueError(
                "AI 書込先の列が見つかりません"
                "（『AIチェック』『AI FB内容』の見出しをご確認ください）"
            )
        # 既存データの右端の外から順に空き列を割り当てる（既存を一切動かさない）
        next_col = max((len(r) for r in all_rows), default=0)
        if c_verdict is None:
            c_verdict = next_col
            next_col += 1
            new_headers.append((c_verdict, AI_VERDICT_HEADER))
            created.append(col_letter(c_verdict))
        if c_fb is None:
            c_fb = next_col
            next_col += 1
            new_headers.append((c_fb, AI_FB_HEADER))
            created.append(col_letter(c_fb))

    header_row_1 = h_idx + 1
    for col_i, header in new_headers:
        client.update_single_cell(
            sheet_id=sheet_id,
            tab_name=title,
            cell=f"{col_letter(col_i)}{header_row_1}",
            value=header,
            request_id=request_id,
        )

    row_1 = data_idx + 1
    verdict_cell = f"{col_letter(c_verdict)}{row_1}"
    fb_cell = f"{col_letter(c_fb)}{row_1}"
    client.update_single_cell(
        sheet_id=sheet_id, tab_name=title, cell=verdict_cell, value=verdict, request_id=request_id
    )
    client.update_single_cell(
        sheet_id=sheet_id, tab_name=title, cell=fb_cell, value=fb_body, request_id=request_id
    )
    logger.info(
        "ai_review_written",
        management_no=management_no,
        tab=title,
        verdict_cell=verdict_cell,
        fb_cell=fb_cell,
        created_columns=created,
    )
    return AiReviewWriteResult(
        management_no=management_no,
        tab_name=title,
        verdict_cell=verdict_cell,
        fb_cell=fb_cell,
        created_columns=tuple(created),
    )
