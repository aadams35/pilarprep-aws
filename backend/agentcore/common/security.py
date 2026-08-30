from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Mapping

from .identifiers import require_identifier


SCOPE_FIELDS = ("tenantId", "clientId", "projectId", "userId", "sessionId")


class ScopeTokenError(ValueError):
    """Raised when an internal AgentCore scope token cannot be trusted."""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def validate_scope(scope: Mapping[str, Any]) -> dict[str, str]:
    validated: dict[str, str] = {}
    for field in SCOPE_FIELDS:
        validated[field] = require_identifier(scope.get(field), field)
    return validated


def sign_scope_token(
    secret: str,
    scope: Mapping[str, Any],
    *,
    ttl_seconds: int = 300,
    now: int | None = None,
) -> str:
    if len(secret) < 32:
        raise ScopeTokenError("Scope signing secret is not sufficiently strong")

    issued_at = int(time.time() if now is None else now)
    payload: dict[str, Any] = {
        **validate_scope(scope),
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "v": 1,
    }
    header = {"alg": "HS256", "typ": "PPSCOPE", "v": 1}
    signing_input = f"{_b64encode(_json_bytes(header))}.{_b64encode(_json_bytes(payload))}"
    signature = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64encode(signature)}"


def verify_scope_token(
    token: object,
    secret: str,
    *,
    now: int | None = None,
    clock_skew_seconds: int = 30,
) -> dict[str, str]:
    if not isinstance(token, str):
        raise ScopeTokenError("Missing AgentCore scope token")

    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        signing_input = f"{encoded_header}.{encoded_payload}"
        expected = hmac.new(
            secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
        ).digest()
        supplied = _b64decode(encoded_signature)
        if not hmac.compare_digest(expected, supplied):
            raise ScopeTokenError("Invalid AgentCore scope token signature")

        header = json.loads(_b64decode(encoded_header))
        payload = json.loads(_b64decode(encoded_payload))
    except ScopeTokenError:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ScopeTokenError("Malformed AgentCore scope token") from exc

    if header != {"alg": "HS256", "typ": "PPSCOPE", "v": 1}:
        raise ScopeTokenError("Unsupported AgentCore scope token")

    current = int(time.time() if now is None else now)
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        raise ScopeTokenError("AgentCore scope token is missing timestamps")
    if issued_at > current + clock_skew_seconds:
        raise ScopeTokenError("AgentCore scope token is not active")
    if expires_at < current - clock_skew_seconds:
        raise ScopeTokenError("AgentCore scope token has expired")

    return validate_scope(payload)


def assert_event_scope(event: Mapping[str, Any], token_scope: Mapping[str, str]) -> None:
    for field in ("tenantId", "clientId", "projectId"):
        if event.get(field) != token_scope[field]:
            raise ScopeTokenError(f"{field} does not match the authorized project scope")
