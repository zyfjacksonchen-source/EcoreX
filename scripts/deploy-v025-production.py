#!/usr/bin/env python3
"""Deploy EcoreX v0.2.5 with the versioned production deploy harness."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


if __name__ == "__main__":
    env = os.environ.copy()
    env["ECOREX_DEPLOY_VERSION"] = "0.2.5"
    script = Path(__file__).with_name("deploy-v024-production.py")
    raise SystemExit(subprocess.call([sys.executable, str(script)], env=env))
