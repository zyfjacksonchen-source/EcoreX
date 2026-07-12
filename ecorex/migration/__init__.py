"""Public v0.3.0 -> v1.0 migration API."""

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
from .inventory import inventory_source
from .migrator import (
    BACKUP_MANIFEST_NAME,
    INVENTORY_NAME,
    QUARANTINE_NAME,
    REPORT_NAME,
    TARGET_ARTIFACT_ROOT_NAME,
    TARGET_DATABASE_NAME,
    MigrationOptions,
    V030ToV1Migrator,
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

__all__ = [
    "BACKUP_MANIFEST_NAME",
    "DuplicateLegacyIdError",
    "INVENTORY_NAME",
    "LegacyDatabaseError",
    "LegacySchemaError",
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
    "TARGET_ARTIFACT_ROOT_NAME",
    "TARGET_DATABASE_NAME",
    "TargetConflictError",
    "V030ToV1Migrator",
    "decrypt_quarantine",
    "create_migration_quarantine_router",
    "inventory_source",
    "load_quarantine_key",
    "migrate_v030_to_v1",
    "MigrationQuarantineService",
    "write_product_migration_plan",
]
