# e-Mate Enterprise 2.0.0

e-Mate is an enterprise desktop Agent for Windows and macOS. The current React
application is the only product UI and projects facts from the local Python
Runtime, which remains authoritative for Agent execution, tools, permissions,
jobs, connectors, artifacts, audit and recovery.

The 2.0.0 distribution contains:

- an Electron desktop shell that serves the current e-Mate UI from a stable
  authenticated loopback Runtime;
- a bundled Python 3.11+ backend;
- tenant-scoped enterprise model policy and the existing Usage audit panel;
- Windows x64 NSIS and macOS arm64/x64 DMG/ZIP packages; and
- verified update metadata and artifact integrity checks.

Platform signing is intentionally deferred for 2.0.0. Windows retains the
desktop updater flow. macOS performs update discovery and guides the user to a
manually verified installer until Developer ID signing is enabled.

## Development

The durable engineering record is in `release/v1/DEVELOPMENT_LOG.md`. The React
project and Electron shell are under `desktop/`.

```powershell
python -m pip install -e ".[dev]"
python scripts/run-v1-lint.py
python -m pytest -q tests/v1

cd desktop
npm ci
npm run typecheck
npm run test:v1
npm run build
```

Production startup launches the bundled Runtime and loads its loopback URL.
User machines never run `git pull`, `npm build` or online `pip` assembly.

## Repository

- GitHub: `https://github.com/zyfjacksonchen-source/EcoreX`
- Enterprise update source: `https://mvdcm.ecoremedia.net/e-mate/update/`

## License

MIT. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
