# S2 Web Core Runtime Ready

## Intent

Fix the user-visible "environment dependencies are basically missing" class of failures for the Web service package. The Web runtime must validate its own Python packages, Node/npm/npx, and tool entrypoints without treating host system PATH as release readiness.

## Implemented Changes

- Web Linux installer now installs or reuses an EcoreX-owned Node runtime at `$INSTALL_ROOT/node`.
- The service unit exports `ECOREX_INSTALL_ROOT`, `ECOREX_STATE_DIR`, `ECOREX_NODE_ROOT`, and `ECOREX_PYTHON_PATH`.
- Web runtime config now stores `appdata_dir` under `$STATE_DIR/appdata`, aligning user data, permission audit, capability state, and runtime baseline evidence under the service state root.
- Web release packages include `runtime/scripts/check-web-core-runtime-baseline.py`.
- Web Linux installer runs `check-web-core-runtime-baseline.py --strict` after Python dependencies and Node are installed, writing `$STATE_DIR/runtime-baseline.json`.
- Node archive install verifies SHA256 when downloading from Node dist, manually extracts only safe member types, normalizes file modes, and allows official relative npm/npx symlinks only when they resolve inside the install target.
- Downloaded Node archives fail closed when the Node dist checksum file does not contain the expected archive.
- Web release checker parses `$STATE_DIR/runtime-baseline.json` and requires `releaseReady=true` plus `blocking=0`.
- `RuntimeDependencyProvider` now discovers Node under install-root-owned directories and honors `ECOREX_STATE_DIR`.
- Web defaults no longer enable `tools.feishu_cli.allow_system_node`; Feishu must use owned runtime Node/npm/npx unless explicitly configured later.
- The packaged systemd example now exports the same install root, state root, Node root, venv Python, capability state, and Playwright browser paths as the installer-generated unit.
- Tongxin/Xin Assistant bootstrap health checks now enforce that the remote package itself exposes required `models.database` / `models.DATABASE` exports through `models.py` or `models/__init__.py`.
- `models/__init__.py` makes EcoreX's model package explicit without fabricating Tongxin data-layer exports.

## Acceptance

- Owned install-root fixture passes strict Web core baseline with no system PATH.
- Web release scripts require the baseline checker and installed Node/npm/npx.
- Missing model credentials remain non-blocking and are not reported as package/runtime failures.
- Tongxin remote bootstrap package compatibility accepts provider packages that expose `database`/`DATABASE`, and rejects packages or stale local files that do not.
- Tongxin bootstrap does not mutate downloaded files after SHA verification.

## Remaining Notes

- The installer can pull fixed-version Node from `nodejs.org` when no package archive is bundled. S3 should move this into shared runtime-pack manifest semantics.
- S9 should run the same strict baseline on a clean Linux Web install as a release gate artifact.
