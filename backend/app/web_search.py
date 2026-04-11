"""
backend.app.web_search
======================
Tavily-backed web search helper used by the global chat endpoint to retrieve
up-to-date information.

Environment variables
---------------------
TAVILY_API_KEY        Required to call Tavily.  When absent, web_search() raises
                      TavilyKeyMissing so callers can return a clear 503.
TAVILY_CACHE_TTL_S    In-process cache TTL in seconds (default 600 / 10 minutes).

Caching
-------
Identical queries (same query string + recency_days + max_results) are cached
in-process for TAVILY_CACHE_TTL_S seconds to avoid redundant API calls.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

try:
    from tavily import TavilyClient  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    TavilyClient = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sentinel error for missing API key
# ---------------------------------------------------------------------------


class TavilyKeyMissing(RuntimeError):
    """Raised when TAVILY_API_KEY is not configured."""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

# Supports both TAVILY_CACHE_TTL_S (preferred) and the older name.
def _cache_ttl() -> int:
    val = (
        os.environ.get("TAVILY_CACHE_TTL_S")
        or os.environ.get("WEB_SEARCH_CACHE_TTL_SECONDS", "600")
    )
    return int(val)


# { cache_key: (expiry_timestamp, results_payload) }
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cache_key(query: str, recency_days: int | None, max_results: int) -> str:
    raw = f"{query}|{recency_days}|{max_results}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _get_cached(key: str) -> dict[str, Any] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expiry, payload = entry
    if time.monotonic() > expiry:
        del _cache[key]
        return None
    return payload


def _set_cached(key: str, payload: dict[str, Any]) -> None:
    _cache[key] = (time.monotonic() + _cache_ttl(), payload)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_PROVIDER = "tavily"


def web_search(
    query: str,
    *,
    recency_days: int | None = None,
    max_results: int = 5,
) -> dict[str, Any]:
    """
    Search the web via Tavily and return normalised results.

    Returns
    -------
    {
        "results": [
            {
                "title": str,
                "url": str,
                "snippet": str,
                "published_at": str | None,
                "source": str,
            },
            ...
        ],
        "provider": "tavily",
    }

    Raises
    ------
    TavilyKeyMissing  when TAVILY_API_KEY is not set.
    """
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise TavilyKeyMissing(
            "TAVILY_API_KEY is not configured. Set this environment variable to enable web search."
        )

    if TavilyClient is None:
        logger.warning("web_search: tavily-python is not installed; returning empty results.")
        return {"results": [], "provider": _PROVIDER}

    key = _cache_key(query, recency_days, max_results)
    cached = _get_cached(key)
    if cached is not None:
        logger.debug("web_search: cache hit for query %r", query)
        return cached

    try:
        client = TavilyClient(api_key=api_key)

        kwargs: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        }
        if recency_days is not None:
            kwargs["days"] = recency_days

        raw = client.search(**kwargs)

        results = []
        for item in raw.get("results", []):
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", ""),
                    "published_at": item.get("published_date"),
                    "source": _source_from_url(item.get("url", "")),
                }
            )

        payload: dict[str, Any] = {"results": results, "provider": _PROVIDER}
        _set_cached(key, payload)
        return payload

    except TavilyKeyMissing:
        raise
    except Exception as exc:
        logger.warning("web_search: Tavily call failed: %s", exc)
        return {"results": [], "provider": _PROVIDER}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _source_from_url(url: str) -> str:
    """Extract the hostname from a URL for use as a source label."""
    try:
        from urllib.parse import urlparse

        hostname = urlparse(url).netloc
        return hostname.removeprefix("www.") if hostname else url
    except Exception:
        return url
