#!/usr/bin/env python3
"""Materialize or remove one digest-fenced platform-stage Runtime config."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = ROOT / "ecorex" / "release" / "stage_runtime_config.py"


def _load_runtime_config_module() -> object:
    """Load the stdlib-only leaf without importing the release package graph.

    The workflow's cleanup step is ``always()`` and can run before dependency
    installation. Importing ``ecorex.release`` would eagerly require Pydantic
    and make cleanup fail precisely on an early Stage failure.
    """

    metadata = _MODULE_PATH.lstat()
    if not stat.S_ISREG(metadata.st_mode) or _MODULE_PATH.is_symlink():
        raise RuntimeError("stage_runtime_config_module_invalid")
    specification = importlib.util.spec_from_file_location(
        "_ecorex_stage_runtime_config_leaf",
        _MODULE_PATH,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("stage_runtime_config_module_invalid")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


_runtime_config = _load_runtime_config_module()
StageRuntimeConfigError = _runtime_config.StageRuntimeConfigError
materialize_stage_runtime_config = _runtime_config.materialize_stage_runtime_config
remove_stage_runtime_config = _runtime_config.remove_stage_runtime_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("materialize", "remove"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    return parser


def _write_receipt(path: Path, value: object) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    encoded = os.environ.get("ECOREX_STAGE_RUNTIME_CONFIG_BASE64", "")
    expected = os.environ.get("ECOREX_STAGE_RUNTIME_CONFIG_SHA256")
    try:
        if args.mode == "materialize":
            result = materialize_stage_runtime_config(
                args.output,
                encoded=encoded,
                expected_sha256=expected or "",
            )
        else:
            result = remove_stage_runtime_config(
                args.output,
                expected_sha256=expected,
            )
    except StageRuntimeConfigError as error:
        result = {"error": error.code, "schema_version": 1, "status": "failed"}
        if args.receipt is not None:
            _write_receipt(args.receipt, result)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 1
    if args.receipt is not None:
        _write_receipt(args.receipt, result)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
