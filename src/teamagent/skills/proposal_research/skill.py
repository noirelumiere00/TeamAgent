"""検索メモ → 出典番号のJSON → 実測TikTok → strictなv3 JSON。"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from teamagent.adapters.gemini_client import (
    GeminiClient,
    GeminiGroundedResponse,
    GeminiResponse,
    GroundingSupport,
    _is_retryable_vertex,
)
from teamagent.adapters.retry import retry_long_job_once
from teamagent.adapters.source_url_check import (
    GROUNDING_REDIRECT_HOST,
    SourceUrlChecker,
    UrlCheckResult,
)
from teamagent.adapters.tiktok_scraper import TikTokSearchResult, search_tiktok
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.omiyage_report.skill import (
    _configured_search_timeout_seconds,
    configured_search_depth,
)
from teamagent.skills.proposal_builder.research import parse_gemini_research
from teamagent.skills.proposal_research.brief import ResearchBrief
from teamagent.skills.proposal_research.schema import (
    IntermediateProductMeta,
    IntermediateResearch,
    ProposalResearchOutput,
    ResearchSummary,
)

_JST = timezone(timedelta(hours=9), "JST")  # 本番に IANA の時間帯データが無い（r48）
_SECTIONS = (
    "A_market_data",
    "B_social_trend",
    "F_competitor",
    "G_insight_H_event",
    "D_publicity",
    "E_community",
)
#: 利用者へ返る失敗の文に出す区分名（内部の区分キーは出さない）。
_SECTION_LABELS = {
    "A_market_data": "市場・インサイト",
    "B_social_trend": "ソーシャルの動き",
    "C_tiktok": "TikTok の代表投稿",
    "D_publicity": "パブリシティの文脈",
    "E_community": "界隈",
    "F_competitor": "競合",
    "G_insight": "不満と欲求",
    "G_insight_H_event": "不満と欲求・季節とイベント",
    "H_event": "季節とイベント",
    "product_meta": "商材の基本情報",
}


def _label(section: str) -> str:
    return _SECTION_LABELS.get(section, section)


def _prompt(name: str) -> str:
    return load_prompt("proposal_research", "v1", name)


_URL = re.compile(
    r"https?://(?:\[[0-9a-fA-F:]+\]|[^\s/\[\]（）()<>\x00、。；：！？\"']+)"
    r"[A-Za-z0-9\-._~:/?#@!$&*+,;=%]*",
    re.IGNORECASE,
)
_REF = re.compile(r"\[S([1-9][0-9]*)\]")
_TOTAL_UNAVAILABLE = "取得不可（UI非表示）"
_POST_PATH = re.compile(r"^/@[^/]+/video/[1-9][0-9]*/?$")
_TIKTOK_TERMS = 6
SOURCE_STAGE_DEADLINE_S = 60.0


class ResearchError(ValueError):
    """区分名だけを安全に利用者へ伝える調査失敗。"""


class _Gemini(Protocol):
    def generate_with_google_search(
        self,
        prompt: str,
        request_id: str,
        *,
        system: str | None = None,
        timeout_s: float | None = None,
    ) -> GeminiGroundedResponse: ...

    def generate_text(
        self,
        prompt: str,
        request_id: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
    ) -> GeminiResponse: ...


class _UrlChecker(Protocol):
    def resolve_grounding_redirect(self, uri: str) -> str: ...

    def verify_public_url(self, url: str) -> UrlCheckResult: ...


@dataclass(frozen=True)
class _Source:
    number: str
    url: str
    title: str
    usable: bool
    confirmed: bool


def _memo_text(text: str) -> str:
    # 本文のURL/モデル自作の番号は証拠にしない。supportsも同じ変換で照合する。
    return _REF.sub("", _URL.sub("", text))


def _contains_name(text: str, name: str) -> bool:
    """未発表商材名の簡易な照合（NFKC・casefold・空白無視）。

    止めるためではなく、TikTok の検索語を飛ばす防御用。
    """

    def fold(value: str) -> str:
        return "".join(unicodedata.normalize("NFKC", value).casefold().split())

    needle = fold(name)
    return bool(needle) and needle in fold(text)


def _support_span(raw: str, support: GroundingSupport) -> tuple[int, int] | None:
    """UTF-8 境界と本文の一致を確認し、位置が信頼できる場合だけ採用する。"""
    if support.end_byte is None:
        return None
    encoded = raw.encode("utf-8")
    if support.end_byte < 0 or support.end_byte > len(encoded):
        return None
    try:
        prefix = encoded[: support.end_byte].decode("utf-8")
        if support.start_byte is not None:
            if support.start_byte < 0 or support.start_byte > support.end_byte:
                return None
            segment = encoded[support.start_byte : support.end_byte].decode("utf-8")
            if segment.strip() != support.text.strip():
                return None
    except UnicodeDecodeError:
        return None
    prefix = prefix.rstrip()
    fragment = support.text.strip()
    if not fragment or not prefix.endswith(fragment):
        return None
    return len(prefix) - len(fragment), len(prefix)


def _retryable_check(result: UrlCheckResult) -> bool:
    return not result.ok and (
        result.reason in {"timeout", "http_error", "dns_failed", "deadline_exceeded"}
        or (result.status_code or 0) >= 500
    )


class _Sources:
    def __init__(self, checker: _UrlChecker) -> None:
        self.checker = checker
        self.by_url: dict[str, _Source] = {}
        self.by_number: dict[str, _Source] = {}
        self._resolved: dict[str, tuple[str, bool]] = {}
        self._checks: dict[str, UrlCheckResult] = {}

    def prepare(
        self,
        responses: dict[str, GeminiGroundedResponse],
    ) -> tuple[dict[str, str], dict[str, set[str]]]:
        uris = list(
            dict.fromkeys(
                source.uri
                for response in responses.values()
                for source in response.sources
                if source.uri
            )
        )

        def resolve(uri: str) -> tuple[str, bool]:
            try:
                resolved_url = self.checker.resolve_grounding_redirect(uri)
                return resolved_url, urlsplit(resolved_url).hostname != GROUNDING_REDIRECT_HOST
            except Exception:
                return uri, False

        deadline = time.monotonic() + SOURCE_STAGE_DEADLINE_S
        pool = ThreadPoolExecutor(max_workers=8)
        try:
            new_uris = [uri for uri in uris if not self._resolved.get(uri, ("", False))[1]]
            resolving = {uri: pool.submit(resolve, uri) for uri in new_uris}
            resolved_done, _ = wait(resolving.values(), timeout=max(0, deadline - time.monotonic()))
            self._resolved.update(
                (uri, future.result() if future in resolved_done else (uri, False))
                for uri, future in resolving.items()
            )
            resolved = {uri: self._resolved[uri] for uri in uris}
            urls = list(
                dict.fromkeys(
                    url
                    for url, ok in resolved.values()
                    if ok and (url not in self._checks or _retryable_check(self._checks[url]))
                )
            )

            def verify(url: str) -> UrlCheckResult:
                try:
                    return self.checker.verify_public_url(url)
                except Exception:
                    return UrlCheckResult(url=url, final_url=url, ok=False, reason="http_error")

            verifying = {url: pool.submit(verify, url) for url in urls}
            verified_done, _ = wait(verifying.values(), timeout=max(0, deadline - time.monotonic()))
            self._checks.update(
                (
                    url,
                    future.result()
                    if future in verified_done
                    else UrlCheckResult(url=url, final_url=url, ok=False, reason="timeout"),
                )
                for url, future in verifying.items()
            )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        for response in responses.values():
            for source in response.sources:
                if not source.uri:
                    continue
                url, resolved_ok = resolved[source.uri]
                result = self._checks.get(url)
                usable = resolved_ok and result is not None and result.ok
                confirmed = usable and result is not None and not result.bot_blocked
                if url not in self.by_url:
                    record = _Source(
                        number=f"S{len(self.by_url) + 1}",
                        url=url,
                        title=_memo_text(source.title),
                        usable=usable,
                        confirmed=confirmed,
                    )
                    self.by_url[url] = record
                    self.by_number[record.number] = record
                elif usable:
                    record = replace(self.by_url[url], usable=usable, confirmed=confirmed)
                    self.by_url[url] = record
                    self.by_number[record.number] = record

        memos: dict[str, str] = {}
        memo_refs: dict[str, set[str]] = {}
        for section, response in responses.items():
            raw = response.text
            marker = "\0grounding:"
            while marker in raw:
                marker += ":"
            insertions: dict[int, list[str]] = {}
            occupied: list[tuple[int, int]] = []
            refs: set[str] = set()
            for support in sorted(
                response.supports,
                key=lambda item: (_support_span(raw, item) is not None, len(item.text)),
                reverse=True,
            ):
                fragment = support.text.strip()
                if not _memo_text(fragment).strip():
                    continue
                records = [
                    self.by_url[resolved[response.sources[index].uri][0]]
                    for index in support.source_indices
                    if 0 <= index < len(response.sources)
                    and response.sources[index].uri in resolved
                ]
                span = _support_span(raw, support)
                if span is None:
                    positions = [
                        match.start()
                        for match in re.finditer(re.escape(fragment), raw)
                        if not any(
                            match.start() < end and start < match.end() for start, end in occupied
                        )
                    ]
                    if len(positions) == 1:
                        position = positions[0]
                        end = position + len(fragment)
                        if not any(
                            position < used_end and used_start < end
                            for used_start, used_end in occupied
                        ):
                            span = position, end
                if span is None:
                    continue
                occupied.append(span)
                suffix = insertions.setdefault(span[1], [])
                for record in records:
                    refs.add(record.number)
                    if record.number not in suffix:
                        suffix.append(record.number)
            for end in sorted(insertions, reverse=True):
                raw = raw[:end] + "".join(f"{marker}{ref}\0" for ref in insertions[end]) + raw[end:]
            text = _memo_text(raw)
            for ref in refs:
                text = text.replace(f"{marker}{ref}\0", f"[{ref}]")
            memos[section] = text
            memo_refs[section] = refs if response.grounded else set()
        return memos, memo_refs

    def allowed(self, refs: set[str]) -> set[str]:
        return {ref for ref in refs if self.by_number[ref].usable}


def _strip_urls(value: Any) -> Any:
    if isinstance(value, str):
        return _URL.sub("", value)
    if isinstance(value, list):
        return [_strip_urls(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_urls(item) for key, item in value.items()}
    return value


def _strip_urls_and_refs(value: Any) -> Any:
    if isinstance(value, str):
        return _REF.sub("", _URL.sub("", value)).strip()
    if isinstance(value, list):
        return [clean for item in value if (clean := _strip_urls_and_refs(item)) != ""]
    if isinstance(value, dict):
        return {key: _strip_urls_and_refs(item) for key, item in value.items()}
    return value


def _replace_inline_refs(value: Any, sources: _Sources) -> Any:
    if isinstance(value, str):
        return _REF.sub(lambda match: f"(出典: {sources.by_number['S' + match[1]].url})", value)
    if isinstance(value, list):
        return [_replace_inline_refs(item, sources) for item in value]
    if isinstance(value, dict):
        return {key: _replace_inline_refs(item, sources) for key, item in value.items()}
    return value


def _materialize(
    intermediate: IntermediateResearch,
    sources: _Sources,
    memo_refs: dict[str, set[str]],
) -> tuple[dict[str, Any], Counter[str], set[str]]:
    data: dict[str, Any] = intermediate.model_dump(by_alias=True)
    data["product_meta"] = IntermediateProductMeta.model_validate(
        _strip_urls_and_refs(data["product_meta"]), strict=True
    ).model_dump()
    discarded: Counter[str] = Counter()
    used: set[str] = set()
    for section, url_key in (
        ("A_market_data", "url"),
        ("B_social_trend", "url"),
        ("D_publicity", "evidence_url"),
        ("E_community", "data_url"),
        ("F_competitor", "url"),
        ("H_event", "url"),
    ):
        memo_section = "G_insight_H_event" if section == "H_event" else section
        allowed = sources.allowed(memo_refs[memo_section])

        def item_with_url(
            item: dict[str, Any],
            key: str = url_key,
            allowed: set[str] = allowed,
            section: str = section,
        ) -> dict[str, Any] | None:
            ref = item[key]
            result: dict[str, Any] = _strip_urls(item)
            inline = {f"S{number}" for number in _REF.findall(json.dumps(result))}
            if ref not in allowed or not inline.issubset(allowed):
                discarded[section] += 1
                return None
            if any(
                not _REF.sub("", value).strip()
                for value in result.values()
                if isinstance(value, str)
            ):
                discarded[section] += 1
                return None
            # 名前・タグは検索や対応付けの識別子なので引用文に書き換えない。
            if "name" in result:
                result["name"] = _strip_urls_and_refs(result["name"])
            if section == "E_community":
                result["tiktok_tags"] = _strip_urls_and_refs(result["tiktok_tags"])
            result = _replace_inline_refs(result, sources)
            result[key] = sources.by_number[ref].url
            used.update({ref} | inline)
            return result

        if section == "H_event":
            data[section] = item_with_url(data[section]) or {}
            continue
        kept: list[dict[str, Any]] = []
        for item in data[section]:
            # 補足ごとに出典を検証。親の主張のURLに相乗りさせない。
            alternatives = item.pop("alt_data", None)
            resolved_item = item_with_url(item)
            if resolved_item is None:
                if alternatives:
                    discarded[section] += len(alternatives)
                continue
            if alternatives is not None:
                resolved_item["alt_data"] = [
                    alternative
                    for alt in alternatives
                    if (alternative := item_with_url(alt, "url")) is not None
                ]
            kept.append(resolved_item)
        data[section] = kept

    allowed_g = sources.allowed(memo_refs["G_insight_H_event"])
    insight: dict[str, str] = {}
    for key, text in data["G_insight"].items():
        text = _URL.sub("", text)
        refs = {f"S{number}" for number in _REF.findall(text)}
        if not refs or not refs.issubset(allowed_g) or not _REF.sub("", text).strip():
            discarded["G_insight"] += 1
            continue
        insight[key] = _REF.sub(
            lambda match: f"(出典: {sources.by_number['S' + match[1]].url})",
            text,
        )
        used.update(refs)
    data["G_insight"] = insight if len(insight) == 4 else {}
    # CとEの投稿URLはモデルの文字列を一切採用しない。
    data["C_tiktok"] = []
    for community in data["E_community"]:
        community["tiktok_tags"] = []
    return data, discarded, used


def _missing_sections(data: dict[str, Any]) -> list[str]:
    missing = [
        section
        for section in (
            "A_market_data",
            "B_social_trend",
            "D_publicity",
            "E_community",
            "G_insight",
            "H_event",
        )
        if not data[section]
    ]
    competitors = data["F_competitor"]
    # v3 の決まりは「主要 3 社」。名前が 3 種類でも同じ会社の別商品なら足りない
    # （10-09 実測でロッテ 2 件）。
    if len(competitors) < 3 or len({_company_of(item["name"]) for item in competitors}) < 3:
        missing.append("F_competitor")
    return missing


_COMPANY_SPLIT = re.compile(r"[\s　「『（(／/・]")


def _company_of(name: str) -> str:
    """「会社名 商品名」の先頭を会社名とみなす（区切りが無ければ名前全体）。"""
    head = _COMPANY_SPLIT.split(unicodedata.normalize("NFKC", name).strip(), maxsplit=1)[0]
    return head.casefold() or name.strip().casefold()


class ProposalResearchSkill(BaseSkill[ResearchBrief, ProposalResearchOutput]):
    name: ClassVar[str] = "proposal_research"
    description: ClassVar[str] = "提案書の市場・競合・SNSを出典付きで調査する内部処理"
    input_schema: ClassVar[type[BaseModel]] = ResearchBrief
    output_schema: ClassVar[type[BaseModel]] = ProposalResearchOutput

    def __init__(
        self,
        *,
        gemini: _Gemini | None = None,
        url_checker: _UrlChecker | None = None,
        tiktok_searcher: Callable[..., TikTokSearchResult] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gemini = gemini or GeminiClient.from_env()
        self._checker = url_checker
        self._tiktok_searcher = tiktok_searcher or search_tiktok
        self._clock = clock

    @staticmethod
    def _search_brief(brief: ResearchBrief) -> dict[str, Any]:
        if not brief.unreleased:
            return brief.model_dump()
        # 未発表商材は「消す」のではなく「渡さない」（10-09 方針変更）。自由文の与件は表記ゆれで
        # 名前が漏れるので、検索（段 A）にもまとめ（段 B）にも渡さず、カテゴリ語だけで調べる。
        return {
            "product_name": brief.category_term,
            "official_url": "なし",
            "brief": "",
            "unreleased": True,
            "category_term": brief.category_term,
        }

    def run(self, input: ResearchBrief, ctx: SkillContext) -> ProposalResearchOutput:
        start = self._clock()
        checker = self._checker or SourceUrlChecker()
        try:
            return self._run(input, ctx, start, checker)
        finally:
            if self._checker is None:
                assert isinstance(checker, SourceUrlChecker)
                checker.close()

    def _run(
        self,
        brief: ResearchBrief,
        ctx: SkillContext,
        start: float,
        checker: _UrlChecker,
    ) -> ProposalResearchOutput:
        log = ctx.bind_logger(self.name)
        search_instruction = _prompt("search")
        brief_json = json.dumps(self._search_brief(brief), ensure_ascii=False)

        def search(section: str) -> GeminiGroundedResponse:
            task = _prompt(section)
            search_prompt = f"SECTION: {section}\n{task}\n{brief_json}"
            # 固定の指示・JSONキーは商材名の照合対象にしない。与件の値は上で確認済み。

            def generate() -> GeminiGroundedResponse:
                try:
                    return self._gemini.generate_with_google_search(
                        search_prompt,
                        ctx.request_id,
                        system=search_instruction,
                        timeout_s=90,
                    )
                except Exception as exc:
                    # Vertex の一時エラーを長いジョブ共通の1回再試行へ渡す。
                    if _is_retryable_vertex(exc):
                        raise ConnectionError("temporary research search failure") from exc
                    raise

            try:
                return retry_long_job_once(generate)
            except Exception:
                raise ResearchError(f"調査に失敗しました: {_label(section)}") from None

        log.info("proposal_research_stage", stage="A")
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = dict(zip(_SECTIONS, pool.map(search, _SECTIONS), strict=True))
        cost = sum(response.cost_usd for response in responses.values())
        all_responses: list[GeminiGroundedResponse | GeminiResponse] = list(responses.values())
        sources = _Sources(checker)
        memos, memo_refs = sources.prepare(responses)
        failed = [section for section in _SECTIONS if not sources.allowed(memo_refs[section])]
        if failed:
            with ThreadPoolExecutor(max_workers=3) as pool:
                retries = dict(zip(failed, pool.map(search, failed), strict=True))
            cost += sum(response.cost_usd for response in retries.values())
            all_responses.extend(retries.values())
            responses.update(retries)
            memos, memo_refs = sources.prepare(responses)
            failed = [section for section in _SECTIONS if not sources.allowed(memo_refs[section])]
            if failed:
                raise ResearchError(
                    "調査の出典を確認できませんでした: " + "、".join(map(_label, failed))
                )

        log.info("proposal_research_stage", stage="B")
        instruction = _prompt("structure")
        supported = set().union(*memo_refs.values())
        catalog = [
            {
                "number": record.number,
                "domain": urlsplit(record.url).hostname,
                "title": record.title,
            }
            for record in sources.by_number.values()
            if record.usable and record.number in supported
        ]
        unavailable = [record.number for record in sources.by_number.values() if not record.usable]
        prompt = json.dumps(
            {
                "brief": self._search_brief(brief),
                "annotated_memos": memos,
                "usable_sources": catalog,
                "unusable_sources": unavailable,
                "schema": IntermediateResearch.model_json_schema(),
            },
            ensure_ascii=False,
        )
        error = ""
        shortfall = ""
        for attempt in range(2):
            try:
                response = self._gemini.generate_text(
                    prompt + ("\n" + error if error else ""),
                    ctx.request_id,
                    system=instruction,
                    json_mode=True,
                )
            except Exception:
                raise ResearchError("調査結果のまとめに失敗しました") from None
            cost += response.cost_usd
            all_responses.append(response)
            try:
                intermediate = IntermediateResearch.model_validate_json(response.text, strict=True)
                data, discarded, used = _materialize(intermediate, sources, memo_refs)
                missing = _missing_sections(data)
                if missing:
                    error = "不足した区分: " + ", ".join(missing)
                    shortfall = "（足りない区分: " + "、".join(map(_label, missing)) + "）"
                else:
                    break
            except (ValidationError, ValueError, TypeError) as exc:
                if isinstance(exc, ValidationError):
                    # エラーの入力値は含めず、欄と型だけを再生成へ渡す。
                    locations = [
                        ".".join(map(str, issue["loc"])) + ":" + issue["type"]
                        for issue in exc.errors(include_input=False)
                    ]
                    error = "中間JSONの型または構文が不正: " + ", ".join(locations[:12])
                    broken = sorted(
                        {
                            str(issue["loc"][0])
                            for issue in exc.errors(include_input=False)
                            if issue["loc"] and str(issue["loc"][0]) in _SECTION_LABELS
                        }
                    )
                    shortfall = (
                        "（直せなかった区分: " + "、".join(map(_label, broken)) + "）"
                        if broken
                        else ""
                    )
                else:
                    error = "中間JSONの型または構文が不正"
                    shortfall = ""
            if attempt == 1:
                # 再生成への指示（error）は内部の欄名を含むので、利用者向けの文には出さない。
                raise ResearchError("調査結果を提案書の形にまとめられませんでした" + shortfall)

        log.info("proposal_research_stage", stage="C")
        searches = self._fill_tiktok(data, intermediate, brief, ctx)
        data["research_date"] = datetime.now(_JST).date().isoformat()
        data["brand"] = unicodedata.normalize("NFKC", brief.product_name)
        try:
            validated = parse_gemini_research(data)
        except (ValidationError, ValueError, TypeError):
            raise ResearchError("調査結果の最終確認に失敗しました") from None
        summary = ResearchSummary(
            source_count=len(used),
            unconfirmed_count=sum(not sources.by_number[ref].confirmed for ref in used),
            discarded_count=sum(discarded.values()),
            discarded_by_section=dict(discarded),
            elapsed_seconds=max(0, self._clock() - start),
            gemini_cost_usd=round(cost, 6),
            tiktok_search_count=searches,
        )
        log.info(
            "proposal_research_done",
            source_count=summary.source_count,
            discarded_count=summary.discarded_count,
            latency_ms=int(summary.elapsed_seconds * 1000),
            # 呼び出し別 adapter ログの cost_usd と日次警報で二重計上しない。
            research_cost_usd=summary.gemini_cost_usd,
            tiktok_search_count=searches,
            token_usage={
                "input_tokens": sum(r.input_tokens for r in all_responses),
                "output_tokens": sum(r.output_tokens for r in all_responses),
                "thoughts_tokens": sum(r.thoughts_tokens for r in all_responses),
            },
        )
        return ProposalResearchOutput(
            research_json=validated.model_dump(by_alias=True), summary=summary
        )

    def _fill_tiktok(
        self,
        data: dict[str, Any],
        intermediate: IntermediateResearch,
        brief: ResearchBrief,
        ctx: SkillContext,
    ) -> int:
        candidates: dict[str, list[str]] = {}
        kept_names = {community["name"] for community in data["E_community"]}
        for community in intermediate.e_community:
            community_name = _strip_urls_and_refs(community.name)
            if community_name not in kept_names:
                continue
            candidates[community_name] = [
                cleaned
                for tag in community.tiktok_tags
                if not _URL.search(tag.tag)
                and (cleaned := _strip_urls_and_refs(tag.tag).lstrip("#").strip())
            ]
        # 界隈ごとに少なくとも1候補を先に選び、その後を細かい検索語で埋める。
        terms = [tags[0] for tags in candidates.values() if tags]
        terms.extend(data["product_meta"]["kaiwai_keywords"])
        terms.extend(tag for tags in candidates.values() for tag in tags)
        terms = list(
            dict.fromkeys(
                cleaned
                for term in terms
                if (cleaned := _strip_urls_and_refs(term).lstrip("#").strip())
            )
        )
        if brief.unreleased:
            terms = [term for term in terms if not _contains_name(term, brief.product_name)]
        terms = terms[:_TIKTOK_TERMS]
        searches = 0
        results: dict[str, dict[str, Any]] = {}
        # お土産資料と同じ、検索軸を順次取得・一時的な失敗のみ1回再試行・深度上限。
        for term in terms:
            for search_type in ("hashtag", "keyword"):

                def search(term: str = term, search_type: str = search_type) -> TikTokSearchResult:
                    nonlocal searches
                    searches += 1
                    return self._tiktok_searcher(
                        term,
                        search_type=search_type,
                        max_videos=configured_search_depth(),
                        request_id=ctx.request_id,
                        timeout_s=_configured_search_timeout_seconds(),
                    )

                try:
                    result = retry_long_job_once(search)
                except Exception:
                    continue
                videos = [video for video in result.videos if _valid_post_url(video.url)]
                if not videos:
                    continue
                entry = {
                    "related_tag": term,
                    "representative_post_url": videos[0].url,
                    "search_demand_note": (
                        f"上位 {len(videos)} 本・最多再生 "
                        f"{max(video.play_count for video in videos)} 回"
                    ),
                    "total_count": _TOTAL_UNAVAILABLE,
                }
                results[term] = entry
                data["C_tiktok"].append(entry)
                break
        if not results:
            raise ResearchError("TikTok の代表投稿を取得できませんでした")
        for community in data["E_community"]:
            matching = [term for term in candidates.get(community["name"], []) if term in results]
            community["tiktok_tags"] = [
                {"tag": term, "representative_post_url": results[term]["representative_post_url"]}
                for term in matching
            ]
        return searches


def _valid_post_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in {"www.tiktok.com", "tiktok.com"}
            and parsed.username is None
            and parsed.port in {None, 443}
            and not any(char.isspace() for char in url)
            and bool(_POST_PATH.fullmatch(parsed.path))
        )
    except ValueError:
        return False
