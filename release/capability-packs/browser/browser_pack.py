"""Fixed Playwright/CDP lifecycle and bounded HTTPS fetch implementation."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import sys
import tempfile
from typing import Any, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request as URLRequest, build_opener
import zipfile

from ecorex_pack_protocol import (
    ContractError,
    Request,
    bounded_int,
    bounded_text,
    require_exact_arguments,
)


_RUNTIME_ARCHIVE = "browser-runtime.zip"
_RUNTIME_MANIFEST = "browser-runtime.json"
_MAX_FETCH_BYTES = 1024 * 1024
_MAX_RUNTIME_BYTES = 512 * 1024 * 1024
_MAX_SCREENSHOT_BYTES = 3 * 1024 * 1024
_MAX_LOCAL_DOCUMENT_URL_BYTES = 64 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CLOUD_METADATA_IPS = frozenset({ipaddress.ip_address("fd00:ec2::254")})
_CDP_OPERATIONS = frozenset(
    {
        "navigate",
        "snapshot",
        "click",
        "fill",
        "select",
        "scroll",
        "wait",
        "screenshot",
        "back",
        "forward",
        "get_text",
        "press",
        "evaluate",
    }
)
_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def handle(request: Request) -> Mapping[str, Any]:
    if request.tool_id == "web_fetch":
        return _fetch(request.arguments)
    if request.tool_id == "web_search":
        return _web_search(request.arguments)
    if request.tool_id == "browser":
        handler = BrowserPackHandler()
        try:
            return handler(request)
        finally:
            handler.close()
    raise ContractError("browser_tool_unsupported")


class BrowserPackHandler:
    """CowAgent-style browser lifetime scoped to one Runtime process.

    The signed Pack process and page are long-lived. Navigation, cookies and
    element refs therefore survive later tool calls just as they do in
    CowAgent; the enclosing Runtime process remains the account boundary.
    """

    def __init__(self) -> None:
        self._engine: _BrowserEngine | None = None
        self._session: _BrowserSession | None = None

    def __call__(self, request: Request) -> Mapping[str, Any]:
        if request.tool_id == "web_fetch":
            return _fetch(request.arguments)
        if request.tool_id == "web_search":
            return _web_search(request.arguments)
        if request.tool_id != "browser":
            raise ContractError("browser_tool_unsupported")
        if self._session is None:
            if self._engine is None:
                self._engine = _BrowserEngine()
            self._session = self._engine.new_session()
        return self._session.execute(request.arguments)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        if self._engine is not None:
            self._engine.close()
            self._engine = None


class _BrowserEngine:
    def __init__(self) -> None:
        self._runtime_context = _browser_runtime()
        self._runtime: Path | None = None
        self._python_root: Path | None = None
        self._manager: Any = None
        self._browser: Any = None

    def _start(self) -> None:
        if self._browser is not None:
            return
        try:
            runtime = self._runtime_context.__enter__()
            self._runtime = runtime
            self._python_root = runtime / "python"
            browser_executable = runtime / _runtime_descriptor(runtime)[
                "browser_executable"
            ]
            sys.path.insert(0, str(self._python_root))
            try:
                from playwright.sync_api import sync_playwright
            except Exception:
                raise ContractError("browser_runtime_import_failed") from None
            self._manager = _browser_phase(
                "browser_driver_start_failed", sync_playwright, retryable=True
            )
            playwright = _browser_phase(
                "browser_driver_start_failed", self._manager.start, retryable=True
            )
            self._browser = _browser_phase(
                "browser_launch_failed",
                lambda: playwright.chromium.launch(
                    executable_path=str(browser_executable),
                    headless=True,
                    args=[
                        "--disable-background-networking",
                        "--disable-breakpad",
                        "--disable-component-update",
                        "--disable-default-apps",
                        "--disable-extensions",
                        "--disable-sync",
                        "--metrics-recording-only",
                        "--no-first-run",
                    ],
                ),
                retryable=True,
            )
        except BaseException:
            self.close()
            raise

    def new_session(self) -> "_BrowserSession":
        self._start()
        context = _browser_phase(
            "browser_context_create_failed",
            lambda: self._browser.new_context(
                accept_downloads=False,
                service_workers="block",
                viewport={"width": 1280, "height": 800},
            ),
            retryable=True,
        )
        _browser_phase(
            "browser_network_guard_failed",
            lambda: _install_browser_network_guard(context),
        )
        page = _browser_phase(
            "browser_page_create_failed", context.new_page, retryable=True
        )
        return _BrowserSession(context, page)

    def close(self) -> None:
        if self._browser is not None:
            _browser_cleanup_phase("browser_close_failed", self._browser.close, suppress=True)
            self._browser = None
        if self._manager is not None:
            _browser_cleanup_phase(
                "browser_driver_stop_failed",
                lambda: self._manager.__exit__(None, None, None),
                suppress=True,
            )
            self._manager = None
        if self._python_root is not None:
            try:
                sys.path.remove(str(self._python_root))
            except ValueError:
                pass
            self._python_root = None
        if self._runtime is not None:
            try:
                self._runtime_context.__exit__(None, None, None)
            finally:
                self._runtime = None


class _BrowserSession:
    def __init__(self, context: Any, page: Any) -> None:
        self._context = context
        self._page = page

    def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        require_exact_arguments(
            arguments,
            required=frozenset({"action"}),
            optional=frozenset(
                {
                    "url", "ref", "selector", "text", "value", "key",
                    "direction", "amount", "script", "full_page", "timeout",
                }
            ),
        )
        action = bounded_text(arguments.get("action"), 128)
        if action not in _CDP_OPERATIONS:
            raise ContractError("browser_operation_not_supported")
        timeout_ms = bounded_int(arguments.get("timeout", 20_000), 100, 60_000)
        self._page.set_default_timeout(timeout_ms)
        url = arguments.get("url")
        if action == "navigate":
            url = bounded_text(url, 4096)
            if "://" not in url and not url.startswith(("about:", "data:")):
                url = "https://" + url
            if not url.startswith("data:text/html,") and url != "about:blank":
                _validate_browser_navigation_url(url)
            _browser_phase(
                "browser_navigation_failed",
                lambda: self._page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                ),
                retryable=True,
            )
        elif url is not None:
            raise ContractError("browser_target_only_for_navigation")
        return _browser_phase(
            "browser_page_operation_failed",
            lambda: _perform_page_operation(self._page, action, arguments),
            retryable=True,
        )

    def close(self) -> None:
        _browser_cleanup_phase(
            "browser_context_close_failed", self._context.close, suppress=True
        )


def _fetch(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    require_exact_arguments(
        arguments,
        required=frozenset({"url"}),
        optional=frozenset({"max_bytes"}),
    )
    url = bounded_text(arguments.get("url"), 4096)
    maximum = bounded_int(arguments.get("max_bytes", _MAX_FETCH_BYTES), 1, _MAX_FETCH_BYTES)
    _validate_public_url(url)
    opener = build_opener(_ValidatedRedirect())
    request = URLRequest(
        url,
        headers={
            "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.5",
            "User-Agent": "EcoreX/1.0 managed-browser-pack",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=20) as response:
            final_url = response.geturl()
            _validate_public_url(final_url)
            body = response.read(maximum + 1)
            if len(body) > maximum:
                raise ContractError("browser_fetch_response_too_large")
            content_type = str(response.headers.get("Content-Type") or "")[:256]
            status = int(response.status)
    except ContractError:
        raise
    except HTTPError as error:
        raise ContractError("browser_fetch_http_error", retryable=500 <= error.code < 600) from None
    except (OSError, URLError, TimeoutError):
        raise ContractError("browser_fetch_unavailable", retryable=True) from None
    title, text, links = _readable_web_content(body, content_type, final_url)
    return {
        "url": final_url,
        "status": status,
        "content_type": content_type,
        "title": title,
        "text": text,
        "links": links,
        "sha256": hashlib.sha256(body).hexdigest(),
        "size_bytes": len(body),
    }


class _DuckSearchHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._result: dict[str, str] | None = None
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        classes = set(values.get("class", "").split())
        if tag == "a" and "result__a" in classes:
            if self._result and self._result.get("title") and self._result.get("url"):
                self.results.append(self._result)
            self._result = {"title": "", "url": _search_result_url(values.get("href", "")), "snippet": ""}
            self._capture = "title"
        elif self._result is not None and "result__snippet" in classes:
            self._capture = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag in {"a", "div", "span"}:
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._result is None or self._capture is None:
            return
        value = " ".join(data.split())
        if value:
            current = self._result[self._capture]
            self._result[self._capture] = (current + " " + value).strip()[:4096]

    def close(self) -> None:
        super().close()
        if self._result and self._result.get("title") and self._result.get("url"):
            self.results.append(self._result)
        self._result = None


def _web_search(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    require_exact_arguments(
        arguments,
        required=frozenset({"query"}),
        optional=frozenset({"count", "freshness"}),
    )
    query = bounded_text(arguments.get("query"), 4096)
    count = bounded_int(arguments.get("count", 5), 1, 10)
    freshness = arguments.get("freshness", "any")
    if freshness not in {"any", "day", "week", "month", "year"}:
        raise ContractError("browser_search_freshness_invalid")
    parameters = {"q": query}
    if freshness != "any":
        parameters["df"] = {"day": "d", "week": "w", "month": "m", "year": "y"}[freshness]
    url = "https://html.duckduckgo.com/html/?" + urlencode(parameters)
    request = URLRequest(
        url,
        headers={
            "Accept": "text/html",
            "User-Agent": "Mozilla/5.0 e-Mate/2 browser-search",
        },
        method="GET",
    )
    try:
        with build_opener(_ValidatedRedirect()).open(request, timeout=20) as response:
            body = response.read(_MAX_FETCH_BYTES + 1)
            if len(body) > _MAX_FETCH_BYTES:
                raise ContractError("browser_search_response_too_large")
    except ContractError:
        raise
    except HTTPError as error:
        raise ContractError("browser_search_http_error", retryable=500 <= error.code < 600) from None
    except (OSError, URLError, TimeoutError):
        raise ContractError("browser_search_unavailable", retryable=True) from None
    parser = _DuckSearchHTML()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
    except Exception:
        raise ContractError("browser_search_response_invalid") from None
    results = parser.results[:count]
    if not results:
        raise ContractError("browser_search_no_results", retryable=True)
    return {
        "query": query,
        "provider": "duckduckgo-html",
        "results": results,
    }


def _search_result_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.path == "/l/":
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        value = unquote(target)
        parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return value[:4096]


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._title = False
        self.title: list[str] = []
        self.text: list[str] = []
        self.links: list[dict[str, str]] = []
        self._active_link: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1
            return
        if tag == "title":
            self._title = True
        if tag == "a" and len(self.links) < 100:
            href = next((value for name, value in attrs if name == "href"), None)
            if isinstance(href, str) and href:
                self.links.append({"url": href[:4096], "text": ""})
                self._active_link = len(self.links) - 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        if tag == "title":
            self._title = False
        if tag == "a":
            self._active_link = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        value = " ".join(data.split())
        if not value:
            return
        if self._title:
            self.title.append(value)
        self.text.append(value)
        if self._active_link is not None:
            current = self.links[self._active_link]["text"]
            self.links[self._active_link]["text"] = (current + " " + value).strip()[:512]


def _readable_web_content(
    body: bytes, content_type: str, final_url: str
) -> tuple[str, str, list[dict[str, str]]]:
    lowered = content_type.casefold()
    if not any(token in lowered for token in ("text/", "json", "xml", "html")):
        raise ContractError("browser_fetch_content_type_unsupported")
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, re.IGNORECASE)
    charset = match.group(1) if match else "utf-8"
    try:
        decoded = body.decode(charset, errors="replace")
    except LookupError:
        decoded = body.decode("utf-8", errors="replace")
    if "html" not in lowered and "<html" not in decoded[:1024].casefold():
        return "", decoded[:200_000], []
    parser = _ReadableHTML()
    try:
        parser.feed(decoded)
    except Exception:
        raise ContractError("browser_fetch_content_invalid") from None
    title = " ".join(parser.title)[:4096]
    text = "\n".join(parser.text)[:200_000]
    links = []
    for item in parser.links:
        url = item["url"]
        if url.startswith("/"):
            parsed = urlsplit(final_url)
            url = f"{parsed.scheme}://{parsed.netloc}{url}"
        links.append({"url": url[:4096], "text": item["text"]})
    return title, text, links


class _ValidatedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_public_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port not in {None, 80, 443}
    ):
        raise ContractError("browser_url_not_allowed")
    try:
        addresses = {
            ipaddress.ip_address(record[4][0])
            for record in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, ValueError):
        raise ContractError("browser_dns_unavailable", retryable=True) from None
    try:
        literal_host = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_host = None

    def blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        # Clash and compatible system TUN resolvers deliberately map public
        # DNS names into RFC 2544's non-routable benchmark range.  The local
        # proxy then intercepts that destination.  Permit this mapping only
        # for a hostname; a user-supplied literal 198.18/15 address remains
        # denied, as do private, loopback, link-local and other reserved IPs.
        if literal_host is None and address in _PROXY_FAKE_IP_NETWORK:
            return False
        return bool(
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )

    if not addresses or any(blocked(address) for address in addresses):
        raise ContractError("browser_url_not_allowed")


def _browser_phase(
    code: str,
    operation: Any,
    *,
    retryable: bool = False,
) -> Any:
    try:
        return operation()
    except ContractError:
        raise
    except Exception:
        raise ContractError(code, retryable=retryable) from None


def _browser_cleanup_phase(
    code: str,
    operation: Any,
    *,
    suppress: bool,
) -> None:
    try:
        operation()
    except Exception:
        if not suppress:
            raise ContractError(code, retryable=True) from None


def _install_browser_network_guard(context: Any) -> None:
    context.route("**/*", _guard_browser_request)
    context.route_web_socket("**/*", lambda websocket: websocket.close())


def _guard_browser_request(route: Any) -> None:
    try:
        _validate_browser_request_url(str(route.request.url))
    except ContractError:
        route.abort(error_code="blockedbyclient")
        return
    route.continue_()


def _validate_browser_request_url(value: str) -> None:
    if value == "about:blank":
        return
    if value.startswith("data:"):
        if len(value.encode("utf-8")) > _MAX_LOCAL_DOCUMENT_URL_BYTES:
            raise ContractError("browser_url_not_allowed")
        return
    _validate_browser_navigation_url(value)


def _validate_browser_navigation_url(value: str) -> None:
    """Keep CowAgent's local/LAN browser workflow while blocking metadata."""

    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ContractError("browser_url_not_allowed")
    try:
        addresses = {
            ipaddress.ip_address(record[4][0])
            for record in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, ValueError):
        raise ContractError("browser_dns_unavailable", retryable=True) from None
    if not addresses or any(
        address.is_link_local or address in _CLOUD_METADATA_IPS
        for address in addresses
    ):
        raise ContractError("browser_url_not_allowed")


