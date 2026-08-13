from __future__ import annotations

import json
import os
import threading
import time


def test_agent_desktop_update_writes_one_owner_bound_request(tmp_path, monkeypatch) -> None:
    from agent.tools.desktop_update.desktop_update import DesktopUpdateTool

    nonce = "n" * 43
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "runtime-owner.json").write_text(
        json.dumps({
            "schema_version": 2,
            "nonce": nonce,
            "pid": os.getpid(),
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
    consumed = []
    def acknowledge() -> None:
        request = tmp_path / "desktop-update" / "request.json"
        for _ in range(100):
            if request.exists():
                consumed.append(json.loads(request.read_text(encoding="utf-8")))
                request.unlink()
                (request.parent / "receipt.json").write_text(
                    json.dumps({
                        "schema_version": 1,
                        "owner_nonce": nonce,
                        "tool_call_id": "call-update-205",
                        "status": "accepted",
                        "completed": False,
                    }),
                    encoding="utf-8",
                )
                return
            time.sleep(0.01)
    thread = threading.Thread(target=acknowledge)
    thread.start()
    result = tool.execute({"action": "install_latest"})
    thread.join()

    assert result.status == "success"
    assert result.result == {
        "status": "accepted",
        "action": "install_latest",
        "completed": False,
        "message": "The desktop updater accepted the request; do not claim completion until e-Mate relaunches on the new version.",
        "willRelaunch": True,
    }
    assert consumed == [{
        "schema_version": 1,
        "action": "install_latest",
        "owner_nonce": nonce,
        "tool_call_id": "call-update-205",
    }]
    assert not (tmp_path / "desktop-update" / "request.json").exists()


def test_agent_desktop_update_fails_closed_outside_owned_desktop(tmp_path, monkeypatch) -> None:
    from agent.tools.desktop_update.desktop_update import DesktopUpdateTool

    monkeypatch.setenv("EMATE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("EMATE_DESKTOP", raising=False)
    monkeypatch.delenv("EMATE_PACKAGED_RUNTIME", raising=False)
    monkeypatch.delenv("ECOREX_RUNTIME_OWNER_NONCE", raising=False)

    result = DesktopUpdateTool().execute({"action": "install_latest"})

    assert result.status == "error"
    assert not (tmp_path / "desktop-update" / "request.json").exists()


def test_cloud_catalog_does_not_register_desktop_update(tmp_path, monkeypatch) -> None:
    from agent.tools.tool_manager import ToolManager

    nonce = "n" * 43
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "runtime-owner.json").write_text(
        json.dumps({"schema_version": 2, "nonce": nonce, "pid": os.getpid() + 1}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EMATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMATE_DESKTOP", "1")
    monkeypatch.setenv("EMATE_PACKAGED_RUNTIME", "1")
    monkeypatch.setenv("ECOREX_RUNTIME_OWNER_NONCE", nonce)

    manager = ToolManager(workspace_root=tmp_path)
    manager.load_tools(start_mcp=False)

    assert "desktop_update" not in manager.list_tools()


def test_owned_desktop_catalog_registers_desktop_update(tmp_path, monkeypatch) -> None:
    from agent.tools.tool_manager import ToolManager

    nonce = "n" * 43
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "runtime-owner.json").write_text(
        json.dumps({"schema_version": 2, "nonce": nonce, "pid": os.getpid()}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EMATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMATE_DESKTOP", "1")
    monkeypatch.setenv("EMATE_PACKAGED_RUNTIME", "1")
    monkeypatch.setenv("ECOREX_RUNTIME_OWNER_NONCE", nonce)

    manager = ToolManager(workspace_root=tmp_path)
    manager.load_tools(start_mcp=False)

    assert "desktop_update" in manager.list_tools()


def test_agent_desktop_update_requires_electron_receipt(tmp_path, monkeypatch) -> None:
    import agent.tools.desktop_update.desktop_update as module

    nonce = "n" * 43
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "runtime-owner.json").write_text(
        json.dumps({
            "schema_version": 2,
            "nonce": nonce,
            "pid": os.getpid(),
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
    monkeypatch.setattr(module, "_RECEIPT_WAIT_SECONDS", 0, raising=False)

    tool = module.DesktopUpdateTool()
    tool.tool_call_id = "call-no-electron"
    result = tool.execute({"action": "install_latest"})

    assert result.status == "error"
    assert not (tmp_path / "desktop-update" / "request.json").exists()
