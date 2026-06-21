from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, HttpUrl


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    domain: str
    date: Optional[str] = None
    rank: int
    summary: Optional[str] = None


class FetchedPage(BaseModel):
    title: str
    url: str
    date: Optional[str] = None
    word_count: int
    content: str
    fetch_method: Literal["httpx", "webkit"]
    error: Optional[str] = None
    injection_suspected: bool = False


class SearchMeta(BaseModel):
    engine_used: str
    fetch_method: Literal["httpx", "webkit"]
    elapsed_ms: float
    result_count: int
    # Safety / anti-injection metadata.
    # All returned web content is fenced as untrusted data; the boundary token
    # is content_boundary_nonce. injection_suspected flags heuristic matches.
    untrusted_content: bool = True
    content_boundary_nonce: Optional[str] = None
    injection_suspected: bool = False
    injection_signals: list[str] = []


class WebSearchResponse(BaseModel):
    meta: SearchMeta
    results: list[SearchResult]


class FetchPageResponse(BaseModel):
    meta: SearchMeta
    page: FetchedPage


class DeepSearchResult(BaseModel):
    search_result: SearchResult
    page: Optional[FetchedPage] = None


class DeepSearchResponse(BaseModel):
    meta: SearchMeta
    results: list[DeepSearchResult]


class NewsResult(BaseModel):
    title: str
    url: str
    snippet: str
    source: str
    published_date: Optional[str] = None
    rank: int


class SearchNewsResponse(BaseModel):
    meta: SearchMeta
    results: list[NewsResult]
