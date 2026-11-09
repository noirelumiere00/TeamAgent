"""Human-reviewed product taxonomy, shared by the library UI and read-only MCP."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from html import escape
from typing import Any, Literal
from urllib.parse import quote

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

KIND = "product_classification"
VERSION = "v1"
CATEGORIES = {
    "makeup": "メイク",
    "skincare": "スキンケア",
    "fragrance": "香水・フレグランス",
    "nail": "ネイル",
    "haircare": "ヘアケア",
    "bodycare": "ボディケア",
    "other": "その他",
}
GROUPS = {"cosmetics": ("makeup", "skincare", "fragrance", "nail")}
ALIASES = {
    "化粧品": "cosmetics",
    "コスメ": "cosmetics",
    "香水": "fragrance",
    "フレグランス": "fragrance",
    **{label: code for code, label in CATEGORIES.items()},
}
Category = Literal["makeup", "skincare", "fragrance", "nail", "haircare", "bodycare", "other"]


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    product_category: Category | None = None
    brand: str = Field(default="", max_length=200)
    product_name: str = Field(default="", max_length=200)
    evidence: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def evidence_required(self) -> Product:
        if self.product_category and not self.evidence.strip():
            raise ValueError("分類には商品情報を確認した根拠が必要です")
        for value in (self.brand, self.product_name, self.evidence):
            if any(ord(char) < 32 for char in value):
                raise ValueError("制御文字は使えません")
        self.brand = self.brand.strip()
        self.product_name = self.product_name.strip()
        self.evidence = self.evidence.strip()
        return self


def category_ids(value: str) -> set[str]:
    value = unicodedata.normalize("NFKC", value).strip().casefold()
    code = ALIASES.get(value, value)
    if code in GROUPS:
        return set(GROUPS[code])
    if code in CATEGORIES:
        return {code}
    raise HTTPException(400, "商品分類は get_search_options の候補から選んでください")


def normalize(params: Mapping[str, str]) -> tuple[dict[str, str], set[str] | None]:
    """Category words are strict product filters, never broad industry matches."""
    result = {key: value for key, value in params.items() if not key.startswith("_")}
    query = unicodedata.normalize("NFKC", params.get("q", ""))
    words = query.split()
    if max(len(query), len(params.get("q", ""))) > 500 or len(words) > 10:
        raise HTTPException(400, "検索語は500文字・10語以内で指定してください")
    selected: set[str] | None = None
    terms = []
    for word in words:
        value = word.lstrip("#").casefold()
        if value in ALIASES or value in GROUPS or value in CATEGORIES:
            ids = category_ids(value)
            selected = ids if selected is None else selected & ids
        else:
            terms.append(word)
    if params.get("product_category"):
        value = unicodedata.normalize("NFKC", params["product_category"]).strip()
        ids = category_ids(value)
        selected = ids if selected is None else selected & ids
    result["q"] = " ".join(terms)
    return result, selected


VALID = """p.payload->>'status'='verified' AND p.payload->>'source'='human'
    AND p.payload->>'product_category' IN
        ('makeup','skincare','fragrance','nail','haircare','bodycare','other')
    AND length(trim(p.payload->>'evidence'))>0"""
LOOKUP = """FROM vl_video_analysis p WHERE p.platform=v.platform AND p.video_id=v.video_id
    AND p.kind='product_classification' AND p.version='v1'"""
VERIFIED = f"EXISTS (SELECT 1 {LOOKUP} AND {VALID})"  # nosec B608
PROJECTION = f"""(SELECT jsonb_build_object(
    'brand',p.payload->>'brand','product_name',p.payload->>'product_name',
    'product_category',CASE WHEN {VALID} THEN p.payload->>'product_category' END,
    'classification_status',CASE WHEN {VALID} THEN 'verified' ELSE 'unclassified' END)
    {LOOKUP} AND p.payload->>'source'='human') AS product_metadata"""  # nosec B608


def clauses(params: Mapping[str, str], ids: set[str] | None) -> tuple[list[str], list[Any]]:
    conditions = []
    values: list[Any] = []
    if ids is not None:
        conditions.append(
            f"EXISTS (SELECT 1 {LOOKUP} AND {VALID} "
            "AND p.payload->>'product_category'=ANY(%s::text[]))"
        )
        values.append(sorted(ids))
    for field in ("brand", "product_name"):
        if params.get(field):
            if len(params[field]) > 200:
                raise HTTPException(400, "商品名・ブランドは200文字以内で指定してください")
            conditions.append(
                f"EXISTS (SELECT 1 {LOOKUP} AND p.payload->>'source'='human' "
                f"AND p.payload->>'{field}'=%s)"
            )
            values.append(params[field])
    return conditions, values


def metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("product_metadata") or {}
    verified = (
        value.get("classification_status") == "verified"
        and value.get("product_category") in CATEGORIES
    )
    return {
        "brand": value.get("brand") or None,
        "product_name": value.get("product_name") or None,
        "product_category": value.get("product_category") if verified else None,
        "classification_status": "verified" if verified else "unclassified",
    }


def reasons(row: Mapping[str, Any], params: Mapping[str, str]) -> list[str]:
    from teamagent.connect_web.library import parse_search

    normalized, ids = normalize(params)
    product = metadata(row)
    result = []
    if ids is not None and product["product_category"] in ids:
        result.append("確認済み商品分類: " + CATEGORIES[product["product_category"]])
    for field, label in (
        ("advertiser", "広告主"),
        ("campaign", "案件"),
        ("industry", "業種"),
        ("purpose", "目的"),
        ("brand", "ブランド"),
        ("product_name", "商品"),
    ):
        if normalized.get(field):
            result.append(label + "の完全一致")
    terms, is_pr = parse_search(normalized)
    if terms:
        result.append("検索語のAND一致")
    if is_pr is not None:
        result.append("PRあり" if is_pr else "PRなし")
    if normalized.get("category"):
        result.append("区分の完全一致")
    return result


def relevance(params: Mapping[str, str]) -> tuple[str, list[Any]]:
    from teamagent.connect_web.library import parse_search

    normalized, ids = normalize(params)
    terms, _ = parse_search(normalized)
    scores = ["100"] if ids is not None else []
    values: list[Any] = []
    for term in terms:
        scores.append(
            "(CASE WHEN lower(v.advertiser)=lower(%s) OR lower(v.campaign)=lower(%s) "
            f"OR EXISTS (SELECT 1 {LOOKUP} AND p.payload->>'source'='human' "
            "AND (lower(p.payload->>'brand')=lower(%s) "
            "OR lower(p.payload->>'product_name')=lower(%s))) "
            "THEN 200 WHEN v.caption ILIKE %s THEN 100 ELSE 10 END)"  # nosec B608
        )
        values.extend([term, term, term, term, "%" + term + "%"])
    return " + ".join(scores) or "0", values


def applied_filters(params: Mapping[str, str]) -> dict[str, Any]:
    normalized, ids = normalize(params)
    result: dict[str, Any] = {
        key: value for key, value in normalized.items() if value and key not in {"page", "sort"}
    }
    result.pop("product_category", None)
    if ids is not None:
        result["product_categories"] = sorted(ids)
    return result


def pending_count(conn: Any, params: Mapping[str, str], *, admin: bool) -> int | None:
    from teamagent.connect_web.library import _where

    normalized, ids = normalize(params)
    if ids is None:
        return None
    normalized.pop("product_category", None)
    where, values, _, _ = _where(normalized, admin)
    row = conn.execute(
        f"SELECT count(*) AS n FROM vl_videos_effective v WHERE {where} "  # nosec B608
        f"AND NOT ({VERIFIED})",
        values,
    ).fetchone()
    return int(row["n"])


OPTION_FIELDS = {
    "industry": "v.industry",
    "purpose": "v.purpose",
    "advertiser": "v.advertiser",
    "brand": "(SELECT p.payload->>'brand' " + LOOKUP + " AND p.payload->>'source'='human')",
}


def options(conn: Any, field: str, query: str, *, admin: bool) -> dict[str, Any]:
    if field == "product_category":
        items = [{"value": "cosmetics", "label": "化粧品（メイク・スキンケア・香水・ネイル）"}]
        items += [{"value": code, "label": label} for code, label in CATEGORIES.items()]
        return {
            "options": [item for item in items if query in item["label"] or query in item["value"]],
            "has_more": False,
        }
    column = OPTION_FIELDS[field]
    visibility = "true" if admin else "NOT v.is_private"
    rows = conn.execute(
        f"SELECT DISTINCT {column} AS value FROM vl_videos_effective v "  # nosec B608
        f"WHERE {visibility} AND {column} IS NOT NULL AND {column}<>'' "
        f"AND {column} ILIKE %s ORDER BY value LIMIT 201",
        ("%" + query + "%",),
    ).fetchall()
    return {
        "options": [{"value": row["value"], "label": row["value"]} for row in rows[:200]],
        "has_more": len(rows) > 200,
    }


def validate_options(conn: Any, params: Mapping[str, str], *, admin: bool) -> None:
    visibility = "true" if admin else "NOT v.is_private"
    for field, column in OPTION_FIELDS.items():
        if params.get(field):
            row = conn.execute(
                f"SELECT EXISTS (SELECT 1 FROM vl_videos_effective v "  # nosec B608
                f"WHERE {visibility} AND {column}=%s) AS found",
                (params[field],),
            ).fetchone()
            if not row["found"]:
                raise HTTPException(400, "get_search_options で絞り込み候補を確認してください")


def search_cases(
    conn: Any, params: Mapping[str, str], *, admin: bool, page_size: int
) -> dict[str, Any]:
    from teamagent.connect_web.library import _METRICS_JOIN, _where

    if type(page_size) is not int or not 1 <= page_size <= 20:
        raise ValueError("Invalid page size")
    where, values, page, _ = _where(params, admin)
    rank, rank_values = relevance(params)
    visible = "true" if admin else "NOT v.is_private"
    cte = f"""WITH matched AS (
        SELECT v.advertiser,v.campaign,v.origin,v.posted_at,{PROJECTION},
            {rank} AS relevance,m.total_plays,m.captured_on
        FROM vl_videos_effective v {_METRICS_JOIN} WHERE {where}
    ), cases AS (
        SELECT advertiser,campaign,origin,count(*) AS matched_video_count,
            CASE WHEN count(total_plays)=count(*) THEN sum(total_plays) END
                AS matched_total_plays,
            max(posted_at) AS last_posted,max(relevance) AS relevance,
            array_agg(DISTINCT product_metadata->>'product_category')
                FILTER (WHERE product_metadata->>'product_category' IS NOT NULL)
                AS product_categories,
            array_agg(DISTINCT product_metadata->>'brand')
                FILTER (WHERE coalesce(product_metadata->>'brand','')<>'') AS brands,
            min(captured_on) AS metrics_captured_from,max(captured_on) AS metrics_captured_to
        FROM matched WHERE coalesce(advertiser,'')<>'' AND coalesce(campaign,'')<>''
        GROUP BY advertiser,campaign,origin
    ), full_cases AS (
        SELECT v.advertiser,v.campaign,v.origin,count(*) AS case_video_count,
            CASE WHEN count(m.total_plays)=count(*) THEN sum(m.total_plays) END AS case_total_plays
        FROM vl_videos_effective v {_METRICS_JOIN} WHERE {visible}
        GROUP BY v.advertiser,v.campaign,v.origin
    )"""  # nosec B608
    args = [*rank_values, *values]
    counts = conn.execute(
        cte
        + """SELECT (SELECT count(*) FROM cases) AS n,
            (SELECT count(*) FROM matched WHERE coalesce(advertiser,'')=''
             OR coalesce(campaign,'')='') AS unassigned_video_count""",  # nosec B608
        args,
    ).fetchone()
    orders = {
        "relevance": "c.relevance DESC,c.matched_total_plays",
        "plays": "c.matched_total_plays",
        "new": "c.last_posted",
    }
    order = orders[params.get("sort", "relevance")]
    rows = conn.execute(
        cte
        + f"""SELECT c.*,f.case_video_count,f.case_total_plays FROM cases c
            JOIN full_cases f USING (advertiser,campaign,origin)
            ORDER BY {order} DESC NULLS LAST,c.advertiser,c.campaign,c.origin
            LIMIT %s OFFSET %s""",  # nosec B608
        [*args, page_size, (page - 1) * page_size],
    ).fetchall()
    return {
        "cases": rows,
        "total": counts["n"],
        "unassigned_video_count": counts["unassigned_video_count"],
    }


def editor(platform: str, video_id: str, payload: Mapping[str, Any] | None) -> str:
    product = payload or {}
    action = "/library/v/" + quote(platform, safe="") + "/" + quote(video_id, safe="") + "/product"
    body = (
        '<section class="metric-group"><h2>商品情報（管理者確認）</h2>'
        "<p>業種・案件名は変更しません。商品を確認できない場合は未分類にします。</p>"
        f'<form method="post" action="{escape(action, quote=True)}">'
        '<label>商品分類<select name="product_category"><option value="">未分類</option>'
    )
    for code, label in CATEGORIES.items():
        selected = " selected" if product.get("product_category") == code else ""
        body += f'<option value="{code}"{selected}>{label}</option>'
    body += "</select></label>"
    for field, label, length in (
        ("brand", "ブランド", 200),
        ("product_name", "商品名", 200),
        ("evidence", "確認した商品情報・根拠（分類時は必須）", 500),
    ):
        value = escape(str(product.get(field) or ""), quote=True)
        body += f'<label>{label}<input name="{field}" maxlength="{length}" value="{value}"></label>'
    return body + "<button>商品情報を保存</button></form></section>"


def save(conn: Any, platform: str, video_id: str, product: Product, email: str) -> None:
    payload = {
        **product.model_dump(),
        "source": "human",
        "reviewed_by": email,
        "status": "verified" if product.product_category else "unclassified",
    }
    conn.execute(
        """INSERT INTO vl_video_analysis(platform,video_id,kind,version,payload)
           VALUES (%s,%s,%s,%s,%s::jsonb)
           ON CONFLICT (platform,video_id,kind,version)
           DO UPDATE SET payload=EXCLUDED.payload,analyzed_at=now()""",
        (platform, video_id, KIND, VERSION, json.dumps(payload, ensure_ascii=False)),
    )
