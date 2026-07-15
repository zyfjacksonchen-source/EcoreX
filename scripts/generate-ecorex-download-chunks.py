#!/usr/bin/env python3
"""Generate fixed-size CDN download chunks and patch the public manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path.cwd()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate_chunks(artifact: dict[str, Any], artifact_path: Path, output_root: Path, chunk_size: int) -> dict[str, Any]:
    file_name = str(artifact["fileName"])
    chunk_dir = output_root / file_name
    chunk_dir.mkdir(parents=True, exist_ok=True)

    chunks: list[dict[str, Any]] = []
    index = 0
    with artifact_path.open("rb") as source:
        while True:
            data = source.read(chunk_size)
            if not data:
                break
            chunk_name = f"{index:04d}.part"
            chunk_path = chunk_dir / chunk_name
            chunk_path.write_bytes(data)
            chunks.append(
                {
                    "index": index,
                    "fileName": chunk_name,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest().upper(),
                }
            )
            index += 1

    manifest = {
        "mode": "file-list",
        "baseHref": f"chunks/{file_name}",
        "chunkSize": chunk_size,
        "chunkCount": len(chunks),
        "totalSize": artifact_path.stat().st_size,
        "sha256": sha256_path(artifact_path),
        "chunks": chunks,
    }
    write_json(chunk_dir / "chunks.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="deploy/ecorex-site/manifest.json")
    parser.add_argument("--artifact-id", action="append", default=["webui-windows-x64"])
    parser.add_argument("--artifact-dir", default="release-artifacts")
    parser.add_argument("--output-dir", default="release-artifacts/download-chunks")
    parser.add_argument("--chunk-mib", type=int, default=1)
    args = parser.parse_args()

    manifest_path = (ROOT / args.manifest).resolve()
    artifact_dir = (ROOT / args.artifact_dir).resolve()
    output_root = (ROOT / args.output_dir).resolve()
    chunk_size = int(args.chunk_mib) * 1024 * 1024
    if chunk_size <= 0:
        raise SystemExit("--chunk-mib must be positive")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    wanted = set(args.artifact_id)
    patched: list[str] = []
    for artifact in manifest.get("artifacts") or []:
        if artifact.get("id") not in wanted:
            continue
        artifact_path = artifact_dir / str(artifact.get("fileName") or "")
        if not artifact_path.is_file():
            raise SystemExit(f"artifact not found: {artifact_path}")
        if artifact_path.stat().st_size != int(artifact.get("size") or 0):
            raise SystemExit(f"artifact size mismatch: {artifact_path.name}")
        if sha256_path(artifact_path) != str(artifact.get("sha256") or "").upper():
            raise SystemExit(f"artifact sha256 mismatch: {artifact_path.name}")
        artifact["chunked"] = generate_chunks(artifact, artifact_path, output_root, chunk_size)
        patched.append(str(artifact.get("id")))

    if sorted(patched) != sorted(wanted):
        raise SystemExit(f"missing artifact ids: {sorted(wanted - set(patched))}")
    write_json(manifest_path, manifest)
    print(json.dumps({"ok": True, "patched": patched, "chunkMiB": args.chunk_mib}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
