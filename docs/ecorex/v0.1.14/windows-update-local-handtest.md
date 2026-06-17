# Windows Update Local Hand Test

## 本地产物

- Installer: `C:\CowAgent\desktop\release\EcoreX_0.1.14_x64-setup.exe`
- Blockmap: `C:\CowAgent\desktop\release\EcoreX_0.1.14_x64-setup.exe.blockmap`
- Feed: `C:\CowAgent\desktop\release\latest.yml`
- Status: local unsigned build, `NotSigned`; do not publish as final release.

## 启动本地 Feed

```powershell
cd C:\CowAgent\desktop\release
python -m http.server 18014 --bind 127.0.0.1
```

Open `http://127.0.0.1:18014/latest.yml` and confirm it returns `version: 0.1.14`.

## 指向本地 Feed

For a build that includes the v0.1.14 update override support, create or update:

`%APPDATA%\EcoreX\enterprise-policy.json`

```json
{
  "updateFeedUrl": "http://127.0.0.1:18014/"
}
```

Restart EcoreX, then run update check from the app. Expected states:

- `checking`: app reads `latest.yml`.
- `available` / `downloading`: Windows auto-download starts.
- `downloaded`: update is ready, but not force-installed.
- `blocked`: if active requests exist, install is blocked with an active task count.
- `installing`: only after user clicks install and no active request is running.

## Session Preservation Check

Before installing:

- Create at least two sessions.
- Send text messages.
- Upload one file.
- Generate or attach one image artifact.
- Keep one active session/project selected.

After restart/install:

- Session count is unchanged.
- Chat text is unchanged.
- Message extras/tool records remain visible.
- Uploaded file and generated artifact still preview/open.
- Active session/project is restored.

## Real v0.1.13 Client Note

The final v0.1.13-to-v0.1.14 upgrade validation must use the public feed URL embedded in the installed v0.1.13 client. The local `updateFeedUrl` override is available for builds that include the v0.1.14 updater override support and for future regression testing.

For real public validation after user hand test:

- Upload signed installer.
- Upload `.blockmap`.
- Upload `latest.yml`.
- Keep `latest.yml` next to the installer under the generic feed URL.
- Verify an installed v0.1.13 Windows client detects `0.1.14`, downloads it, blocks install during active requests, then preserves all session data after restart.
