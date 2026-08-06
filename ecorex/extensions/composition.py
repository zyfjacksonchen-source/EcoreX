"""Product composition for one durable Extension authority."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import threading

from ecorex.capabilities import CapabilityRegistry
from ecorex.connectors import ConnectorRegistry
from ecorex.update import SignatureVerifier

from .builtin import builtin_extension_manifests, register_builtin_extensions
from .local_bundle import LocalSkillBundleStore
from .repository import SQLiteExtensionRepository
from .service import ExtensionService
from .skill_runner import ControlledSkillRunner
from .skill_migration import migrate_skill_directories


class _ProductExtensionService(ExtensionService):
    """Extension authority whose product defaults converge only after Phase A."""

    def __init__(
        self,
        *args,
        startup_declarations,
        builtin_skill_root,
        legacy_skill_roots,
        skill_runner_factory,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._startup_declarations = tuple(startup_declarations)
        self._builtin_skill_root = builtin_skill_root
        self._legacy_skill_roots = tuple(legacy_skill_roots)
        self._skill_runner_factory = skill_runner_factory
        self._startup_lock = threading.Lock()
        self._startup_converged = False

    @property
    def startup_converged(self) -> bool:
        return self._startup_converged

    def converge_startup(self) -> None:
        with self._startup_lock:
            if self._startup_converged:
                return
            self.repository.converge_startup()
            if self.local_bundle_store is not None:
                self.local_bundle_store.converge_startup()
                if self._skill_runner_factory is not None:
                    self.bind_skill_runner(
                        self._skill_runner_factory(self.local_bundle_store)
                    )
            register_builtin_extensions(self, self._startup_declarations)
            migrated_names = migrate_skill_directories(
                self,
                builtin_root=self._builtin_skill_root,
                custom_roots=self._legacy_skill_roots,
            )
            self.import_legacy_skill_states(skip_names=migrated_names)
            self._startup_converged = True


def compose_extension_service(
    *,
    database_path: str | Path,
    product_version: str,
    core_build_digest: str,
    runtime_api_version: str,
    platform: str,
    architecture: str,
    capability_registry: CapabilityRegistry,
    connector_registry: ConnectorRegistry,
    installed_pack_ids: frozenset[str],
    signature_verifier: SignatureVerifier,
    builtin_skill_root: str | Path | None = None,
    legacy_skill_roots: tuple[str | Path, ...] = (),
    skill_runner_factory: Callable[[LocalSkillBundleStore], ControlledSkillRunner]
    | None = None,
    initialize: bool = True,
    create_storage: bool | None = None,
) -> ExtensionService:
    if not isinstance(initialize, bool):
        raise TypeError("initialize must be boolean")
    if create_storage is None:
        create_storage = initialize
    if not isinstance(create_storage, bool):
        raise TypeError("create_storage must be boolean")
    database = Path(database_path).expanduser().resolve()
    declarations = builtin_extension_manifests(
        product_version=product_version,
        core_build_digest=core_build_digest,
        runtime_api_version=runtime_api_version,
        platform=platform,
        architecture=architecture,
        capability_registry=capability_registry,
        connector_registry=connector_registry,
        installed_pack_ids=installed_pack_ids,
    )
    bundle_store = LocalSkillBundleStore(
        database.parent / "extension-cas",
        create=create_storage,
    )
    service = _ProductExtensionService(
        SQLiteExtensionRepository(database, initialize=False),
        runtime_api_version=runtime_api_version,
        platform=platform,
        architecture=architecture,
        signature_verifier=signature_verifier,
        known_tool_ids=frozenset(
            spec.tool_id for spec in capability_registry.all()
        ),
        known_connector_ids=frozenset(
            definition.connector_id for definition in connector_registry.definitions()
        ),
        known_pack_ids=installed_pack_ids,
        local_bundle_store=bundle_store,
        startup_declarations=declarations,
        builtin_skill_root=builtin_skill_root,
        legacy_skill_roots=legacy_skill_roots,
        skill_runner_factory=skill_runner_factory,
    )
    if initialize:
        service.converge_startup()
    return service


__all__ = ["compose_extension_service"]
