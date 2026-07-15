# S2 Multi-Agent Consensus

## Scope

This consensus covers `S2 Web Core Runtime Ready`.

Review was read-only. No reviewer edited files.

## Final Decision

`PASS_WITH_NOTES`

All required perspectives reached `PASS` or `PASS_WITH_NOTES` after blocking findings were fixed and the Web Linux service artifact was rebuilt.

## Review Matrix

| Perspective | Verdict | Blocking Findings |
| --- | --- | --- |
| Architecture consistency | `PASS_WITH_NOTES` | None |
| Security / permissions | `PASS_WITH_NOTES` | None |
| Runtime dependencies | `PASS_WITH_NOTES` | None |
| Web UX / observability | `PASS` | None |
| Test / release gate | `PASS_WITH_NOTES` | None |

## Resolved Blocking Findings

- Tongxin bootstrap no longer mutates downloaded Python files after SHA verification. Required `models.database` / `models.DATABASE` exports must come from the provider package itself.
- `models/__init__.py` no longer fabricates `database` / `DATABASE` aliases when no real `models.database` exists.
- Node archive extraction no longer uses `tar.extract(...)`; it manually writes only directories, regular files, and safe relative symlinks, while rejecting unsupported member types and unsafe paths.
- Downloaded Node archive verification now fails closed when `SHASUMS256.txt` does not contain the expected archive.
- Web defaults now set `tools.feishu_cli.allow_system_node=false` in `config.py`, `config-template.json`, and the Web Linux installer-generated config.
- The Web Linux service tarball was rebuilt after source fixes. Artifact hash:
  `F5E688E2AC4AD7481808CD7E5E8BD1E00DC1C8C60DD1D0DCAE3361FCAE928C6B`.

## Accepted Evidence

- `docs/web-runtime-goal/artifacts/S02-web-core-runtime-tests.json`
- `release-artifacts/EcoreX_0.2.5-web-linux-service.tar.gz`
- `release-artifacts/ecorex-web-linux-service-0.2.5/`
- Focused regression: `120 passed, 1 skipped`
- `py_compile` passed for changed Python modules and scripts.
- `bash -n` passed for Web Linux installer and release checker.
- Package review confirmed source/staged/tar copies include S2 fixes.

## Non-Blocking Notes To Carry Forward

- S3 should move direct Node download and runtime-pack provenance into shared runtime-pack manifest semantics.
- Some desktop/WebUI local packaging paths still mention system Node defaults; they are outside the S2 Web Linux service scope, but should be cleaned up when Web-only runtime pack semantics are centralized.
- Add executable malicious-archive tests for Node tar edge cases in the long-term release gate; current S2 coverage includes static contract tests and reviewer package inspection.
- Legacy launcher convenience may still prepend host paths, but S2 strict baseline and tool execution boundaries do not treat host PATH as release readiness.

## Consensus

S2 passes. The Web Linux service source and rebuilt package now enforce owned-runtime readiness for Python, Node/npm/npx, core Python packages, and tool entrypoints before the Web UI starts. Missing model credentials remain non-blocking credential configuration issues rather than dependency failures.
