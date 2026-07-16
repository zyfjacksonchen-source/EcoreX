# v1.0 CDN Replica publication boundary

The production Control Plane exposes exactly two authenticated mutation routes:

- `PUT /api/v1/releases/{release_id}/replicas/cdn/assets/{name}`
- `POST /api/v1/releases/{release_id}/replicas/cdn/finalize`

`source_id` is not caller-selectable. It is always `cdn`. The Bearer credential
is read only from the server process environment. `CURRENT` and `NEXT` slots
permit overlap during rotation; neither value is persisted in configuration,
receipts, errors or audit payloads.

## Storage and commit protocol

The only production write root is:

```text
/srv/ecorex-agent-download/v1-artifacts/{release_namespace}/{stable,canary}
```

`release_namespace` must be exactly `v{product_version}`. The product version is
bound to the repository's single version source, while the stable storage and
public roots remain version-neutral. A later release therefore changes generated
deployment configuration, not backend route code.

Uploads stream into a 0700 dot-prefixed release staging directory. Each request
must provide one exact Content-Length, size, SHA-256 and deterministic
Idempotency-Key. A same-name/same-digest retry is accepted; a different digest
is a conflict and no existing inode is replaced. Temporary files and directory
entries cross `fsync` before an `O_EXCL`/hard-link no-clobber commit.

Finalize verifies all of the following before public visibility:

1. the requested manifest digest and strict manifest schema;
2. the manifest Ed25519 signature against the release keyring;
3. `version=1.0.0`, the release/channel identity and signed CDN source URL;
4. every Artifact signature, size and SHA-256;
5. the exact file set: manifest, metadata, SBOM and declared Artifacts;
6. metadata bindings for manifest, SBOM and all Artifact records;
7. a parseable CycloneDX SBOM.

The final release directory is reserved with a no-clobber mkdir and populated by
no-clobber hard links. The hidden `.ready.json` marker is written and synced
while the directory remains 0700. Files then become 0644 and directory
visibility changes to 0755 last. Nginx maps only a strict release ID and safe
asset filename; staging paths and `.ready.json` cannot match the public route.

Finalize retries re-verify the entire published directory. A crash before the
ready marker safely rebuilds a private partial directory only when every partial
inode is linked to the authenticated staging set. A crash after the marker but
before visibility completes the permission transition and cleanup. Any public
directory or marker mutation fails closed and is never overwritten.

Successful asset and finalize facts enter the encrypted, integrity-chained
Cloud Audit repository using deterministic event identities. Payloads contain
only release/source/name/size/digest/state fields and never include credentials
or local paths.
