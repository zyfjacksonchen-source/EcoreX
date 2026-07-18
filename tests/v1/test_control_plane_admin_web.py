from __future__ import annotations

from base64 import b64decode
from datetime import UTC, datetime
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from ecorex import __version__
from ecorex.control_plane.admin_web import (
    AdminResumeAdapter,
    AdminResumeFacts,
    AdminWebAssetError,
    AdminWebAssets,
    ResumeStateProjection,
    create_admin_resume_router,
    create_admin_web_router,
)
from ecorex.control_plane.models import (
    CandidateProjection,
    DistributionProjection,
    KillSwitchProjection,
    RolloutProjection,
)


STATIC = (
    Path(__file__).resolve().parents[2]
    / "ecorex"
    / "control_plane"
    / "admin_web"
    / "static"
)


class _DomContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.buttons: list[dict[str, str | None]] = []
        self.inline_handlers: list[str] = []
        self.inline_scripts = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if "id" in values:
            self.ids.append(values["id"])
        if tag == "button":
            self.buttons.append(values)
        for name, _value in attrs:
            if name.casefold().startswith("on"):
                self.inline_handlers.append(name)
        if tag == "script" and not values.get("src"):
            self.inline_scripts += 1


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(create_admin_web_router())
    return app


def test_admin_web_is_content_addressed_allowlisted_and_security_headered() -> None:
    app = _app()
    client = TestClient(app)

    for path in ("/admin", "/admin/"):
        page = client.get(path)
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store, max-age=0"
        assert page.headers["pragma"] == "no-cache"
        assert page.headers["x-content-type-options"] == "nosniff"
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["referrer-policy"] == "no-referrer"
        assert page.headers["x-ecorex-product-version"] == __version__
        csp = page.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "script-src 'self'" in csp
        assert "style-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp
        assert "unsafe-eval" not in csp

    page = client.get("/admin/")
    matches = re.findall(
        r'(?:href|src)="(/admin/assets/admin\.([0-9a-f]{64})\.(css|js))" '
        r'integrity="sha256-([A-Za-z0-9+/=]+)"',
        page.text,
    )
    assert len(matches) == 2
    for asset_url, prefix, suffix, sri_value in matches:
        response = client.get(asset_url)
        assert response.status_code == 200
        digest = hashlib.sha256(response.content).hexdigest()
        assert digest.startswith(prefix)
        assert b64decode(sri_value) == bytes.fromhex(digest)
        assert (
            response.headers["cache-control"] == "public, max-age=31536000, immutable"
        )
        assert response.headers["etag"] == f'"sha256-{digest}"'
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert response.headers["content-type"].startswith(
            "text/css" if suffix == "css" else "text/javascript"
        )
        cached = client.get(
            asset_url, headers={"If-None-Match": response.headers["etag"]}
        )
        assert cached.status_code == 304
        assert cached.content == b""

    missing = client.get("/admin/assets/not-allowlisted.js")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store, max-age=0"
    assert "/admin" not in app.openapi()["paths"]


def test_asset_verifier_fails_closed_for_tamper_extra_files_and_bad_prefix(
    tmp_path,
) -> None:
    copied = tmp_path / "static"
    shutil.copytree(STATIC, copied)
    bundle = AdminWebAssets.load(copied)
    assert len(bundle.assets) == 2
    assert all(
        re.fullmatch(r"admin\.[0-9a-f]{64}\.(?:css|js)", name) for name in bundle.assets
    )
    with pytest.raises(AdminWebAssetError, match="URL prefix"):
        bundle.render_index("https://example.invalid/admin/assets")

    script = copied / "admin.js"
    script.write_bytes(script.read_bytes() + b"\n// tamper")
    with pytest.raises(AdminWebAssetError, match="digest mismatch"):
        AdminWebAssets.load(copied)

    shutil.rmtree(copied)
    shutil.copytree(STATIC, copied)
    (copied / "debug.map").write_text("{}", encoding="utf-8")
    with pytest.raises(AdminWebAssetError, match="allowlist"):
        AdminWebAssets.load(copied)

    with pytest.raises(ValueError, match="prefix"):
        create_admin_web_router(prefix="//unsafe")

    external = FastAPI()
    external.include_router(
        create_admin_web_router(
            external_asset_prefix="/ecorex-agent/admin/assets"
        )
    )
    external_html = TestClient(external).get("/admin/").text
    assert "/ecorex-agent/admin/assets/admin." in external_html
    assert '="/admin/assets/admin.' not in external_html


