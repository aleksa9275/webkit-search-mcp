from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal, Optional
from urllib.parse import urlparse

import httpx

from extract import extract_content, is_js_required, summarize_content
from models import FetchedPage

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; ARM Mac OS X 15_0) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_CLIENT: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.AsyncClient(
            headers=_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(15.0, connect=5.0),
            http2=True,
        )
    return _CLIENT


async def fetch_page(
    url: str,
    detail: Literal["standard", "full"] = "standard",
    max_tokens: int = 2000,
) -> FetchedPage:
    """
    Fetch a URL and extract clean markdown content.
    Tries httpx first; falls back to WebKit if JS rendering is needed.
    """
    t0 = time.monotonic()
    method: Literal["httpx", "webkit"] = "httpx"

    html = await _fetch_httpx(url)
    if html and is_js_required(html):
        logger.debug("JS required for %s, switching to WebKit", url)
        webkit_html = await _fetch_webkit(url)
        if webkit_html:
            html = webkit_html
            method = "webkit"

    if not html:
        return FetchedPage(
            title="",
            url=url,
            word_count=0,
            content="",
            fetch_method=method,
            error="Failed to fetch page",
        )

    title, content, word_count = extract_content(html, max_tokens=max_tokens)

    return FetchedPage(
        title=title,
        url=url,
        word_count=word_count,
        content=content,
        fetch_method=method,
    )


async def fetch_summary(url: str) -> Optional[str]:
    """Fetch a page and return a 2-3 sentence summary (for web_search standard detail)."""
    page = await fetch_page(url, detail="standard", max_tokens=800)
    if page.error or not page.content:
        return None
    return summarize_content(page.content, max_sentences=3)


async def _fetch_httpx(url: str) -> Optional[str]:
    client = _get_client()
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "html" not in ct and "text" not in ct:
            return None
        return resp.text
    except Exception as e:
        logger.debug("httpx fetch failed for %s: %s", url, e)
        return None


async def _fetch_webkit(url: str) -> Optional[str]:
    try:
        from webkit_renderer import render_page, is_available
        if not is_available():
            return None
        return await render_page(url, timeout=10.0)
    except Exception as e:
        logger.debug("WebKit fetch failed for %s: %s", url, e)
        return None


async def fetch_pages_parallel(
    urls: list[str],
    detail: Literal["standard", "full"] = "standard",
    max_tokens: int = 2000,
) -> list[FetchedPage]:
    """Fetch multiple URLs concurrently."""
    tasks = [fetch_page(url, detail=detail, max_tokens=max_tokens) for url in urls]
    return await asyncio.gather(*tasks, return_exceptions=False)
