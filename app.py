"""Fail-closed tombstone for the retired v0.3 executable.

EcoreX v1 is launched only from a Bootstrap-verified slot.  Keeping this
small source-level tombstone prevents an old checkout or operator habit from
silently starting the removed WebChannel/runtime graph.
"""

from __future__ import annotations

import sys


EXIT_RETIRED = 78


def main() -> int:
    print(
        "The v0.3 app.py entrypoint is retired. "
        "Launch the signed product with `python -m ecorex.server serve` "
        "through EcoreX Bootstrap.",
        file=sys.stderr,
    )
    return EXIT_RETIRED


if __name__ == "__main__":
    raise SystemExit(main())
