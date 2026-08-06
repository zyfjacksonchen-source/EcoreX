# e-Mate

e-Mate v0.3.2 is a WebUI-first office Agent. React is the only product UI and
maps backend projections; the local Python Runtime is the authority for Agent
execution, tools, permissions, jobs, connectors, artifacts and updates.

The v0.3.2 product distribution contains:

- a content-addressed React WebUI served on a stable loopback URL;
- a Python 3.11+ FastAPI/ASGI Runtime;
- a signed Core plus the required browser, channels, image, OCR, Office-format
  and sandbox Capability Pack archives;
- managed model, release and observability control-plane contracts; and
- a user-confirmed “更新并刷新” online-update flow.

e-Mate v0.3.2 does **not** ship or develop an Electron/native desktop application,
DMG application, desktop window shell or native-app signing/notarization chain.
Windows x64 and macOS arm64/x64 are Runtime/WebUI host targets only. Release
archives still require Ed25519 signatures and SHA-256 verification before the
local Runtime can install or activate them.

## Development

The durable v0.3.2 engineering record is in `docs/v0.3.2/`. The React project remains
under the historical `desktop/` directory name, but it builds a browser WebUI,
not a desktop app.

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

Production startup is the signed Runtime serving the exact Web manifest. User
machines never run `git pull`, `npm build` or online `pip` assembly.

## Repository

- GitHub: `https://github.com/zhangyifanjackson-dotcom/EcoreX`
- Public product/download page: `https://dl.ecoremedia.net/ecorex-agent/`

## License

MIT. See `LICENSE`.
