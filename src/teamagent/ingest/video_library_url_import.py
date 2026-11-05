"""URL intake and one-post import, shared by the queue worker and future P3b discovery."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urljoin

import httpx

from teamagent.adapters.video_library_page import (
    PAGE_USER_AGENT,
    cover_url,
    fetch_page,
    save_cover,
    stop_reason,
)
from teamagent.adapters.video_library_storage import VideoLibraryStorage
from teamagent.ingest.video_library_url_models import (
    ImportResult,
    NormalizedURL,
    Source,
    URLImportError,
    normalize_url,
    parse_observation,
)
from teamagent.ingest.video_library_url_repository import JST, Row, URLImportRepository


def expand_short_url(
    client: httpx.Client, target: NormalizedURL, before_request: Callable[[], None]
) -> NormalizedURL:
    seen: set[str] = set()
    for _ in range(5):
        if target.video_id:
            return target
        if target.url in seen:
            raise URLImportError("redirect_loop")
        seen.add(target.url)
        before_request()
        # Streaming is intentional: short-link bodies are never read, including errors.
        with client.stream(
            "GET",
            target.url,
            follow_redirects=False,
            timeout=20,
            headers={"User-Agent": PAGE_USER_AGENT},
        ) as response:
            if response.status_code not in {301, 302, 303, 307, 308}:
                response.raise_for_status()
                raise URLImportError("short_url_unresolved")
            location = response.headers.get("location")
            if not location:
                raise URLImportError("redirect_location_missing")
            if any(ord(char) < 32 or ord(char) == 127 for char in location):
                raise URLImportError("invalid_url")
            target = normalize_url(urljoin(target.url, location), redirect=True)
    if target.video_id:
        return target
    raise URLImportError("redirect_limit")


def import_one_url(
    url: str,
    repository: URLImportRepository,
    client: httpx.Client,
    storage: VideoLibraryStorage,
    *,
    source: Source,
    requested_by: str,
    daily_limit: int = 300,
) -> ImportResult:
    """Fetch and save one post; callers provide a trusted requester and acquisition route."""
    with repository.fetch_lock() as locked:
        if not locked:
            return ImportResult(status="queued", error_code="worker_busy")
        return process_url(
            url,
            repository,
            client,
            storage,
            source=source,
            requested_by=requested_by,
            daily_limit=daily_limit,
        )


def process_url(
    url: str,
    repository: URLImportRepository,
    client: httpx.Client,
    storage: VideoLibraryStorage,
    *,
    source: Source,
    requested_by: str,
    daily_limit: int,
    row: Row | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ImportResult:
    """Core operation. Caller holds fetch_lock; queue completion shares the save transaction."""
    if source not in {"url_manual", "p3b"} or not requested_by.strip() or daily_limit < 0:
        raise URLImportError("invalid_import_options")
    target = normalize_url(url)
    retries = 0
    stop = None
    try:
        while True:
            try:
                target = expand_short_url(
                    client, target, lambda: repository.before_request(daily_limit, row)
                )
                if row and target.video_id:
                    duplicate = repository.resolve_row(row, target.url, target.video_id)
                    if duplicate is not None:
                        result = ImportResult(
                            status="duplicate", video_id=target.video_id, duplicate_of=duplicate
                        )
                        repository.finish_failure(row, result)
                        return result
                repository.before_request(daily_limit, row)
                page = fetch_page(client, target.url)
                observation = parse_observation(page, target, datetime.now(JST))
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                transient = isinstance(exc, httpx.TransportError) or exc.response.status_code >= 500
                if not transient or retries >= 2:
                    raise
                sleep(2 ** (retries + 1))
                retries += 1
        thumbnail = None
        if repository.thumbnail_missing(observation.video_id):
            cover = cover_url(page)
            if cover is None:
                observation.warnings.append("cover_missing")
            else:
                try:
                    thumbnail = save_cover(client, storage, "tiktok", observation.video_id, cover)
                except Exception as exc:
                    stop = stop_reason(exc)
                    observation.warnings.append(stop or "thumbnail_failed")
        result = ImportResult(
            status="partial" if observation.warnings else "succeeded",
            video_id=target.video_id,
            warnings=observation.warnings,
        )
        return repository.save(
            observation,
            source=source,
            requested_by=requested_by,
            thumbnail=thumbnail,
            result=result,
            row=row,
            stop=stop,
        )
    except Exception as exc:
        stop = stop_reason(exc)
        if isinstance(exc, URLImportError):
            code = str(exc)
        elif isinstance(exc, ValueError) and str(exc) == "page_too_large":
            code = "page_too_large"
        elif isinstance(exc, httpx.HTTPStatusError):
            code = f"http_{exc.response.status_code}"
        elif isinstance(exc, httpx.TransportError):
            code = "network_error"
        else:
            # Never expose exception text (SQL, S3 errors, and input can contain PII).
            code = "storage_error"
        status = (
            "deferred"
            if code == "daily_limit"
            else "queued"
            if code in {"fetch_paused", "lease_lost"}
            else "failed"
        )
        result = ImportResult(status=status, video_id=target.video_id, error_code=code)
        if row and code != "lease_lost":
            repository.finish_failure(row, result, stop=stop)
        elif not row and status == "failed":
            repository.record_failure(result, stop=stop)
        return result


def submit_import(
    text: str,
    repository: URLImportRepository,
    *,
    requested_by: str,
    idempotency_key: str,
    source: Source = "url_manual",
    wake: Callable[[], None] | None = None,
    advertiser: str | None = None,
    campaign: str | None = None,
    parent_batch_id: str | None = None,
    admin: bool = False,
    requested_origin: str = "external",
) -> str:
    """Validate all rows and commit the idempotent intake before waking a local worker."""
    batch = repository.submit(
        text,
        requested_by=requested_by,
        idempotency_key=idempotency_key,
        source=source,
        advertiser=advertiser,
        campaign=campaign,
        parent_batch_id=parent_batch_id,
        admin=admin,
        requested_origin=requested_origin,
    )
    if wake:
        wake()
    return batch
