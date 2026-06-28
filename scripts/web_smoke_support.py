#!/usr/bin/env python3
"""Shared helpers for browser smokes against the static Web channel."""

from __future__ import annotations

import contextlib
import functools
import http.server
import sys
import threading
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "channel" / "web"
STATIC_ROOT = WEB_ROOT / "static"


class WebAssetHandler(http.server.SimpleHTTPRequestHandler):
    """Serve chat.html plus /assets/* from channel/web/static."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def translate_path(self, path: str) -> str:
        parsed_path = unquote(urlparse(path).path)
        if parsed_path in ("", "/", "/chat.html"):
            return str((WEB_ROOT / "chat.html").resolve())
        if parsed_path.startswith("/assets/"):
            relative = parsed_path[len("/assets/") :].lstrip("/")
            candidate = (STATIC_ROOT / relative).resolve()
            static_root = STATIC_ROOT.resolve()
            if candidate == static_root or static_root in candidate.parents:
                return str(candidate)
        return str((WEB_ROOT / "__missing__").resolve())


class QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """Threading server that keeps browser-smoke failure output structured."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        _exc_type, exc, _traceback = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class StaticSiteHandler(http.server.SimpleHTTPRequestHandler):
    """Serve an arbitrary static site root with path traversal protection."""

    site_root: Path = ROOT
    index_name: str = "index.html"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(self.site_root), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def translate_path(self, path: str) -> str:
        parsed_path = unquote(urlparse(path).path)
        if parsed_path in ("", "/"):
            parsed_path = f"/{self.index_name}"
        relative = parsed_path.lstrip("/")
        candidate = (self.site_root / relative).resolve()
        root = self.site_root.resolve()
        if candidate == root or root in candidate.parents:
            return str(candidate)
        return str((root / "__missing__").resolve())


@contextlib.contextmanager
def web_asset_server() -> Iterator[str]:
    handler = functools.partial(WebAssetHandler)
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/chat.html"
    finally:
        server.shutdown()
        with contextlib.suppress(Exception):
            server.server_close()


@contextlib.contextmanager
def static_site_server(root: Path, index_name: str = "index.html") -> Iterator[str]:
    site_root = Path(root).resolve()
    if not site_root.is_dir():
        raise FileNotFoundError(f"static site root does not exist: {site_root}")
    if not (site_root / index_name).is_file():
        raise FileNotFoundError(f"static site index does not exist: {site_root / index_name}")

    handler_cls = type(
        "BoundStaticSiteHandler",
        (StaticSiteHandler,),
        {"site_root": site_root, "index_name": index_name},
    )
    handler = functools.partial(handler_cls)
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/{index_name}"
    finally:
        server.shutdown()
        with contextlib.suppress(Exception):
            server.server_close()


def base_api_stub_script(extra_fetch_cases: str = "") -> str:
    """Return a browser init script with deterministic Web API stubs.

    `extra_fetch_cases` is inserted inside the fetch handler after `path` and
    `url` are available. It may return a Response for slice-specific APIs.
    """

    return r"""
(() => {
  const makeResponse = (body, status = 200) => Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' }
    })
  );

  window.__ecorexRuntimePath = (value) => value;
  window.__ecorexSmoke = window.__ecorexSmoke || {};
  window.fetch = (input, init) => {
    const raw = String(input && input.url ? input.url : input || '');
    const url = new URL(raw, window.location.href);
    const path = url.pathname;
""" + (extra_fetch_cases or "") + r"""
    if (path === '/config') return makeResponse({ status: 'success', title: 'EcoreX Browser Smoke' });
    if (path === '/auth/check') return makeResponse({ auth_required: false, authenticated: true });
    if (path === '/poll') return makeResponse({ status: 'success', has_content: false });
    if (path === '/api/history') return makeResponse({ status: 'success', messages: [], has_more: false });
    if (path === '/api/runtime-projection') {
      return makeResponse({
        status: 'success',
        projection: {
          request_id: url.searchParams.get('request_id') || '',
          session_id: url.searchParams.get('session_id') || '',
          latest_event_id: 0,
          event_count: 0,
          messages: [],
          requests: [],
          history: { messages: [], has_more: false }
        }
      });
    }
    if (path === '/api/knowledge/list') return makeResponse({ status: 'success', tree: [], root_files: [] });
    if (path === '/api/version') return makeResponse({ version: '0.2.2-browser-smoke' });
    if (path === '/api/models') return makeResponse({ status: 'success', capabilities: {} });
    if (path === '/api/tools') return makeResponse({ status: 'success', tools: [] });
    if (path === '/api/skills') return makeResponse({ status: 'success', skills: [] });
    if (path === '/api/channels') return makeResponse({ status: 'success', channels: [] });
    if (path === '/api/scheduler') return makeResponse({
      status: 'success',
      enabled: true,
      initialized: true,
      running: false,
      serviceStatus: 'browser_smoke',
      taskCount: 0,
      counts: { total: 0, enabled: 0, disabled: 0, error: 0 },
      taskStore: {},
      tasks: []
    });
    return makeResponse({ status: 'success' });
  };

  class SmokeEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.listeners = {};
      setTimeout(() => this._emit('open', {}), 0);
    }
    addEventListener(type, handler) {
      (this.listeners[type] ||= []).push(handler);
    }
    removeEventListener(type, handler) {
      this.listeners[type] = (this.listeners[type] || []).filter((item) => item !== handler);
    }
    _emit(type, payload) {
      const event = { type, data: payload && payload.data ? payload.data : '' };
      (this.listeners[type] || []).forEach((handler) => handler(event));
      const direct = this[`on${type}`];
      if (typeof direct === 'function') direct(event);
    }
    close() {
      this.readyState = 2;
    }
  }
  window.EventSource = SmokeEventSource;
})();
"""
