from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Literal, Optional
from urllib.parse import quote_plus, urlparse

import httpx

from models import NewsResult, SearchResult

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

_Recency = Literal["any", "day", "week", "month"]

# ---------------------------------------------------------------------------
# Bing
# ---------------------------------------------------------------------------

_BING_FRESHNESS = {"any": None, "day": "Day", "week": "Week", "month": "Month"}
_BING_NEWS_FRESHNESS = {"day": "Day", "week": "Week", "month": "Month"}


async def _bing_search(
    client: httpx.AsyncClient,
    query: str,
    num_results: int,
    recency: _Recency = "any",
) -> list[SearchResult]:
    params: dict = {"q": query, "count": str(min(num_results, 10))}
    freshness = _BING_FRESHNESS.get(recency)
    if freshness:
        params["freshness"] = freshness

    try:
        resp = await client.get(
            "https://www.bing.com/search",
            params=params,
            headers={**_HEADERS, "Accept-Language": "en-US,en;q=0.9"},
            timeout=10.0,
        )
        resp.raise_for_status()
        return _parse_bing_results(resp.text, num_results)
    except Exception as e:
        logger.debug("Bing search failed: %s", e)
        return []


def _parse_bing_results(html: str, num_results: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    # Bing result containers: <li class="b_algo">
    blocks = re.findall(r'<li class="b_algo">(.*?)</li>', html, re.DOTALL)
    rank = 1
    for block in blocks[:num_results]:
        # Title and URL
        title_m = re.search(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not title_m:
            continue
        url = title_m.group(1).split("?")[0] if "bing.com" in title_m.group(1) else title_m.group(1)
        # Follow Bing redirect URLs
        if url.startswith("/"):
            url_m = re.search(r'href="(https?://[^"]+)"', block)
            if url_m:
                url = url_m.group(1)
        title = re.sub(r"<[^>]+>", "", title_m.group(2)).strip()

        # Snippet
        snip_m = re.search(r'<p[^>]*class="b_algoSlug[^"]*"[^>]*>(.*?)</p>', block, re.DOTALL)
        if not snip_m:
            snip_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        snippet = re.sub(r"<[^>]+>", "", snip_m.group(1)).strip() if snip_m else ""

        # Date
        date_m = re.search(r'<span class="news_dt"[^>]*>(.*?)</span>', block)
        date = date_m.group(1).strip() if date_m else None

        domain = _extract_domain(url)
        if domain and url.startswith("http"):
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet[:300],
                    domain=domain,
                    date=date,
                    rank=rank,
                )
            )
            rank += 1
    return results


# ---------------------------------------------------------------------------
# Bing News
# ---------------------------------------------------------------------------

async def _bing_news_search(
    client: httpx.AsyncClient,
    query: str,
    num_results: int,
    recency: str = "week",
) -> list[NewsResult]:
    freshness = _BING_NEWS_FRESHNESS.get(recency, "Week")
    params = {"q": query, "freshness": freshness}
    try:
        resp = await client.get(
            "https://www.bing.com/news/search",
            params=params,
            headers=_HEADERS,
            timeout=10.0,
        )
        resp.raise_for_status()
        return _parse_bing_news(resp.text, num_results)
    except Exception as e:
        logger.debug("Bing News search failed: %s", e)
        return []


def _parse_bing_news(html: str, num_results: int) -> list[NewsResult]:
    results: list[NewsResult] = []
    # Bing News cards
    cards = re.findall(r'<div class="news-card[^"]*"[^>]*>(.*?)</div>\s*</div>', html, re.DOTALL)
    rank = 1
    for card in cards[:num_results]:
        url_m = re.search(r'<a[^>]+href="(https?://[^"]+)"', card)
        title_m = re.search(r'<a[^>]+title="([^"]+)"', card)
        snip_m = re.search(r'<div class="snippet[^"]*"[^>]*>(.*?)</div>', card, re.DOTALL)
        source_m = re.search(r'<div class="source[^"]*"[^>]*>(.*?)</div>', card, re.DOTALL)
        date_m = re.search(r'<span[^>]+class="[^"]*time[^"]*"[^>]*>(.*?)</span>', card, re.DOTALL)

        if not url_m or not title_m:
            continue

        url = url_m.group(1)
        title = title_m.group(1).strip()
        snippet = re.sub(r"<[^>]+>", "", snip_m.group(1)).strip() if snip_m else ""
        source = re.sub(r"<[^>]+>", "", source_m.group(1)).strip() if source_m else _extract_domain(url)
        date = re.sub(r"<[^>]+>", "", date_m.group(1)).strip() if date_m else None

        results.append(
            NewsResult(
                title=title,
                url=url,
                snippet=snippet[:300],
                source=source or "",
                published_date=date,
                rank=rank,
            )
        )
        rank += 1
    return results


