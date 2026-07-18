from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WRAPPERS = ROOT / "deploy" / "ecorex-cloud-sidecar" / "signers"


def test_server_signer_wrappers_support_root_and_service_account_callers() -> None:
    roles = ("publication", "rollback", "device-access", "device-lease")
    for role in roles:
        payload = (WRAPPERS / f"ecorex-sign-{role}").read_text(encoding="ascii")
        assert payload.startswith("#!/bin/sh\nset -eu\n")
        assert 'if [ "$(id -u)" -eq 0 ]; then' in payload
        assert "exec /usr/sbin/runuser --user ecorex-cloud" in payload
        assert '[ "$(id -un)" = "ecorex-cloud" ] || exit 1' in payload
        assert payload.count("ECOREX_SERVER_SIGNER_KEY_ROOT=") == 2
        assert (
            f"ecorex-v1-server-sign-{role}.py"
            if role != "publication"
            else "ecorex-v1-server-sign-publication.py"
        ) in payload
