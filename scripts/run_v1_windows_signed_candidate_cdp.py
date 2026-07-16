"""Run CDP acceptance inside the final verified signed-candidate window.

The ordinary candidate drill remains disposable. This wrapper asks it to run a
fixed repository-owned Node harness only after the fault candidate has rolled
back to the signed current known-good slot. The Runtime, browser process tree,
isolated browser profile, signing material and install root are still removed
by the drill on every terminal path.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, Mapping, Sequence


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ecorex.release.process_boundary import (  # noqa: E402
    BoundedProcessError,
    run_bounded_process,
)


_MAX_STDOUT_BYTES = 256 * 1024
_MAX_STDERR_BYTES = 4 * 1024
_MAX_BROWSER_SECONDS = 180.0
_MIN_BROWSER_SECONDS = 15.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _load_drill() -> Any:
    path = _REPO_ROOT / "scripts" / "drill_v1_windows_signed_candidate.py"
    spec = importlib.util.spec_from_file_location("ecorex_signed_candidate_drill", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("signed candidate drill is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


drill = _load_drill()


def _node_executable() -> Path:
    value = shutil.which("node")
    if value is None:
        raise drill.DrillError("the installed-signed CDP harness requires Node.js")
    try:
        executable = Path(value).resolve(strict=True)
    except OSError as exc:
        raise drill.DrillError("the installed-signed CDP Node.js path is invalid") from exc
    if executable.name.casefold() not in {"node", "node.exe"}:
        raise drill.DrillError("the installed-signed CDP Node.js identity is invalid")
    return executable


def _browser_environment() -> dict[str, str]:
    """Return a narrow environment without model, tenant or proxy credentials."""

    allowed = (
        "APPDATA",
        "ComSpec",
        "LOCALAPPDATA",
        "PATH",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SystemDrive",
        "SystemRoot",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    )
    result = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    result["NO_COLOR"] = "1"
    return result


def _validate_browser_report(
    value: Any,
    *,
    context: Any,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise drill.DrillError("installed-signed CDP output is not an object")
    runtime = value.get("runtime")
    browser = value.get("browser")
    diagnostics = value.get("diagnostics")
    if (
        value.get("schema_version") != 1
        or value.get("status") != "passed"
        or value.get("evidence_class") != "installed-signed-runtime-cdp"
        or value.get("transport") != "google-chrome-cdp"
        or value.get("acceptance_scope") != "unauthenticated-shell-smoke"
        or value.get("mock_server_spawned") is not False
        or value.get("ga_endpoint_contacted") is not False
        or value.get("full_office_scenario_acceptance_claimed") is not False
        or value.get("promotion_claimed") is not False
        or not isinstance(runtime, Mapping)
        or runtime.get("origin") != context.base_url
        or runtime.get("release_id") != context.release_id
        or runtime.get("version") != context.version
        or runtime.get("api_version") != "v1"
        or runtime.get("event_schema_version") != 1
        or runtime.get("storage_schema_version") != 1
        or not isinstance(browser, Mapping)
        or browser.get("isolated_profile") is not True
        or browser.get("external_network_blocked") is not True
        or not isinstance(browser.get("product"), str)
        or not re.match(r"^(?:Headless)?Chrome/", browser["product"])
        or not isinstance(diagnostics, Mapping)
        or any(diagnostics.get(key) != 0 for key in (
            "console_errors",
            "page_errors",
            "failed_requests",
            "external_requests",
        ))
        or not isinstance(value.get("screenshot_sha256"), str)
        or _SHA256.fullmatch(value["screenshot_sha256"]) is None
    ):
        raise drill.DrillError("installed-signed CDP output failed its contract")
    return value


def _run_browser_acceptance(context: Any, deadline: Any) -> Mapping[str, Any]:
    script = (
        _REPO_ROOT / "desktop" / "tools" / "run-installed-signed-runtime-cdp.mjs"
    ).resolve(strict=True)
    desktop = (_REPO_ROOT / "desktop").resolve(strict=True)
    try:
        script.relative_to(desktop)
    except ValueError as exc:
        raise drill.DrillError("the installed-signed CDP harness escaped desktop") from exc
    remaining = min(_MAX_BROWSER_SECONDS, deadline.remaining())
    if remaining < _MIN_BROWSER_SECONDS:
        raise drill.DrillError("the installed-signed CDP window is too short")
    node_timeout_ms = max(10_000, int((remaining - 5.0) * 1_000))
    command = (
        str(_node_executable()),
        str(script),
        f"--base-url={context.base_url}",
        f"--expected-release-id={context.release_id}",
        f"--expected-version={context.version}",
        f"--timeout-ms={node_timeout_ms}",
    )
    try:
        result = run_bounded_process(
            command,
            payload=None,
            cwd=desktop,
            environment=_browser_environment(),
            timeout_seconds=remaining,
            max_stdout_bytes=_MAX_STDOUT_BYTES,
            max_stderr_bytes=_MAX_STDERR_BYTES,
            hide_window=True,
        )
    except (OSError, BoundedProcessError) as exc:
        raise drill.DrillError(
            "installed-signed Runtime CDP process failed safely: "
            f"{type(exc).__name__}"
        ) from None
    if result.returncode != 0:
        raise drill.DrillError("installed-signed Runtime CDP process rejected the candidate")
    try:
        browser_report = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise drill.DrillError("installed-signed CDP output is invalid JSON") from exc
    verified = _validate_browser_report(browser_report, context=context)
    return {
        "schema_version": 1,
        "status": "passed",
        "evidence_class": "installed-signed-runtime-cdp",
        "candidate": {
            "source_commit": context.source_commit,
            "release_id": context.release_id,
            "version": context.version,
            "build_digest": context.build_digest,
            "artifact_id": context.artifact_id,
            "artifact_sha256": context.artifact_sha256,
            "slot_id": context.slot_id,
            "current_known_good_before_and_after": True,
            "signed_receipt_before_and_after": True,
            "sandbox_attestation_before_and_after": True,
            "rollback_terminal_before_and_after": True,
        },
        "browser": verified,
        "process_boundary": {
            "argv_only": True,
            "bounded_output": True,
            "bounded_timeout_seconds": round(remaining, 3),
            "windows_job_kills_descendants": True,
            "external_network_blocked": True,
            "isolated_profile_removed": True,
        },
        "mock_or_fixture_runtime_used": False,
        "promotion_claimed": False,
    }


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run real Google Chrome/CDP against the final installed signed Runtime "
            "inside the disposable Windows candidate ceremony."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="EcoreX repository root",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=drill.DEFAULT_TIMEOUT_SECONDS,
        help="total candidate ceremony deadline",
    )
    parser.add_argument("--report", type=Path, help="optional redacted JSON report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    started_at = time.monotonic()
    try:
        repo = args.repo_root.resolve(strict=True)
        if repo != _REPO_ROOT.resolve(strict=True):
            raise drill.DrillError("the CDP wrapper must run from its source repository")
        report = drill.run_drill(
            repo=repo,
            timeout_seconds=args.timeout_seconds,
            live_acceptance=_run_browser_acceptance,
        )
    except drill.DrillError as exc:
        print(f"Installed-signed Runtime CDP acceptance failed: {exc}", file=sys.stderr)
        return 1
    report = {
        **report,
        "installed_signed_runtime_cdp_wrapper": {
            "status": "passed",
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
            "ordinary_drill_cleanup_preserved": True,
        },
    }
    if args.report is not None:
        _write_report(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
