# EcoreX Python dependency locks

These files are the repository-owned dependency authority for EcoreX v1
Candidate and CI environments. Candidate jobs install a hash-locked profile,
then install the local EcoreX project with `--no-deps --no-build-isolation`.
This prevents `pyproject.toml` resolution from changing the installed closure
for an unchanged commit.

Profiles:

- `bootstrap.lock`: pinned pip/setuptools/wheel frontend.
- `runtime.lock`: local Runtime and release CLI closure.
- `dev.lock`: Runtime plus source-quality/test dependencies.
- `cloud.lock`: Runtime plus Control Plane/Image PostgreSQL and S3 clients.
- `platform-stage.lock`: Runtime plus the pinned browser, OCR and Office
  Capability Pack staging closures. Channel and sandbox packs add no external
  Python dependency.

Locks are universal Python 3.11.9 resolutions generated from the adjacent `.in`
files with public PyPI metadata and hashes. Regeneration requires the exact
generator recorded in `manifest.json` and must run:

```text
uv pip compile --python-version 3.11.9 --universal --generate-hashes \
  --default-index https://pypi.org/simple --no-emit-index-url --no-annotate \
  <profile>.in -o <profile>.lock
```

Run `python scripts/check-v1-dependency-locks.py` after regeneration. The gate
rejects missing hashes, non-exact/VCS/path requirements, manifest drift,
floating direct Python dependencies, an incomplete npm lock, or a Candidate
workflow that installs Python outside these locks. A resolver that cannot
produce a truthful cross-platform lock must fail the Candidate; hashes must
never be fabricated.
