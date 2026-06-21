from __future__ import annotations

"""
Headless WebKit renderer using PyObjC, via subprocess isolation.

WebKit (WKWebView) is strictly main-thread-only — it aborts with a fatal
assertion if initialized or driven from any other thread. The MCP server's
main thread is occupied by the asyncio stdio loop, so WebKit cannot run there.

Instead, each render runs in a short-lived helper SUBPROCESS whose own main
thread drives the Cocoa/WebKit run loop. The async parent spawns it, reads the
rendered HTML from stdout, and enforces a timeout by killing the child.

Benefits beyond correctness:
  * Crash isolation — a hostile page that crashes WebKit kills the child, not
    the server.
  * Privacy isolation — every render is a fresh process with an ephemeral
    (non-persistent) data store; nothing leaks between requests.
  * No asyncio/Cocoa run-loop interleaving, so no deadlocks.

Run as a script ("python webkit_renderer.py render <url> <timeout>") it performs
a single render on its main thread and writes the page HTML to stdout.
"""

import asyncio
import json
import logging
import sys
from typing import Optional

logger = logging.getLogger(__name__)

_WEBKIT_AVAILABLE = False

# Bound concurrent WebKit subprocesses — each is a full browser engine; a
# looping agent firing many deep_search calls could otherwise fork-bomb the host.
_MAX_CONCURRENT_RENDERS = 2
_render_sem: Optional["asyncio.Semaphore"] = None


def _get_render_sem() -> "asyncio.Semaphore":
    global _render_sem
    if _render_sem is None:
        _render_sem = asyncio.Semaphore(_MAX_CONCURRENT_RENDERS)
    return _render_sem


def is_available() -> bool:
    return _WEBKIT_AVAILABLE


def _try_import() -> bool:
    global _WEBKIT_AVAILABLE
    try:
        import AppKit  # noqa: F401
        import WebKit  # noqa: F401
        import objc  # noqa: F401
        _WEBKIT_AVAILABLE = True
    except ImportError:
        logger.warning("PyObjC WebKit not available — httpx-only mode active")
        _WEBKIT_AVAILABLE = False
    return _WEBKIT_AVAILABLE


_try_import()


# ---------------------------------------------------------------------------
# Async parent: spawn the helper subprocess and read its output
# ---------------------------------------------------------------------------

async def render_page(url: str, timeout: float = 10.0) -> Optional[str]:
    """
    Render a URL with WebKit in an isolated subprocess and return raw HTML.
    Returns None on failure or timeout.
    """
    if not _WEBKIT_AVAILABLE:
        return None

    async with _get_render_sem():
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                __file__,
                "render",
                url,
                str(timeout),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            logger.debug("failed to spawn WebKit subprocess: %s", e)
            return None

        try:
            # The child self-limits via an in-loop timer; this is a hard backstop.
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout + 5.0
            )
        except asyncio.TimeoutError:
            logger.warning("WebKit subprocess timed out for %s — killing", url)
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return None

    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip()
        logger.debug("WebKit subprocess exit %s for %s: %s", proc.returncode, url, msg[:200])
        return None

    html = stdout.decode("utf-8", errors="replace")
    return html or None


# ---------------------------------------------------------------------------
# Child process: render a single URL on the main thread
# ---------------------------------------------------------------------------

def _render_main(url: str, timeout: float) -> int:
    """
    Render `url` on this process's main thread and write HTML to stdout.
    Returns a process exit code (0 = success, even for partial content).
    """
    from safety import check_url_sync, is_url_safe_sync

    # SSRF guard inside the child too (defense in depth).
    reason = check_url_sync(url)
    if reason:
        sys.stderr.write(f"blocked: {reason}\n")
        return 2

    try:
        import AppKit
        import WebKit
        import objc
    except ImportError as e:
        sys.stderr.write(f"PyObjC unavailable: {e}\n")
        return 3

    holder: dict[str, Optional[str]] = {"html": None, "done": False}

    def _finish(html: Optional[str]) -> None:
        if holder["done"]:
            return
        holder["done"] = True
        holder["html"] = html
        app = AppKit.NSApplication.sharedApplication()
        app.stop_(None)
        # stop_ only takes effect on the next event — post one to wake the loop.
        event = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            AppKit.NSEventTypeApplicationDefined,
            AppKit.NSMakePoint(0, 0),
            0, 0, 0, None, 0, 0, 0,
        )
        app.postEvent_atStart_(event, True)

    NavigationDelegate = _build_child_delegate(WebKit, objc, AppKit, holder, _finish, is_url_safe_sync)

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    config = WebKit.WKWebViewConfiguration.alloc().init()
    config.setWebsiteDataStore_(WebKit.WKWebsiteDataStore.nonPersistentDataStore())
    prefs = WebKit.WKPreferences.alloc().init()
    try:
        prefs.setJavaScriptEnabled_(True)
    except Exception:
        pass
    config.setPreferences_(prefs)

    frame = AppKit.NSMakeRect(0, 0, 1280, 800)
    webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(frame, config)
    delegate = NavigationDelegate.alloc().init()
    webview.setNavigationDelegate_(delegate)

    nsurl = AppKit.NSURL.URLWithString_(url)
    if nsurl is None:
        sys.stderr.write("invalid URL\n")
        return 4
    request = AppKit.NSURLRequest.requestWithURL_(nsurl)

    def _start_load():
        webview.loadRequest_(request)
        # Timeout fires on the main run loop; grabs partial content if any.
        delegate.scheduleTimeout_withWebView_(timeout, webview)

    # Block the page's own JS subresource/XHR loads to internal hosts (the main
    # frame is already validated, but JS fetch() could otherwise reach internal
    # services). Compile a content rule list, then start the load once it's in
    # effect. Falls back to loading without the rules if compilation fails.
    _install_ssrf_rules_then_load(WebKit, webview, _start_load)

    app.run()

    html = holder["html"]
    if html:
        sys.stdout.write(html)
        sys.stdout.flush()
    return 0


