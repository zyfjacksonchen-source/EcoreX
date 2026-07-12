"""Start the packaged EcoreX activation app and prove an actual loopback GET."""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from urllib.request import Request, urlopen

import uvicorn

from ecorex.runtime.database import SCHEMA_VERSION
from ecorex.server.activation import ActivationProbeSettings, create_activation_probe_app
from ecorex.update import (
    ACTIVATION_HEALTH_PATH,
    ACTIVATION_NONCE_HEADER,
    ActivationHealthIdentity,
)


def main() -> int:
    digest = hashlib.sha256(b"ecorex-platform-stage").hexdigest()
    nonce = "stage-probe-nonce-000000000000000000000000"
    identity = ActivationHealthIdentity(
        schema_version=1,
        transaction_id="1" * 32,
        slot_id="stage-probe",
        release_id="stage-probe",
        version="1.0.0",
        build_digest=digest,
        artifact_id="core-stage-probe",
        artifact_sha256=digest,
        payload_digest=digest,
        runtime_config_sha256=digest,
        web_bundle_sha256=digest,
        storage_schema_version=SCHEMA_VERSION,
        storage_identity=digest,
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()
    settings = ActivationProbeSettings(
        host="127.0.0.1",
        port=port,
        identity=identity,
        nonce=nonce,
        watchdog_seconds=30,
        exit_process=lambda _code: None,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_activation_probe_app(settings),
            host="127.0.0.1",
            port=port,
            log_config=None,
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    response = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                request = Request(
                    f"http://127.0.0.1:{port}{ACTIVATION_HEALTH_PATH}",
                    headers={ACTIVATION_NONCE_HEADER: nonce},
                )
                with urlopen(request, timeout=1) as opened:
                    response = json.loads(opened.read().decode("utf-8"))
                break
            except OSError:
                time.sleep(0.05)
        if not isinstance(response, dict) or response.get("status") != "ready":
            return 1
        print(json.dumps({"status": "passed", "proof": response}, sort_keys=True, separators=(",", ":")))
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
