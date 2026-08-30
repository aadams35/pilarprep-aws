from __future__ import annotations

import hashlib
import re
from typing import Iterable


IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field} must contain 1-64 lowercase letters, numbers, or hyphens"
        )
    return value


def slugify(value: str, fallback: str = "project") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    normalized = normalized[:64].rstrip("-")
    return normalized or fallback


def stable_identifier(prefix: str, parts: Iterable[str], length: int = 48) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def project_partition_key(scope: dict[str, str]) -> str:
    return (
        f"TENANT#{scope['tenantId']}|CLIENT#{scope['clientId']}|"
        f"PROJECT#{scope['projectId']}"
    )


def project_artifact_prefix(scope: dict[str, str]) -> str:
    return (
        f"tenants/{scope['tenantId']}/clients/{scope['clientId']}/"
        f"projects/{scope['projectId']}"
    )
