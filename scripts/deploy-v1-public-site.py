#!/usr/bin/env python3
"""Server-local entrypoint for the fenced public-site activation authority."""

from ecorex.deployment.public_site import main


if __name__ == "__main__":
    raise SystemExit(main())
