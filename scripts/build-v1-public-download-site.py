#!/usr/bin/env python3
"""Materialize content-addressed public JS/CSS and atomically rebind HTML pages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ecorex.update.locking import ProductFileLock  # noqa: E402


_SCRIPT = re.compile(r"^site(?:\.[0-9a-f]{12})?\.js$")
_STYLE = re.compile(r"^styles(?:\.[0-9a-f]{12})?\.css$")
_SCRIPT_REFERENCE = re.compile(r"\./site(?:\.[0-9a-f]{12})?\.js")
_STYLE_REFERENCE = re.compile(r"\./styles(?:\.[0-9a-f]{12})?\.css")
_MAX_ASSET_BYTES = 2 * 1024 * 1024
_MAX_HTML_BYTES = 256 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build-v1-public-download-site",
        description=(
            "hash the one public JS/CSS input, write new immutable names first, "
            "then atomically switch index.html"
        ),
    )
    parser.add_argument(
        "--site-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "deploy" / "ecorex-site",
    )
    return parser


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _read_regular(path: Path, *, maximum: int, label: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} cannot be read: {error.__class__.__name__}") from None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(before.st_mode)
        or bool(getattr(before, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(before.st_mode)
        or not 1 <= before.st_size <= maximum
    ):
        raise ValueError(f"{label} must be one bounded regular non-link file")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before):
                raise ValueError(f"{label} changed while opening")
            payload = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"{label} cannot be read: {error.__class__.__name__}") from None
    if _identity(opened) != _identity(after) or not 1 <= len(payload) <= maximum:
        raise ValueError(f"{label} changed while reading or exceeds its limit")
    return payload


def _active_candidate(
    root: Path,
    pattern: re.Pattern[str],
    active_name: str,
    label: str,
) -> tuple[Path, tuple[Path, ...]]:
    candidates = sorted(
        path for path in root.iterdir() if path.is_file() and pattern.fullmatch(path.name)
    )
    active = root / active_name
    if active not in candidates:
        raise ValueError(f"{label} HTML reference does not name a regular source")
    for candidate in candidates:
        _read_regular(candidate, maximum=_MAX_ASSET_BYTES, label=label)
    return active, tuple(candidates)


def _write_new_asset(root: Path, name: str, payload: bytes) -> Path:
    target = root / name
    if target.exists():
        if _read_regular(target, maximum=_MAX_ASSET_BYTES, label=name) != payload:
            raise ValueError(f"content-address collision for {name}")
        return target
    temporary = root / f".{name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{hashlib.sha256(payload).hexdigest()[:12]}"
    )
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def build(site_root: Path) -> dict[str, object]:
    root = site_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("site root must be a directory")
    lock_identity = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]
    lock_path = Path(tempfile.gettempdir()) / f"ecorex-public-site-{lock_identity}.lock"
    with ProductFileLock(lock_path, timeout=30):
        return _build_locked(root)


def _build_locked(root: Path) -> dict[str, object]:
    html_path = root / "index.html"
    documents: dict[Path, tuple[bytes, str]] = {}
    for path in sorted(root.glob("*.html")):
        payload = _read_regular(path, maximum=_MAX_HTML_BYTES, label=f"public {path.name}")
        try:
            document = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"public {path.name} must be UTF-8") from None
        if len(_SCRIPT_REFERENCE.findall(document)) != 1 or len(_STYLE_REFERENCE.findall(document)) != 1:
            raise ValueError(f"public {path.name} must reference exactly one JS and one CSS")
        documents[path] = payload, document
    if html_path not in documents:
        raise ValueError("public index.html is missing")
    html_payload, html = documents[html_path]
    script_references = _SCRIPT_REFERENCE.findall(html)
    style_references = _STYLE_REFERENCE.findall(html)
    if len(script_references) != 1 or len(style_references) != 1:
        raise ValueError("public index.html must reference exactly one JS and one CSS")
    script, script_candidates = _active_candidate(
        root,
        _SCRIPT,
        script_references[0].removeprefix("./"),
        "public JS",
    )
    style, style_candidates = _active_candidate(
        root,
        _STYLE,
        style_references[0].removeprefix("./"),
        "public CSS",
    )
    script_payload = _read_regular(script, maximum=_MAX_ASSET_BYTES, label="public JS")
    style_payload = _read_regular(style, maximum=_MAX_ASSET_BYTES, label="public CSS")
    script_digest = hashlib.sha256(script_payload).hexdigest()
    style_digest = hashlib.sha256(style_payload).hexdigest()
    script_name = f"site.{script_digest[:12]}.js"
    style_name = f"styles.{style_digest[:12]}.css"

    # New immutable assets exist before the mutable HTML pointer can name them.
    _write_new_asset(root, script_name, script_payload)
    _write_new_asset(root, style_name, style_payload)

    next_html = html_payload
    for path, (payload, document) in documents.items():
        document = _SCRIPT_REFERENCE.sub(f"./{script_name}", document)
        document = _STYLE_REFERENCE.sub(f"./{style_name}", document)
        next_document = document.encode("utf-8")
        if path == html_path:
            next_html = next_document
        if next_document != payload:
            _atomic_write(path, next_document)

    for candidates, current in (
        (script_candidates, root / script_name),
        (style_candidates, root / style_name),
    ):
        for previous in candidates:
            if previous != current:
                previous.unlink()
    if os.name != "nt":
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return {
        "schema_version": 1,
        "site_root": str(root),
        "javascript": {"name": script_name, "sha256": script_digest},
        "stylesheet": {"name": style_name, "sha256": style_digest},
        "html_sha256": hashlib.sha256(next_html).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build(args.site_root)
    except (OSError, ValueError) as error:
        print(
            json.dumps(
                {"ok": False, "error": error.__class__.__name__, "message": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
