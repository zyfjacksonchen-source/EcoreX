# Attested encrypted local CAS production contract

## Decision

EcoreX v1 supports `attested-encrypted-local-cas` as the formal production
storage mode for the current single-host cloud deployment. It exists because
MinIO Community does not implement the AWS Account/Bucket PublicAccessBlock
API required by the exact S3 gate. The deployment must not report that the S3
gate passed when that API is absent.

The private S3 implementation remains supported and unchanged. Operators may
select S3 again when the endpoint passes its complete privacy, encryption,
conditional-write, checksum and delete probes. The local CAS is not an S3
emulator and does not weaken those S3 checks.

## Availability boundary

- `replica_count` must be exactly `1`.
- Control Plane, Image API and every Image worker must run as separate
  processes on the same machine and use the same mounted encrypted volume.
- The backend reports `availability_scope=single-host` and
  `supports_multi_host_ha=false`. It must never be described as multi-host HA.
- Share and Image use separate namespaces so deletion in one domain cannot
  remove another domain's live object. Their quota, mount, attestation, host
  identity and health authority are shared at volume level.

## Storage and integrity contract

The CAS root is `/var/lib/ecorex/cas`, strictly below the live encrypted mount
`/var/lib/ecorex`. Blob identity is SHA-256 and the on-disk path is derived
only from the validated digest. Creates use an exclusive temporary file,
file `fsync`, atomic create-if-absent, directory `fsync`, and full digest/size
verification. Metadata uses compare-and-swap records. Reads validate file
identity, size and SHA-256 before returning bytes.

A volume-wide OS file lock serializes quota accounting, record transitions,
recovery and deletion across processes. Crash-left temporary files are removed
under that lock. Image recovery repairs a blob committed before its reference
record and completes deletion tombstones. Quota includes every namespace;
minimum free space is also enforced before writes.

Readiness must perform a real write/read/delete probe. Deep health additionally
rehashes stored blobs and returns only bounded counters and the attestation,
evidence and machine identity digests.

## Attestation and permissions

Startup fails closed unless all of these agree:

- the immutable attestation file SHA-256;
- provider `luks2` or `alibaba-cloud-kms`, `encrypted=true`, volume ID and
  evidence digest;
- live mount root and device identity;
- expected `/etc/machine-id` SHA-256;
- immutable CAS marker identity and `replica_count=1`;
- non-symlink/reparse paths and the configured POSIX group policy.

Provision `/var/lib/ecorex/cas` as `root:ecorex-storage` (or the explicitly
pinned shared storage group), directories `2770`, and files `0660`. Provision
`/var/lib/ecorex/secrets` as `root:ecorex-cloud` mode `0750`, and each
`*.secret.env` as `root:ecorex-cloud` mode `0640`. Secret env files are not
stored under `/etc`: that filesystem has not been attested as encrypted.
Non-secret configuration remains under `/etc/ecorex/cloud`.

The systemd units use `RequiresMountsFor=/var/lib/ecorex`, point secret
`EnvironmentFile` entries at `/var/lib/ecorex/secrets`, and grant Control Plane
and Image services write access to `/var/lib/ecorex/cas`. They do not depend on
`minio.service`; S3 endpoints, when selected, are validated by the application
storage contract rather than by a local unit name.

## Required configuration

Control Plane and Image production composition must each accept storage mode
`attested-encrypted-local-cas` plus the same values for:

- CAS root and attestation file path;
- attestation SHA-256, evidence/volume identity and machine-id SHA-256;
- replica count (`1` only), quota, minimum free bytes and storage group GID.

Control Plane constructs namespace `share`; Image API and workers construct
namespace `image`. S3 configuration remains a distinct mode. Mixed local-CAS
identities, roots, quota fences or attestations are a startup error, not a
runtime fallback.

## Release gate

Before activating a candidate, run schema checks for every service and require
the selected storage backend's real health probe. For local CAS this includes
the live encrypted mount, marker, permissions, quota and write/read/delete
probe. For S3 it remains the exact S3 control and object lifecycle probe. A
failure in either mode blocks activation and leaves the known-good slot active.
