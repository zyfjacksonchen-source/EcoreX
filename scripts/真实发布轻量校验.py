#!/usr/bin/env python3
"""真实发布轻量校验入口。

Use this cheap local preflight during everyday development.  It does not SSH to
production and does not call external models.
"""

from __future__ import annotations

import runpy
from pathlib import Path


TARGET = Path(__file__).with_name("light-real-release-validation.py")


if __name__ == "__main__":
    runpy.run_path(str(TARGET), run_name="__main__")
