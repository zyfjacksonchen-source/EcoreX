#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_status(status_path: Path, pack_id: str, state: str, message: str, **extra) -> None:
    status = {
        "packId": pack_id,
        "state": state,
        "message": message,
        "updatedAt": utc_stamp(),
        **extra,
    }
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def find_runtime_python(runtime_dir: Path) -> Path:
    candidates = [
        runtime_dir / "python" / "python.exe",
        runtime_dir / "python" / "bin" / "python3",
        runtime_dir / "python" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def missing_modules(modules) -> list[str]:
    missing = []
    for module in modules or []:
        try:
            found = importlib.util.find_spec(str(module)) is not None
        except ModuleNotFoundError:
            found = False
        if not found:
            missing.append(str(module))
    return missing


def missing_modules_in_target(modules, target_dir: Path | None) -> list[str]:
    if not target_dir:
        return missing_modules(modules)
    missing = []
    for module in modules or []:
        module_name = str(module).split(".", 1)[0]
        try:
            found = importlib.machinery.PathFinder.find_spec(module_name, [str(target_dir)]) is not None
        except Exception:
            found = False
        if not found:
            missing.append(str(module))
    return missing


def prepend_path_env(env: dict[str, str], key: str, value: Path | None) -> None:
    if not value:
        return
    raw = str(value)
    current = env.get(key, "")
    env[key] = raw if not current else raw + os.pathsep + current


def run_logged(command: list[str], log_file, env: dict[str, str], timeout: int) -> None:
    log_file.write(f"Running: {' '.join(command)}\n")
    log_file.flush()
    try:
        result = subprocess.run(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        log_file.write(f"\nCommand timed out after {timeout}s: {' '.join(command)}\n")
        log_file.flush()
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(command)}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {' '.join(command)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install an EcoreX optional capability pack.")
    parser.add_argument("--pack-id", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--index-dir", default="")
    parser.add_argument("--target-dir", default="")
    parser.add_argument("--playwright-browsers-dir", default="")
    parser.add_argument("--index-url", default="")
    parser.add_argument("--find-links", default="")
    parser.add_argument("--no-index", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    pack_id = args.pack_id
    runtime_dir = Path(args.runtime_dir).resolve()
    manifest_path = Path(args.manifest).resolve()
    state_dir = Path(args.index_dir).resolve() if args.index_dir else runtime_dir / "capability-state"
    target_dir = Path(args.target_dir).resolve() if args.target_dir else None
    playwright_browsers_dir = Path(args.playwright_browsers_dir).resolve() if args.playwright_browsers_dir else None
    timeout = max(30, int(args.timeout or 600))
    state_dir.mkdir(parents=True, exist_ok=True)
    if target_dir:
        target_dir.mkdir(parents=True, exist_ok=True)
        if str(target_dir) not in sys.path:
            sys.path.insert(0, str(target_dir))
    status_extra = {"targetDir": str(target_dir)} if target_dir else {}

    status_path = state_dir / f"{pack_id}.json"
    log_path = state_dir / f"{pack_id}.log"
    lock_path = state_dir / f"{pack_id}.lock"
    python = find_runtime_python(runtime_dir)
    pack = None

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
        return 3

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        packs = manifest.get("packs", [])
        pack = next((item for item in packs if item.get("id") == pack_id), None)
        if not pack:
            raise RuntimeError(f"Unknown capability pack: {pack_id}")

        env = os.environ.copy()
        env["PYTHONNOUSERSITE"] = "1"
        env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        prepend_path_env(env, "PYTHONPATH", target_dir)
        if playwright_browsers_dir:
            playwright_browsers_dir.mkdir(parents=True, exist_ok=True)
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(playwright_browsers_dir)

        write_status(
            status_path,
            pack_id,
            "checking",
            f"Checking whether {pack.get('name', pack_id)} is available.",
            logPath=str(log_path),
            installed=False,
            **status_extra,
        )

        missing_before = missing_modules_in_target(pack.get("moduleChecks", []), target_dir)
        if not missing_before:
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
            log_file.write(f"Runtime: {runtime_dir}\n")
            log_file.write(f"Python: {python}\n")
            if target_dir:
                log_file.write(f"Target: {target_dir}\n")
            for requirement in pack.get("requirements", []):
                command = [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-cache-dir",
                    "--no-warn-script-location",
                ]
                if args.index_url:
                    command.extend(["--index-url", args.index_url])
                if args.find_links:
                    command.extend(["--find-links", args.find_links])
                if args.no_index:
                    command.append("--no-index")
                if target_dir:
                    command.extend(["--target", str(target_dir), "--upgrade"])
                command.append(str(requirement))
                run_logged(command, log_file, env, timeout)

            for command in pack.get("postInstallCommands", []) or []:
                if command == "python -m playwright install chromium":
                    run_logged([str(python), "-m", "playwright", "install", "chromium"], log_file, env, timeout)
                else:
                    raise RuntimeError(f"Unsupported post-install command: {command}")

        missing_after = missing_modules_in_target(pack.get("moduleChecks", []), target_dir)
        if missing_after:
            raise RuntimeError(f"Installed but module check failed: {', '.join(missing_after)}")

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
        return 0
    except Exception as exc:
        message = str(exc)
        if pack and pack.get("failureHint") and pack.get("failureHint") not in message:
            message = f"{message}. {pack.get('failureHint')}"
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
        return 1
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except TypeError:
            if lock_path.exists():
                lock_path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
