"""カルテに同梱する「関連資料」セクションを **決定論コード**で組み立てる。

ユーザー要求（2026-08-19）:
  「カルテを見て資料をクリックする」のは工数が多い。カルテを出した時点で資料も一緒に出す。

ホスピタリティ分岐（最重要指示）— 案件名の資料が 0 件でも「無い」で終わらせない:
  1. 案件一致あり            → 「📎 関連資料（N件）」として同梱（+ 一致資料だけ実ファイル添付）
  2. 案件一致 0 件 / 顧客に他資料あり → 代わりに **実在する資料名**を最大 3 件挙げて提案
     （この分岐では自動添付しない。名前とリンクの提示に留める＝勝手に送らない）
  3. 顧客の資料も 0 件        → セクション自体を出さない（空セクション禁止）

このセクション（リンク一覧を含む）は ``KARTE_ATTACH_DOCS`` の配下にある
（2026-08-19 レビュー H1 裁定）。カルテの answer は「聞かれたチャンネル」へ出るため、
実ファイルだけを止めて資料名と Drive リンクを出し続けるのは kill switch とは呼べない。
OFF なら本機能追加前と同じ出力（カルテ本文＋出典 URL のみ）に戻る。

**出力面の裁定（2026-08-20 ユーザー裁定 A）**:
  資料セクション（資料名・Drive リンク・実ファイル）は **まるごと依頼者本人の DM** へ出す。
  チャンネル / スラッシュコマンドでカルテを呼ばれたとき、チャンネル本文へ出してよいのは
  ``channel_notice()`` / ``attachment_only_notice()`` / ``availability_notice()`` が作る
  **1 行の通知だけ**（資料名は 1 件も出さない・数字だけ）。
  DM で呼ばれたときはその場が既に本人だけの面なので、セクションを本文へ直接足す
  （転送しない＝同じ内容を 2 度出さない）。
  L2 オーケストレーター（``run_agent``）の中間ステップも、DM 面でなければ同じ扱いに
  落とす（あの経路の最終回答もチャンネルへ出るため・2026-08-20 レビュー 指摘4）。

不変条件:
  - 文面は全てこのモジュールの決定論コードが作る（LLM に書かせない）
  - 資料名は ``list_documents_for_client`` が返した実データからしか取らない
    （存在しない資料名を書かない）
  - URL は ``source_link()`` が解決したものだけを出す（推測した URL は出さない。
    解決できない資料は名前だけ出す）
  - 外部由来の文字列（資料名・案件名・顧客名）は必ず ``slack_label()`` を通す
    （bot 発言として装飾リンクを描画させない）
  - **案件名を名指しされたら、自動添付はその案件に一致した資料だけ**。
    一致 1 件のときに残り枠を無関係資料で埋めない（聞かれていない案件の資料を送らない）。
    案件名が無いときは「この顧客の新しい資料から」上位 K 件を送る（＝要求そのもの:
    カルテを出した時点で資料も一緒に出す）。
  - 自動添付の候補は必ず **一覧に表示した行の内側**に限る
    （「N 件お送りしました」が、一覧に 1 行も出ていない資料を指さない）。
  - チャンネル本文へ出す通知（``channel_notice`` / ``attachment_only_notice`` /
    ``availability_notice``）に資料名・URL を 1 文字も入れない。**数字だけ**。
  - 通知の数字は「名前を出した件数」（``listed_count``）と「実ファイルを送れた件数」だけ。
    在庫総数（``count``）を「お送りしました」の主語にしない
    （公開チャンネルへ「その顧客の社内資料が何件あるか」を出さない・2026-08-20 レビュー
    要修正1）。
  - 行の ``client_name`` メタが要求顧客と矛盾する資料は **セクションに 1 行も出さない**
    （``belongs_to_client``）。``list_documents_for_client`` の WHERE は
    ``cls_project / client_name / title`` の部分一致なので、顧客名を 1 回言うだけで
    他社案件の資料が一覧にも自動添付にも載っていた（2026-08-20 レビュー 要修正2・実測）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from teamagent.skills._shared.drive_slack_delivery import (
    extract_drive_binary_file_id,
    extract_drive_file_id,
)
from teamagent.skills._shared.source_url import source_link

# 一覧に並べる最大行数。payload_offload（clientkarte は OFFLOAD_TOOLS・全体 10,000 字）に
# 飲まれないよう本文側を有限に保つ。超過分は「…ほか N 件」で件数だけ正直に示す。
LIST_MAX = 5
# 提案分岐（案件一致 0 件）で挙げる資料名の上限。ユーザー指示「最大 3 件」。
OFFER_MAX = 3
# 本人しか読まない面（Slack の im）の channel_id。形は mcp_gateway/caller_claim.py が
# 署名 claim に要求する ``^[CDG][A-Z0-9]{8,}$`` と同じで、先頭が D のものだけ。
_DM_CHANNEL_RE = re.compile(r"D[A-Z0-9]{8,}")

SectionKind = Literal["matched", "offer", "none"]


@dataclass(frozen=True)
class KarteDoc:
    """カルテに載せる資料 1 件（``list_documents_for_client`` の行 dict から作る）。"""

    title: str
    source_uri: str
    source_type: str
    cls_project: str | None
    client_name: str | None

    @property
    def url(self) -> str | None:
        """ブラウザで開ける出典 URL。解決できなければ None（推測しない）。"""
        return source_link(self.source_uri)

    @property
    def attach_file_id(self) -> str | None:
        """実ファイル添付用の Drive file_id。添付できない資料は None。

        受け付けるのは **アップロード実体ファイルの形だけ**:
          - ``gdrive://<ID>``（web_view_link を持たない行に ingest が焼く内部識別子）
          - ``https://drive.google.com/file/d/<ID>/...``（バイナリの web_view_link）

        ``docs.google.com/...`` の形は **全て弾く**。理由は 2 つあり、どちらも実測事故:
          1. Google ネイティブ文書（Slides/Docs/Sheets）の web_view_link は
             ``https://docs.google.com/presentation/d/<ID>/edit?usp=drivesdk`` で、
             ingest はこれを gdrive 行の source_uri に焼く（ingest/pipeline.py の
             ``f.web_view_link or f"gdrive://{f.id}"``）。しかし
             ``GDriveClient.download_file_bytes`` は ``files.get_media`` 専用で
             ネイティブ文書には **必ず失敗する**（export が要る）。候補に入れると
             提案書が Google Slides の顧客では毎回 Drive API を叩いて必ず失敗し、
             本文末尾に誤った「添付できませんでした」注記が常時付く。
          2. ナレッジシート行（gsheets）の自リンク
             ``docs.google.com/spreadsheets/d/<シート本体 ID>`` を誤抽出して
             **ナレッジシートごと添付する事故**を防ぐ。

        ここは ``source_type`` で分岐しない。gdrive 行にもネイティブ文書が入るため、
        分岐で緩めると 1 の穴がそのまま開く。
        """
        s = self.source_uri.strip()
        if s.startswith("gdrive://"):
            return extract_drive_file_id(s)
        return extract_drive_binary_file_id(s)


@dataclass(frozen=True)
class DocumentsSection:
    """カルテ末尾に足すセクションと、添付してよい資料の組。

    ``count`` は「このセクションが語っている資料の総数」＝ **在庫**。``matched`` なら見出しの
    「📎 関連資料（N件）」の N と同じ、``offer`` なら「…など N 件あります」の N と同じ。

    ``listed_count`` は「``text`` に **名前を書いた**件数」＝ 実際に相手へ渡した資料名の数
    （``matched`` なら ``min(count, LIST_MAX)``、``offer`` なら ``min(count, OFFER_MAX)``）。
    チャンネルへ出す 1 行通知はこちらを使う。``count`` を使うと
    「📎 関連資料8件を DM でお送りしました」と言いながら DM には 5 行しか無い、
    さらに悪いことに **公開チャンネルへ「その顧客の社内資料の在庫数」を最大 50 まで出す**
    （2026-08-20 レビュー 要修正1・実測）。
    """

    kind: SectionKind
    text: str
    attachable: tuple[KarteDoc, ...]
    count: int = 0
    listed_count: int = 0


EMPTY_SECTION = DocumentsSection(kind="none", text="", attachable=(), count=0, listed_count=0)
# 旧名（本モジュール内の参照互換）。
_EMPTY_SECTION = EMPTY_SECTION


def to_docs(rows: Any) -> list[KarteDoc]:
    """``list_documents_for_client`` の戻り（行 dict の list）を KarteDoc へ写す。

    - 資料名（title）が空の行は落とす（名前を出せない資料は提示しても意味が無い）
    - (title, source_uri) が同じ行は 1 件に畳む（同じ資料の重複表示を防ぐ）
    - 入力が list でない（DI モック等）場合は空リスト（fail-open）
    """
    if not isinstance(rows, list):
        return []
    docs: list[KarteDoc] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        source_uri = str(row.get("source_uri") or "").strip()
        key = (title, source_uri)
        if key in seen:
            continue
        seen.add(key)
        docs.append(
            KarteDoc(
                title=title,
                source_uri=source_uri,
                source_type=str(row.get("source_type") or "").strip(),
                cls_project=_opt_str(row.get("cls_project")),
                client_name=_opt_str(row.get("client_name")),
            )
        )
    return docs


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def belongs_to_client(doc: KarteDoc, client_name: str) -> bool:
    """行の顧客メタが「要求された顧客」と矛盾しないなら True（＝出してよい）。

    ``list_documents_for_client`` の WHERE は
    ``cls_project ILIKE '%name%' OR client_name ILIKE '%name%' OR title ILIKE '%name%'`` の
    **部分一致**なので、「花王」と言っただけで
    ``competitor_比較_花王_vs_ライオン.pdf``（行の client_name は「ライオン」）が返る。
    実測（2026-08-20 レビュー 要修正2）ではその他社資料が一覧に載り、**実ファイルとして
    本人 DM にアップロードされていた**。宛先は本人なので RLS 越えではないが、
    資料の拡散トリガが「明示依頼（knowledge_deliver）」から「顧客名を 1 回言う」へ
    降りてしまう。

    判定は **行の client_name 列だけ**で行う（``matches_project`` は案件名照合であって
    顧客同定ではない）:
      - メタが空 → 判定できない。落とさない（fail-open。ingest が client_name を
        埋めていない行が実在し、そこを切ると機能が主経路で 1 度も動かない）
      - メタがある → 要求顧客と **どちらかがどちらかを含む**なら同一顧客とみなす
        （「花王」⊂「花王株式会社」を落とさないため。「ライオン」と「花王」は
        どちらも含まないので落ちる）
    """
    owner = (doc.client_name or "").strip().casefold()
    if not owner:
        return True
    needle = client_name.strip().casefold()
    if not needle:
        return True
    return owner in needle or needle in owner


def matches_project(doc: KarteDoc, project_name: str) -> bool:
    """資料が案件名に一致するか（title / cls_project / client_name の部分一致・大小無視）。

    照合先は ``list_documents_for_client`` の WHERE と同じ 3 つに揃える
    （案件名の正本キー ``client_case`` は同メソッドが射影しないため使えない）。
    """
    needle = project_name.strip().casefold()
    if not needle:
        return False
    for field in (doc.title, doc.cls_project, doc.client_name):
        if field and needle in field.casefold():
            return True
    return False


def rank_documents(
    docs: list[KarteDoc], project_name: str | None
) -> tuple[list[KarteDoc], list[KarteDoc]]:
    """(案件名に一致した資料, それ以外) に分ける。順序は入力（= 新しい順）を保つ。"""
    if not project_name or not project_name.strip():
        return [], list(docs)
    matched: list[KarteDoc] = []
    rest: list[KarteDoc] = []
    for doc in docs:
        (matched if matches_project(doc, project_name) else rest).append(doc)
    return matched, rest


def slack_label(text: str) -> str:
    """Slack のリンクラベルとして安全な形にする（mrkdwn の特殊文字を潰す）。"""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # ``<url|label>`` の区切りに使われるため、ラベル内の "|" はリンクを壊す。
    return escaped.replace("|", "／")


def doc_line(doc: KarteDoc) -> str:
    """資料 1 行。URL が解決できた資料だけリンクにし、できない資料は名前だけ出す。"""
    url = doc.url
    label = slack_label(doc.title)
    return f"• <{url}|{label}>" if url else f"• {label}"


def build_documents_section(
    *,
    client_name: str,
    project_name: str | None,
    docs: list[KarteDoc],
) -> DocumentsSection:
    """カルテ末尾に足す関連資料セクションを決定論で組み立てる。

    返り値の ``attachable`` は「実ファイルを添付してよい資料」:
    - 案件名あり・一致あり → **一致資料だけ**（残り枠を無関係資料で埋めない）
    - 案件名なし          → 顧客資料の新しい順（要求「カルテを出した時点で資料も一緒に」）
    - 提案分岐（offer, 案件一致 0 件）→ 空タプル＝自動添付しない（お送りしますか？と聞くだけ）

    どの分岐でも候補は ``LIST_MAX`` 行の一覧の内側に収める。添付上限 K <= LIST_MAX なので、
    **添付した資料は必ず一覧に表示されている**（表示されていない資料を送らない）。
    """
    # 他社案件の資料をここで落とす。一覧・提案・自動添付の **全部**が
    # このフィルタの後ろにあるので、「一覧には出るが添付はしない」のような
    # 半端な穴が構造的に作れない（2026-08-20 レビュー 要修正2 / 指摘5）。
    docs = [d for d in docs if belongs_to_client(d, client_name)]
    if not docs:
        return _EMPTY_SECTION

    has_project = bool(project_name and project_name.strip())
    matched, rest = rank_documents(docs, project_name)
    if has_project and not matched:
        return _build_offer_section(
            client_name=client_name,
            project_name=project_name or "",
            docs=rest,
        )

    # 案件名に一致した資料を上位に、その後ろを新しい順（= 取得順）で並べる。
    ordered = matched + rest
    total = len(ordered)
    listed = ordered[:LIST_MAX]
    lines = [f"📎 関連資料（{total}件）"]
    lines.extend(doc_line(d) for d in listed)
    if total > LIST_MAX:
        lines.append(f"…ほか{total - LIST_MAX}件")
    # 一覧は全件を対象にする。送ってよいのは、案件を名指しされたなら一致資料だけ・
    # 名指しが無いなら新しい順の先頭（どちらも一覧に見えている行の内側）。
    candidates = matched if has_project else ordered
    return DocumentsSection(
        kind="matched",
        text="\n".join(lines),
        attachable=tuple(candidates[:LIST_MAX]),
        count=total,
        listed_count=len(listed),
    )


def _build_offer_section(
    *, client_name: str, project_name: str, docs: list[KarteDoc]
) -> DocumentsSection:
    """案件一致 0 件でも「無い」で終わらせず、実在する別資料を差し出す。"""
    if not docs:
        return _EMPTY_SECTION
    offered = docs[:OFFER_MAX]
    head = (
        f"📎 「{slack_label(project_name.strip())}」の資料は見つかりませんでした。"
        f"{slack_label(client_name.strip())}さんでは"
        f"「{slack_label(offered[0].title)}」など{len(docs)}件あります。"
        "お送りしますか？"
    )
    lines = [head]
    lines.extend(doc_line(d) for d in offered)
    # 提案しただけでは送らない（attachable は空）。
    return DocumentsSection(
        kind="offer",
        text="\n".join(lines),
        attachable=(),
        count=len(docs),
        listed_count=len(offered),
    )


def is_dm_surface(channel_id: Any) -> bool:
    """呼び出し元が「依頼者本人しか読まない面」なら True（Slack の ``D…`` だけ）。

    **判定できないときは False**（＝チャンネル扱い）に倒す。これは
    ``slack_summary._is_channel_surface`` の裏返しでは **ない**ので注意:

      - slack_summary は「出力を止めるか」を決めるので、宛先不明（空）は
        本人 DM へフォールバックする＝本人の可視範囲を出ない＝False（安全）。
      - こちらは「資料名をその場に出してよいか」を決める。**出してよいと言い切れる**
        のは D… と分かったときだけで、空 / None は「チャンネルかもしれない」。
        よって空は DM ではない側（＝資料名を出さない側）へ倒す。

    実測（2026-08-20）: 本番の常時経路は OpenClaw → mcp_gateway dispatch で、
    ``_resolve_metadata`` が署名済み caller claim の ``channel_id`` を metadata へ載せる
    （mcp_gateway/server.py）。一方 EC2 systemd の slack_bot 経路（``run_karte``）は
    channel_id を metadata に入れず、応答は ``response_type="in_channel"`` で返る。
    その経路でも安全側（＝資料名をチャンネルに出さない）になるのがこの既定値。

    判定は ``caller_claim`` が署名 claim に要求するのと **同じ形**
    （``^[CDG][A-Z0-9]{8,}$``）の D 版で fullmatch する。``startswith("D")`` や
    ``"D" in channel_id`` のような prefix / substring 判定は、公開チャンネル ID に
    D が普通に含まれる（例 ``C08D3KQ7ABC``）ため一撃で裁定 A を破る
    （2026-08-20 レビュー 要修正1・変異 M4 で実測）。
    """
    if not isinstance(channel_id, str):
        return False
    return bool(_DM_CHANNEL_RE.fullmatch(channel_id.strip()))


def channel_notice(section: DocumentsSection, *, delivered: int = 0) -> str:
    """資料セクションを本人 DM へ **転送できたとき**にチャンネルへ出す 1 行の通知。

    ここに外部由来の文字列（資料名・案件名・顧客名）を混ぜないこと。混ぜた瞬間に
    「チャンネルには資料名を出さない」という裁定 A が壊れる。数字だけを出す。

    数字は ``listed_count``（＝ DM に **名前を書いた**件数）と ``delivered``
    （＝実ファイルを送れた件数）だけ。在庫総数 ``count`` を「お送りしました」の主語に
    しない: 公開チャンネルへ「その顧客の社内資料が何件あるか」を出してしまううえ、
    ``KARTE_ATTACH_DOCS_MAX=0`` で 1 件も送っていないのに「8件をお送りしました」と
    断言していた（2026-08-20 レビュー 要修正1 / 指摘6・実測）。
    """
    if section.kind == "none" or section.listed_count <= 0:
        return ""
    if section.kind == "matched" and delivered > 0:
        return (
            f"📎 関連資料{section.listed_count}件（うち実ファイル{delivered}件）を"
            " DM でお送りしました"
        )
    return f"📎 関連資料{section.listed_count}件のご案内を DM でお送りしました"


def attachment_only_notice(delivered: int) -> str:
    """実ファイルは DM へ届いたが、一覧の DM 転送に失敗したときの 1 行（数字だけ）。

    ここを空文字にすると「聞いてもいない資料が DM に湧いて、チャンネルには何の説明も
    出ない」＝機能が黙って半分死ぬ状態が利用者から不可視になる
    （2026-08-20 レビュー 要修正3(b)）。送れた事実だけを、件数で正直に述べる。
    """
    if delivered <= 0:
        return ""
    return (
        f"📎 関連資料の実ファイル{delivered}件を DM でお送りしました"
        "（一覧はお送りできませんでした）"
    )


def availability_notice(section: DocumentsSection) -> str:
    """副作用を 1 つも出さない経路（L2 オーケストレーターの中間ステップ）用の 1 行。

    ``run_agent`` の戻り値はエージェントへの材料だが、L2 の最終回答は **呼ばれた
    チャンネルへ出る**。従来はこの経路だけ資料名＋Drive リンクを戻り値に載せており、
    ``USE_AGENT_ORCHESTRATOR`` を ON にした瞬間に裁定 A が破れた
    （2026-08-20 レビュー 指摘4）。DM 面でない限りここも件数だけにする。
    まだ 1 通も送っていないので「お送りしました」とは言わない。
    """
    if section.kind == "none" or section.listed_count <= 0:
        return ""
    return f"📎 関連資料{section.listed_count}件が見つかりました（DM でお送りできます）"


def dm_forward_text(*, client_name: str, section_text: str, attach_note: str) -> str:
    """チャンネルから追い出した資料セクションを、本人 DM へ出す形に組み立てる。

    DM 側には「どの顧客のカルテの話か」が無いと文脈が切れる（提案分岐では実ファイルの
    initial_comment すら付かない）ので、先頭に顧客名の 1 行を足す。顧客名は外部由来なので
    ``slack_label()`` を通す。
    """
    head = f"🗂️ 「{slack_label(client_name.strip())}」のカルテの関連資料です。"
    body = f"{head}\n{section_text}"
    return f"{body}\n{attach_note}" if attach_note else body


__all__ = [
    "EMPTY_SECTION",
    "LIST_MAX",
    "OFFER_MAX",
    "DocumentsSection",
    "KarteDoc",
    "attachment_only_notice",
    "availability_notice",
    "belongs_to_client",
    "build_documents_section",
    "channel_notice",
    "dm_forward_text",
    "doc_line",
    "is_dm_surface",
    "matches_project",
    "rank_documents",
    "slack_label",
    "to_docs",
]
