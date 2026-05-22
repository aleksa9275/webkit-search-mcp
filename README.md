# webkit-search-mcp

A fast, macOS-native web search MCP server for local LLM workloads. Uses
Safari's WebKit engine (via PyObjC) for JavaScript-rendered pages, with an
httpx fast path for everything else. No Playwright, no Chromium, no Electron.

## Features

- **4 tools**: `web_search`, `fetch_page`, `deep_search`, `search_news`
- **Multi-engine search**: Bing + Brave + DuckDuckGo, concurrent, deduplicated
- **Smart rendering**: httpx fast path → WebKit fallback (auto-detected)
- **LLM-optimized output**: clean markdown, no nav/ads/footers, token-budget aware
- **Fully async**: no blocking on the event loop; WebKit runs on a dedicated NSRunLoop thread
- **Graceful degradation**: if PyObjC is unavailable, httpx-only mode works fully

## Requirements

- macOS 15 (Sequoia) or 26 (Tahoe), Apple Silicon
- Python 3.10+

## Installation

```bash
git clone https://github.com/aleksa9275/webkit-search-mcp.git
cd webkit-search-mcp
python -m venv .webkit-mcp
source .webkit-mcp/bin/activate
pip install -r requirements.txt
```

Verify it works:

```bash
python server.py
# Should block waiting for MCP stdio — Ctrl-C to exit
```

## MCP client configuration

### oMLX

Add to your oMLX `mcp_servers` configuration (typically in `~/Library/Application Support/oMLX/config.json`):

```json
{
  "mcp_servers": {
    "webkit-search": {
      "command": "/path/to/webkit-search-mcp/.venv/bin/python",
      "args": ["/path/to/webkit-search-mcp/server.py"],
      "transport": "stdio"
    }
  }
}
```

### OpenCode

Add to `~/.config/opencode/config.json` (or your project's `.opencode/config.json`):

```json
{
  "mcp": {
    "servers": {
      "webkit-search": {
        "type": "stdio",
        "command": "/path/to/webkit-search-mcp/.venv/bin/python",
        "args": ["/path/to/webkit-search-mcp/server.py"]
      }
    }
  }
}
```

### LM Studio

In LM Studio → Settings → MCP Servers, add a new server entry:

```json
{
  "name": "webkit-search",
  "transport": "stdio",
  "command": "/path/to/webkit-search-mcp/.venv/bin/python",
  "args": ["/path/to/webkit-search-mcp/server.py"]
}
```

Or edit `~/Library/Application Support/LM Studio/mcp-servers.json` directly:

```json
{
  "servers": [
    {
      "name": "webkit-search",
      "transport": "stdio",
      "command": "/path/to/webkit-search-mcp/.venv/bin/python",
      "args": ["/path/to/webkit-search-mcp/server.py"]
    }
  ]
}
```

## System prompt recommendation

Local LLMs don't know the current date. Inject it via your client's system prompt:

```
Today's date is {{CURRENT_DATE}}. When searching for recent information,
use the recency parameter (day/week/month) to filter results appropriately.
```

Most clients support dynamic variables; replace `{{CURRENT_DATE}}` with
whatever syntax your client uses (e.g. `{date}`, `<date>`, or a hardcoded value).

## Tools reference

### `web_search`
First-pass search. Returns ranked results with title, URL, snippet, domain, date.
- `detail: "brief"` — list only (fast, cheap)
- `detail: "standard"` — list + 2-3 sentence page summaries
- `recency`: filter by `any` / `day` / `week` / `month`

### `fetch_page`
Fetch and clean a single URL. Auto-detects JS requirement and uses WebKit if needed.
- `detail: "standard"` — main content (default)
- `detail: "full"` — full extracted content
- `max_tokens` — output length cap (default 2000)

### `deep_search`
One-shot search + fetch. Runs search then concurrently fetches top N pages.
Best for: API docs, specs, research needing comprehensive coverage.

### `search_news`
Recency-biased search via Bing News + DDG News. Results sorted by date descending.
Best for: releases, CVEs, library updates, current events.

## Troubleshooting

### `ImportError: No module named 'AppKit'`
PyObjC is not installed or the wrong Python is being used.
```bash
which python  # must be inside .venv
pip install pyobjc-framework-WebKit pyobjc-framework-Cocoa
```
The server runs in httpx-only mode if PyObjC is missing — search and fetch still work for non-JS pages.

### WebKit renders a blank page
Some sites check for a real browser User-Agent. The server uses a Safari/macOS UA by default.
If a specific site still renders blank, try `fetch_page` with `detail: "full"` — the WebKit fallback will engage automatically.

### `NSInternalInconsistencyException` on startup
This can happen if another process has already initialized NSApplication with a conflicting policy.
Restart your MCP client and try again. If it persists, file an issue with your macOS version.

### Deadlock / server hangs
WebKit APIs must never be called from asyncio's event loop thread. This server enforces that via a dedicated `threading.Thread` with its own `NSRunLoop`. If you fork this code, never call `render_page` outside of `asyncio.run()` or from a sync context — wrap it in `asyncio.run_coroutine_threadsafe`.

### Slow on first request
WebKit initializes lazily on the first JS-required page. Subsequent requests on the same renderer instance are faster. Cold start is ~1-2s on Apple Silicon M-series.

### `lxml` build fails
```bash
pip install --upgrade pip
pip install lxml --no-binary lxml
# or: brew install libxml2 libxslt && pip install lxml
```
