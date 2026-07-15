"""EcoreX sandbox Capability Pack entrypoint."""

from __future__ import annotations

from ecorex_pack_protocol import run
from sandbox_pack import handle


if __name__ == "__main__":
    raise SystemExit(run("sandbox", frozenset({"shell"}), handle))
