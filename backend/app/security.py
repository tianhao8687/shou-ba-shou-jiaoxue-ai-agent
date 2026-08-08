from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from typing import Any
from uuid import uuid4

from .schemas import AuthResponse, UserIdentity


class AuthenticationError(RuntimeError):
    pass


class AuthorizationError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(secret: str, header: dict[str, Any], claims: dict[str, Any]) -> str:
    encoded_header = _b64url_encode(canonical_json(header).encode("utf-8"))
    encoded_claims = _b64url_encode(canonical_json(claims).encode("utf-8"))
    material = f"{encoded_header}.{encoded_claims}"
    signature = hmac.new(secret.encode("utf-8"), material.encode("ascii"), hashlib.sha256).digest()
    return f"{material}.{_b64url_encode(signature)}"


def verify_signed_token(token: str, secret: str, audience: str) -> dict[str, Any]:
    try:
        encoded_header, encoded_claims, encoded_signature = token.split(".")
        material = f"{encoded_header}.{encoded_claims}"
        expected = hmac.new(secret.encode("utf-8"), material.encode("ascii"), hashlib.sha256).digest()
        actual = _b64url_decode(encoded_signature)
        if not hmac.compare_digest(expected, actual):
            raise AuthenticationError("token signature is invalid")
        header = json.loads(_b64url_decode(encoded_header))
        claims = json.loads(_b64url_decode(encoded_claims))
    except AuthenticationError:
        raise
    except Exception as exc:
        raise AuthenticationError("token is malformed") from exc
    if header.get("alg") != "HS256":
        raise AuthenticationError("token algorithm is not allowed")
    if claims.get("aud") != audience:
        raise AuthenticationError("token audience is invalid")
    now = int(datetime.now(timezone.utc).timestamp())
    if int(claims.get("exp", 0)) <= now:
        raise AuthenticationError("token has expired")
    if int(claims.get("nbf", 0)) > now + 5:
        raise AuthenticationError("token is not active yet")
    return claims


@dataclass(frozen=True)
class DemoAccount:
    username: str
    display_name: str
    roles: tuple[str, ...]
    tenant_id: str = "xm-ops"


DEMO_ACCOUNTS: dict[str, DemoAccount] = {
    "viewer@harbor.local": DemoAccount("viewer@harbor.local", "只读观察员", ("observer",)),
    "operator@harbor.local": DemoAccount(
        "operator@harbor.local", "值班操作员", ("observer", "operator")
    ),
    "lead@harbor.local": DemoAccount(
        "lead@harbor.local", "值班负责人", ("observer", "operator", "on-call-lead")
    ),
    "security@harbor.local": DemoAccount(
        "security@harbor.local", "安全值班", ("observer", "operator", "security-on-call")
    ),
    "approver@harbor.local": DemoAccount(
        "approver@harbor.local",
        "独立变更审批人",
        ("observer", "operator", "on-call-lead", "security-on-call"),
    ),
    "other@harbor.local": DemoAccount(
        "other@harbor.local",
        "其他租户观察员",
        ("observer",),
        "other-tenant",
    ),
    "admin@harbor.local": DemoAccount(
        "admin@harbor.local",
        "平台管理员",
        ("observer", "operator", "on-call-lead", "security-on-call", "admin"),
    ),
}


