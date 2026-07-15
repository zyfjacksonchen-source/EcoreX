"""Request-scoped validation executed at the authoritative commit boundary."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


_TRANSACTION_COMMIT_GUARD: ContextVar[Callable[[], None] | None] = ContextVar(
    "ecorex_transaction_commit_guard",
    default=None,
)


@contextmanager
def transaction_commit_guard(guard: Callable[[], None]) -> Iterator[None]:
    """Apply one validator to every product write committed in this context.

    Context variables propagate through Starlette/AnyIO worker threads.  The
    HTTP adapter can therefore fence synchronous repositories without adding
    an opaque Runtime permit to public request contracts.  Background workers
    still use their durable Job/control permits explicitly.
    """

    if not callable(guard):
        raise TypeError("transaction commit guard must be callable")
    parent = _TRANSACTION_COMMIT_GUARD.get()

    def validate_nested_boundary() -> None:
        # A Worker Job guard may wrap a Connector/Artifact/service-specific
        # guard. Replacing the outer validator would let a cancelled lease
        # commit under a still-healthy inner Runtime permit. Every nested
        # authority must remain true at the same physical commit.
        if parent is not None:
            parent()
        guard()

    token = _TRANSACTION_COMMIT_GUARD.set(validate_nested_boundary)
    try:
        yield
    finally:
        _TRANSACTION_COMMIT_GUARD.reset(token)


def assert_transaction_commit_guard() -> None:
    """Validate the current execution epoch immediately before a write commit."""

    guard = _TRANSACTION_COMMIT_GUARD.get()
    if guard is not None:
        guard()


def assert_current_mutation_guard() -> None:
    """Fence a non-SQL mutation at its explicit linearization point.

    Filesystem state machines cannot rely on ``Connection.commit``.  They call
    this immediately before each durable intent, rename/unlink, or completion
    receipt.  With no request/Job guard the function is deliberately a no-op
    for signed migration/bootstrap authorities that own a separate boundary.
    """

    assert_transaction_commit_guard()


def transaction_commit_guard_active() -> bool:
    """Return whether the current call context owns a commit validator."""

    return _TRANSACTION_COMMIT_GUARD.get() is not None


__all__ = [
    "assert_transaction_commit_guard",
    "assert_current_mutation_guard",
    "transaction_commit_guard",
    "transaction_commit_guard_active",
]
