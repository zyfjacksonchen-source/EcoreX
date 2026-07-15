#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SECRET_QUERY_KEYS = {
    "access_token",
    "apikey",
    "api_key",
    "auth",
    "key",
    "password",
    "passwd",
    "secret",
    "sig",
    "signature",
    "token",
}


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def redact_url(value: str) -> str:
    try:
        parsed = urlsplit(str(value))
    except Exception:
        return str(value)
    if not parsed.scheme or not parsed.netloc:
        return str(value)
    host = parsed.hostname or ""
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username or parsed.password:
        netloc = f"***@{netloc}"
    query = urlencode(
        [
            (key, "***" if key.lower() in SECRET_QUERY_KEYS else val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def redact_text(value: str) -> str:
    text = str(value)
    return re.sub(r"https?://[^\s\]\)\"'<>]+", lambda match: redact_url(match.group(0)), text)


def redact_command(command: list[str]) -> str:
    redacted: list[str] = []
    for item in command:
        redacted.append(redact_text(str(item)))
    return " ".join(redacted)


def redact_payload(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_payload(item) for key, item in value.items()}
    return value


def write_status(status_path: Path, pack_id: str, state: str, message: str, **extra) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "packId": pack_id,
                "state": state,
                "message": redact_text(message),
                "updatedAt": utc_stamp(),
                **redact_payload(extra),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def emit_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def safe_pack_dir_name(pack_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(pack_id or "").strip())
    return safe.strip(".-") or "pack"


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def state_root(runtime_dir: Path) -> Path:
    raw = os.environ.get("ECOREX_STATE_DIR", "")
    return Path(raw).resolve() if raw else runtime_dir.resolve()


def confined_path(path: Path, roots: list[Path], *, label: str) -> Path:
    resolved = path.resolve()
    for root in roots:
        if is_relative_to(resolved, root):
            return resolved
    allowed = ", ".join(str(root.resolve()) for root in roots)
    raise ValueError(f"{label} must be inside an EcoreX owned state/runtime directory: {allowed}")


def manifest_candidates(runtime_dir: Path) -> list[Path]:
    script_root = Path(__file__).resolve().parents[1]
    return [
        runtime_dir / "capabilities.json",
        runtime_dir / "runtime-packs" / "capabilities.json",
        script_root / "runtime-packs" / "capabilities.json",
        script_root / "desktop" / "runtime-packs" / "capabilities.json",
    ]


def resolve_manifest(runtime_dir: Path, value: str) -> Path:
    if value:
        return Path(value).resolve()
    for candidate in manifest_candidates(runtime_dir):
        if candidate.exists():
            return candidate.resolve()
    return (runtime_dir / "capabilities.json").resolve()


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return data if isinstance(data, dict) else {}


def find_pack(manifest: dict, pack_id: str) -> dict | None:
    packs = manifest.get("packs", []) if isinstance(manifest, dict) else []
    return next(
        (item for item in packs if isinstance(item, dict) and item.get("id") == pack_id),
        None,
    )


def resolve_state_dir(runtime_dir: Path, value: str) -> Path:
    root = state_root(runtime_dir)
    raw = value or os.environ.get("ECOREX_CAPABILITY_STATE_DIR", "")
    candidate = Path(raw).resolve() if raw else (root / "capability-state").resolve()
    if candidate.name != "capability-state":
        raise ValueError("capability state dir must end with capability-state")
    return confined_path(candidate, [root, runtime_dir], label="capability state dir")


def resolve_target_dir(runtime_dir: Path, pack_id: str, value: str) -> Path:
    root = state_root(runtime_dir)
    if value:
        candidate = Path(value).resolve()
    else:
        raw_root = os.environ.get("ECOREX_CAPABILITY_TARGET_DIR", "")
        base = Path(raw_root).resolve() if raw_root else (root / "capability-packages").resolve()
        candidate = base / safe_pack_dir_name(pack_id)
    if candidate.name != safe_pack_dir_name(pack_id):
        raise ValueError("capability target dir must end with the sanitized pack id")
    return confined_path(candidate, [root, runtime_dir], label="capability target dir")


def resolve_browsers_dir(runtime_dir: Path, value: str) -> Path | None:
    if not value:
        return None
    return confined_path(Path(value), [state_root(runtime_dir), runtime_dir], label="playwright browsers dir")


def public_source_projection(pack: dict) -> dict:
    return {
        "sourceConfigured": bool(pack.get("sourceUrl")),
        "mirrorConfigured": bool(pack.get("mirrorUrls")),
    }


def find_runtime_python(runtime_dir: Path) -> Path | None:
    for candidate in (
        runtime_dir / "python" / "python.exe",
        runtime_dir / "python" / "bin" / "python3",
        runtime_dir / "python" / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    return None


def runtime_module_dirs(runtime_dir: Path, target_dir: Path | None) -> list[Path]:
    roots: list[Path] = []
    if target_dir:
        roots.append(target_dir)
    roots.extend([
        runtime_dir / "python" / "Lib" / "site-packages",
        runtime_dir / "python" / "lib" / "site-packages",
    ])
    roots.extend((runtime_dir / "python" / "lib").glob("python*/site-packages"))
    roots.extend((runtime_dir / "python").glob("lib/python*/site-packages"))
    return [root.resolve() for root in roots if root.exists()]


def module_available(module_name: str, search_dirs: list[Path]) -> bool:
    probe = str(module_name).split(".", 1)[0]
    try:
        return any(importlib.machinery.PathFinder.find_spec(probe, [str(path)]) is not None for path in search_dirs)
    except Exception:
        return False


def missing_modules(modules, runtime_dir: Path, target_dir: Path | None) -> list[str]:
    missing = []
    search_dirs = runtime_module_dirs(runtime_dir, target_dir)
    for module in modules or []:
        name = str(module)
        if module_available(name, search_dirs):
            continue
        missing.append(name)
    return missing


def prepend_path_env(env: dict[str, str], key: str, value: Path | None) -> None:
    if not value:
        return
    raw = str(value)
    current = env.get(key, "")
    env[key] = raw if not current else raw + os.pathsep + current


def run_logged(command: list[str], log_file, env: dict[str, str], timeout: int) -> None:
    log_file.write(f"Running: {redact_command(command)}\n")
    log_file.flush()
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        timeout=timeout,
    )
    if result.stdout:
        log_file.write(redact_text(result.stdout))
    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {redact_command(command)}")


def build_status(pack_id: str, pack: dict, runtime_dir: Path, target_dir: Path) -> dict:
    missing = missing_modules(pack.get("moduleChecks", []), runtime_dir, target_dir)
    base = {
        "packId": pack_id,
        "name": pack.get("name", pack_id),
        "installed": False,
        "targetDir": str(target_dir),
        "missingModules": missing,
        "discoveryOnly": pack.get("discoveryOnly") is True,
        "configureOnly": pack.get("configureOnly") is True,
        "readOnly": pack.get("readOnly") is True,
        "defaultEnabled": pack.get("defaultEnabled") is True,
        "updatedAt": utc_stamp(),
    }
    if pack.get("discoveryOnly") is True:
        return {
            **base,
            **public_source_projection(pack),
            "state": "discovery_only",
            "message": pack.get("installHint") or f"{pack.get('name', pack_id)} must be resolved in the agent session.",
            "retryable": False,
            "nextAction": "discover",
        }
    if pack.get("configureOnly") is True:
        return {
            **base,
            "state": "needs_configuration",
            "message": pack.get("installHint") or f"{pack.get('name', pack_id)} requires configuration.",
            "retryable": True,
            "nextAction": "configure",
            "configureAction": f"configure-capability --pack-id {pack_id}",
        }
    if not missing:
        return {
            **base,
            "state": "installed",
            "installed": True,
            "message": f"{pack.get('name', pack_id)} is available.",
            "retryable": False,
            "nextAction": "none",
        }
    return {
        **base,
        "state": "missing_dependency",
        "message": f"{pack.get('name', pack_id)} is missing dependencies: {', '.join(missing)}",
        "retryable": True,
        "nextAction": "repair",
        "repairAction": f"install-capability --action repair --pack-id {pack_id}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Install an EcoreX optional capability pack.")
    parser.add_argument("--action", choices=("status", "install", "repair", "doctor"), default="install")
    parser.add_argument("--pack-id", default="")
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--manifest", default="")
    parser.add_argument("--index-dir", default="")
    parser.add_argument("--target-dir", default="")
    parser.add_argument("--playwright-browsers-dir", default="")
    parser.add_argument("--index-url", default="")
    parser.add_argument("--primary-index-url", default=os.environ.get("ECOREX_PIP_PRIMARY_INDEX_URL", ""))
    parser.add_argument("--fallback-index-url", default=os.environ.get("ECOREX_PIP_FALLBACK_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple"))
    parser.add_argument("--find-links", default="")
    parser.add_argument("--no-index", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    action = str(args.action or "install").strip().lower()
    pack_id = str(args.pack_id or "").strip()
    runtime_dir = Path(args.runtime_dir).resolve()
    manifest_path = resolve_manifest(runtime_dir, args.manifest)
    try:
        state_dir = resolve_state_dir(runtime_dir, args.index_dir)
        if action != "doctor" and not pack_id:
            emit_json({"status": "error", "action": action, "message": "--pack-id is required"})
            return 2
        safe_pack_id = safe_pack_dir_name(pack_id or "doctor")
        target_dir = resolve_target_dir(runtime_dir, pack_id or "doctor", args.target_dir)
        browsers_dir = resolve_browsers_dir(runtime_dir, args.playwright_browsers_dir)
    except Exception as exc:
        emit_json({"status": "error", "action": action, "message": redact_text(str(exc))})
        return 2
    timeout = max(30, int(args.timeout or 600))

    state_dir.mkdir(parents=True, exist_ok=True)
    if action in {"install", "repair"}:
        target_dir.mkdir(parents=True, exist_ok=True)
    if str(target_dir) not in sys.path:
        sys.path.insert(0, str(target_dir))

    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        emit_json({"status": "error", "action": action, "message": f"Failed reading manifest: {exc}"})
        return 2

    if action == "doctor":
        packs = manifest.get("packs", []) if isinstance(manifest, dict) else []
        rows = []
        for item in packs:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            item_id = str(item["id"])
            rows.append(build_status(item_id, item, runtime_dir, resolve_target_dir(runtime_dir, item_id, "")))
        blocking = [row for row in rows if row.get("state") == "missing_dependency"]
        payload = {
            "status": "success",
            "action": "doctor",
            "manifestPath": str(manifest_path),
            "stateDir": str(state_dir),
            "targetRoot": str(resolve_target_dir(runtime_dir, "doctor", "").parent),
            "updatedAt": utc_stamp(),
            "summary": {
                "packs": len(rows),
                "installed": sum(1 for row in rows if row.get("installed")),
                "blocking": len(blocking),
            },
            "packs": rows,
        }
        (state_dir / "capability-doctor.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        emit_json(payload)
        return 0

    status_path = state_dir / f"{safe_pack_id}.json"
    log_path = state_dir / f"{safe_pack_id}.log"
    lock_path = state_dir / f"{safe_pack_id}.lock"
    pack = find_pack(manifest, pack_id)
    status_extra = {"targetDir": str(target_dir)}
    if not pack:
        payload = {"status": "error", "action": action, "packId": pack_id, "message": f"Unknown capability pack: {pack_id}"}
        emit_json(payload)
        return 2

    if action == "status":
        state = build_status(pack_id, pack, runtime_dir, target_dir)
        state_status = "success" if state.get("installed") else "missing"
        write_status(
            status_path,
            pack_id,
            str(state["state"]),
            str(state["message"]),
            logPath=str(log_path),
            **{key: value for key, value in state.items() if key not in {"packId", "state", "message"}},
        )
        emit_json({"status": state_status, "action": "status", "capabilityState": state})
        return 0

    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
    except FileExistsError:
        write_status(
            status_path,
            pack_id,
            "busy",
            "Capability pack is already installing; please retry later.",
            logPath=str(log_path),
            installed=False,
            **status_extra,
        )
        emit_json({"status": "busy", "action": action, "packId": pack_id, "logPath": str(log_path)})
        return 3

    try:
        if pack.get("discoveryOnly") is True:
            state = build_status(pack_id, pack, runtime_dir, target_dir)
            write_status(
                status_path,
                pack_id,
                "discovery_only",
                str(state["message"]),
                logPath=str(log_path),
                installed=False,
                discoveryOnly=True,
                **public_source_projection(pack),
                **status_extra,
            )
            emit_json({"status": "error", "action": action, "packId": pack_id, "capabilityState": state})
            return 4

        if pack.get("configureOnly") is True:
            state = build_status(pack_id, pack, runtime_dir, target_dir)
            write_status(
                status_path,
                pack_id,
                "needs_configuration",
                str(state["message"]),
                logPath=str(log_path),
                installed=False,
                configureOnly=True,
                **status_extra,
            )
            emit_json({"status": "error", "action": action, "packId": pack_id, "capabilityState": state})
            return 4

        python = find_runtime_python(runtime_dir)
        if not python:
            message = "Runtime Python is missing from the owned EcoreX runtime."
            write_status(
                status_path,
                pack_id,
                "missing_runtime_python",
                message,
                logPath=str(log_path),
                installed=False,
                **status_extra,
            )
            emit_json({"status": "error", "action": action, "packId": pack_id, "message": message, "logPath": str(log_path)})
            return 2

        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {
                "APPDATA",
                "HOME",
                "HTTPS_PROXY",
                "HTTP_PROXY",
                "LANG",
                "LOCALAPPDATA",
                "NO_PROXY",
                "PATH",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "WINDIR",
            }
            or key.upper().endswith("_PROXY")
        }
        env["PYTHONNOUSERSITE"] = "1"
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        env["PIP_CONFIG_FILE"] = os.devnull
        prefer_binary = os.environ.get("ECOREX_PIP_PREFER_BINARY", "1").strip() != "0"
        only_binary = os.environ.get("ECOREX_PIP_ONLY_BINARY", "").strip()
        env["PYTHONPATH"] = str(target_dir)
        if browsers_dir:
            browsers_dir.mkdir(parents=True, exist_ok=True)
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)

        write_status(
            status_path,
            pack_id,
            "checking",
            f"Checking whether {pack.get('name', pack_id)} is available.",
            logPath=str(log_path),
            installed=False,
            **status_extra,
        )
        missing_before = missing_modules(pack.get("moduleChecks", []), runtime_dir, target_dir)
        if not missing_before:
            state = build_status(pack_id, pack, runtime_dir, target_dir)
            write_status(
                status_path,
                pack_id,
                "installed",
                f"{pack.get('name', pack_id)} is already available.",
                logPath=str(log_path),
                installed=True,
                missingModules=[],
                **status_extra,
            )
            emit_json({"status": "success", "action": action, "packId": pack_id, "capabilityState": state})
            return 0

        write_status(
            status_path,
            pack_id,
            "installing",
            f"Installing {pack.get('name', pack_id)}; first install may take a few minutes.",
            logPath=str(log_path),
            installed=False,
            missingModules=missing_before,
            estimatedSizeMb=pack.get("estimatedSizeMb"),
            **status_extra,
        )

        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(f"[{utc_stamp()}] Installing {pack_id}\n")
            log_file.write(f"Runtime: {runtime_dir}\nPython: {python}\n")
            if target_dir:
                log_file.write(f"Target: {target_dir}\n")
            for requirement in pack.get("requirements", []) or []:
                base_command = [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-cache-dir",
                    "--no-warn-script-location",
                ]
                if prefer_binary:
                    base_command.append("--prefer-binary")
                if only_binary:
                    base_command.extend(["--only-binary", only_binary])
                primary_index_url = args.primary_index_url or args.index_url
                command = list(base_command)
                if primary_index_url:
                    command.extend(["--index-url", primary_index_url])
                if args.find_links:
                    command.extend(["--find-links", args.find_links])
                if args.no_index:
                    command.append("--no-index")
                if target_dir:
                    command.extend(["--target", str(target_dir), "--upgrade"])
                    command.append(str(requirement))
                try:
                    if primary_index_url:
                        log_file.write(f"[{utc_stamp()}] Primary install index: {redact_url(primary_index_url)}\n")
                    run_logged(command, log_file, env, timeout)
                except Exception:
                    if args.no_index or not args.fallback_index_url:
                        raise
                    fallback = list(base_command)
                    fallback.extend(["--index-url", args.fallback_index_url])
                    if args.find_links:
                        fallback.extend(["--find-links", args.find_links])
                    if target_dir:
                        fallback.extend(["--target", str(target_dir), "--upgrade"])
                    fallback.append(str(requirement))
                    log_file.write(f"[{utc_stamp()}] Primary install failed; retrying fallback index {redact_url(args.fallback_index_url)}\n")
                    run_logged(fallback, log_file, env, timeout)

            for command in pack.get("postInstallCommands", []) or []:
                if command == "python -m playwright install chromium":
                    run_logged([str(python), "-m", "playwright", "install", "chromium"], log_file, env, timeout)
                else:
                    raise RuntimeError(f"Unsupported post-install command: {command}")

        missing_after = missing_modules(pack.get("moduleChecks", []), runtime_dir, target_dir)
        if missing_after:
            raise RuntimeError(f"Installed but module check failed: {', '.join(missing_after)}")

        state = build_status(pack_id, pack, runtime_dir, target_dir)
        write_status(
            status_path,
            pack_id,
            "installed",
            f"{pack.get('name', pack_id)} installed.",
            logPath=str(log_path),
            installed=True,
            missingModules=[],
            **status_extra,
        )
        emit_json({"status": "success", "action": action, "packId": pack_id, "capabilityState": state})
        return 0
    except subprocess.TimeoutExpired as exc:
        message = f"Install timed out after {timeout}s: {exc}"
    except Exception as exc:
        message = redact_text(str(exc))
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except TypeError:
            if lock_path.exists():
                lock_path.unlink()

    if pack and pack.get("failureHint") and str(pack.get("failureHint")) not in message:
        message = f"{message}. {pack.get('failureHint')}"
    message = redact_text(message)
    write_status(
        status_path,
        pack_id,
        "failed",
        message,
        logPath=str(log_path),
        installed=False,
        **status_extra,
    )
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n[{utc_stamp()}] ERROR: {message}\n")
    emit_json({"status": "error", "action": action, "packId": pack_id, "message": message, "logPath": str(log_path)})
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
