from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys
import time

import pytest

from ecorex.release.process_boundary import (
    BoundedProcessOutputOverflow,
    BoundedProcessTimedOut,
    run_bounded_process,
)


def _run_python(
    tmp_path: Path,
    source: str,
    *,
    payload: bytes | None = None,
    timeout: float = 5,
    stdout_limit: int = 4096,
    stderr_limit: int = 4096,
):
    return run_bounded_process(
        (sys.executable, "-I", "-B", "-c", source),
        payload=payload,
        cwd=tmp_path,
        environment={
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "COMSPEC",
                "SYSTEMDRIVE",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "WINDIR",
            }
        },
        timeout_seconds=timeout,
        max_stdout_bytes=stdout_limit,
        max_stderr_bytes=stderr_limit,
    )


def test_bounded_process_exchanges_payload_and_reaps(tmp_path: Path) -> None:
    result = _run_python(
        tmp_path,
        "import sys; data=sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(data[::-1]); sys.stderr.write('bounded')",
        payload=b"ecorex",
    )

    assert result.returncode == 0
    assert result.stdout == b"xeroce"
    assert result.stderr == b"bounded"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_bounded_process_kills_output_flood_without_retaining_it(
    tmp_path: Path,
    stream: str,
) -> None:
    source = (
        "import sys,time; stream=getattr(sys,"
        f"'{stream}').buffer; stream.write(b'x'*1048576); "
        "stream.flush(); time.sleep(30)"
    )
    started = time.monotonic()
    with pytest.raises(BoundedProcessOutputOverflow):
        _run_python(
            tmp_path,
            source,
            timeout=10,
            stdout_limit=64,
            stderr_limit=64,
        )

    assert time.monotonic() - started < 8


def test_bounded_process_timeout_terminates_and_reaps(tmp_path: Path) -> None:
    started = time.monotonic()
    with pytest.raises(BoundedProcessTimedOut):
        _run_python(tmp_path, "import time; time.sleep(30)", timeout=0.2)

    assert time.monotonic() - started < 8


def test_bounded_process_rejects_invalid_limits_without_launching(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="limits"):
        run_bounded_process(
            (sys.executable, "-c", "raise SystemExit(0)"),
            payload=None,
            cwd=tmp_path,
            environment={},
            timeout_seconds=0,
            max_stdout_bytes=1,
            max_stderr_bytes=1,
        )