def test_asset_verifier_reads_signed_resources_through_zipimport(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "admin-resources.zip"
    source = (
        Path(__file__).resolve().parents[2]
        / "ecorex"
        / "control_plane"
        / "admin_web"
        / "assets.py"
    )
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("zipped_admin/__init__.py", b"")
        output.write(source, "zipped_admin/assets.py")
        for path in sorted(STATIC.iterdir()):
            output.write(path, f"zipped_admin/static/{path.name}")
    completed = subprocess.run(
        (
            sys.executable,
            "-I",
            "-S",
            "-c",
            (
                "import sys;"
                f"sys.path.insert(0,{str(archive)!r});"
                "from zipped_admin.assets import AdminWebAssets;"
                "bundle=AdminWebAssets.load();"
                "assert len(bundle.assets)==2;"
                "assert len(bundle.index_digest)==64"
            ),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def test_built_wheel_contains_and_loads_verified_admin_assets(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    wheel_directory = tmp_path / "wheel"
    wheel_directory.mkdir()
    built = subprocess.run(
        (
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_directory),
            str(root),
        ),
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, built.stderr.decode("utf-8", errors="replace")
    wheels = tuple(wheel_directory.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    expected = {
        f"ecorex/control_plane/admin_web/static/{path.name}"
        for path in STATIC.iterdir()
    }
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert expected <= names

    loaded = subprocess.run(
        (
            sys.executable,
            "-I",
            "-c",
            (
                "import sys;"
                f"sys.path.insert(0,{str(wheel)!r});"
                "from ecorex.control_plane.admin_web.assets import AdminWebAssets;"
                "bundle=AdminWebAssets.load();"
                "assert len(bundle.assets)==2;"
                "assert len(bundle.index_digest)==64"
            ),
        ),
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert loaded.returncode == 0, loaded.stderr.decode("utf-8", errors="replace")


def test_admin_dom_and_script_contract_are_csp_safe_and_ephemeral() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "admin.js").read_text(encoding="utf-8")
    css = (STATIC / "admin.css").read_text(encoding="utf-8")

    parser = _DomContractParser()
    parser.feed(html)
    assert not parser.inline_handlers
    assert parser.inline_scripts == 0
    assert len(parser.ids) == len(set(parser.ids))
    assert parser.buttons
    assert all(
        button.get("type") == "button" or button.get("type") == "submit"
        for button in parser.buttons
    )
    assert "<style" not in html.casefold()
    assert "<base" not in html.casefold()
    assert 'type="password"' in html
    assert 'autocomplete="off"' in html
    assert 'id="refresh-state-button"' in html
    assert 'id="device-login-button"' in html
    assert 'id="device-login-state"' in html
    assert 'id="manual-token-fallback"' in html
    assert "访问令牌和刷新令牌仅保留在当前页面内存" in html
    assert "全部必需发布门禁" in html
    assert "签名门禁包的只读投影" in html
    assert "activate" in html and "pause" in html and "halt" in html
    assert "stable" in html and "canary" in html
    assert all(label in html for label in (">用户<", ">用量<", ">模型<", ">发布<"))
    assert 'id="user-table-body"' in html
    assert 'id="model-table-body"' in html
    assert 'id="rollout-mode"' in html and 'value="full"' in html
    assert "测试并启用" in html
    assert "__ADMIN_CSS_SRI__" in html and "__ADMIN_JS_SRI__" in html

    forbidden_script = (
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "location.search",
        "URLSearchParams",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    )
    assert all(value not in script for value in forbidden_script)
    assert 'const API_BASE = "/api/v1/admin";' in script
    assert 'fetch(`${API_BASE}${path}${query}`' in script
    assert 'fetch("./api/' not in script
    assert 'fetch("api/' not in script
    assert "Authorization: `Bearer ${adminToken}`" in script
    assert 'redirect: "error"' in script
    assert 'cache: "no-store"' in script
    assert 'credentials: "same-origin"' in script
    assert "crypto.randomUUID" in script
    assert 'crypto.subtle.digest("SHA-256", bytes)' in script
    assert "manifest_sha256: manifestSha256" in script
    assert "const requestIds = new Map()" in script
    assert 'adminToken = ""' in script
    assert "beforeunload" in script
    assert 'client_id: "ecorex-admin-web"' in script
    assert 'grant_type: "refresh_token"' in script
    assert '"/v1/device/authorize"' in script
    assert '"/v1/device/token"' in script
    assert "const refreshPromise" not in script
    assert "let refreshPromise = null" in script
    assert "elements.deviceLoginButton.addEventListener" in script
    assert 'adminRefreshToken = ""' in script
    assert "showModal()" in script
    assert script.count("askConfirmation({") >= 5
    for contract in (
        'apiRequest("/releases"',
        "/publish",
        'apiRequest("/rollouts"',
        "/rollouts/${rolloutId}/${action}",
        'apiRequest("/rollbacks"',
        "/rollbacks/${rollbackId}/${action}",
        "/channels/${safeSegment(channel)}/${suffix}",
        'apiRequest("/resume")',
        'apiRequest("/distribution"',
        'apiRequest("/users"',
        'apiRequest("/usage/summary"',
        'apiRequest("/models"',
        "/test-and-activate",
        "/usage-adjustments",
    ):
        assert contract in script
    assert 'elements.modelApiKey.value = ""' in script
    assert "full ? 100" in script
    assert "full ? []" in script
    assert "/gates/${gate}" not in script
    assert "/gate-bundle" not in script
    assert ".gate-action" not in script
    assert "gate-status" not in script
    assert "gate-evidence" not in script
    assert "latest_candidate_id" in script and "latest_rollout_id" in script
    assert "candidates[0]" not in script and "rollouts[0]" not in script
    for mutation in (
        "create-candidate-button",
        "publish-button",
        "create-rollout-button",
        "data-rollout-action",
        "create-rollback-button",
        "data-rollback-action",
        "data-kill-action",
    ):
        assert mutation in html

    assert css.startswith("/* Hallmark · genre: modern-minimal")
    assert "· macrostructure: Workbench" in css.splitlines()[0]
    assert "transition: all" not in css
    assert not re.search(r"z-index:\s*[0-9]", css)
    assert not re.search(r"border-radius:\s*[0-9]", css)
    shadows = re.findall(r"box-shadow:\s*([^;]+);", css)
    assert shadows and all(value.strip().startswith("var(") for value in shadows)
    for line in css.splitlines():
        if "oklch(" in line:
            assert re.match(r"\s*--[a-z0-9-]+:\s*", line)
    assert "html,\nbody" in css and "overflow-x: clip" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (forced-colors: active)" in css


def _candidate(release_id: str) -> CandidateProjection:
    return CandidateProjection(
        release_id=release_id,
        version="1.0.0",
        build_digest="a" * 64,
        channel="stable",
        status="published",
        gates={"unit": "passed"},
        missing_gates=[],
    )


def _rollout(rollout_id: str, release_id: str) -> RolloutProjection:
    return RolloutProjection(
        rollout_id=rollout_id,
        release_id=release_id,
        channel="stable",
        status="active",
        percentage=10,
        target_organization_ids=[],
        target_account_ids=[],
        minimum_compatible_version=None,
        created_at="2026-07-10T08:00:00+00:00",
    )


def _resume_data() -> dict:
    return {
        "schema_version": 1,
        "candidates": [_candidate("release_new"), _candidate("release_selected")],
        "latest_candidate_id": "release_selected",
        "rollouts": [
            _rollout("rollout_new", "release_new"),
            _rollout("rollout_selected", "release_selected"),
        ],
        "latest_rollout_id": "rollout_selected",
        "channel_kill_switches": [
            KillSwitchProjection(
                channel="stable",
                halted_rollout_ids=[],
                kill_switch_active=False,
            ),
            KillSwitchProjection(
                channel="canary",
                halted_rollout_ids=["rollout_stopped"],
                kill_switch_active=True,
            ),
        ],
        "distribution": DistributionProjection(
            total_clients=2,
            versions={"0.3.0": 1, "1.0.0": 1},
            update_states={"idle": 1, "available": 1},
        ),
        "captured_at": datetime(2026, 7, 10, 8, 30, tzinfo=UTC),
    }


@pytest.mark.parametrize(
    "change,match",
    [
        ({"latest_candidate_id": "release_missing"}, "latest candidate"),
        ({"latest_rollout_id": None}, "latest rollout"),
        ({"captured_at": datetime(2026, 7, 10, 8, 30)}, "timezone-aware"),
        (
            {
                "channel_kill_switches": [
                    KillSwitchProjection(
                        channel="stable",
                        halted_rollout_ids=[],
                        kill_switch_active=False,
                    ),
                    KillSwitchProjection(
                        channel="stable", halted_rollout_ids=[], kill_switch_active=True
                    ),
                ]
            },
            "exactly one fact",
        ),
    ],
)
def test_resume_state_rejects_ambiguous_or_inconsistent_facts(change, match) -> None:
    payload = _resume_data()
    payload.update(change)
    with pytest.raises(ValidationError, match=match):
        ResumeStateProjection.model_validate(payload)

    duplicate = _resume_data()
    duplicate["candidates"] = [duplicate["candidates"][0], duplicate["candidates"][0]]
    duplicate["latest_candidate_id"] = "release_new"
    with pytest.raises(ValidationError, match="unique release IDs"):
        ResumeStateProjection.model_validate(duplicate)


def test_resume_adapter_preserves_atomic_facts_and_explicit_selection() -> None:
    payload = _resume_data()
    facts = AdminResumeFacts(
        candidates=tuple(payload["candidates"]),
        latest_candidate_id=payload["latest_candidate_id"],
        rollouts=tuple(payload["rollouts"]),
        latest_rollout_id=payload["latest_rollout_id"],
        channel_kill_switches=tuple(payload["channel_kill_switches"]),
        distribution=payload["distribution"],
        captured_at=payload["captured_at"],
    )
    projection = AdminResumeAdapter(lambda: facts).resume_state()

    assert [item.release_id for item in projection.candidates] == [
        "release_new",
        "release_selected",
    ]
    assert projection.latest_candidate_id == "release_selected"
    assert projection.latest_rollout_id == "rollout_selected"
    assert projection.captured_at.tzinfo is UTC


def test_resume_router_is_read_only_authenticated_and_no_store() -> None:
    class Provider:
        calls = 0

        def resume_state(self) -> ResumeStateProjection:
            self.calls += 1
            return ResumeStateProjection.model_validate(_resume_data())

    provider = Provider()

    def release_admin(request: Request) -> str:
        if request.headers.get("authorization") != "Bearer test-release-admin":
            raise HTTPException(
                status_code=401, detail="release administrator required"
            )
        return "release-admin"

    router = create_admin_resume_router(
        provider,
        authorization_dependency=release_admin,
    )
    assert len(router.routes) == 1
    assert router.routes[0].path == "/api/v1/admin/resume"
    assert router.routes[0].methods == {"GET"}

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    unauthorized = client.get("/api/v1/admin/resume")
    assert unauthorized.status_code == 401
    assert provider.calls == 0

    response = client.get(
        "/api/v1/admin/resume",
        headers={"Authorization": "Bearer test-release-admin"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json()["latest_candidate_id"] == "release_selected"
    assert response.json()["latest_rollout_id"] == "rollout_selected"
    assert provider.calls == 1

    mutation = client.post(
        "/api/v1/admin/resume",
        headers={"Authorization": "Bearer test-release-admin"},
    )
    assert mutation.status_code == 405
    assert provider.calls == 1

    with pytest.raises(TypeError, match="authorization"):
        create_admin_resume_router(provider, authorization_dependency=None)  # type: ignore[arg-type]
