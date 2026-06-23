# EcoreX

EcoreX is a desktop and WebUI AI agent product maintained in the
`zhangyifanjackson-dotcom/EcoreX` GitHub repository.

The v0.2.0 distribution is built as:

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

Current development target: `v0.2.0`.

Main areas in this release:

- Production-grade run ledger, request terminal states, and Run Center task control
- SSE replay-gap handling, request-scoped recovery, cancellation, and high-concurrency backpressure
- Provider capability matrix, model-call telemetry, retry ownership, and fail-closed fallback rules
- Hardened image-generation retry handling across OpenAI-compatible and native providers
- Release promotion gates for signed Windows, macOS DMG evidence, Web/WebUI hashes, and deployment evidence

## Repository

- GitHub: `https://github.com/zhangyifanjackson-dotcom/EcoreX`
- Public product/download page: `https://www.ecoreai.cn/ecorex-agent/`

## License

MIT. See `LICENSE`.
