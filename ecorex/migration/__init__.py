"""Public released EcoreX -> v1.0 migration API."""

from .crypto import decrypt_quarantine, load_quarantine_key
from .api import create_migration_quarantine_router
from .errors import (
    DuplicateLegacyIdError,
    LegacyDatabaseError,
    LegacySchemaError,
    MigrationError,
    MigrationVerificationError,
    QuarantineKeyRequired,
    QuarantineStateError,
    SourceChangedError,
    SourceLayoutError,
    TargetConflictError,
)
from .inventory import (
    DEFAULT_SOURCE_VERSION,
    SUPPORTED_SOURCE_VERSIONS,
    inventory_source,
)
from .migrator import (
    BACKUP_MANIFEST_NAME,
    INVENTORY_NAME,
    QUARANTINE_NAME,
    REPORT_NAME,
    TARGET_ARTIFACT_ROOT_NAME,
    TARGET_DATABASE_NAME,
    MigrationOptions,
    V030ToV1Migrator,
    migrate_legacy_to_v1,
    migrate_v030_to_v1,
)
from .models import MigrationReport, SourceInventory
from .product import (
    PRODUCT_MIGRATION_COMPLETION_NAME,
    PRODUCT_MIGRATION_PLAN_NAME,
    PRODUCT_MIGRATION_RECEIPT_NAME,
    ProductLegacyMigrationCoordinator,
    ProductMigrationError,
    ProductMigrationPlan,
    write_product_migration_plan,
)
from .quarantine import MigrationQuarantineService, QuarantineProjection
from .legacy_identity_export import (
    LegacyIdentityExportError,
    LegacyIdentityExportReport,
    export_v0292_legacy_identities,
)
from .cowagent_data import (
    CowAgentDataMigrationError,
    LegacyDataMigrationResult,
    LegacyDataRoot,
    RECEIPT_RELATIVE_PATH as COWAGENT_DATA_RECEIPT_RELATIVE_PATH,
    default_cowagent_data_roots,
    default_emate_data_root,
    migrate_cowagent_data,
)

LegacyDesktopDataMigrationError = CowAgentDataMigrationError
migrate_legacy_desktop_data = migrate_cowagent_data


def __getattr__(name: str):
    if name in {
        "LegacyAdminManagementImportError",
        "LegacyAdminManagementImportReport",
        "import_v0292_admin_management",
    }:
        from . import legacy_admin_management

        return getattr(legacy_admin_management, name)
    if name in {
        "LegacyPasswordCredentialImportError",
        "LegacyPasswordCredentialImportReport",
        "import_v0292_password_credentials",
    }:
        from . import legacy_password_credentials

        return getattr(legacy_password_credentials, name)
    raise AttributeError(name)


__all__ = [
    "BACKUP_MANIFEST_NAME",
    "COWAGENT_DATA_RECEIPT_RELATIVE_PATH",
    "CowAgentDataMigrationError",
    "DuplicateLegacyIdError",
    "DEFAULT_SOURCE_VERSION",
    "INVENTORY_NAME",
    "LegacyDatabaseError",
    "LegacySchemaError",
    "LegacyIdentityExportError",
    "LegacyIdentityExportReport",
    "LegacyDataMigrationResult",
    "LegacyDataRoot",
    "LegacyDesktopDataMigrationError",
    "LegacyAdminManagementImportError",
    "LegacyAdminManagementImportReport",
    "LegacyPasswordCredentialImportError",
    "LegacyPasswordCredentialImportReport",
    "MigrationError",
    "MigrationOptions",
    "MigrationReport",
    "MigrationVerificationError",
    "PRODUCT_MIGRATION_COMPLETION_NAME",
    "PRODUCT_MIGRATION_PLAN_NAME",
    "PRODUCT_MIGRATION_RECEIPT_NAME",
    "ProductLegacyMigrationCoordinator",
    "ProductMigrationError",
    "ProductMigrationPlan",
    "QUARANTINE_NAME",
    "QuarantineKeyRequired",
    "QuarantineStateError",
    "QuarantineProjection",
    "REPORT_NAME",
    "SourceChangedError",
    "SourceInventory",
    "SourceLayoutError",
    "SUPPORTED_SOURCE_VERSIONS",
    "TARGET_ARTIFACT_ROOT_NAME",
    "TARGET_DATABASE_NAME",
    "TargetConflictError",
    "V030ToV1Migrator",
    "decrypt_quarantine",
    "default_cowagent_data_roots",
    "default_emate_data_root",
    "create_migration_quarantine_router",
    "inventory_source",
    "export_v0292_legacy_identities",
    "import_v0292_admin_management",
    "import_v0292_password_credentials",
    "load_quarantine_key",
    "migrate_legacy_to_v1",
    "migrate_cowagent_data",
    "migrate_legacy_desktop_data",
    "migrate_v030_to_v1",
    "MigrationQuarantineService",
    "write_product_migration_plan",
]