def test_platform_stager_wrapper_fails_closed_on_adapter_output_flood(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    executable = Path(sys.executable).resolve(strict=True)
    adapter = tmp_path / "flood_stager.py"
    adapter.write_text(
        "import sys,time\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(b'x'*1048576)\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
        newline="\n",
    )
    output = tmp_path / "stage-output"
    environment = dict(os.environ)
    environment.update(
        {
            "ECOREX_PLATFORM_STAGER_EXECUTABLE": str(executable),
            "ECOREX_PLATFORM_STAGER_EXECUTABLE_SHA256": hashlib.sha256(
                executable.read_bytes()
            ).hexdigest(),
            "ECOREX_PLATFORM_STAGER_ADAPTER": str(adapter),
            "ECOREX_PLATFORM_STAGER_ADAPTER_SHA256": hashlib.sha256(
                adapter.read_bytes()
            ).hexdigest(),
            "ECOREX_PUBLIC_BOOTSTRAP_INDEX_URL": (
                "https://releases.example.test/ecorex/public-bootstrap-index.json"
            ),
            "ECOREX_PUBLICATION_PUBLIC_KEYS_JSON": json.dumps(
                {"publication-v1": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}
            ),
        }
    )
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "invoke-v1-platform-stager.py"),
            "--repo-root",
            str(repository),
            "--output-root",
            str(output),
            "--platform",
            "windows",
            "--architecture",
            "x64",
            "--commit-sha",
            "a" * 40,
            "--workflow-run-id",
            "1",
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert result.returncode == 1
    assert time.monotonic() - started < 15
    assert len(result.stdout) <= 4096
    assert len(result.stderr) <= 4096
    failure = json.loads((output / "stage-failure.json").read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["code"] == "platform_stager_boundedprocessoutputoverflow"


def test_platform_stager_wrapper_retains_only_adapter_public_failure_code(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    executable = Path(sys.executable).resolve(strict=True)
    adapter = tmp_path / "rejecting_stager.py"
    adapter.write_text(
        "import json,sys\n"
        "sys.stdin.buffer.read()\n"
        "print(json.dumps({'code':'browser_pack_smoke_failed',"
        "'detail':'must-not-surface'}),file=sys.stderr)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
        newline="\n",
    )
    output = tmp_path / "stage-output"
    environment = dict(os.environ)
    environment.update(
        {
            "ECOREX_PLATFORM_STAGER_EXECUTABLE": str(executable),
            "ECOREX_PLATFORM_STAGER_EXECUTABLE_SHA256": hashlib.sha256(
                executable.read_bytes()
            ).hexdigest(),
            "ECOREX_PLATFORM_STAGER_ADAPTER": str(adapter),
            "ECOREX_PLATFORM_STAGER_ADAPTER_SHA256": hashlib.sha256(
                adapter.read_bytes()
            ).hexdigest(),
            "ECOREX_PUBLIC_BOOTSTRAP_INDEX_URL": (
                "https://releases.example.test/ecorex/public-bootstrap-index.json"
            ),
            "ECOREX_PUBLICATION_PUBLIC_KEYS_JSON": json.dumps(
                {"publication-v1": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}
            ),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "invoke-v1-platform-stager.py"),
            "--repo-root",
            str(repository),
            "--output-root",
            str(output),
            "--platform",
            "windows",
            "--architecture",
            "x64",
            "--commit-sha",
            "a" * 40,
            "--workflow-run-id",
            "1",
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert result.returncode == 1
    failure = json.loads((output / "stage-failure.json").read_text(encoding="utf-8"))
    assert failure["code"] == "browser_pack_smoke_failed"
    assert "must-not-surface" not in (output / "stage-failure.json").read_text(
        encoding="utf-8"
    )


def test_platform_stager_accepts_only_hashed_secret_scan_diagnostic() -> None:
    repository = Path(__file__).resolve().parents[2]
    module = runpy.run_path(
        str(repository / "scripts/invoke-v1-platform-stager.py")
    )
    location = "a" * 64
    content = "b" * 64
    safe = json.dumps(
        {
            "code": "stage_supply_chain_secret_match",
            "diagnostic": {
                "content_sha256": content,
                "detector_id": "github_token",
                "kind": "archive_member",
                "location_sha256": location,
            },
            "status": "failed",
        }
    ).encode()

    assert module["_adapter_failure_diagnostic"](safe) == {
        "content_sha256": content,
        "detector_id": "github_token",
        "kind": "archive_member",
        "location_sha256": location,
    }
    unsafe = safe.replace(location.encode(), b"/private/runner/path")
    assert module["_adapter_failure_diagnostic"](unsafe) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object tree contract")
def test_windows_job_kills_descendant_after_root_exits_with_inherited_pipe(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "escaped-child.txt"
    child_source = (
        "import pathlib,time; time.sleep(2); "
        f"pathlib.Path({str(marker)!r}).write_text('escaped',encoding='utf-8')"
    )
    parent_source = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-I','-B','-c',"
        f"{child_source!r}],stdout=sys.stdout,stderr=sys.stderr,close_fds=False)"
    )

    with pytest.raises(BoundedProcessTimedOut):
        _run_python(tmp_path, parent_source, timeout=0.4)

    time.sleep(2.5)
    assert not marker.exists(), "a descendant escaped the kill-on-close Job Object"


def test_repository_platform_stage_wrapper_uses_the_same_bounded_boundary() -> None:
    repository = Path(__file__).resolve().parents[2]
    source = (repository / "scripts" / "run-v1-repo-platform-stage.py").read_text(
        encoding="utf-8"
    )

    assert "run_bounded_process(" in source
    assert "_WRAPPER_TIMEOUT_SECONDS = 50 * 60" in source
    assert "timeout_seconds=_WRAPPER_TIMEOUT_SECONDS" in source
    assert "subprocess.run(" not in source


def test_platform_stage_nested_timeouts_leave_ci_cleanup_budget() -> None:
    repository = Path(__file__).resolve().parents[2]
    invoke_source = (
        repository / "scripts" / "invoke-v1-platform-stager.py"
    ).read_text(encoding="utf-8")
    wrapper_source = (
        repository / "scripts" / "run-v1-repo-platform-stage.py"
    ).read_text(encoding="utf-8")
    workflow = (
        repository / ".github" / "workflows" / "ecorex-v1-platform-stage.yml"
    ).read_text(encoding="utf-8")

    assert "_STAGER_TIMEOUT_SECONDS = 45 * 60" in invoke_source
    assert "timeout_seconds=_STAGER_TIMEOUT_SECONDS" in invoke_source
    assert "_WRAPPER_TIMEOUT_SECONDS = 50 * 60" in wrapper_source
    assert "timeout-minutes: 60" in workflow


@pytest.mark.skipif(os.name != "nt", reason="Windows nested Job Object contract")
def test_windows_nested_job_boundary_cascades_when_outer_runner_is_killed(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    marker = tmp_path / "nested-child-escaped.txt"
    grandchild = (
        "import pathlib,time; time.sleep(2); "
        f"pathlib.Path({str(marker)!r}).write_text('escaped',encoding='utf-8')"
    )
    outer = (
        "import os,sys; "
        f"sys.path.insert(0,{str(repository)!r}); "
        "from ecorex.release.process_boundary import run_bounded_process; "
        "run_bounded_process((sys.executable,'-I','-B','-c',"
        f"{grandchild!r}),payload=None,cwd={str(tmp_path)!r},"
        "environment=dict(os.environ),timeout_seconds=10,"
        "max_stdout_bytes=64,max_stderr_bytes=64)"
    )

    with pytest.raises(BoundedProcessTimedOut):
        run_bounded_process(
            (sys.executable, "-I", "-B", "-c", outer),
            payload=None,
            cwd=tmp_path,
            environment=dict(os.environ),
            timeout_seconds=0.5,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
        )

    time.sleep(2.5)
    assert not marker.exists(), "a nested Job descendant escaped outer cancellation"
