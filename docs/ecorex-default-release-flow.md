# EcoreX Default Release Flow

The default project release packaging entry is:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\release-ecorex-default.ps1
```

From `desktop/`, the equivalent npm entry is:

```powershell
npm run release:default
```

This default flow:

- builds the WebUI Windows and macOS packages;
- promotes those package hashes into `deploy/ecorex-site/manifest.json`;
- creates `release-artifacts/EcoreX_<version>-public-release.zip`;
- externalizes large downloads by default;
- writes the public installer-only GitHub Release mirror:
  `https://github.com/zhangyifanjackson-dotcom/EcoreX-installers/releases/download/v<version>`;
- keeps the production origin download path as fallback through the manifest installer logic.

Use `-EmbedDownloads` only for explicit offline/internal handoff builds where the public release zip must include large package binaries.
