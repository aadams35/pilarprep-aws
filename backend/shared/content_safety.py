from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import boto3


MAX_GUARDRAIL_CHARS = 8_000
CONTENT_BOUNDARY = "\n\n[PilarPrep content boundary]\n\n"
CONTROL_FIELDS = {
    "action",
    "approvedBrief",
    "audienceRole",
    "briefRequest",
    "clientId",
    "confirmWrite",
    "contentType",
    "idempotencyKey",
    "identityType",
    "inputVersion",
    "meetingId",
    "mode",
    "modelId",
    "modelPreference",
    "phase",
    "projectId",
    "provider",
    "role",
    "scenarioId",
    "scopeToken",
    "sessionId",
    "source",
    "status",
    "tenantId",
    "traceId",
    "userId",
}
_CLIENTS: dict[str, Any] = {}
PRIVATE_MEETING_ACTIONS = {
    "meeting.process",
    "meeting.approve",
    "analyze_meeting",
}


def preserves_private_meeting_context(action: str) -> bool:
    return action in PRIVATE_MEETING_ACTIONS


class ContentSafetyError(RuntimeError):
    pass


class ContentSafetyConfigurationError(ContentSafetyError):
    pass


class ContentPolicyViolation(ContentSafetyError):
    pass


class GuardrailIntervention(ContentPolicyViolation):
    pass


def clear_client_cache() -> None:
    _CLIENTS.clear()


def aws_client(service_name: str) -> Any:
    if service_name not in _CLIENTS:
        _CLIENTS[service_name] = boto3.client(
            service_name,
            region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        )
    return _CLIENTS[service_name]


def enabled() -> bool:
    return os.getenv("CONTENT_SAFETY_ENABLED", "false").strip().lower() == "true"


def _guardrail_configuration() -> tuple[str, str]:
    identifier = os.getenv("BEDROCK_GUARDRAIL_ID", "").strip()
    version = os.getenv("BEDROCK_GUARDRAIL_VERSION", "").strip()
    if not identifier or not version:
        raise ContentSafetyConfigurationError(
            "Required AI content-safety controls are not configured"
        )
    return identifier, version


def _chunks(text: str, maximum: int) -> list[str]:
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        end = min(len(text), cursor + maximum)
        if end < len(text):
            split = max(text.rfind("\n", cursor, end), text.rfind(" ", cursor, end))
            if split > cursor:
                end = split + 1
        chunks.append(text[cursor:end])
        cursor = end
    return chunks


def _text_leaves(
    value: object,
    *,
    field_name: str = "",
    path: tuple[object, ...] = (),
) -> list[tuple[tuple[object, ...], str]]:
    if field_name in CONTROL_FIELDS:
        return []
    if isinstance(value, str):
        return [(path, value)] if value.strip() else []
    if isinstance(value, Mapping):
        leaves: list[tuple[tuple[object, ...], str]] = []
        for key, item in value.items():
            leaves.extend(
                _text_leaves(
                    item,
                    field_name=str(key),
                    path=(*path, key),
                )
            )
        return leaves
    if isinstance(value, (list, tuple)):
        leaves = []
        for index, item in enumerate(value):
            leaves.extend(_text_leaves(item, path=(*path, index)))
        return leaves
    return []


def _text_batches(
    leaves: list[tuple[tuple[object, ...], str]],
    maximum: int,
) -> list[
    tuple[
        str,
        list[tuple[tuple[object, ...], int, int, int]],
    ]
]:
    batches: list[
        tuple[str, list[tuple[tuple[object, ...], int, int, int]]]
    ] = []
    current = ""
    segments: list[tuple[tuple[object, ...], int, int, int]] = []

    def flush() -> None:
        nonlocal current, segments
        if current:
            batches.append((current, segments))
        current = ""
        segments = []

    for path, text in leaves:
        leaf_offset = 0
        for chunk in _chunks(text, maximum):
            separator = CONTENT_BOUNDARY if current else ""
            if current and len(current) + len(separator) + len(chunk) > maximum:
                flush()
                separator = ""
            current += separator
            document_begin = len(current)
            current += chunk
            segments.append((path, leaf_offset, document_begin, len(current)))
            leaf_offset += len(chunk)
    flush()
    return batches


def _apply_guardrail(texts: list[str], source: str) -> int:
    identifier, version = _guardrail_configuration()
    batches = _text_batches(
        [((index,), text) for index, text in enumerate(texts)],
        MAX_GUARDRAIL_CHARS,
    )
    for chunk, _segments in batches:
        response = aws_client("bedrock-runtime").apply_guardrail(
            guardrailIdentifier=identifier,
            guardrailVersion=version,
            source=source,
            content=[{"text": {"text": chunk}}],
        )
        action = str(response.get("action") or "")
        if action == "GUARDRAIL_INTERVENED":
            raise GuardrailIntervention(
                "Content did not pass the configured AI safety policy"
            )
        if action != "NONE":
            raise ContentSafetyError(
                "Guardrail returned an unknown policy result"
            )
    return len(batches)

def screen_payload(
    value: object,
    *,
    source: str,
    action: str,
    trace_id: str = "",
) -> tuple[object, dict[str, object]]:
    del trace_id
    normalized_source = source.strip().upper()
    if normalized_source not in {"INPUT", "OUTPUT"}:
        raise ValueError("Content-safety source must be INPUT or OUTPUT")
    if not enabled():
        return value, {
            "source": normalized_source,
            "policyResult": "disabled",
            "redactionCount": 0,
            "piiTypes": [],
            "piiMode": "disabled",
            "comprehendChunks": 0,
            "guardrailChunks": 0,
        }

    texts = [text for _path, text in _text_leaves(value)]
    guardrail_chunks = _apply_guardrail(texts, normalized_source)
    # Keep legacy diagnostics for saved packets without screening or rewriting PII.
    return value, {
        "source": normalized_source,
        "policyResult": "passed",
        "redactionCount": 0,
        "piiTypes": [],
        "piiMode": (
            "preserved-private-context"
            if preserves_private_meeting_context(action)
            else "disabled"
        ),
        "comprehendChunks": 0,
        "guardrailChunks": guardrail_chunks,
    }
