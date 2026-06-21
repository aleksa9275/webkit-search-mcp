from __future__ import annotations

import asyncio
import logging
from typing import Literal, Optional

import httpx

from extract import extract_content, is_js_required, summarize_content
from models import FetchedPage
from safety import (
    MAX_RESPONSE_BYTES,
    audit,
    check_url,
    next_redirect_url,
    pick_safe_ip,
)

logger = logging.getLogger(__name__)

_MAX_REDIRECTS = 5

# Bound total concurrent outbound fetches so a looping agent can't fan out
# unboundedly (resource-exhaustion / self-inflicted DoS).
_MAX_CONCURRENT_FETCHES = 8
_fetch_sem: Optional[asyncio.Semaphore] = None


def _get_fetch_sem() -> asyncio.Semaphore:
    # Lazily created so it binds to the running event loop.
    global _fetch_sem
    if _fetch_sem is None:
        _fetch_sem = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)
    return _fetch_sem


class _PinnedResolverTransport(httpx.AsyncHTTPTransport):
    """
    Resolves the host and pins the TCP connection to a validated IP, closing
    the DNS-rebinding (TOCTOU) window: the IP we validate is the exact IP we
    connect to. For HTTPS, SNI and certificate verification still use the real
    hostname (via the sni_hostname extension), so TLS remains correct.
    """

    async def handle_async_request(self, request):
        host = request.url.host
        try:
            ip = await asyncio.to_thread(pick_safe_ip, host)
        except Exception as e:
            audit("ssrf_blocked", host=host, reason=str(e), layer="transport")
            raise httpx.ConnectError(f"SSRF blocked: {e}", request=request)

        if ip != host:
            original_host_header = request.headers.get("Host")
            request.url = request.url.copy_with(host=ip)
            request.extensions = dict(request.extensions)
            request.extensions["sni_hostname"] = host
            # Preserve the original Host header (httpx may have set it to host:port).
            request.headers["Host"] = original_host_header or host
        return await super().handle_async_request(request)

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
        # Redirects are followed manually so every hop is SSRF-validated;
        # httpx auto-redirect would let an allowed host 302 to an internal one.
        # The pinned transport additionally guarantees we connect to the exact
        # validated IP (DNS-rebinding protection).
        _CLIENT = httpx.AsyncClient(
            headers=_HEADERS,
            follow_redirects=False,
            timeout=httpx.Timeout(15.0, connect=5.0),
            transport=_PinnedResolverTransport(http2=True, retries=0),
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
    method: Literal["httpx", "webkit"] = "httpx"

    # SSRF guard: refuse non-http(s) schemes and private/internal hosts.
    block_reason = await check_url(url)
    if block_reason:
        logger.warning("blocked fetch of %s: %s", url, block_reason)
        audit("ssrf_blocked", url=url, reason=block_reason, layer="fetch_page")
        return FetchedPage(
            title="",
            url=url,
            word_count=0,
            content="",
            fetch_method=method,
            error=f"blocked: {block_reason}",
        )

    async with _get_fetch_sem():
        html = await _fetch_httpx(url)
        if html and is_js_required(html):
            logger.debug("JS required for %s, switching to WebKit", url)
            webkit_html = await _fetch_webkit(url)
            if webkit_html:
                html = webkit_html
                method = "webkit"

    if not html:
        audit("fetch", url=url, method=method, result="empty")
        return FetchedPage(
            title="",
            url=url,
            word_count=0,
            content="",
            fetch_method=method,
            error="Failed to fetch page",
        )

    title, content, word_count = extract_content(html, max_tokens=max_tokens)
    audit("fetch", url=url, method=method, result="ok", words=word_count)

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
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        # Re-validate each hop — a permitted host can redirect to an internal one.
        # (The pinned transport re-validates at connect time too; this gives an
        # early, cheaper rejection.)
        if await check_url(current):
            logger.warning("blocked redirect target %s", current)
            audit("ssrf_blocked", url=current, reason="redirect target", layer="redirect")
            return None
        try:
            # Stream so we can enforce a byte cap before buffering the whole body.
            async with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        return None
                    current = next_redirect_url(current, location)
                    continue

                try:
                    resp.raise_for_status()
                except Exception as e:
                    logger.debug("httpx status error for %s: %s", current, e)
                    return None

                ct = resp.headers.get("content-type", "")
                if "html" not in ct and "text" not in ct:
                    return None

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        logger.warning("response exceeded %d bytes for %s — truncating",
                                       MAX_RESPONSE_BYTES, current)
                        chunks.append(chunk[: max(0, MAX_RESPONSE_BYTES - (total - len(chunk)))])
                        break
                    chunks.append(chunk)

                body = b"".join(chunks)
                encoding = resp.charset_encoding or "utf-8"
                try:
                    return body.decode(encoding, errors="replace")
                except (LookupError, TypeError):
                    return body.decode("utf-8", errors="replace")
        except Exception as e:
            logger.debug("httpx fetch failed for %s: %s", current, e)
            return None

    logger.warning("too many redirects for %s", url)
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