# Block loads whose URL host is a private/loopback/link-local literal. WebKit's
# content-rule regex engine rejects alternation/groups ("Disjunctions are not
# supported"), so each range is its own rule using only literals + char classes.
_PRIVATE_URL_FILTERS = [
    r"^https?://localhost",
    r"^https?://127\.",
    r"^https?://0\.0\.0\.0",
    r"^https?://10\.",
    r"^https?://169\.254\.",
    r"^https?://192\.168\.",
    r"^https?://172\.1[6-9]\.",
    r"^https?://172\.2[0-9]\.",
    r"^https?://172\.3[01]\.",
    r"^https?://\[::1",
    r"^https?://\[fc",
    r"^https?://\[fd",
    r"^https?://\[fe80",
]
_SSRF_RULE_JSON = json.dumps(
    [{"trigger": {"url-filter": f}, "action": {"type": "block"}} for f in _PRIVATE_URL_FILTERS]
)


def _install_ssrf_rules_then_load(WebKit, webview, start_load) -> None:
    """
    Compile the SSRF content-rule list and, in its completion handler, attach it
    to the web view before starting the load. On any failure, load anyway.
    """
    try:
        store = WebKit.WKContentRuleListStore.defaultStore()
    except Exception:
        start_load()
        return

    def _compiled(rule_list, error):
        try:
            if rule_list is not None and error is None:
                ucc = webview.configuration().userContentController()
                ucc.addContentRuleList_(rule_list)
            elif error is not None:
                sys.stderr.write(f"content-rule compile error: {error}\n")
        except Exception as e:
            sys.stderr.write(f"content-rule attach error: {e}\n")
        finally:
            start_load()

    try:
        store.compileContentRuleListForIdentifier_encodedContentRuleList_completionHandler_(
            "ssrf-block", _SSRF_RULE_JSON, _compiled
        )
    except Exception as e:
        sys.stderr.write(f"content-rule compile call failed: {e}\n")
        start_load()


def _build_child_delegate(WebKit, objc, AppKit, holder, finish, is_url_safe_sync):
    """Build the WKNavigationDelegate class used inside the child process."""

    class NavigationDelegate(
        objc.lookUpClass("NSObject"),
        protocols=[objc.protocolNamed("WKNavigationDelegate")],
    ):
        def webView_didReceiveServerRedirectForProvisionalNavigation_(self, webview, navigation):
            # SSRF guard on HTTP redirects: a public page can 3xx to an internal host.
            try:
                url_obj = webview.URL()
                url_str = str(url_obj.absoluteString()) if url_obj else None
            except Exception:
                url_str = None
            if url_str and not is_url_safe_sync(url_str):
                sys.stderr.write(f"blocked redirect: {url_str}\n")
                try:
                    webview.stopLoading()
                except Exception:
                    pass
                finish(None)

        def webView_didFinishNavigation_(self, webview, navigation):
            self._extract(webview)

        def webView_didFailNavigation_withError_(self, webview, navigation, error):
            finish(holder["html"])

        def webView_didFailProvisionalNavigation_withError_(self, webview, navigation, error):
            finish(holder["html"])

        @objc.python_method
        def _extract(self, webview):
            def _js_done(result, error):
                finish(result if result else holder["html"])
            webview.evaluateJavaScript_completionHandler_(
                "document.documentElement.outerHTML", _js_done
            )

        def scheduleTimeout_withWebView_(self, timeout, webview):
            self._webview = webview
            AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                float(timeout), self, b"timeoutFired:", None, False
            )

        def timeoutFired_(self, timer):
            if holder["done"]:
                return
            # Try to salvage whatever has rendered so far, then stop.
            try:
                self._extract(self._webview)
            except Exception:
                finish(holder["html"])

    return NavigationDelegate


# ---------------------------------------------------------------------------
# Script entry point (child process)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "render":
        _url = sys.argv[2]
        _timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
        sys.exit(_render_main(_url, _timeout))
    sys.stderr.write("usage: python webkit_renderer.py render <url> [timeout]\n")
    sys.exit(64)
