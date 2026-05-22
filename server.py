from __future__ import annotations

"""
webkit-search-mcp — macOS-native web search MCP server.

Stdio transport. Consumed by oMLX, OpenCode, LM Studio, and similar
local LLM clients that support the Model Context Protocol.
"""

import asyncio
import json
import logging
import time
from typing import Any, Literal

import mcp.server.stdio
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import TextContent, Tool

import fetch as _fetch
import search as _search
from models import (
    DeepSearchResponse,
    DeepSearchResult,
    FetchPageResponse,
    SearchMeta,
    SearchNewsResponse,
    WebSearchResponse,
)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

app = Server("webkit-search-mcp")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description=(
                "Search the web and return a ranked list of results. "
                "Use 'brief' detail for a fast first-pass orientation (just titles, URLs, snippets). "
                "Use 'standard' detail to also get a 2-3 sentence summary of each page's main content. "
                "Searches Bing, Brave, and DuckDuckGo concurrently, deduplicates by URL. "
                "Returns JSON with a 'meta' field (engine_used, elapsed_ms, result_count) "
                "and a 'results' list (title, url, snippet, domain, date, rank, summary)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num_results": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Number of results to return (max 10)",
                    },
                    "detail": {
                        "type": "string",
                        "enum": ["brief", "standard"],
                        "default": "brief",
                        "description": "'brief' = list only; 'standard' = list + page summaries",
                    },
                    "recency": {
                        "type": "string",
                        "enum": ["any", "day", "week", "month"],
                        "default": "any",
                        "description": "Filter results by recency",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="fetch_page",
            description=(
                "Fetch a single URL and return clean, LLM-ready markdown. "
                "Strips navigation, ads, footers, and cookie banners. Preserves code blocks and tables. "
                "Uses httpx fast path by default; automatically falls back to macOS WebKit "
                "if the page requires JavaScript rendering. "
                "Returns JSON with 'meta' and 'page' fields "
                "(title, url, date, word_count, content, fetch_method)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "detail": {
                        "type": "string",
                        "enum": ["standard", "full"],
                        "default": "standard",
                        "description": "'standard' = main content capped at max_tokens; 'full' = full extracted content",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "default": 2000,
                        "minimum": 200,
                        "maximum": 16000,
                        "description": "Approximate output length cap in tokens (1 token ≈ 4 chars)",
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="deep_search",
            description=(
                "Combine web search with full page fetching in a single call. "
                "Runs a search, then concurrently fetches the top N results. "
                "Ideal when comprehensive coverage is needed in one shot — "
                "API docs, spec lookups, research questions. "
                "Returns JSON with 'meta' and 'results' (each has search_result + page content)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num_results": {
                        "type": "integer",
                        "default": 3,
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Number of top results to fetch full content for (max 5)",
                    },
                    "detail": {
                        "type": "string",
                        "enum": ["standard", "full"],
                        "default": "standard",
                        "description": "Content detail level per page",
                    },
                    "recency": {
                        "type": "string",
                        "enum": ["any", "day", "week", "month"],
                        "default": "any",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="search_news",
            description=(
                "Search recent news articles. Uses Bing News and DuckDuckGo News, "
                "merges and deduplicates results, sorts by date descending. "
                "Best for: recent software releases, CVEs, library updates, current events. "
                "Returns JSON with 'meta' and 'results' (title, url, snippet, source, published_date, rank)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "News search query"},
                    "num_results": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 10,
                    },
                    "recency": {
                        "type": "string",
                        "enum": ["day", "week", "month"],
                        "default": "week",
                        "description": "How far back to search",
                    },
                },
                "required": ["query"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "web_search":
        return await _handle_web_search(arguments)
    elif name == "fetch_page":
        return await _handle_fetch_page(arguments)
    elif name == "deep_search":
        return await _handle_deep_search(arguments)
    elif name == "search_news":
        return await _handle_search_news(arguments)
    else:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


async def _handle_web_search(args: dict[str, Any]) -> list[TextContent]:
    query: str = args["query"]
    num_results: int = min(int(args.get("num_results", 5)), 10)
    detail: str = args.get("detail", "brief")
    recency: str = args.get("recency", "any")

    t0 = time.monotonic()
    results, engine = await _search.search(query, num_results=num_results, recency=recency)  # type: ignore[arg-type]

    if detail == "standard" and results:
        summaries = await asyncio.gather(
            *[_fetch.fetch_summary(r.url) for r in results],
            return_exceptions=True,
        )
        for r, s in zip(results, summaries):
            if isinstance(s, str):
                r.summary = s

    elapsed = (time.monotonic() - t0) * 1000
    response = WebSearchResponse(
        meta=SearchMeta(
            engine_used=engine,
            fetch_method="httpx",
            elapsed_ms=round(elapsed, 1),
            result_count=len(results),
        ),
        results=results,
    )
    return [TextContent(type="text", text=response.model_dump_json(indent=2))]


async def _handle_fetch_page(args: dict[str, Any]) -> list[TextContent]:
    url: str = args["url"]
    detail: str = args.get("detail", "standard")
    max_tokens: int = int(args.get("max_tokens", 2000))

    t0 = time.monotonic()
    page = await _fetch.fetch_page(url, detail=detail, max_tokens=max_tokens)  # type: ignore[arg-type]
    elapsed = (time.monotonic() - t0) * 1000

    response = FetchPageResponse(
        meta=SearchMeta(
            engine_used="none",
            fetch_method=page.fetch_method,
            elapsed_ms=round(elapsed, 1),
            result_count=1 if not page.error else 0,
        ),
        page=page,
    )
    return [TextContent(type="text", text=response.model_dump_json(indent=2))]


async def _handle_deep_search(args: dict[str, Any]) -> list[TextContent]:
    query: str = args["query"]
    num_results: int = min(int(args.get("num_results", 3)), 5)
    detail: str = args.get("detail", "standard")
    recency: str = args.get("recency", "any")

    t0 = time.monotonic()
    search_results, engine = await _search.search(query, num_results=num_results, recency=recency)  # type: ignore[arg-type]

    pages = await _fetch.fetch_pages_parallel(
        [r.url for r in search_results],
        detail=detail,  # type: ignore[arg-type]
        max_tokens=3000,
    )

    combined = [
        DeepSearchResult(search_result=sr, page=pg)
        for sr, pg in zip(search_results, pages)
    ]

    # Determine dominant fetch method
    webkit_count = sum(1 for p in pages if p and p.fetch_method == "webkit")
    fetch_method = "webkit" if webkit_count > len(pages) / 2 else "httpx"

    elapsed = (time.monotonic() - t0) * 1000
    response = DeepSearchResponse(
        meta=SearchMeta(
            engine_used=engine,
            fetch_method=fetch_method,  # type: ignore[arg-type]
            elapsed_ms=round(elapsed, 1),
            result_count=len(combined),
        ),
        results=combined,
    )
    return [TextContent(type="text", text=response.model_dump_json(indent=2))]


async def _handle_search_news(args: dict[str, Any]) -> list[TextContent]:
    query: str = args["query"]
    num_results: int = min(int(args.get("num_results", 5)), 10)
    recency: str = args.get("recency", "week")

    t0 = time.monotonic()
    results, engine = await _search.search_news(query, num_results=num_results, recency=recency)  # type: ignore[arg-type]
    elapsed = (time.monotonic() - t0) * 1000

    response = SearchNewsResponse(
        meta=SearchMeta(
            engine_used=engine,
            fetch_method="httpx",
            elapsed_ms=round(elapsed, 1),
            result_count=len(results),
        ),
        results=results,
    )
    return [TextContent(type="text", text=response.model_dump_json(indent=2))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="webkit-search-mcp",
                server_version="0.1.0",
                capabilities=app.get_capabilities(
                    notification_options=None,
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
