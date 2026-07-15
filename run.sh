#!/usr/bin/env bash
set -eu

cat >&2 <<'EOF'
The v0.3 source installer/runtime entrypoint is retired.
EcoreX v1 must be installed as a signed release and launched by ecorex-bootstrap.
Direct source assembly, git pull, pip install, npm build, and app.py startup are not supported.
EOF
exit 78