def _perform_page_operation(
    page: Any,
    action: str,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    if action in {"navigate", "snapshot"}:
        return _page_snapshot(page)
    if action == "evaluate":
        script = bounded_text(
            arguments.get("script"), 20_000, code="browser_script_invalid"
        )
        try:
            value = page.evaluate(script)
        except Exception as error:
            if "Illegal return statement" not in str(error):
                raise
            value = page.evaluate(f"(() => {{\n{script}\n}})()")
        return {"url": page.url, "value": value}
    if action == "back":
        page.go_back(wait_until="domcontentloaded")
        return {"url": page.url, "completed": True}
    if action == "forward":
        page.go_forward(wait_until="domcontentloaded")
        return {"url": page.url, "completed": True}
    if action == "scroll":
        direction = arguments.get("direction", "down")
        if direction not in {"up", "down", "left", "right"}:
            raise ContractError("browser_scroll_invalid")
        amount = bounded_int(arguments.get("amount", 500), 1, 10_000)
        delta_x = (
            amount if direction == "right" else -amount if direction == "left" else 0
        )
        delta_y = (
            amount if direction == "down" else -amount if direction == "up" else 0
        )
        page.mouse.wheel(delta_x, delta_y)
        return {
            "url": page.url,
            "scroll_x": page.evaluate("window.scrollX"),
            "scroll_y": page.evaluate("window.scrollY"),
        }
    if action == "press":
        key = bounded_text(
            arguments.get("key"), 128, code="browser_key_invalid"
        )
        page.keyboard.press(key)
        return {"url": page.url, "completed": True}
    if (
        action == "wait"
        and not arguments.get("selector")
        and not arguments.get("ref")
    ):
        page.wait_for_timeout(
            bounded_int(arguments.get("timeout", 500), 100, 60_000)
        )
        return {"url": page.url, "completed": True}
    if (
        action == "screenshot"
        and not arguments.get("selector")
        and not arguments.get("ref")
    ):
        return _screenshot_result(
            page.screenshot(
                type="png", full_page=bool(arguments.get("full_page", False))
            )
        )
    locator = _page_locator(page, arguments)
    if action == "click":
        locator.click()
        return {"url": page.url, "completed": True}
    if action == "fill":
        text = bounded_text(
            arguments.get("text"), 20_000, code="browser_text_invalid"
        )
        locator.fill(text)
        return {"url": page.url, "completed": True}
    if action == "select":
        value = bounded_text(
            arguments.get("value"), 20_000, code="browser_value_invalid"
        )
        locator.select_option(value)
        return {"url": page.url, "completed": True}
    if action == "wait":
        locator.wait_for(state="visible")
        return {"url": page.url, "visible": True}
    if action == "get_text":
        return {
            "url": page.url,
            "text": locator.inner_text(timeout=10_000)[:200_000],
        }
    if action == "screenshot":
        return _screenshot_result(locator.screenshot(type="png"))
    raise ContractError("browser_operation_not_supported")


_SNAPSHOT_SCRIPT = r"""
() => {
  let nextRef = 0;
  const interactive = [];
  const candidates = document.querySelectorAll(
    'a,button,input,textarea,select,summary,[role],[onclick],[tabindex],[contenteditable="true"]'
  );
  for (const element of candidates) {
    if (!(element instanceof HTMLElement)) continue;
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    if (style.display === 'none' || style.visibility === 'hidden' ||
        Number(style.opacity) === 0 || box.width === 0 || box.height === 0) continue;
    nextRef += 1;
    element.setAttribute('data-emate-ref', String(nextRef));
    const text = (element.innerText || element.textContent || '').trim().slice(0, 240);
    interactive.push({
      ref: nextRef,
      tag: element.tagName.toLowerCase(),
      role: element.getAttribute('role') || null,
      text,
      aria_label: element.getAttribute('aria-label') || null,
      placeholder: element.getAttribute('placeholder') || null,
      href: element instanceof HTMLAnchorElement ? element.getAttribute('href') : null,
      value: 'value' in element ? String(element.value || '').slice(0, 240) : null
    });
    if (interactive.length >= 500) break;
  }
  return {
    text: (document.body ? document.body.innerText : '').slice(0, 200000),
    interactive
  };
}
"""


def _page_snapshot(page: Any) -> Mapping[str, Any]:
    snapshot = page.evaluate(_SNAPSHOT_SCRIPT)
    if not isinstance(snapshot, Mapping):
        raise ContractError("browser_snapshot_invalid")
    interactive = snapshot.get("interactive")
    text = snapshot.get("text")
    if not isinstance(interactive, list) or not isinstance(text, str):
        raise ContractError("browser_snapshot_invalid")
    return {
        "url": str(page.url)[:4096],
        "title": str(page.title())[:4096],
        "text": text[:200_000],
        "interactive": interactive[:500],
    }


def _page_locator(page: Any, parameters: Mapping[str, Any]) -> Any:
    ref = parameters.get("ref")
    if ref is not None:
        ref = bounded_int(ref, 1, 1000, code="browser_selector_invalid")
        return page.locator(f'[data-emate-ref="{ref}"]').first
    selector = bounded_text(
        parameters.get("selector"), 2048, code="browser_selector_invalid"
    )
    return page.locator(selector).first


def _screenshot_result(payload: bytes) -> Mapping[str, Any]:
    if len(payload) > _MAX_SCREENSHOT_BYTES:
        raise ContractError("browser_screenshot_too_large")
    return {
        "mime_type": "image/png",
        "content_base64": base64.b64encode(payload).decode("ascii"),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


@contextmanager
def _browser_runtime() -> Iterator[Path]:
    outer = Path(sys.argv[0]).resolve(strict=True)
    try:
        with zipfile.ZipFile(outer) as pack:
            manifest_payload = pack.read(_RUNTIME_MANIFEST)
            archive_payload = pack.read(_RUNTIME_ARCHIVE)
    except (OSError, KeyError, zipfile.BadZipFile):
        raise ContractError("browser_runtime_missing") from None
    manifest = _parse_runtime_manifest(manifest_payload, archive_payload)
    # Windows keeps imported extension modules (notably greenlet's ``.pyd``)
    # mapped until this one-shot Pack process exits.  Core gives every Pack
    # call a private TEMP/TMP domain and removes that domain after the child is
    # reaped, so the child must not turn an otherwise successful operation into
    # ``pack_internal_failure`` while trying to unlink its still-mapped DLL.
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        temporary = tempfile.TemporaryDirectory(
            prefix="ecorex-browser-runtime-",
            ignore_cleanup_errors=os.name == "nt",
        )
        root = Path(temporary.name).resolve(strict=True)
        archive_path = root / _RUNTIME_ARCHIVE
        archive_path.write_bytes(archive_payload)
        _extract_verified_runtime(archive_path, root / "payload", manifest)
    except ContractError:
        if temporary is not None:
            try:
                temporary.cleanup()
            except Exception:
                pass
        raise
    except Exception:
        if temporary is not None:
            try:
                temporary.cleanup()
            except Exception:
                pass
        raise ContractError("browser_runtime_prepare_failed") from None
    try:
        yield root / "payload"
    except BaseException:
        try:
            temporary.cleanup()
        except Exception:
            pass
        raise
    else:
        try:
            temporary.cleanup()
        except Exception:
            raise ContractError("browser_runtime_cleanup_failed") from None


def _parse_runtime_manifest(payload: bytes, archive_payload: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ContractError("browser_runtime_manifest_invalid") from None
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema_version", "archive_sha256", "browser_executable", "files"}
        or value.get("schema_version") != 1
        or value.get("archive_sha256") != hashlib.sha256(archive_payload).hexdigest()
        or not isinstance(value.get("browser_executable"), str)
        or not isinstance(value.get("files"), list)
        or not 1 <= len(value["files"]) <= 50_000
        or payload
        != json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ):
        raise ContractError("browser_runtime_manifest_invalid")
    executable = _safe_runtime_relative_path(value["browser_executable"])
    paths: set[str] = set()
    for record in value["files"]:
        if (
            not isinstance(record, Mapping)
            or set(record) != {"path", "size_bytes", "sha256", "mode"}
            or not isinstance(record.get("path"), str)
            or isinstance(record.get("size_bytes"), bool)
            or not isinstance(record.get("size_bytes"), int)
            or not 0 <= record["size_bytes"] <= _MAX_RUNTIME_BYTES
            or _SHA256.fullmatch(str(record.get("sha256"))) is None
            or record.get("mode") not in {0o644, 0o755}
        ):
            raise ContractError("browser_runtime_manifest_invalid")
        path = _safe_runtime_relative_path(record["path"]).as_posix()
        if path in paths:
            raise ContractError("browser_runtime_manifest_invalid")
        paths.add(path)
    if executable.as_posix() not in paths:
        raise ContractError("browser_runtime_manifest_invalid")
    return value


def _safe_runtime_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if (
        path.is_absolute()
        or not path.parts
        or path.as_posix() != value
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
    ):
        raise ContractError("browser_runtime_manifest_invalid")
    return path


def _extract_verified_runtime(archive_path: Path, destination: Path, manifest: Mapping[str, Any]) -> None:
    expected: dict[str, tuple[int, str, int]] = {}
    for record in manifest["files"]:
        expected[record["path"]] = (
            record["size_bytes"],
            record["sha256"],
            record["mode"],
        )
    destination.mkdir()
    observed: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                path = PurePosixPath(member.filename.replace("\\", "/"))
                mode = member.external_attr >> 16
                if (
                    member.is_dir()
                    or path.is_absolute()
                    or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
                    or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                    or member.filename in observed
                    or member.filename not in expected
                ):
                    raise ContractError("browser_runtime_archive_invalid")
                observed.add(member.filename)
                total += member.file_size
                if total > _MAX_RUNTIME_BYTES:
                    raise ContractError("browser_runtime_archive_too_large")
                data = archive.read(member)
                size, digest, file_mode = expected[member.filename]
                if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                    raise ContractError("browser_runtime_digest_mismatch")
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(file_mode)
    except ContractError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError):
        raise ContractError("browser_runtime_archive_invalid") from None
    if observed != set(expected):
        raise ContractError("browser_runtime_archive_incomplete")
    executable = destination.joinpath(
        *_safe_runtime_relative_path(manifest["browser_executable"]).parts
    )
    if not executable.is_file():
        raise ContractError("browser_runtime_executable_missing")


def _runtime_descriptor(runtime: Path) -> Mapping[str, Any]:
    # The outer signed manifest was already verified before extraction.  Read
    # only the one field needed after the temporary directory is established.
    outer = Path(sys.argv[0]).resolve(strict=True)
    with zipfile.ZipFile(outer) as pack:
        value = json.loads(pack.read(_RUNTIME_MANIFEST).decode("utf-8"))
    return value
