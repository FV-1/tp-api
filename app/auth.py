"""Operator API keys and short-lived, sandbox-scoped agent tokens."""
import hmac
import os
import re
import time
from dataclasses import dataclass

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse

from . import audit

OPERATOR_KEY_ENV = "SANDBOXD_OPERATOR_KEY"
JWT_SECRET_ENV = "SANDBOXD_JWT_SECRET"
TOKEN_LIFETIME_SECONDS = 15 * 60
JWT_ISSUER = "sandboxd"
JWT_AUDIENCE = "sandboxd"
SANDBOX_PATH = re.compile(r"^/v1/sandboxes/([^/]+)(?:/|$)")


@dataclass(frozen=True)
class Principal:
    role: str
    subject: str


class AuthenticationError(Exception):
    def __init__(self, status: int, slug: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.slug = slug
        self.detail = detail


def _jwt_secret() -> str:
    secret = os.environ.get(JWT_SECRET_ENV, "")
    if len(secret.encode("utf-8")) < 32:
        raise AuthenticationError(
            503, "auth-not-configured",
            f"{JWT_SECRET_ENV} doit contenir au moins 32 octets",
        )
    return secret


def issue_agent_token(sandbox_id: str) -> dict[str, str | int]:
    secret = _jwt_secret()
    issued_at = int(time.time())
    token = jwt.encode(
        {
            "sub": sandbox_id,
            "role": "agent",
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "iat": issued_at,
            "exp": issued_at + TOKEN_LIFETIME_SECONDS,
        },
        secret,
        algorithm="HS256",
    )
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": TOKEN_LIFETIME_SECONDS,
    }


def _authenticate(request: Request) -> Principal:
    operator_key = request.headers.get("X-Operator-Key")
    if operator_key is not None:
        expected = os.environ.get(OPERATOR_KEY_ENV, "")
        if not expected:
            raise AuthenticationError(503, "auth-not-configured", f"{OPERATOR_KEY_ENV} n'est pas configurée")
        if hmac.compare_digest(operator_key.encode("utf-8"), expected.encode("utf-8")):
            return Principal("operator", "operator")
        raise AuthenticationError(401, "unauthorized", "clé opérateur invalide")

    authorization = request.headers.get("Authorization", "")
    scheme, separator, credentials = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not credentials:
        raise AuthenticationError(401, "unauthorized", "authentification requise")

    try:
        claims = jwt.decode(
            credentials,
            _jwt_secret(),
            algorithms=["HS256"],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": ["sub", "role", "iat", "exp", "iss", "aud"]},
        )
    except AuthenticationError:
        raise
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError(401, "unauthorized", "jeton agent invalide ou expiré") from exc

    if claims.get("role") != "agent" or not isinstance(claims.get("sub"), str):
        raise AuthenticationError(401, "unauthorized", "jeton agent invalide")
    return Principal("agent", claims["sub"])


def _operator_only(method: str, path: str) -> bool:
    segments = path.strip("/").split("/")
    if segments == ["v1", "sandboxes"]:
        return True
    if len(segments) == 4 and segments[:2] == ["v1", "sandboxes"] and segments[3] == "tokens":
        return True
    if len(segments) == 3 and segments[:2] == ["v1", "sandboxes"]:
        return method not in {"GET", "HEAD"}
    if len(segments) == 4 and segments[:2] == ["v1", "sandboxes"]:
        if segments[3] == "exec":
            return method != "POST"
        if segments[3] == "files":
            return method not in {"GET", "PUT"}
    return True


def _problem(request: Request, status: int, slug: str, detail: str, headers: dict | None = None) -> JSONResponse:
    body = {
        "type": f"/errors/{slug}",
        "title": {401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 503: "Service Unavailable"}[status],
        "status": status,
        "detail": detail,
        "instance": request.url.path,
    }
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)


def install(app) -> None:
    @app.middleware("http")
    async def authenticate_and_audit(request: Request, call_next):
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)

        principal = None
        try:
            principal = _authenticate(request)
        except AuthenticationError as exc:
            headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
            response = _problem(request, exc.status, exc.slug, exc.detail, headers)
            audit.record(request, response.status_code, None)
            return response

        request.state.principal = principal
        sandbox_match = SANDBOX_PATH.match(request.url.path)
        if principal.role == "agent" and sandbox_match and sandbox_match.group(1) != principal.subject:
            response = _problem(request, 404, "not-found", "sandbox introuvable")
            audit.record(request, response.status_code, principal)
            return response
        if principal.role == "agent" and _operator_only(request.method, request.url.path):
            response = _problem(request, 403, "forbidden", "cette action est réservée à l'opérateur")
            audit.record(request, response.status_code, principal)
            return response

        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            audit.record(request, status, principal)
