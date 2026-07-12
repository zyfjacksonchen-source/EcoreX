"""EcoreX v1 local Runtime public API.

Application composition and worker exports are loaded lazily.  Storage and
update modules can therefore import a narrow Runtime submodule during process
bootstrap without executing the capability/update dependency graph again.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .api import RuntimeSettings
    from .composition import PreparedTurn, RuntimeComposition
    from .supervisor import AgentWorkerSupervisor, WorkerSupervisorSnapshot
    from .worker import AgentTurnWorker, WorkerOutcome, WorkerRunResult
from .database import SQLiteDatabase
from .activation_drain import (
    RuntimeActivationDrainController,
    RuntimeActivationDrainError,
    RuntimeActivationDrainLease,
    RuntimeActivationDrainTimeout,
)
from .event_store import EventPage, EventStore
from .interactions import InteractionStore
from .interaction_maintenance import (
    InteractionMaintenanceSnapshot,
    InteractionMaintenanceSupervisor,
)
from .invariants import (
    RuntimeInvariantAuditor,
    RuntimeInvariantError,
    RuntimeInvariantReport,
    RuntimeInvariantViolation,
)
from .invariant_guard import (
    RuntimeExecutionAdmission,
    RuntimeExecutionDenied,
    RuntimeExecutionGate,
    RuntimeExecutionGateSnapshot,
    RuntimeExecutionPermit,
    RuntimeDrainPermit,
    RuntimeExecutionStatus,
    RuntimeInvariantSupervisor,
    RuntimeInvariantSupervisorSnapshot,
)
from .jobs import DurableJobStore
from .kernel import RuntimeKernel
from .recovery_gate import (
    RECOVERY_EXECUTION_SCOPES,
    RecoveryExecutionDenied,
    RecoveryExecutionGate,
    RecoveryExecutionGateSnapshot,
    RecoveryExecutionPermit,
    RecoveryExecutionScope,
)
from .snapshots import (
    RuntimeSnapshot,
    RuntimeSnapshotConflict,
    RuntimeSnapshotError,
    RuntimeSnapshotNotFound,
    RuntimeSnapshotRepository,
    RuntimeSnapshotStale,
    TurnSnapshotContext,
)
from .tool_executions import (
    DurableDeferredDisclosureAuthority,
    DurableInvocationAdmissionAuthority,
    StaleInvocationAdmission,
    ToolExecutionConflict,
    ToolExecutionError,
    ToolExecutionRecord,
    ToolExecutionRepository,
    UncertainToolExecution,
)
from .turn_inputs import (
    TurnExecutionBatchRepository,
    TurnInputRevisionRepository,
    intent_fingerprint,
)
from .permissions import PermissionAuthority

__all__ = [
    "DurableJobStore",
    "RuntimeActivationDrainController",
    "RuntimeActivationDrainError",
    "RuntimeActivationDrainLease",
    "RuntimeActivationDrainTimeout",
    "EventPage",
    "EventStore",
    "InteractionStore",
    "InteractionMaintenanceSnapshot",
    "InteractionMaintenanceSupervisor",
    "RuntimeInvariantAuditor",
    "RuntimeInvariantError",
    "RuntimeInvariantReport",
    "RuntimeInvariantViolation",
    "RuntimeExecutionAdmission",
    "RuntimeExecutionDenied",
    "RuntimeExecutionGate",
    "RuntimeExecutionGateSnapshot",
    "RuntimeExecutionPermit",
    "RuntimeDrainPermit",
    "RuntimeExecutionStatus",
    "RuntimeInvariantSupervisor",
    "RuntimeInvariantSupervisorSnapshot",
    "RuntimeKernel",
    "RECOVERY_EXECUTION_SCOPES",
    "RecoveryExecutionDenied",
    "RecoveryExecutionGate",
    "RecoveryExecutionGateSnapshot",
    "RecoveryExecutionPermit",
    "RecoveryExecutionScope",
    "PreparedTurn",
    "PermissionAuthority",
    "RuntimeComposition",
    "RuntimeSnapshot",
    "RuntimeSnapshotConflict",
    "RuntimeSnapshotError",
    "RuntimeSnapshotNotFound",
    "RuntimeSnapshotRepository",
    "RuntimeSnapshotStale",
    "TurnSnapshotContext",
    "TurnExecutionBatchRepository",
    "TurnInputRevisionRepository",
    "intent_fingerprint",
    "ToolExecutionConflict",
    "DurableDeferredDisclosureAuthority",
    "DurableInvocationAdmissionAuthority",
    "StaleInvocationAdmission",
    "ToolExecutionError",
    "ToolExecutionRecord",
    "ToolExecutionRepository",
    "UncertainToolExecution",
    "AgentTurnWorker",
    "AgentWorkerSupervisor",
    "WorkerOutcome",
    "WorkerRunResult",
    "WorkerSupervisorSnapshot",
    "RuntimeSettings",
    "SQLiteDatabase",
    "create_app",
]


def __getattr__(name: str) -> Any:
    if name in {"RuntimeSettings", "create_app"}:
        from .api import RuntimeSettings, create_app

        return RuntimeSettings if name == "RuntimeSettings" else create_app
    if name in {"PreparedTurn", "RuntimeComposition"}:
        from .composition import PreparedTurn, RuntimeComposition

        return PreparedTurn if name == "PreparedTurn" else RuntimeComposition
    if name in {"AgentTurnWorker", "WorkerOutcome", "WorkerRunResult"}:
        from .worker import AgentTurnWorker, WorkerOutcome, WorkerRunResult

        return {
            "AgentTurnWorker": AgentTurnWorker,
            "WorkerOutcome": WorkerOutcome,
            "WorkerRunResult": WorkerRunResult,
        }[name]
    if name in {"AgentWorkerSupervisor", "WorkerSupervisorSnapshot"}:
        from .supervisor import AgentWorkerSupervisor, WorkerSupervisorSnapshot

        return {
            "AgentWorkerSupervisor": AgentWorkerSupervisor,
            "WorkerSupervisorSnapshot": WorkerSupervisorSnapshot,
        }[name]
    raise AttributeError(name)
