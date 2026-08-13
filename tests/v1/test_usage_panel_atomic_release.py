from __future__ import annotations

import hashlib
import builtins
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tomllib

import pytest

from ecorex import __version__ as PRODUCT_VERSION
from ecorex.control_plane import usage_panel_service


ROOT = Path(__file__).resolve().parents[2]


def _deployer() -> dict:
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return runpy.run_path(str(ROOT / "scripts/deploy-v030-production-usage-panel.py"))


def test_remote_release_library_uses_only_the_server_stdlib(tmp_path: Path) -> None:
    deployer = _deployer()
    original_import = builtins.__import__

    def stdlib_only(name, *args, **kwargs):
        if name == "ecorex" or name.startswith("ecorex."):
            raise AssertionError("remote release must not import the undeployed product package")
        return original_import(name, *args, **kwargs)

    namespace = {"__name__": "usage_panel_release_stdlib_test"}
    builtins.__import__ = stdlib_only
    try:
        exec(deployer["_REMOTE_LIBRARY"], namespace)
    finally:
        builtins.__import__ = original_import

    lock_path = tmp_path / "deployment.lock"
    with namespace["DeploymentLock"](lock_path, timeout=0):
        with pytest.raises(RuntimeError, match="deployment_lock_busy"):
            with namespace["DeploymentLock"](lock_path, timeout=0):
                pass
    with namespace["DeploymentLock"](lock_path, timeout=0):
        assert lock_path.is_file()


def test_candidate_upload_uses_paramiko_exclusive_writable_mode() -> None:
    deployer = _deployer()

    class RemoteFile(io.BytesIO):
        def __init__(self, owner, path, mode):
            super().__init__()
            self.owner = owner
            self.path = path
            self.mode = mode

        def __exit__(self, exc_type, exc, traceback):
            if exc_type is None:
                if "+" not in self.mode:
                    raise OSError("paramiko handle is not writable")
                self.owner.files[self.path] = self.getvalue()
            self.close()

    class Sftp:
        def __init__(self):
            self.files = {}
            self.modes = []
            self.directories = set()

        def mkdir(self, path, _mode):
            if path in self.directories:
                raise OSError("remote candidate collision")
            self.directories.add(path)

        def open(self, path, mode):
            if "x" not in mode or path in self.files:
                raise OSError("remote candidate collision")
            self.modes.append(mode)
            return RemoteFile(self, path, mode)

        def chmod(self, _path, _mode):
            pass

    sftp = Sftp()
    files = {name: name.encode() for name in deployer["CANDIDATE_NAMES"]}
    deployer["_upload_candidate"](sftp, "/releases/.incoming-test", files)

    assert set(sftp.files) == {
        f"/releases/.incoming-test/{name}" for name in deployer["CANDIDATE_NAMES"]
    }
    assert sftp.modes == ["x+"] * len(files)
    preserved = dict(sftp.files)
    with pytest.raises(OSError, match="remote candidate collision"):
        deployer["_upload_candidate"](sftp, "/releases/.incoming-test", files)
    assert sftp.files == preserved


def test_candidate_binds_exact_source_and_every_runtime_file() -> None:
    source_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    candidate = _deployer()["_build_candidate"](
        ROOT,
        expected_version=PRODUCT_VERSION,
        expected_projection=usage_panel_service.USAGE_PROJECTION_VERSION,
        expected_source_sha=source_sha,
    )

    files = candidate["files"]
    assert set(files) == {
        "usage_panel_api.py",
        "index.html",
        "app.js",
        "styles.css",
        "data.js",
        "release-manifest.json",
        "release-receipt.json",
    }
    manifest = json.loads(files["release-manifest.json"])
    receipt = json.loads(files["release-receipt.json"])
    assert manifest["schema_version"] == 1
    assert manifest["source_sha"] == source_sha
    assert manifest["version"] == PRODUCT_VERSION
    assert manifest["projection_version"] == usage_panel_service.USAGE_PROJECTION_VERSION
    assert receipt == {
        "schema_version": 1,
        "status": "prepared",
        "source_sha": source_sha,
        "version": PRODUCT_VERSION,
        "manifest_sha256": hashlib.sha256(files["release-manifest.json"]).hexdigest(),
    }
    assert manifest["inventory"] == {
        name: {"size": len(files[name]), "sha256": hashlib.sha256(files[name]).hexdigest()}
        for name in (
            "usage_panel_api.py",
            "index.html",
            "app.js",
            "styles.css",
            "data.js",
        )
    }
    compile(files["usage_panel_api.py"], "usage_panel_api.py", "exec")

    package_data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]
    assert package_data["ecorex.control_plane.usage_panel_web"] == ["*.html", "*.js", "*.css"]


