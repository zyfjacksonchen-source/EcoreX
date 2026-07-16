#!/usr/bin/env python3
"""Local-only entrypoint for the fail-closed v1 cloud sidecar deployer."""

from ecorex.deployment.cloud_sidecar import main


if __name__ == "__main__":
    raise SystemExit(main())
