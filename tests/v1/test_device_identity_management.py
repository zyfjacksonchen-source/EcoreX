from __future__ import annotations

from pathlib import Path

import pytest

from ecorex.control_plane.device_identity_management import (
    AdminManagementDeviceAccountDirectory,
)
from ecorex.control_plane.device_identity_production import (
    DeviceIdentityProductionConfig,
    DeviceIdentityProductionConfigurationError,
    ExternalSignerConfig,
)
from ecorex.control_plane.management import (
    AdminManagementRepository,
    ModelConnectionTestResult,
)
from ecorex.control_plane.management_models import (
    CreateAdminUserRequest,
    CreateModelConfigurationRequest,
    UpdateAdminUserRequest,
)
from ecorex.control_plane.management_schema import AdminManagementSchemaManager
from ecorex.control_plane.models import ControlPrincipal


ACTOR = ControlPrincipal(
    subject="bootstrap-administrator",
    client_id="migration",
    account_id="bootstrap-admin",
    roles=frozenset({"platform_admin"}),
)


def _repository(path: Path) -> AdminManagementRepository:
    AdminManagementSchemaManager(path).migrate()
    repository = AdminManagementRepository(path, encryption_key=b"d" * 32)
    model = repository.create_model_configuration(
        CreateModelConfigurationRequest(
            local_model_id="ecorex-chat",
            modality="chat",
            display_name="GPT-5.6 SOL",
            upstream_model_id="gpt-5.6-sol",
            provider_preset="responses",
            is_default=True,
            enabled=True,
            api_key="sk-test-admin-directory-key",
            client_request_id="create-admin-directory-model",
        ),
        actor=ACTOR,
    )
    assert model.draft is not None
    test = repository.begin_model_test(
        model.config_id,
        model.draft.revision,
        actor=ACTOR,
        client_request_id="test-admin-directory-model",
    )
    repository.finish_model_test(
        test,
        ModelConnectionTestResult(passed=True),
        actor=ACTOR,
    )
    return repository


def _create_user(
    repository: AdminManagementRepository,
    account_id: str,
) -> None:
    repository.create_user(
        CreateAdminUserRequest(
            account_id=account_id,
            display_name=account_id,
            email=None,
            organization_id=None,
            token_limit=0,
            image_limit=0,
            client_request_id=f"create-{account_id}",
        ),
        actor=ACTOR,
    )


def test_platform_roles_come_only_from_deployment_allowlist(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "control.db")
    _create_user(repository, "admin-1")
    _create_user(repository, "member-1")

    directory = AdminManagementDeviceAccountDirectory(
        repository,
        platform_admin_account_ids=frozenset({"admin-1"}),
    )

    assert directory.resolve("member-1").roles == ("user",)
    assert set(directory.resolve("admin-1").roles) == {
        "platform_admin",
        "user_admin",
        "model_admin",
        "release_admin",
        "user",
    }


def test_platform_allowlist_fails_closed_for_missing_or_suspended_user(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "control.db")
    _create_user(repository, "admin-1")

    with pytest.raises(ValueError, match="does not exist"):
        AdminManagementDeviceAccountDirectory(
            repository,
            platform_admin_account_ids=frozenset({"missing-admin"}),
        )

    current = repository.get_user("admin-1")
    repository.update_user(
        "admin-1",
        UpdateAdminUserRequest(
            display_name=current.display_name,
            email=current.email,
            organization_id=current.organization_id,
            status="suspended",
            token_limit=current.token_limit,
            image_limit=current.image_limit,
            expected_revision=current.revision,
            client_request_id="suspend-admin-1",
        ),
        actor=ACTOR,
    )
    with pytest.raises(ValueError, match="not active"):
        AdminManagementDeviceAccountDirectory(
            repository,
            platform_admin_account_ids=frozenset({"admin-1"}),
        )


def test_production_identity_requires_product_runtime_and_admin_web_clients(
    tmp_path: Path,
) -> None:
    access = ExternalSignerConfig(
        key_id="device-access",
        public_key=b"a" * 32,
        executable=(tmp_path / "access-signer").resolve(),
        executable_sha256="a" * 64,
        adapter=None,
        adapter_sha256=None,
    )
    lease = ExternalSignerConfig(
        key_id="device-lease",
        public_key=b"b" * 32,
        executable=(tmp_path / "lease-signer").resolve(),
        executable_sha256="b" * 64,
        adapter=None,
        adapter_sha256=None,
    )
    with pytest.raises(DeviceIdentityProductionConfigurationError):
        DeviceIdentityProductionConfig(
            database_path=(tmp_path / "control.db").resolve(),
            issuer="https://identity.ecorex.test",
            audience="ecorex-control-plane",
            verification_url="https://identity.ecorex.test/device",
            allowed_client_ids=frozenset({"ecorex-webui", "ecorex-admin-web"}),
            platform_admin_account_ids=frozenset({"admin-1"}),
            access_signer=access,
            lease_signer=lease,
        )

    configured = DeviceIdentityProductionConfig(
        database_path=(tmp_path / "control.db").resolve(),
        issuer="https://identity.ecorex.test",
        audience="ecorex-control-plane",
        verification_url="https://identity.ecorex.test/device",
        allowed_client_ids=frozenset(
            {"ecorex-product", "ecorex-webui", "ecorex-admin-web"}
        ),
        platform_admin_account_ids=frozenset({"admin-1"}),
        access_signer=access,
        lease_signer=lease,
    )
    assert "ecorex-product" in configured.allowed_client_ids
    assert "ecorex-admin-web" in configured.allowed_client_ids
