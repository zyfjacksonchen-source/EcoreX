from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "platform-staging" / "native" / "windows"


def _source(name: str) -> str:
    return (NATIVE / name).read_text(encoding="utf-8")


def test_workspace_security_uses_exact_package_dacl_and_attests_existing_mic() -> None:
    security = _source("ecorex_sandbox_security.cpp")

    assert "ResourceIntegrityAtMostMedium" in security
    assert "WinUntrustedLabelSid" in security
    assert "WinLowLabelSid" in security
    assert "WinMediumLabelSid" in security
    assert "mandatory_labels > 1" in security
    assert "GetEffectiveRightsFromAclW" in security
    assert "effective != required" in security
    assert "access.grfAccessMode = SET_ACCESS" in security
    assert 'failure) *failure = "read_label"' in security
    assert "ApplyLowIntegrity" not in security
    assert "RemoveIntegrityLabel" not in security
    # Only exact Package-SID DACL grant/revoke calls may mutate filesystem
    # security.  The product must neither rewrite nor erase a user's SACL.
    assert security.count("SetNamedSecurityInfoW") == 2
    assert "LABEL_SECURITY_INFORMATION" in security  # read-only MIC attestation
    assert "WinBuiltinAnyPackageSid" not in security
    assert "ALL APPLICATION PACKAGES" not in security


def test_suspended_child_identity_is_verified_before_job_and_resume() -> None:
    process = _source("ecorex_sandbox_process.cpp")

    required = (
        "TokenIsAppContainer",
        "TokenAppContainerSid",
        "EqualSid(identity->TokenAppContainer, expected_sid)",
        "TokenIntegrityLevel",
        "WinLowLabelSid",
        "TokenCapabilities",
        "capabilities->GroupCount == 0",
        "child_token_identity",
    )
    for symbol in required:
        assert symbol in process

    created = process.index("CreateProcessAsUserW")
    attested = process.index("AttestSuspendedAppContainerToken(process.hProcess")
    assigned = process.index("AssignProcessToJobObject(job, process.hProcess)")
    resumed = process.index("ResumeThread(process.hThread)")
    assert created < attested < assigned < resumed
    assert "CREATE_SUSPENDED" in process


def test_windows_security_receipt_contract_is_v3_and_fail_closed() -> None:
    security = (ROOT / "ecorex" / "integration" / "windows_sandbox_security.py").read_text(
        encoding="utf-8"
    )
    runtime = (ROOT / "ecorex" / "integration" / "sandbox.py").read_text(
        encoding="utf-8"
    )

    contract = "windows-appcontainer-stable-provision-v3"
    proof = "immutable-read-tree-mutable-workspace-acl-mic-v3"
    assert f'_STABLE_PROVISION_CONTRACT = "{contract}"' in security
    assert f'_STRICT_INHERITANCE_PROOF = "{proof}"' in security
    assert contract in runtime
    assert "windows-appcontainer-stable-provision-v2" not in security
    assert "windows-appcontainer-stable-provision-v2" not in runtime
