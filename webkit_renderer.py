from __future__ import annotations

"""
Headless WebKit renderer using PyObjC.

Runs WKWebView in a dedicated background thread with its own NSRunLoop.
Never call WebKit APIs from asyncio's event loop thread — deadlock.
Bridge back to asyncio via concurrent.futures.Future.
"""

import asyncio
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_WEBKIT_AVAILABLE = False
_renderer: Optional["_WebKitRenderer"] = None
_renderer_lock = threading.Lock()


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


def get_renderer() -> Optional["_WebKitRenderer"]:
    global _renderer
    if not _WEBKIT_AVAILABLE:
        return None
    with _renderer_lock:
        if _renderer is None:
            _renderer = _WebKitRenderer()
            _renderer.start()
        return _renderer


async def render_page(url: str, timeout: float = 10.0) -> Optional[str]:
    """Render a URL with WebKit and return raw HTML. Returns None on failure."""
    renderer = get_renderer()
    if renderer is None:
        return None
    loop = asyncio.get_event_loop()
    future: asyncio.Future[Optional[str]] = loop.create_future()

    def _callback(html: Optional[str], error: Optional[str]) -> None:
        if not future.done():
            loop.call_soon_threadsafe(
                future.set_result, html if html is not None else None
            )

    renderer.load_url(url, _callback, timeout=timeout)

    try:
        return await asyncio.wait_for(future, timeout=timeout + 2)
    except asyncio.TimeoutError:
        logger.warning("WebKit render timed out for %s", url)
        return None


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

class _WebKitRenderer:
    """Wraps a WKWebView running on a dedicated thread with its own NSRunLoop."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._webview = None
        self._app = None
        self._delegate_class = None
        self._pending: Optional[tuple] = None  # (url, callback, timeout)
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="webkit-runloop")
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        self._stop.set()

    def load_url(self, url: str, callback, timeout: float = 10.0) -> None:
        # The runloop polls self._pending every 50ms and picks this up automatically.
        with self._lock:
            self._pending = (url, callback, timeout)

    def _run_loop(self) -> None:
        """Entry point for the dedicated WebKit thread."""
        if not _WEBKIT_AVAILABLE:
            return
        try:
            import AppKit
            import WebKit
            import objc

            # Initialize a minimal NSApplication (needed for WebKit)
            app = AppKit.NSApplication.sharedApplication()
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

            # Build delegate class dynamically
            NavigationDelegate = self._build_delegate_class()

            # WKWebViewConfiguration: ephemeral (no persistent store)
            config = WebKit.WKWebViewConfiguration.alloc().init()
            config.setWebsiteDataStore_(
                WebKit.WKWebsiteDataStore.nonPersistentDataStore()
            )
            prefs = WebKit.WKPreferences.alloc().init()
            prefs.setJavaScriptEnabled_(True)
            config.setPreferences_(prefs)

            # Off-screen frame (1×1, never shown)
            frame = AppKit.NSMakeRect(0, 0, 1280, 800)
            webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(frame, config)

            delegate = NavigationDelegate.alloc().init()
            delegate._renderer = self  # back-reference
            webview.setNavigationDelegate_(delegate)

            self._webview = webview
            self._webview_controller = delegate
            self._ready.set()

            # Run the loop indefinitely, processing pending loads
            runloop = AppKit.NSRunLoop.currentRunLoop()
            while True:
                if self._stop.is_set():
                    break

                with self._lock:
                    pending = self._pending
                    self._pending = None

                if pending is not None:
                    url_str, callback, timeout = pending
                    self._do_load(url_str, callback, timeout)

                # Run loop for 50ms to process WebKit callbacks
                runloop.runUntilDate_(
                    AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.05)
                )

        except Exception as e:
            logger.error("WebKit thread crashed: %s", e)
            self._ready.set()

    def _build_delegate_class(self):
        """Dynamically build an ObjC NavigationDelegate class."""
        import WebKit
        import objc

        class NavigationDelegate(
            objc.lookUpClass("NSObject"),
            protocols=[objc.protocolNamed("WKNavigationDelegate")],
        ):
            _renderer = None
            _callback = None
            _timer_start = 0.0
            _timeout = 10.0
            _done = False

            def webView_didFinishNavigation_(self, webview, navigation):
                if self._done:
                    return
                self._extract_html(webview)

            def webView_didFailNavigation_withError_(self, webview, navigation, error):
                if self._done:
                    return
                self._done = True
                if self._callback:
                    self._callback(None, str(error))
                    self._callback = None

            def webView_didFailProvisionalNavigation_withError_(self, webview, navigation, error):
                if self._done:
                    return
                self._done = True
                if self._callback:
                    self._callback(None, str(error))
                    self._callback = None

            def _extract_html(self, webview):
                self._done = True
                cb = self._callback
                self._callback = None

                def _js_done(result, error):
                    if cb:
                        cb(result, None if error is None else str(error))

                webview.evaluateJavaScript_completionHandler_(
                    "document.documentElement.outerHTML", _js_done
                )


        return NavigationDelegate

    def _do_load(self, url_str: str, callback, timeout: float) -> None:
        """Called from the WebKit thread to initiate a page load."""
        import AppKit
        import WebKit

        delegate = self._webview.navigationDelegate()
        delegate._callback = callback
        delegate._timer_start = time.monotonic()
        delegate._timeout = timeout
        delegate._done = False

        nsurl = AppKit.NSURL.URLWithString_(url_str)
        if nsurl is None:
            callback(None, "Invalid URL")
            return
        request = AppKit.NSURLRequest.requestWithURL_(nsurl)
        self._webview.loadRequest_(request)

        # Timeout watchdog
        def _watchdog():
            time.sleep(timeout)
            if not delegate._done:
                logger.warning("WebKit timeout for %s — extracting partial", url_str)
                delegate._extract_html(self._webview)

        threading.Thread(target=_watchdog, daemon=True).start()