class AuthService:
    def __init__(self, secret: str, demo_password: str, ttl_minutes: int = 480) -> None:
        if len(secret) < 24:
            raise ValueError("auth signing secret must contain at least 24 characters")
        if len(demo_password) < 8:
            raise ValueError("demo password must contain at least 8 characters")
        self.secret = secret
        self.demo_password = demo_password
        self.ttl_minutes = ttl_minutes

    def _password_digest(self, username: str, password: str) -> bytes:
        salt = hashlib.sha256(f"harbor-auth:{username}:{self.secret}".encode("utf-8")).digest()
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)

    def authenticate(self, username: str, password: str) -> AuthResponse:
        account = DEMO_ACCOUNTS.get(username.lower())
        supplied = self._password_digest(username.lower(), password)
        expected = self._password_digest(username.lower(), self.demo_password)
        if account is None or not hmac.compare_digest(supplied, expected):
            # Always calculate the password digest before rejecting to reduce user enumeration signal.
            raise AuthenticationError("username or password is invalid")
        return self.issue(
            UserIdentity(
                username=account.username,
                display_name=account.display_name,
                roles=list(account.roles),
                tenant_id=account.tenant_id,
            )
        )

    def issue(self, user: UserIdentity) -> AuthResponse:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=self.ttl_minutes)
        claims = {
            "aud": "harbor-api",
            "sub": user.username,
            "name": user.display_name,
            "roles": sorted(set(user.roles)),
            "tenant_id": user.tenant_id,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": f"AUTH-{uuid4().hex}",
        }
        token = _sign(self.secret, {"alg": "HS256", "typ": "HARBOR-AUTH"}, claims)
        return AuthResponse(access_token=token, expires_at=expires_at, user=user)

    def verify(self, token: str) -> UserIdentity:
        claims = verify_signed_token(token, self.secret, "harbor-api")
        roles = claims.get("roles")
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            raise AuthenticationError("token roles are invalid")
        tenant_id = claims.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise AuthenticationError("token tenant is invalid")
        return UserIdentity(
            username=str(claims.get("sub", "")),
            display_name=str(claims.get("name", claims.get("sub", ""))),
            roles=roles,
            tenant_id=tenant_id,
        )


def require_any_role(user: UserIdentity, required_roles: list[str] | tuple[str, ...]) -> None:
    if "admin" in user.roles:
        return
    required = set(required_roles)
    if required and not required.intersection(user.roles):
        raise AuthorizationError(f"one of the roles is required: {sorted(required)}")


def require_all_roles(user: UserIdentity, required_roles: list[str] | tuple[str, ...]) -> None:
    if "admin" in user.roles:
        return
    missing = set(required_roles) - set(user.roles)
    if missing:
        raise AuthorizationError(f"all roles are required; missing: {sorted(missing)}")


@dataclass(frozen=True)
class CapabilityGrant:
    token: str
    jti: str
    expires_at: datetime


class CapabilityService:
    def __init__(self, secret: str, ttl_seconds: int = 90) -> None:
        if len(secret) < 24:
            raise ValueError("capability signing secret must contain at least 24 characters")
        self.secret = secret
        self.ttl_seconds = ttl_seconds

    def issue(
        self,
        *,
        run_id: str,
        plan_hash_value: str,
        step_id: str,
        tool_name: str,
        payload: dict[str, Any],
        actor: UserIdentity,
        required_role: str,
        max_uses: int = 1,
    ) -> CapabilityGrant:
        require_any_role(actor, [required_role])
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        jti = f"CAP-{uuid4().hex}"
        claims = {
            "aud": "harbor-tool",
            "sub": actor.username,
            "roles": sorted(set(actor.roles)),
            "tenant_id": actor.tenant_id,
            "required_role": required_role,
            "run_id": run_id,
            "plan_hash": plan_hash_value,
            "step_id": step_id,
            "tool": tool_name,
            "payload_hash": payload_hash(payload),
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": jti,
            "max_uses": max_uses,
        }
        token = _sign(self.secret, {"alg": "HS256", "typ": "HARBOR-CAP"}, claims)
        return CapabilityGrant(token=token, jti=jti, expires_at=expires_at)

    def verify(
        self,
        token: str,
        *,
        tool_name: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        plan_hash_value: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        claims = verify_signed_token(token, self.secret, "harbor-tool")
        if claims.get("tool") != tool_name:
            raise AuthorizationError("capability does not grant this tool")
        if claims.get("payload_hash") != payload_hash(payload):
            raise AuthorizationError("capability payload binding is invalid")
        if run_id is not None and claims.get("run_id") != run_id:
            raise AuthorizationError("capability does not grant this run")
        if plan_hash_value is not None and claims.get("plan_hash") != plan_hash_value:
            raise AuthorizationError("capability plan binding is invalid")
        if tenant_id is not None and claims.get("tenant_id") != tenant_id:
            raise AuthorizationError("capability tenant binding is invalid")
        roles = claims.get("roles", [])
        required_role = claims.get("required_role")
        if not claims.get("tenant_id"):
            raise AuthorizationError("capability tenant binding is missing")
        if required_role not in roles and "admin" not in roles:
            raise AuthorizationError("capability role binding is invalid")
        return claims
