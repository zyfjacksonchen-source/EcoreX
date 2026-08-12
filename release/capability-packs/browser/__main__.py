"""EcoreX browser Capability Pack entrypoint."""

from __future__ import annotations

import sys

from browser_pack import BrowserPackHandler, handle
from ecorex_pack_protocol import run, run_session


if __name__ == "__main__":
    tools = frozenset({"browser", "web_fetch", "web_search"})
    if sys.argv[1:] == ["--session"]:
        raise SystemExit(run_session("browser", tools, BrowserPackHandler()))
    raise SystemExit(run("browser", tools, handle))
