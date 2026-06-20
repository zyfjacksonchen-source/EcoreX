# EcoreX

EcoreX is a desktop and WebUI AI agent product maintained in the
`zhangyifanjackson-dotcom/EcoreX` GitHub repository.

The v0.1.16 distribution is built as:

- Electron desktop application
- Local React frontend
- Local Python agent runtime
- Optional WebUI runtime package
- Public download and update manifest for EcoreX releases

The desktop app starts a local runtime sidecar and communicates with it through
loopback HTTP. It is not a remote webpage wrapper: the packaged app includes the
frontend bundle, runtime code, capability tools, Skills, and local file/artifact
handling.

## Release Notes

Current development target: `v0.1.16`.

Main areas in this release:

- Desktop streaming stability, completion-state cleanup, and safer SSE recovery
- Artifact thumbnails, large image preview, local file stat/open, and diagnostics hardening
- Broader global/plugin Skill discovery and scrollable `@skill` mention results
- Windows local hand-test package plus release manifest validation
- Production promotion gates for signing, non-Windows packages, and deployment evidence

## Repository

- GitHub: `https://github.com/zhangyifanjackson-dotcom/EcoreX`
- Public product/download page: `https://www.ecoreai.cn/ecorex-agent/`

## License

MIT. See `LICENSE`.
