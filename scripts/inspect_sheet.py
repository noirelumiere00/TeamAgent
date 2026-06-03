"""案件スプレッドシートの構造を覗くデバッグツール（オリエン抽出のマッピング作成用）。

タブ一覧と、各タブの「ヘッダ行＋先頭数行」をセル単位で切り詰めて表示する。
商材情報など機微を丸ごとは出さない（既定で各セル 40 文字に truncate）。

認証は GSheetsClient.from_env() に委譲。本番では Vertex SA が
GOOGLE_APPLICATION_CREDENTIALS に居るので、Sheets は OAuth 強制で読む:

    set -a; source .env.production; set +a
    source scripts/load_secrets.sh        # Secrets Manager → OAuth env
    GOOGLE_FORCE_OAUTH=1 python scripts/inspect_sheet.py <sheet_id> [--rows 3] [--width 40]

Sheet ID は URL の /d/<ここ>/edit。
"""

from __future__ import annotations

import argparse
import sys

from teamagent.adapters.gsheets_client import GSheetsClient


def _trunc(value: str, width: int) -> str:
    value = value.replace("\n", "⏎")
    return value if len(value) <= width else value[: width - 1] + "…"


def main() -> int:
    ap = argparse.ArgumentParser(description="Google Sheet の構造を覗く")
    ap.add_argument("sheet_id", help="スプレッドシート ID (URL の /d/<id>/edit)")
    ap.add_argument("--rows", type=int, default=3, help="各タブで表示するデータ行数")
    ap.add_argument("--width", type=int, default=40, help="セルの最大表示幅")
    ap.add_argument("--tab", default=None, help="特定タブのみ詳しく見る")
    args = ap.parse_args()

    client = GSheetsClient.from_env()
    meta = client.get_sheet_metadata(sheet_id=args.sheet_id, request_id="inspect")
    print(f"# スプレッドシート: {meta.title}  (tabs={len(meta.tabs)})\n")
    for tab in meta.tabs:
        print(f"## [{tab.gid}] {tab.title}")
        if args.tab and args.tab != tab.title:
            print("   (--tab 指定によりスキップ)\n")
            continue
        try:
            data = client.get_tab_rows(
                sheet_id=args.sheet_id, tab_name=tab.title, request_id="inspect"
            )
        except Exception as e:
            print(f"   ! 読取失敗: {e}\n")
            continue
        headers = data.headers
        print(f"   headers ({len(headers)}列):")
        for i, h in enumerate(headers):
            col = _col_letter(i)
            print(f"     {col:>2} | {_trunc(str(h), args.width)}")
        for r_idx, row in enumerate(data.rows[: args.rows]):
            cells = " | ".join(_trunc(str(c), args.width) for c in row)
            print(f"   row{r_idx + 1}: {cells}")
        print(f"   (総データ行数: {len(data.rows)})\n")
    return 0


def _col_letter(idx: int) -> str:
    """0→A, 25→Z, 26→AA。"""
    letters = ""
    idx += 1
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


if __name__ == "__main__":
    sys.exit(main())