def test_failed_activation_restores_static_pointer_and_service_source(tmp_path: Path) -> None:
    deployer = _deployer()
    namespace = {"__name__": "usage_panel_release_test"}
    exec(deployer["_REMOTE_LIBRARY"], namespace)

    root = tmp_path / "usage-panel"
    releases = root / "releases"
    previous = releases / "previous"
    incoming = releases / (".incoming-" + "1" * 32)
    server = root / "server"
    previous.mkdir(parents=True)
    incoming.mkdir()
    server.mkdir()
    old_files = {
        "index.html": b"old-index",
        "app.js": b"old-app",
        "styles.css": b"old-css",
        "data.js": b"old-data",
        "usage_panel_api.py": b"old-api",
    }
    for name, payload in old_files.items():
        (previous / name).write_bytes(payload)
    old_server = b"old-server-api"
    (server / "usage_panel_api.py").write_bytes(old_server)
    os.symlink(previous, root / "current")

    source_sha = "a" * 40
    payloads = {
        "usage_panel_api.py": b'VERSION = "2.0.5"\n',
        "index.html": b"styles.css data.js app.js",
        "app.js": b"./api/data ./api/runtime-audit",
        "styles.css": b"new-css",
        "data.js": b"new-data",
    }
    inventory = {
        name: {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for name, payload in payloads.items()
    }
    manifest_bytes = json.dumps(
        {
            "schema_version": 1,
            "source_sha": source_sha,
            "version": "2.0.5",
            "projection_version": "projection-1",
            "inventory": inventory,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode() + b"\n"
    receipt_bytes = json.dumps(
        {
            "schema_version": 1,
            "status": "prepared",
            "source_sha": source_sha,
            "version": "2.0.5",
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode() + b"\n"
    for name, payload in {**payloads, "release-manifest.json": manifest_bytes, "release-receipt.json": receipt_bytes}.items():
        (incoming / name).write_bytes(payload)

    restarts = []

    def fail_once() -> None:
        restarts.append("restart")
        if len(restarts) == 1:
            raise RuntimeError("service_restart_failed")

    result = namespace["activate"](
        root=root,
        incoming_name=incoming.name,
        release_name="v2.0.5-aaaaaaaaaaaa-bbbbbbbbbbbb",
        expected_manifest_sha=hashlib.sha256(manifest_bytes).hexdigest(),
        expected_receipt_sha=hashlib.sha256(receipt_bytes).hexdigest(),
        expected_version="2.0.5",
        expected_projection="projection-1",
        restart=fail_once,
        verify=lambda: {"status": "passed"},
        service_active=lambda: True,
        health_check=lambda: {"ok": True, "version": "old"},
        lock_path=tmp_path / "deploy.lock",
    )

    assert result["status"] == "rolled_back"
    assert result["stage"] == "restart"
    assert result["rolled_back"] is True
    assert len(restarts) == 2
    assert (root / "current").resolve() == previous
    assert not (server / "usage_panel_api.py").is_symlink()
    assert (server / "usage_panel_api.py").read_bytes() == old_server
    backup = Path(result["backup"])
    assert (backup / "server" / "usage_panel_api.py").read_bytes() == old_server
    assert (backup / "static" / "index.html").read_bytes() == old_files["index.html"]

    release = releases / "v2.0.5-aaaaaaaaaaaa-bbbbbbbbbbbb"
    shutil.copytree(release, incoming)
    passed = namespace["activate"](
        root=root,
        incoming_name=incoming.name,
        release_name=release.name,
        expected_manifest_sha=hashlib.sha256(manifest_bytes).hexdigest(),
        expected_receipt_sha=hashlib.sha256(receipt_bytes).hexdigest(),
        expected_version="2.0.5",
        expected_projection="projection-1",
        restart=lambda: None,
        verify=lambda: {"status": "passed", "usage_audit_exact": True},
        service_active=lambda: True,
        health_check=lambda: {"ok": True, "version": "old"},
        lock_path=tmp_path / "deploy.lock",
    )
    assert passed["status"] == "passed"
    assert (root / "current").resolve() == release
    assert (server / "usage_panel_api.py").is_symlink()
    assert (server / "usage_panel_api.py").resolve() == release / "usage_panel_api.py"
    assert passed["inventory"] == inventory
    assert passed["verification"]["usage_audit_exact"] is True
