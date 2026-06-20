#!/usr/bin/env python3
"""Smoke checks for the v0.1.17 Xiaohongshu markdown + image reliability gate."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any


CHANGE_IDS = ["XHS-001", "PERF-002", "IMG-001", "ART-002"]


def read_text(path: pathlib.Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{path} has a UTF-8 BOM")
    return raw.decode("utf-8")


def add_check(checks: list[dict[str, Any]], name: str, ok: bool, evidence: str) -> None:
    checks.append({"name": name, "status": "pass" if ok else "fail", "evidence": evidence})


def run_invalid_cache_probe(root: pathlib.Path, tmp_dir: pathlib.Path) -> dict[str, Any]:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    output = tmp_dir / "invalid-cache.png"
    status = tmp_dir / "invalid-cache.status.json"
    output.write_bytes(b"not an image")
    if status.exists():
        status.unlink()

    command = [
        sys.executable,
        str(root / "skills" / "create-xiaohongshu-note" / "scripts" / "generate_cover_image.py"),
        "--prompt",
        "cache validation smoke",
        "--output",
        str(output),
        "--status-path",
        str(status),
        "--dry-run",
    ]
    completed = subprocess.run(command, cwd=str(root), text=True, capture_output=True, timeout=20)
    try:
        payload = json.loads(completed.stdout or "{}")
    except Exception:
        payload = {}
    return {
        "exitCode": completed.returncode,
        "stdout": payload,
        "stderrTail": completed.stderr[-500:],
        "invalidFileRemoved": not output.exists(),
        "statusExists": status.exists(),
    }


def run_failed_status_probe(root: pathlib.Path, tmp_dir: pathlib.Path) -> dict[str, Any]:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    status = tmp_dir / "bootstrap-failed.status.json"
    if status.exists():
        status.unlink()
    command = [
        sys.executable,
        str(root / "skills" / "create-xiaohongshu-note" / "scripts" / "generate_cover_image.py"),
        "--prompt-file",
        str(tmp_dir / "missing-prompt.txt"),
        "--status-path",
        str(status),
        "--output",
        str(tmp_dir / "missing-output.png"),
    ]
    completed = subprocess.run(command, cwd=str(root), text=True, capture_output=True, timeout=20)
    payload: dict[str, Any] = {}
    if status.exists():
        payload = json.loads(status.read_text(encoding="utf-8"))
    return {
        "exitCode": completed.returncode,
        "statusExists": status.exists(),
        "status": payload.get("status"),
        "ok": payload.get("ok"),
        "hasHeartbeat": isinstance(payload.get("heartbeat_at"), (int, float)),
    }


def run_generic_bad_image_probe(root: pathlib.Path, tmp_dir: pathlib.Path) -> dict[str, Any]:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    script = root / "skills" / "image-generation" / "scripts" / "generate.py"
    probe = tmp_dir / "generic-bad-image-probe.py"
    probe.write_text(
        "\n".join(
            [
                "import importlib.util, pathlib, sys",
                f"script = pathlib.Path({str(script)!r})",
                "spec = importlib.util.spec_from_file_location('image_generation_generate', script)",
                "module = importlib.util.module_from_spec(spec)",
                "spec.loader.exec_module(module)",
                "out = pathlib.Path(sys.argv[1])",
                "try:",
                "    module._save_image(b'not an image', str(out))",
                "except Exception as exc:",
                "    print(type(exc).__name__)",
                "    raise SystemExit(0)",
                "raise SystemExit(2)",
            ]
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(probe), str(tmp_dir / "out")],
        cwd=str(root),
        text=True,
        capture_output=True,
        timeout=20,
    )
    final_files = [path.name for path in (tmp_dir / "out").glob("*")] if (tmp_dir / "out").exists() else []
    return {
        "exitCode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "finalFiles": final_files,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--version", default="0.1.17")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    checks: list[dict[str, Any]] = []

    app = read_text(root / "desktop" / "src" / "App.tsx")
    message = read_text(root / "desktop" / "src" / "components" / "MessageContent.tsx")
    ecorex_api = read_text(root / "desktop" / "src" / "services" / "ecorexApi.ts")
    web = read_text(root / "channel" / "web" / "web_channel.py")
    xhs = read_text(root / "skills" / "create-xiaohongshu-note" / "scripts" / "generate_cover_image.py")
    image_gen = read_text(root / "skills" / "image-generation" / "scripts" / "generate.py")

    refresh_slice = app[app.find("async function refreshSessionFromHistory"):app.find("function historyRecoveryKey")]

    add_check(
        checks,
        "history reload preserves local terminal turn",
        "mergeHistoryAndLocalRequestMessage" in app
        and "isSameAssistantTurn" in app
        and "isTerminalAssistantMessage(localMessage)" in app
        and "historyIsClearlyStronger" in app,
        "terminal local content is protected by requestId/botSeq/userSeq during history reload",
    )
    add_check(
        checks,
        "history final requires payload not botSeq only",
        'typeof message.botSeq === "number"' not in refresh_slice,
        "refreshSessionFromHistory no longer treats botSeq-only rows as final payload",
    )
    add_check(
        checks,
        "completed request resume is guarded",
        "isTerminalAssistantMessage(existing)" in app
        and "isTerminalAssistantMessage(existingMessage)" in app
        and "locallyCompletedRequestIdsRef.current[requestId]" in app,
        "resumeRuntimeRequest and attachMessageStream reject stale active requests after local completion",
    )
    add_check(
        checks,
        "voice attach does not preempt final done",
        "function isTerminalVoiceAttach" in app
        and "terminalVoiceAttach" in app
        and 'item.type === "voice_attach"' in app,
        "voice_attach is terminal only with explicit terminal/done flags",
    )
    add_check(
        checks,
        "stream markdown has compact-output normalizer",
        "function normalizeMarkdownForRender" in message
        and "wrappedHeading" in message
        and r"return raw.replace(/^(\s{0,3}#{1,6})(\S)/" in message,
        "compact markdown headings are normalized before render/stream split",
    )
    add_check(
        checks,
        "long markdown chunk fallback is fence-aware",
        "function splitStableMarkdownChunksV2" in message
        and "acceptBoundary" in message
        and "hasBalancedFences(source.slice(start, target))" in message,
        "streaming chunks avoid cutting inside fenced code blocks",
    )
    add_check(
        checks,
        "streaming long markdown uses live window",
        "function streamingWindowMarkdown" in message
        and "STREAM_LIVE_FULL_RENDER_CHARS" in message
        and "chars streaming" in message,
        "100k+ pending markdown renders a bounded head/tail window before terminal collapse",
    )
    streaming_window_slice = message[message.find("function streamingWindowMarkdown"):message.find("function StreamingStableMarkdown")]
    add_check(
        checks,
        "streaming long markdown normalizes bounded window",
        "streamingWindowHeadEnd" in message
        and "trimUnbalancedFenceTail" in message
        and "normalizeMarkdownForRender(rawHead)" in streaming_window_slice
        and "normalizeMarkdownForRender(rawTail)" in streaming_window_slice
        and "normalizeMarkdownForRender(content)" not in streaming_window_slice,
        "Long pending streams slice head/tail before markdown normalization to keep CPU bounded",
    )
    add_check(
        checks,
        "long reply preview is markdown-boundary aware",
        "function markdownPreviewContentSafe" in message
        and "markdownPreviewContentSafe(content)" in message
        and "fenceMatches.length % 2 === 1" in message,
        "collapsed final previews normalize a bounded prefix and avoid unbalanced code fences",
    )
    add_check(
        checks,
        "pending artifact stat retry closes",
        "statRetryCounts" in message
        and 'artifact.status === "pending"' in message
        and "ARTIFACT_PENDING_MAX_RETRIES" in message
        and "pendingRetryExhausted" in message,
        "pending image artifacts retry for long-running jobs and eventually exit permanent pending state",
    )
    add_check(
        checks,
        "backend keeps pending artifacts",
        'artifact_status = "pending"' in web
        and "statusPath" in web
        and "if file_path and not self._artifact_path_available(file_path):" not in web,
        "WebChannel emits pending artifact records instead of dropping missing async outputs",
    )
    add_check(
        checks,
        "backend extracts async output artifacts",
        '"output"' in web
        and '"output_path"' in web
        and "status_path = result.get(\"status_path\") or result.get(\"statusPath\")" in web,
        "WebChannel recognizes XHS async output/status_path payloads as pending artifacts",
    )
    add_check(
        checks,
        "frontend preserves artifact statusPath",
        "statusPath" in app
        and "status_path" in app
        and "statusPath?: string" in ecorex_api,
        "Renderer keeps statusPath metadata for pending artifact lifecycle inspection",
    )
    add_check(
        checks,
        "frontend consumes pending artifact statusPath",
        "function artifactStatusJsonState" in message
        and "localFileJson(artifact.statusPath)" in message
        and "statusRetryCounts" in message
        and "readArtifactStatusJson" in app
        and "localFileJson={messageLocalJson}" in app
        and "export async function readLocalJson" in ecorex_api
        and '"/api/file-json"' in ecorex_api
        and "class FileJsonHandler" in web
        and "'/api/file-json', 'FileJsonHandler'" in web,
        "Pending async artifacts consume status JSON through an authenticated endpoint and converge ready/error without session switching",
    )
    status_state_slice = message[message.find("function artifactStatusJsonState"):message.find("function artifactKindFromFileType")]
    add_check(
        checks,
        "pending status json does not early-ready on ok true",
        "ARTIFACT_STATUS_READY.has(status)" in status_state_slice
        and "if (status) return \"pending\"" in status_state_slice
        and "record.ok === true" in status_state_slice,
        "running/retrying status JSON remains pending even if the worker writes ok=true",
    )
    add_check(
        checks,
        "artifact merge upserts duplicate status",
        "function mergeAgentArtifactRecord" in app
        and "artifactStatusPriority" in app
        and "mergeAgentArtifactRecord(nextArtifacts[index], artifact)" in app
        and "incomingPriority >= existingPriority" in app,
        "Duplicate artifact merges update status metadata without downgrading ready/failed to pending",
    )
    file_to_send_slice = web[web.find('elif event_type == "file_to_send"'):web.find("# ------------------------------------------------------------------", web.find('elif event_type == "file_to_send"'))]
    add_check(
        checks,
        "file_to_send skips empty artifact",
        "if not artifact:" in file_to_send_slice and "return" in file_to_send_slice,
        "file_to_send no longer pushes an empty artifact payload",
    )
    add_check(
        checks,
        "structured tool output recognizes output path",
        "output|outputPath|output_path" in message
        and "record.output || record.output_path || record.outputPath" in message,
        "Legacy structured tool results with output/output_path still render artifact entries",
    )
    add_check(
        checks,
        "xhs image worker validates image outputs and writes atomically",
        "def image_metadata" in xhs
        and "def write_validated_image" in xhs
        and "def validate_existing_image" in xhs
        and "os.replace(tmp, output)" in xhs,
        "XHS cover generator validates image bytes and writes atomically",
    )
    add_check(
        checks,
        "xhs image retry metadata",
        "def is_retryable_error" in xhs
        and 'parser.add_argument("--retries"' in xhs
        and '"job_id": h' in xhs
        and '"retry_index"' in xhs,
        "XHS cover generator records job metadata and bounded retry attempts",
    )
    add_check(
        checks,
        "xhs image retries are clamped",
        "MAX_IMAGE_GENERATION_RETRIES" in xhs
        and "args.retries = max(0, min(args.retries, MAX_IMAGE_GENERATION_RETRIES))" in xhs,
        "XHS image generation retries have a hard upper bound to prevent queue starvation",
    )
    add_check(
        checks,
        "xhs status writes are atomic and bootstrap failures close",
        "def write_status" in xhs
        and "os.replace(tmp, path)" in xhs
        and "target_status" in xhs
        and '"status": "failed"' in xhs,
        "XHS status JSON is atomic and top-level failures write failed status",
    )
    add_check(
        checks,
        "generic image generation rejects empty or invalid success",
        "if not paths:" in image_gen
        and "produced no image paths" in image_gen
        and "def _validate_image_bytes" in image_gen
        and "os.replace(tmp, path)" in image_gen,
        "Generic image-generation script cannot return success with empty paths or invalid image bytes",
    )

    probe_root = root / "docs" / f"v{args.version}" / "tmp"
    invalid_cache_probe = run_invalid_cache_probe(root, probe_root / "xhs-image-cache-smoke")
    add_check(
        checks,
        "invalid cached image is rejected",
        invalid_cache_probe["exitCode"] == 0
        and invalid_cache_probe.get("invalidFileRemoved") is True
        and (invalid_cache_probe.get("stdout") or {}).get("status") == "dry_run",
        json.dumps(invalid_cache_probe, ensure_ascii=False, sort_keys=True),
    )

    failed_status_probe = run_failed_status_probe(root, probe_root / "xhs-failed-status-smoke")
    add_check(
        checks,
        "xhs bootstrap failure writes failed status",
        failed_status_probe["exitCode"] != 0
        and failed_status_probe.get("statusExists") is True
        and failed_status_probe.get("status") == "failed"
        and failed_status_probe.get("ok") is False
        and failed_status_probe.get("hasHeartbeat") is True,
        json.dumps(failed_status_probe, ensure_ascii=False, sort_keys=True),
    )

    bad_image_probe = run_generic_bad_image_probe(root, probe_root / "generic-bad-image-smoke")
    add_check(
        checks,
        "generic invalid image bytes are rejected",
        bad_image_probe["exitCode"] == 0
        and "ValueError" in bad_image_probe.get("stdout", "")
        and bad_image_probe.get("finalFiles") == [],
        json.dumps(bad_image_probe, ensure_ascii=False, sort_keys=True),
    )

    failures = [item for item in checks if item["status"] != "pass"]
    report = {
        "product": "EcoreX",
        "version": args.version,
        "changeIds": CHANGE_IDS,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": "pass" if not failures else "fail",
        "checks": checks,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = pathlib.Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
