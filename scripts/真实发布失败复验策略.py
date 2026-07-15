#!/usr/bin/env python3
"""真实发布失败后复验策略入口。"""

from __future__ import annotations

import runpy
from pathlib import Path


TARGET = Path(__file__).with_name("real-release-rerun-strategy.py")


if __name__ == "__main__":
    runpy.run_path(str(TARGET), run_name="__main__")
