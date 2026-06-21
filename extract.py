from __future__ import annotations

import re
import logging
from typing import Optional

import html2text

from safety import sanitize_html, strip_dangerous_unicode

logger = logging.getLogger(__name__)

# Approximate chars per token
_CHARS_PER_TOKEN = 4


def _html_to_markdown(html: str) -> str:
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = True
    converter.ignore_emphasis = False
    converter.body_width = 0  # no wrapping
    converter.protect_links = True
    converter.wrap_links = False
    return converter.handle(html)


def _strip_noise_elements(html: str) -> str:
    """Remove nav/footer/ad/cookie noise before readability pass."""
    # Remove script and style blocks first
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove common noise elements
    noise_tags = r"<(nav|header|footer|aside|noscript)(\s[^>]*)?>.*?</\1>"
    html = re.sub(noise_tags, "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove elements with noise class/id patterns
    noise_pattern = r'<[^>]+\s(?:class|id)="[^"]*(?:cookie|banner|advertisement|sidebar|popup|modal|overlay|nav|menu|footer|header)[^"]*"[^>]*>.*?</[a-z]+>'
    html = re.sub(noise_pattern, "", html, flags=re.DOTALL | re.IGNORECASE)
    return html


def extract_content(html: str, max_tokens: int = 2000) -> tuple[str, str, int]:
    """
    Extract main content from HTML. Returns (title, markdown_content, word_count).
    Tries readability-lxml first, falls back to trafilatura.
    """
    max_chars = max_tokens * _CHARS_PER_TOKEN
    title = ""
    content_html = ""

    # Strip injection-friendly markup (comments, hidden elements) before extraction
    html = sanitize_html(html)

    # Try readability-lxml
    try:
        from readability import Document
        doc = Document(html)
        title = doc.title() or ""
        content_html = doc.summary(html_partial=False)
        content_html = _strip_noise_elements(content_html)
        markdown = _html_to_markdown(content_html)
        markdown = _clean_markdown(markdown)
        if len(markdown.strip()) > 200:
            word_count = len(markdown.split())
            return title, markdown[:max_chars], word_count
    except Exception as e:
        logger.debug("readability-lxml failed: %s", e)

    # Fallback: trafilatura
    try:
        import trafilatura
        extracted = trafilatura.extract(html, include_tables=True, include_links=True, output_format="markdown")
        if extracted and len(extracted.strip()) > 200:
            # Extract title separately
            try:
                meta = trafilatura.extract_metadata(html)
                if meta and meta.title:
                    title = meta.title
            except Exception:
                pass
            markdown = _clean_markdown(extracted)
            word_count = len(markdown.split())
            return title, markdown[:max_chars], word_count
    except Exception as e:
        logger.debug("trafilatura failed: %s", e)

    # Last resort: strip all tags
    stripped = re.sub(r"<[^>]+>", " ", html)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    stripped = strip_dangerous_unicode(stripped)
    word_count = len(stripped.split())
    return title, stripped[:max_chars], word_count


def _clean_markdown(text: str) -> str:
    """Remove excessive blank lines, whitespace, and steganographic Unicode."""
    text = strip_dangerous_unicode(text)
    # Collapse 3+ blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Remove lines that are just whitespace
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def summarize_content(markdown: str, max_sentences: int = 3) -> str:
    """Return first N sentences of content as a brief summary."""
    # Split on sentence boundaries
    sentences = re.split(r"(?<=[.!?])\s+", markdown.strip())
    # Filter out very short or heading-like sentences
    sentences = [s for s in sentences if len(s) > 40 and not s.startswith("#")]
    summary = " ".join(sentences[:max_sentences])
    return summary[:600]  # hard cap


def is_js_required(html: str) -> bool:
    """
    Heuristic: return True if the page body appears empty or minimal,
    suggesting JS rendering is required.
    """
    # Strip scripts/styles first
    stripped = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"<style[^>]*>.*?</style>", "", stripped, flags=re.DOTALL | re.IGNORECASE)
    # Get text content
    text = re.sub(r"<[^>]+>", " ", stripped)
    text = re.sub(r"\s+", " ", text).strip()
    return len(text) < 500
