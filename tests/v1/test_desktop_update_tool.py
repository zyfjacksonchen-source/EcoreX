from __future__ import annotations

import json


def test_agent_desktop_update_writes_one_owner_bound_request(tmp_path, monkeypatch) -> None:
    from agent.tools.desktop_update.desktop_update import DesktopUpdateTool

    nonce = "n" * 43
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "runtime-owner.json").write_text(
        json.dumps({
            "schema_version": 2,
            "nonce": nonce,
            "pid": 42,
            "runtime_identity": {
                "release_id": "release-test",
                "build_digest": "a" * 64,
                "artifact_id": "artifact-test",
                "artifact_sha256": "b" * 64,
                "payload_digest": "c" * 64,
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("EMATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMATE_DESKTOP", "1")
    monkeypatch.setenv("EMATE_PACKAGED_RUNTIME", "1")
    monkeypatch.setenv("ECOREX_RUNTIME_OWNER_NONCE", nonce)

    tool = DesktopUpdateTool()
    tool.tool_call_id = "call-update-205"
    result = tool.execute({"action": "install_latest"})

    assert result.status == "success"
    assert result.result == {
        "status": "accepted",
        "action": "install_latest",
        "completed": False,
        "message": "The desktop updater accepted the request; do not claim completion until e-Mate relaunches on the new version.",
        "willRelaunch": True,
    }
    request = json.loads((tmp_path / "desktop-update" / "request.json").read_text())
    assert request == {
        "schema_version": 1,
        "action": "install_latest",
        "owner_nonce": nonce,
        "tool_call_id": "call-update-205",
    }


def test_agent_desktop_update_fails_closed_outside_owned_desktop(tmp_path, monkeypatch) -> None:
    from agent.tools.desktop_update.desktop_update import DesktopUpdateTool

    monkeypatch.setenv("EMATE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("EMATE_DESKTOP", raising=False)
    monkeypatch.delenv("EMATE_PACKAGED_RUNTIME", raising=False)
    monkeypatch.delenv("ECOREX_RUNTIME_OWNER_NONCE", raising=False)

    result = DesktopUpdateTool().execute({"action": "install_latest"})

    assert result.status == "error"
    assert not (tmp_path / "desktop-update" / "request.json").exists()
