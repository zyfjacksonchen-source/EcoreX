"""Fail-closed errors for the standalone EcoreX product bootstrap."""

from __future__ import annotations


class BootstrapError(RuntimeError):
    """Base class for failures safe to classify at the process boundary."""


class BootstrapConfigurationError(BootstrapError):
    """The bootstrap was configured with an unsafe or unsupported value."""


class BootstrapTrustError(BootstrapError):
    """The selected runtime slot did not pass the complete trust chain."""


class RuntimeLaunchError(BootstrapError):
    """A verified runtime could not be started safely."""
