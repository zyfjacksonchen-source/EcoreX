"""EcoreX browser Capability Pack entrypoint."""

from __future__ import annotations

from browser_pack import handle
from ecorex_pack_protocol import run


if __name__ == "__main__":
    raise SystemExit(run("browser", frozenset({"cdp", "fetch"}), handle))
