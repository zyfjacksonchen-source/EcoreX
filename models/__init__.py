# encoding: utf-8
"""EcoreX model provider package.

This file intentionally keeps the package explicit without manufacturing
Tongxin/Xin Assistant data-layer exports. If a bootstrap package imports
``models.database`` or ``models.DATABASE``, it must ship the matching
``models.py`` or ``models/__init__.py`` next to its own CLI script.
"""

try:
    from . import database as database  # type: ignore[assignment]
    DATABASE = database
except Exception:
    __all__ = []
else:
    __all__ = ["DATABASE", "database"]
