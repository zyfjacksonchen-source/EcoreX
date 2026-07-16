#!/usr/bin/env python3
"""Assemble the fixed 24-stage Candidate recipe without inventing artifacts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex import __version__  # noqa: E402
from ecorex.pack_catalog import REQUIRED_CAPABILITY_PACK_IDS  # noqa: E402
from ecorex.update import ReleaseChannel  # noqa: E402


TARGETS = (("windows", "x64"), ("macos", "arm64"), ("macos", "x64"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--channel", required=True, choices=("canary", "stable"))
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--repository", required=True)
    return parser


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value.rstrip("/")


def _mirror_channel_root(base_url: str, channel: ReleaseChannel) -> str:
    """Resolve either an uploadable mirror or a GitHub read-through proxy.

    ``ghproxy``/``ghfast`` style services preserve the complete GitHub URL
    after their own host.  Appending ``stable`` would create a path that can
    never exist; the Candidate builder instead appends the same immutable tag
    used by the GitHub source.  Dedicated mirrors retain the v1 channel root.
    """

    if base_url.endswith("/releases/download"):
        return base_url
    return f"{base_url}/{channel.value}"


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = args.input_root.resolve(strict=True)
        inputs: list[dict[str, str]] = []
        for platform, architecture in TARGETS:
            target = f"{platform}-{architecture}"
            stage_sources = (
                ("core", f"stages/{target}/core"),
                ("bootstrap", f"stages/{target}/bootstrap"),
                *(
                    (pack_id, f"stages/{target}/packs/{pack_id}")
                    for pack_id in REQUIRED_CAPABILITY_PACK_IDS
                ),
            )
            for key, source in stage_sources:
                source_path = root / source
                receipt_path = root / f"receipts/{target}/{key}.json"
                if not source_path.is_dir() or not receipt_path.is_file():
                    raise ValueError("candidate_stage_set_incomplete")
                inputs.append(
                    {
                        "source_dir": source,
                        "receipt": f"receipts/{target}/{key}.json",
                    }
                )
        channel = ReleaseChannel(args.channel)
        mirror_base_url = _environment("ECOREX_RELEASE_MIRROR_BASE_URL")
        value = {
            "schema_version": 1,
            "channel": args.channel,
            "created_at": args.created_at,
            "sources": [
                {
                    "source_id": "github-cn",
                    "kind": "github-cn-mirror",
                    "base_url": _mirror_channel_root(mirror_base_url, channel),
                },
                {
                    "source_id": "github",
                    "kind": "github-release",
                    "base_url": (
                        f"https://github.com/{args.repository}/releases/download"
                    ),
                },
                {
                    "source_id": "cdn",
                    "kind": "ecorex-cdn",
                    "base_url": (
                        _environment("ECOREX_RELEASE_CDN_BASE_URL")
                        + f"/{channel.value}"
                    ),
                },
            ],
            "inputs": inputs,
        }
        output = args.output.resolve()
        try:
            output.relative_to(root)
        except ValueError:
            raise ValueError("candidate_recipe_must_be_inside_input_root") from None
        if os.path.lexists(output):
            raise ValueError("candidate_recipe_exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(json.dumps({"ok": True, "input_count": len(inputs)}, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