# ---------------------------------------------------------------------------
# Brave Search
# ---------------------------------------------------------------------------

async def _brave_search(
    client: httpx.AsyncClient,
    query: str,
    num_results: int,
    recency: _Recency = "any",
) -> list[SearchResult]:
    params: dict = {"q": query}
    freshness_map = {"day": "pd", "week": "pw", "month": "pm"}
    if recency in freshness_map:
        params["tf"] = freshness_map[recency]

    try:
        resp = await client.get(
            "https://search.brave.com/search",
            params=params,
            headers={**_HEADERS, "Accept-Language": "en-US,en;q=0.9"},
            timeout=10.0,
        )
        resp.raise_for_status()
        return _parse_brave_results(resp.text, num_results)
    except Exception as e:
        logger.debug("Brave search failed: %s", e)
        return []


def _parse_brave_results(html: str, num_results: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    # Brave result items: <div class="snippet">
    blocks = re.findall(r'<div[^>]+class="[^"]*snippet[^"]*"[^>]*>(.*?)</div>\s*</div>', html, re.DOTALL)
    rank = 1
    for block in blocks[:num_results * 2]:
        url_m = re.search(r'<a[^>]+href="(https?://[^"]+)"', block)
        title_m = re.search(r'<span[^>]+class="[^"]*title[^"]*"[^>]*>(.*?)</span>', block, re.DOTALL)
        snip_m = re.search(r'<p[^>]+class="[^"]*snippet-description[^"]*"[^>]*>(.*?)</p>', block, re.DOTALL)

        if not url_m or not title_m:
            continue

        url = url_m.group(1)
        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip()
        snippet = re.sub(r"<[^>]+>", "", snip_m.group(1)).strip() if snip_m else ""
        domain = _extract_domain(url)

        if domain:
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet[:300],
                    domain=domain,
                    rank=rank,
                )
            )
            rank += 1
            if rank > num_results:
                break
    return results


# ---------------------------------------------------------------------------
# DuckDuckGo
# ---------------------------------------------------------------------------

async def _ddg_search(
    client: httpx.AsyncClient,
    query: str,
    num_results: int,
    recency: _Recency = "any",
) -> list[SearchResult]:
    df_map = {"day": "d", "week": "w", "month": "m"}
    params: dict = {"q": query, "ia": "web"}
    if recency in df_map:
        params["df"] = df_map[recency]

    try:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params=params,
            headers=_HEADERS,
            timeout=10.0,
        )
        resp.raise_for_status()
        return _parse_ddg_results(resp.text, num_results)
    except Exception as e:
        logger.debug("DDG search failed: %s", e)
        return []


def _parse_ddg_results(html: str, num_results: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    blocks = re.findall(r'<div class="result[^"]*"[^>]*>(.*?)</div>\s*</div>', html, re.DOTALL)
    rank = 1
    for block in blocks[:num_results * 2]:
        url_m = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"', block)
        title_m = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', block, re.DOTALL)
        snip_m = re.search(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', block, re.DOTALL)

        if not url_m or not title_m:
            continue

        url = url_m.group(1)
        # DDG uses redirect URLs — extract actual URL
        uddg_m = re.search(r'uddg=([^&"]+)', url)
        if uddg_m:
            from urllib.parse import unquote
            url = unquote(uddg_m.group(1))

        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip()
        snippet = re.sub(r"<[^>]+>", "", snip_m.group(1)).strip() if snip_m else ""
        domain = _extract_domain(url)

        if domain and url.startswith("http"):
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet[:300],
                    domain=domain,
                    rank=rank,
                )
            )
            rank += 1
            if rank > num_results:
                break
    return results


# ---------------------------------------------------------------------------
# DuckDuckGo News
# ---------------------------------------------------------------------------

async def _ddg_news_search(
    client: httpx.AsyncClient,
    query: str,
    num_results: int,
    recency: str = "week",
) -> list[NewsResult]:
    df_map = {"day": "d", "week": "w", "month": "m"}
    params: dict = {"q": query, "ia": "news", "iax": "news"}
    df = df_map.get(recency, "w")
    params["df"] = df

    try:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params=params,
            headers=_HEADERS,
            timeout=10.0,
        )
        resp.raise_for_status()
        return _parse_ddg_news(resp.text, num_results)
    except Exception as e:
        logger.debug("DDG News search failed: %s", e)
        return []


