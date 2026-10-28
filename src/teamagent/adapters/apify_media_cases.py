"""Resolve optional Actor assets exclusively through its own Apify KVS."""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import unquote, urlsplit

from teamagent.adapters.apify_client import ApifyClient, ApifyRunResult

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def _natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def _references(value: Any, depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [ref for entry in value[:30] for ref in _references(entry, depth + 1)]
    if isinstance(value, dict):
        return [
            ref
            for key in ("url", "downloadUrl", "key", "text")
            for ref in _references(value.get(key), depth + 1)
        ]
    return []


@dataclass(frozen=True)
class PostAssets:
    cover_key: str | None
    slide_keys: tuple[str, ...]
    subtitle: str


class ApifyCaseAssets:
    def __init__(self, client: ApifyClient, run: ApifyRunResult) -> None:
        self.client = client
        self.store_id = run.kvs_id
        try:
            self.keys = client.list_kvs_keys(run.kvs_id, max_keys=50000) if run.kvs_id else []
        except Exception:
            self.keys = []

    def _record_key(self, value: str) -> str | None:
        if value in self.keys:
            return value
        try:
            url = urlsplit(value)
            prefix = f"/v2/key-value-stores/{self.store_id}/records/"
            if (
                url.scheme == "https"
                and url.netloc == "api.apify.com"
                and url.path.startswith(prefix)
            ):
                key = unquote(url.path[len(prefix) :])
                if key and "/" not in key:
                    return key
        except ValueError:
            pass
        return None

    def for_post(self, item: dict[str, Any], post_id: str) -> PostAssets:
        meta = item.get("videoMeta")
        meta = meta if isinstance(meta, dict) else {}
        matched = [
            key for key in self.keys if re.search(rf"(?<!\d){re.escape(post_id)}(?!\d)", key)
        ]

        def records(values: list[Any], candidates: list[str]) -> list[str]:
            explicit = [
                key
                for value in values
                for ref in _references(value)
                if (key := self._record_key(ref))
            ]
            return list(dict.fromkeys([*explicit, *sorted(candidates, key=_natural_key)]))

        covers = records(
            [meta.get("coverUrl"), meta.get("cover"), item.get("covers"), item.get("coverKey")],
            [
                key
                for key in matched
                if "cover" in key.lower() and key.lower().endswith(_IMAGE_EXTENSIONS)
            ],
        )
        slides = records(
            [item.get("slideshowImageLinks"), item.get("slideshowImages")],
            [
                key
                for key in matched
                if re.search(r"slide|photo|image", key, re.I)
                and key.lower().endswith(_IMAGE_EXTENSIONS)
                and "cover" not in key.lower()
            ],
        )[:11]
        subtitle_keys = records(
            [item.get("subtitles"), meta.get("subtitles"), item.get("subtitleLinks")],
            [
                key
                for key in matched
                if key.lower().endswith((".vtt", ".srt", ".txt"))
                and re.search(r"subtitle|caption", key, re.I)
            ],
        )
        texts: list[str] = []
        for value in (item.get("subtitleText"), meta.get("subtitleText")):
            if isinstance(value, str) and not value.startswith(("http:", "https:")):
                texts.append(value[:12000])
        for value in (item.get("subtitles"), meta.get("subtitles")):
            entries = value[:30] if isinstance(value, list) else [value]
            for entry in entries:
                text = entry.get("text") if isinstance(entry, dict) else None
                if isinstance(text, str) and not text.startswith(("http:", "https:")):
                    texts.append(text[:12000])
        for key in subtitle_keys[:2]:
            try:
                texts.append(
                    self.client.get_kvs_record(
                        self.store_id, key, max_bytes=64 * 1024, deadline_s=30
                    ).decode("utf-8")
                )
            except Exception:
                continue
        subtitle = re.sub(r"<[^>]*>|\d{2}:\d{2}:\d{2}[.,]\d{3}|-->|WEBVTT", " ", "\n".join(texts))
        return PostAssets(
            covers[0] if covers else (slides[0] if slides else None),
            tuple(slides),
            subtitle[:12000],
        )

    def image(self, key: str) -> tuple[bytes, str]:
        from PIL import Image

        body = self.client.get_kvs_record(
            self.store_id, key, max_bytes=5 * 1024 * 1024, deadline_s=30
        )
        with Image.open(BytesIO(body)) as image:
            mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(
                image.format or ""
            )
            if mime is None or image.width * image.height > 16_000_000:
                raise ValueError("unsupported image bytes")
            image.verify()
        return body, mime
