"""Fixed Playwright/CDP lifecycle and bounded HTTPS fetch implementation."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
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
from urllib.parse import urlsplit
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
_CDP_OPERATIONS = frozenset(
    {"navigate", "snapshot", "click", "type", "wait", "screenshot", "evaluate", "batch"}
)
_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def handle(request: Request) -> Mapping[str, Any]:
    if request.tool_id == "fetch":
        return _fetch(request.arguments)
    if request.tool_id == "cdp":
        return _cdp(
            request.arguments,
            full_access=request.context.get("effective_sandbox") == "danger-full-access",
        )
    raise ContractError("browser_tool_unsupported")


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
    return {
        "url": final_url,
        "status": status,
        "content_type": content_type,
        "body_base64": base64.b64encode(body).decode("ascii"),
        "sha256": hashlib.sha256(body).hexdigest(),
        "size_bytes": len(body),
    }


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


def _cdp(arguments: Mapping[str, Any], *, full_access: bool) -> Mapping[str, Any]:
    require_exact_arguments(
        arguments,
        required=frozenset({"operation"}),
        optional=frozenset({"target", "parameters"}),
    )
    operation = bounded_text(arguments.get("operation"), 128)
    if operation not in _CDP_OPERATIONS:
        raise ContractError("browser_operation_not_supported")
    target = bounded_text(arguments.get("target"), 4096)
    parameters = arguments.get("parameters", {})
    if not isinstance(parameters, Mapping) or len(parameters) > 32:
        raise ContractError("browser_parameters_invalid")
    if operation == "evaluate" and not full_access:
        raise ContractError("browser_evaluate_requires_full_access")
    if operation == "batch":
        steps = parameters.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 16:
            raise ContractError("browser_batch_invalid")
        screenshot_count = 0
        for step in steps:
            if not isinstance(step, Mapping) or set(step) - {"operation", "parameters"}:
                raise ContractError("browser_batch_invalid")
            step_operation = step.get("operation")
            step_parameters = step.get("parameters", {})
            if (
                step_operation not in _CDP_OPERATIONS - {"batch", "navigate"}
                or not isinstance(step_parameters, Mapping)
                or len(step_parameters) > 32
            ):
                raise ContractError("browser_batch_invalid")
            if step_operation == "evaluate" and not full_access:
                raise ContractError("browser_evaluate_requires_full_access")
            screenshot_count += int(step_operation == "screenshot")
        if screenshot_count > 1:
            raise ContractError("browser_batch_invalid")
    if not target.startswith("data:text/html,"):
        _validate_public_url(target)
    timeout_ms = bounded_int(parameters.get("timeout_ms", 20_000), 100, 60_000)
    try:
        with _browser_runtime() as runtime:
            python_root = runtime / "python"
            browser_executable = (
                runtime / _runtime_descriptor(runtime)["browser_executable"]
            )
            sys.path.insert(0, str(python_root))
            try:
                try:
                    from playwright.sync_api import sync_playwright
                except Exception:
                    raise ContractError("browser_runtime_import_failed") from None
                manager = _browser_phase(
                    "browser_driver_start_failed",
                    sync_playwright,
                    retryable=True,
                )
                playwright = _browser_phase(
                    "browser_driver_start_failed",
                    manager.start,
                    retryable=True,
                )
                try:
                    browser = _browser_phase(
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
                    try:
                        context = _browser_phase(
                            "browser_context_create_failed",
                            lambda: browser.new_context(
                                accept_downloads=False,
                                service_workers="block",
                                viewport={"width": 1280, "height": 800},
                            ),
                            retryable=True,
                        )
                        try:
                            _browser_phase(
                                "browser_network_guard_failed",
                                lambda: _install_browser_network_guard(context),
                            )
                            page = _browser_phase(
                                "browser_page_create_failed",
                                context.new_page,
                                retryable=True,
                            )
                            page.set_default_timeout(timeout_ms)
                            _browser_phase(
                                "browser_navigation_failed",
                                lambda: page.goto(
                                    target,
                                    wait_until="domcontentloaded",
                                    timeout=timeout_ms,
                                ),
                                retryable=True,
                            )
                            result = _browser_phase(
                                "browser_page_operation_failed",
                                lambda: _perform_page_operation(
                                    page, operation, parameters, full_access=full_access
                                ),
                                retryable=True,
                            )
                        finally:
                            _browser_cleanup_phase(
                                "browser_context_close_failed",
                                context.close,
                                suppress=sys.exception() is not None,
                            )
                    finally:
                        _browser_cleanup_phase(
                            "browser_close_failed",
                            browser.close,
                            suppress=sys.exception() is not None,
                        )
                finally:
                    _browser_cleanup_phase(
                        "browser_driver_stop_failed",
                        lambda: manager.__exit__(None, None, None),
                        suppress=sys.exception() is not None,
                    )
            finally:
                if sys.path and sys.path[0] == str(python_root):
                    sys.path.pop(0)
    except ContractError:
        raise
    except Exception:
        raise ContractError("browser_runtime_cleanup_failed") from None
    return result


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
    """Validate every browser request, not only the top-level navigation.

    A public page, redirect, or caller-supplied ``data:`` document can create
    subresource requests after the initial URL check.  Intercept the complete
    HTTP request graph so explicit private/loopback/link-local destinations
    fail before Chromium sends them.  WebSockets are not needed by the bounded
    office-browser contract and are denied because HTTP routing does not cover
    their handshake lifecycle.
    """

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
    # Local documents are allowed only as bounded, inert URL payloads.  Any
    # network resource they reference is independently checked by the route
    # guard above.  Other non-HTTP schemes remain unavailable.
    if value == "about:blank":
        return
    if value.startswith("data:"):
        if len(value.encode("utf-8")) > _MAX_LOCAL_DOCUMENT_URL_BYTES:
            raise ContractError("browser_url_not_allowed")
        return
    _validate_public_url(value)


def _perform_page_operation(
    page: Any,
    operation: str,
    parameters: Mapping[str, Any],
    *,
    full_access: bool,
) -> Mapping[str, Any]:
    if operation == "batch":
        return {
            "url": page.url,
            "results": [
                _perform_page_operation(
                    page,
                    str(step["operation"]),
                    step.get("parameters", {}),
                    full_access=full_access,
                )
                for step in parameters["steps"]
            ],
        }
    if operation in {"navigate", "snapshot"}:
        text = page.locator("body").inner_text(timeout=10_000)[:200_000]
        return {"url": page.url, "title": page.title()[:4096], "text": text}
    if operation == "evaluate":
        if not full_access:
            raise ContractError("browser_evaluate_requires_full_access")
        expression = bounded_text(
            parameters.get("expression"),
            20_000,
            code="browser_evaluate_invalid",
        )
        value = page.evaluate(expression)
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise ContractError("browser_evaluate_result_invalid") from None
        if len(encoded) > 200_000:
            raise ContractError("browser_evaluate_result_too_large")
        return {"url": page.url, "value": value}
    selector = bounded_text(parameters.get("selector"), 2048, code="browser_selector_invalid")
    locator = page.locator(selector).first
    if operation == "click":
        locator.click()
        return {"url": page.url, "title": page.title()[:4096], "completed": True}
    if operation == "type":
        text = bounded_text(parameters.get("text"), 20_000)
        locator.fill(text)
        return {"url": page.url, "completed": True}
    if operation == "wait":
        locator.wait_for(state="visible")
        return {"url": page.url, "visible": True}
    if operation == "screenshot":
        payload = locator.screenshot(type="png")
        if len(payload) > _MAX_SCREENSHOT_BYTES:
            raise ContractError("browser_screenshot_too_large")
        return {
            "mime_type": "image/png",
            "content_base64": base64.b64encode(payload).decode("ascii"),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
    raise ContractError("browser_operation_not_supported")


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
