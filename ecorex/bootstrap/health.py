"""Exact loopback probe for a provisional Runtime activation candidate."""

from __future__ import annotations

import http.client
import json
import time
from typing import Protocol

from ecorex.update.activation import (
    ACTIVATION_HEALTH_PATH,
    ACTIVATION_NONCE_HEADER,
    VerifiedProvisionalActivation,
    verify_activation_health_response,
)


# The provisional Runtime intentionally avoids repeating Bootstrap's complete
# Pack hash; it proves only its nonce-bound, no-traffic activation endpoint.
# Keep this recovery boundary short and finite. The full Runtime verifies and
# binds every Pack after confirmation, before it can cross the data barrier.
DEFAULT_ACTIVATION_HEALTH_TIMEOUT_SECONDS = 30.0
MAX_ACTIVATION_HEALTH_TIMEOUT_SECONDS = 60.0


class _Endpoint(Protocol):
    host: str
    port: int


class ActivationHealthProbe(Protocol):
    def probe(
        self,
        endpoint: _Endpoint,
        activation: VerifiedProvisionalActivation,
        nonce: str,
    ) -> bool:
        ...


class LoopbackActivationHealthProbe:
    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_ACTIVATION_HEALTH_TIMEOUT_SECONDS,
        poll_seconds: float = 0.1,
        max_response_bytes: int = 64 * 1024,
    ) -> None:
        if not 1.0 <= timeout_seconds <= MAX_ACTIVATION_HEALTH_TIMEOUT_SECONDS:
            raise ValueError(
                "activation health timeout must be between one and 60 seconds"
            )
        if not 0.01 <= poll_seconds <= 1.0:
            raise ValueError("activation health poll interval is invalid")
        if not 1024 <= max_response_bytes <= 1024 * 1024:
            raise ValueError("activation health response bound is invalid")
        self.timeout_seconds = float(timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self.max_response_bytes = max_response_bytes

    def probe(
        self,
        endpoint: _Endpoint,
        activation: VerifiedProvisionalActivation,
        nonce: str,
    ) -> bool:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            connection: http.client.HTTPConnection | None = None
            try:
                remaining = max(0.05, deadline - time.monotonic())
                connection = http.client.HTTPConnection(
                    endpoint.host,
                    endpoint.port,
                    timeout=min(1.0, remaining),
                )
                connection.request(
                    "GET",
                    ACTIVATION_HEALTH_PATH,
                    headers={
                        ACTIVATION_NONCE_HEADER: nonce,
                        "Accept": "application/json",
                        "Cache-Control": "no-store",
                    },
                )
                response = connection.getresponse()
                if response.status != 200:
                    if response.status in {401, 403, 409}:
                        return False
                    raise OSError("candidate is not ready")
                media_type = response.getheader("Content-Type", "").split(";", 1)[0]
                if media_type.strip().casefold() != "application/json":
                    return False
                declared = response.getheader("Content-Length")
                if declared is not None:
                    try:
                        length = int(declared)
                    except ValueError:
                        return False
                    if length < 0 or length > self.max_response_bytes:
                        return False
                encoded = response.read(self.max_response_bytes + 1)
                if len(encoded) > self.max_response_bytes:
                    return False
                try:
                    raw = json.loads(encoded)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return False
                if not isinstance(raw, dict):
                    return False
                return verify_activation_health_response(
                    activation.intent.health_identity,
                    nonce,
                    raw,
                )
            except (OSError, http.client.HTTPException):
                if time.monotonic() >= deadline:
                    return False
                time.sleep(self.poll_seconds)
            finally:
                if connection is not None:
                    connection.close()


__all__ = [
    "ActivationHealthProbe",
    "DEFAULT_ACTIVATION_HEALTH_TIMEOUT_SECONDS",
    "LoopbackActivationHealthProbe",
    "MAX_ACTIVATION_HEALTH_TIMEOUT_SECONDS",
]
