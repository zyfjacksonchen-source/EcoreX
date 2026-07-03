# S13 Tongxin CLI Bundled Realtime

## Goal

Make the Web runtime stop treating Tongxin Assistant as a missing local file hunt:

- Bundle the verified read-only Tongxin CLI package under `tools/tongxin`.
- Keep credentials outside the package; runtime credentials must come from Admin/env/bootstrap state.
- Preserve the `models.Database` / `models.DATABASE` compatibility surface.
- Preserve the lowercase `models.database` compatibility surface used by older remote packages.
- Declare runtime dependencies required by the bundled package, including `cryptography`.
- Prefer a healthy bundled/local package over remote bootstrap; only use remote bootstrap when explicitly requested or when no healthy local package exists.
- Let `realtime summary --account-id ...` work without a local cache database by routing directly to the upstream realtime API.

## Changes

- Replaced the previous lightweight placeholder `tools/tongxin/xin_agent_cli.py` with the verified read-only CLI package.
- Added bundled package files: `models.py`, `models_init_.py`, `mpi.py`, `bili_column_defs.py`, and safe `config.py`.
- Scrubbed `config.py` so `_RUNTIME_ENV = {}` and no access tokens/user credentials are embedded in the release package.
- Exposed `DATABASE`, `Database`, and `database` from the bundled data-layer compatibility files.
- Updated `tools/tongxin/models/__init__.py` to delegate to the sibling full `models.py`, because Python imports a `models/` package before `models.py`.
- Updated `agent/tools/tongxin_cli/tongxin_cli.py` so:
  - bundled path is an explicit trusted candidate;
  - bundled placeholder output is not counted as healthy data readiness;
  - healthy bundled/local script wins by default;
  - explicit `prefer_remote` still bypasses existing config for authenticated bootstrap.
- Updated Tongxin install/status wording so agent-visible guidance says bundled/trusted local first and remote bootstrap only as fallback.
- Synced the no-secret compatibility files into the production server's existing state package without overwriting server auth config.

## Acceptance

- Local package compiles and emits real CLI schema.
- `import models` resolves to `tools/tongxin/models.py` and exposes `DATABASE`, `Database`, `database`, and `get_db`.
- `cryptography` is declared in root and Web core runtime requirements, and public/desktop runtime-pack requirement files remain identical.
- No static token/password/API-key leak in `tools/tongxin`.
- Wrapper unit tests pass.
- Web runtime/manifest tests pass.
- Remote EcoreX production-test server can fetch real Tongxin realtime spend for the target business account set through the Web runtime package path, without persisting raw business identifiers in review artifacts.
- Production postdeploy probe validates the deployed runtime bundle, existing state package compatibility, wrapper status, real account discovery, and sampled realtime `direct_account_id` path.
