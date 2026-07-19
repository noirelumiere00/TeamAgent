"""Canonical top-level media acquire URL policy."""

from __future__ import annotations

ACQUIRE_HOST_SUFFIXES: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com",
    "instagr.am",
)


def acquire_host_allowed(host: str) -> bool:
    """Return whether ``host`` is an exact allowlisted suffix boundary."""

    normalized = host.rstrip(".").lower()
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in ACQUIRE_HOST_SUFFIXES
    )


__all__ = ["ACQUIRE_HOST_SUFFIXES", "acquire_host_allowed"]
