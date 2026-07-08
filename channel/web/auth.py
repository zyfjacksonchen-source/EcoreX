"""Authentication Web handlers."""

import datetime
import hmac
import json

import web

from common.log import logger


def _legacy_web_channel():
    from channel.web import web_channel

    return web_channel


class AuthCheckHandler:
    def GET(self):
        wc = _legacy_web_channel()
        web.header("Content-Type", "application/json; charset=utf-8")
        if wc._desktop_runtime_token_required():
            return json.dumps({
                "status": "success",
                "auth_required": True,
                "auth_type": "desktop-runtime-token",
                "authenticated": wc._desktop_runtime_token_matches(),
            })
        if not wc._is_password_enabled():
            return json.dumps({"status": "success", "auth_required": False})
        if wc._check_auth():
            token = web.cookies().get("cow_auth_token", "")
            email = wc._auth_token_email(token)
            payload = {"status": "success", "auth_required": True, "authenticated": True}
            if email:
                payload["session"] = AuthLoginHandler._session_payload(email)
            return json.dumps(payload, ensure_ascii=False)
        return json.dumps({"status": "success", "auth_required": True, "authenticated": False})


class AuthLoginHandler:
    @staticmethod
    def _session_payload(email: str = "") -> dict:
        wc = _legacy_web_channel()
        normalized_email = str(email or "").strip().lower()
        has_provided_identity = bool(normalized_email)
        name = normalized_email.split("@", 1)[0] if "@" in normalized_email else normalized_email
        if not normalized_email:
            normalized_email = "ecorex@ecorex.local"
            name = "EcoreX"
        if normalized_email in {"ecorex@ecorex.local", "local@ecorex.local"}:
            name = "EcoreX"
        return {
            "authenticated": True,
            "localFallback": not has_provided_identity,
            "authProvider": "web-password" if has_provided_identity else "local-fallback",
            "identitySource": "login-email" if has_provided_identity else "local-fallback",
            "deviceId": wc._web_device_id(),
            "expiresAt": (
                datetime.datetime.utcnow() + datetime.timedelta(seconds=wc._session_expire_seconds())
            ).isoformat(timespec="seconds") + "Z",
            "user": {
                "id": f"ecorex-password:{normalized_email}" if has_provided_identity else "ecorex-password",
                "name": name or "EcoreX",
                "email": normalized_email,
                "role": "user",
                "status": "active",
            },
            "quota": {"allowed": True},
        }

    def POST(self):
        wc = _legacy_web_channel()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            data = json.loads(web.data() or b"{}")
        except Exception:
            data = {}
        if not isinstance(data, dict):
            if not wc._is_password_enabled():
                data = {}
            else:
                return json.dumps({"status": "error", "message": "Invalid request"})
        if not wc._is_password_enabled():
            email = str(data.get("email", "") or "").strip()
            return json.dumps({
                "status": "success",
                "auth_required": False,
                "session": self._session_payload(email),
            }, ensure_ascii=False)
        email = str(data.get("email", "") or "").strip()
        password = str(data.get("password", "") or "")
        expected = wc._get_web_password()
        if not hmac.compare_digest(password, expected):
            logger.warning("[WebChannel] Invalid login attempt")
            return json.dumps({"status": "error", "message": "Wrong password"})
        token = wc._create_auth_token(email)
        web.setcookie(
            "cow_auth_token",
            token,
            expires=wc._session_expire_seconds(),
            path="/",
            httponly=True,
            samesite="Lax",
        )
        return json.dumps({"status": "success", "session": self._session_payload(email)}, ensure_ascii=False)


class AuthLogoutHandler:
    def POST(self):
        web.header("Content-Type", "application/json; charset=utf-8")
        web.setcookie("cow_auth_token", "", expires=-1, path="/")
        return json.dumps({"status": "success"})