def _parse_ddg_news(html: str, num_results: int) -> list[NewsResult]:
    results: list[NewsResult] = []
    blocks = re.findall(r'<div class="result[^"]*"[^>]*>(.*?)</div>\s*</div>', html, re.DOTALL)
    rank = 1
    for block in blocks[:num_results * 2]:
        url_m = re.search(r'href="(https?://[^"]+)"', block)
        title_m = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>', block, re.DOTALL)
        snip_m = re.search(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', block, re.DOTALL)
        date_m = re.search(r'<span class="[^"]*result__timestamp[^"]*">(.*?)</span>', block, re.DOTALL)

        if not url_m or not title_m:
            continue

        url = url_m.group(1)
        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip()
        snippet = re.sub(r"<[^>]+>", "", snip_m.group(1)).strip() if snip_m else ""
        date = re.sub(r"<[^>]+>", "", date_m.group(1)).strip() if date_m else None
        source = _extract_domain(url) or ""

        results.append(
            NewsResult(
                title=title,
                url=url,
                snippet=snippet[:300],
                source=source,
                published_date=date,
                rank=rank,
            )
        )
        rank += 1
        if rank > num_results:
            break
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=_HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(15.0, connect=5.0),
        http2=True,
    )


def _dedup_results(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    out: list[SearchResult] = []
    rank = 1
    for r in results:
        if r.url not in seen:
            seen.add(r.url)
            out.append(r.model_copy(update={"rank": rank}))
            rank += 1
    return out


def _dedup_news(results: list[NewsResult]) -> list[NewsResult]:
    seen: set[str] = set()
    out: list[NewsResult] = []
    rank = 1
    for r in results:
        if r.url not in seen:
            seen.add(r.url)
            out.append(r.model_copy(update={"rank": rank}))
            rank += 1
    return out


async def search(
    query: str,
    num_results: int = 5,
    recency: _Recency = "any",
) -> tuple[list[SearchResult], str]:
    """
    Run search across engines with fallback. Returns (results, engine_used).
    Tries Bing → Brave → DDG, merges and deduplicates.
    """
    num_results = min(num_results, 10)
    async with _make_client() as client:
        bing, brave, ddg = await asyncio.gather(
            _bing_search(client, query, num_results, recency),
            _brave_search(client, query, num_results, recency),
            _ddg_search(client, query, num_results, recency),
            return_exceptions=True,
        )

    engines_used = []
    combined: list[SearchResult] = []

    if isinstance(bing, list) and bing:
        combined.extend(bing)
        engines_used.append("bing")
    if isinstance(brave, list) and brave:
        combined.extend(brave)
        engines_used.append("brave")
    if isinstance(ddg, list) and ddg:
        combined.extend(ddg)
        engines_used.append("duckduckgo")

    # Sort by rank, then dedup
    combined.sort(key=lambda r: r.rank)
    deduped = _dedup_results(combined)[:num_results]

    engine_label = "+".join(engines_used) if engines_used else "none"
    return deduped, engine_label


async def search_news(
    query: str,
    num_results: int = 5,
    recency: Literal["day", "week", "month"] = "week",
) -> tuple[list[NewsResult], str]:
    """
    Search news via Bing News + DDG News. Returns (results, engine_used).
    """
    num_results = min(num_results, 10)
    async with _make_client() as client:
        bing_news, ddg_news = await asyncio.gather(
            _bing_news_search(client, query, num_results, recency),
            _ddg_news_search(client, query, num_results, recency),
            return_exceptions=True,
        )

    combined: list[NewsResult] = []
    engines_used = []

    if isinstance(bing_news, list) and bing_news:
        combined.extend(bing_news)
        engines_used.append("bing_news")
    if isinstance(ddg_news, list) and ddg_news:
        combined.extend(ddg_news)
        engines_used.append("duckduckgo_news")

    deduped = _dedup_news(combined)[:num_results]
    engine_label = "+".join(engines_used) if engines_used else "none"
    return deduped, engine_label


def _extract_domain(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        host = parsed.netloc
        # Strip www.
        if host.startswith("www."):
            host = host[4:]
        return host or None
    except Exception:
        return None
