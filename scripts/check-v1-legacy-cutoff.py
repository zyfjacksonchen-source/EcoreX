#!/usr/bin/env python3
"""Fail the v1 source gate if a retired executable surface returns.

Retired source trees must contain no executable or cached residue.  Only static
historical notes in a dedicated ``docs``/``history`` subtree are exempt.
Rollback remains the update coordinator's job; a v1 build must never copy
executable v0.3 material into a new signed slot.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]

RETIRED_FILES = (
    ".github/workflows/ecorex-desktop-release.yml",
    ".github/workflows/ecorex-webui-macos-smoke.yml",
    "channel/web/web_channel.py",
    "channel/web/chat.html",
    "channel/web/static/app/index.html",
    "channel/web/static/app/assets/ecorex-v029-overlay.css",
    "channel/web/static/app/assets/ecorex-v029-overlay.js",
    "desktop/electron-builder.yml",
    "desktop/tsconfig.electron.json",
    "desktop/build/README-macos-agent-install.txt",
    "desktop/build/README-migration.txt",
    "desktop/build/entitlements.mac.plist",
    "desktop/build/icon.icns",
    "desktop/build/icon.ico",
    "desktop/build/icon.png",
    "desktop/src/App.tsx",
    "desktop/src/styles/app.css",
    "docker/Dockerfile.latest",
    "docker/build.latest.sh",
    "deploy/ecorex-site/admin/index.html",
    "deploy/ecorex-site/admin/admin.js",
    "deploy/ecorex-site/admin/admin.css",
    "deploy/ecorex-site/caddy/ecorex-web.routes.caddy",
    "deploy/ecorex-site/nginx/ecorex-web.conf.example",
    "deploy/ecorex-site/systemd/ecorex-web.service.example",
    "docker/entrypoint.sh",
    "scripts/check-ecorex-web-release.sh",
    "scripts/check-ecorex-server-release.sh",
    "scripts/check-v022-release-gate.py",
    "scripts/deploy-v022-hotfix-target.py",
    "scripts/deploy-v024-production.py",
    "scripts/light-real-release-validation.py",
    "scripts/install-ecorex-web.sh",
    "scripts/install-ecorex-public-release.sh",
    "scripts/ecorex-mvdcm-clone-migration.sh",
    "scripts/prepare-ecorex-public-release.ps1",
    "scripts/prepare-ecorex-web-release.ps1",
    "scripts/prepare-ecorex-webui-local-release.ps1",
    "scripts/smoke-image-jobs-provider-fallback.py",
    "scripts/smoke-image-jobs-seven-scenarios.py",
    "scripts/smoke-v022-release-deploy-rollback.py",
    "scripts/smoke-v022-release-target-deploy-rollback.py",
    "scripts/smoke-v023-install-packaging-contracts.py",
    "scripts/smoke-v024-office-pdf-web-evidence.py",
    "scripts/smoke-v024-release-artifact-contracts.py",
    "scripts/smoke-v026-production-200-user-behavior.py",
    "scripts/smoke-v026-production-agent-product-acceptance.py",
    "scripts/smoke-web-hotfix-contracts.py",
    "scripts/smoke-web-image-jobs-browser.py",
    "scripts/smoke-server-release-gate.py",
    "scripts/smoke-ecorex-webui-macos.sh",
    "scripts/shutdown.sh",
    "scripts/tout.sh",
    "scripts/update-ecorex-desktop-release-manifest.ps1",
    "scripts/validate-ecorex-release-artifacts.py",
)

RETIRED_TREES = (
    "channel/web",
    "desktop/electron",
    "deploy/ecorex-admin-api",
    "deploy/ecorex-usage-panel",
    "deploy/ecorex-site/admin",
)

LEGACY_IMPORT_ROOTS = frozenset(
    {
        "agent",
        "bridge",
        "channel",
        "cli",
        "common",
        "models",
        "plugins",
        "tools",
        "translate",
        "voice",
    }
)


def _source_files(root: Path) -> tuple[Path, ...]:
    if not root.exists():
        return ()

    def historical_document(path: Path) -> bool:
        relative = path.relative_to(root)
        return (
            bool(relative.parts)
            and relative.parts[0].casefold() in {"docs", "history"}
            and path.suffix.casefold() in {".md", ".rst", ".txt", ".adoc"}
        )

    return tuple(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not historical_document(path)
    )


def _legacy_imports(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            violations.append(f"{path.relative_to(ROOT).as_posix()}:unparseable:{type(exc).__name__}")
            continue
        for node in ast.walk(tree):
            imported: tuple[str, ...]
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported = (node.module,)
            else:
                continue
            for module in imported:
                if module.split(".", 1)[0] in LEGACY_IMPORT_ROOTS:
                    violations.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:{module}"
                    )
    return violations


def check(root: Path = ROOT) -> list[str]:
    violations = [
        f"retired file exists: {relative}"
        for relative in RETIRED_FILES
        if (root / relative).is_file()
    ]
    for relative in RETIRED_TREES:
        for path in _source_files(root / relative):
            violations.append(
                "retired tree contains a non-historical file: "
                f"{path.relative_to(root).as_posix()}"
            )

    app_source = (root / "app.py").read_text(encoding="utf-8")
    if "EXIT_RETIRED = 78" not in app_source:
        violations.append("app.py is not the fail-closed v0.3 tombstone")
    if "from channel" in app_source or "import channel" in app_source:
        violations.append("app.py can still import the legacy channel graph")

    for relative in (
        "run.sh",
        "scripts/run.ps1",
        "scripts/start.sh",
        "scripts/release-ecorex-default.ps1",
        "scripts/release-ecorex-webui-orchestrator.ps1",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        if "exit 78" not in source.casefold():
            violations.append(f"{relative} is not fail-closed")

    violations.extend(
        f"v1 imports legacy runtime: {value}"
        for value in _legacy_imports(root / "ecorex")
    )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check-v1-legacy-cutoff")
    parser.add_argument(
        "--strict-production",
        action="store_true",
        help="validate the complete production cutoff (the default gate is already strict)",
    )
    parser.parse_args(argv)
    violations = check()
    if violations:
        print("EcoreX v1 legacy cutoff gate failed:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print("EcoreX v1 legacy cutoff gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
