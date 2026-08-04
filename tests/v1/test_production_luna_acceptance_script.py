from pathlib import Path
import runpy
import sys


def _module() -> dict:
    scripts = Path(__file__).parents[2] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        return runpy.run_path(str(scripts / "run-v030-production-luna-acceptance.py"), run_name="luna_acceptance")
    finally:
        sys.path.remove(str(scripts))


def test_http_provider_requires_explicit_authorization() -> None:
    program = _module()["_program"]("run", "A" * 16, False)
    assert "ALLOW_HTTP_PROVIDER = False" in program
    assert 'parsed.scheme in ({"https", "http"} if ALLOW_HTTP_PROVIDER else {"https"})' in program


def test_http_provider_authorization_is_auditable() -> None:
    program = _module()["_program"]("run", "A" * 16, True)
    assert "ALLOW_HTTP_PROVIDER = True" in program
    assert '"provider_transport_authorization": "explicit-http"' in program
