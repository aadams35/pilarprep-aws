from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import boto3


MAX_COMPREHEND_CHARS = 4_500
MAX_GUARDRAIL_CHARS = 8_000
CONTENT_BOUNDARY = "\n\n[PilarPrep content boundary]\n\n"
PII_SCORE_THRESHOLD = float(os.getenv("PII_SCORE_THRESHOLD", "0.75"))

HIGH_RISK_PII_TYPES = {
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
    "BANK_ACCOUNT_NUMBER",
    "BANK_ROUTING",
    "CREDIT_DEBIT_CVV",
    "CREDIT_DEBIT_EXPIRY",
    "CREDIT_DEBIT_NUMBER",
    "PASSWORD",
    "PIN",
    "SSN",
}
PRESERVED_PII_TYPES = {"NAME"}
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


class HighRiskPiiViolation(ContentPolicyViolation):
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
    configured = os.getenv("CONTENT_SAFETY_ENABLED")
    if configured is None:
        configured = os.getenv("PII_SCREENING_ENABLED", "false")
    return configured.strip().lower() == "true"


def pii_screening_enabled() -> bool:
    return os.getenv("PII_SCREENING_ENABLED", "false").strip().lower() == "true"


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


def _placeholder(
    pii_type: str,
    raw_value: str,
    placeholders: dict[tuple[str, str], str],
) -> str:
    key = (pii_type, raw_value.casefold())
    if key not in placeholders:
        sequence = sum(1 for known_type, _ in placeholders if known_type == pii_type) + 1
        placeholders[key] = f"[PII:{pii_type}:{sequence:03d}]"
    return placeholders[key]


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


def _pii_findings(
    leaves: list[tuple[tuple[object, ...], str]],
    *,
    block_high_risk: bool,
) -> tuple[dict[tuple[object, ...], list[tuple[int, int, str]]], int]:
    findings: dict[tuple[object, ...], list[tuple[int, int, str]]] = {}
    batches = _text_batches(leaves, MAX_COMPREHEND_CHARS)
    for document, segments in batches:
        response = aws_client("comprehend").detect_pii_entities(
            Text=document,
            LanguageCode="en",
        )
        for entity in response.get("Entities", []):
            if not isinstance(entity, Mapping):
                continue
            pii_type = str(entity.get("Type") or "").upper()
            begin = int(entity.get("BeginOffset") or 0)
            end = int(entity.get("EndOffset") or 0)
            if (
                float(entity.get("Score") or 0) < PII_SCORE_THRESHOLD
                or not pii_type
                or pii_type in PRESERVED_PII_TYPES
                or begin < 0
                or end <= begin
            ):
                continue
            if end > len(document):
                raise ContentSafetyError("PII detector returned invalid offsets")
            if block_high_risk and pii_type in HIGH_RISK_PII_TYPES:
                raise HighRiskPiiViolation(
                    "High-risk sensitive information must be removed before processing"
                )
            for path, leaf_offset, document_begin, document_end in segments:
                if begin >= document_begin and end <= document_end:
                    findings.setdefault(path, []).append(
                        (
                            leaf_offset + begin - document_begin,
                            leaf_offset + end - document_begin,
                            pii_type,
                        )
                    )
                    break
    return findings, len(batches)


def _redact_findings(
    text: str,
    candidates: list[tuple[int, int, str]],
    placeholders: dict[tuple[str, str], str],
    pii_types: set[str],
) -> tuple[str, int]:
    selected: list[tuple[int, int, str]] = []
    for candidate in sorted(candidates, key=lambda item: (item[0], -item[1])):
        if candidate[0] < 0 or candidate[1] > len(text):
            raise ContentSafetyError("PII detector returned invalid offsets")
        if selected and candidate[0] < selected[-1][1]:
            continue
        selected.append(candidate)

    cursor = 0
    transformed: list[str] = []
    for begin, end, pii_type in selected:
        transformed.append(text[cursor:begin])
        transformed.append(_placeholder(pii_type, text[begin:end], placeholders))
        cursor = end
        pii_types.add(pii_type)
    transformed.append(text[cursor:])
    return "".join(transformed), len(selected)


def _replace_text_leaves(
    value: object,
    replacements: Mapping[tuple[object, ...], str],
    *,
    field_name: str = "",
    path: tuple[object, ...] = (),
) -> object:
    if field_name in CONTROL_FIELDS:
        return value
    if isinstance(value, str):
        return replacements.get(path, value)
    if isinstance(value, Mapping):
        return {
            key: _replace_text_leaves(
                item,
                replacements,
                field_name=str(key),
                path=(*path, key),
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = [
            _replace_text_leaves(item, replacements, path=(*path, index))
            for index, item in enumerate(value)
        ]
        return tuple(items) if isinstance(value, tuple) else items
    return value


def _sanitize(
    value: object,
    placeholders: dict[tuple[str, str], str],
    pii_types: set[str],
    text_sink: list[str],
    field_name: str = "",
    *,
    block_high_risk: bool,
) -> tuple[object, int, int]:
    leaves = _text_leaves(value, field_name=field_name)
    findings, chunks_processed = _pii_findings(
        leaves,
        block_high_risk=block_high_risk,
    )
    replacements: dict[tuple[object, ...], str] = {}
    redactions = 0
    for path, text in leaves:
        sanitized, count = _redact_findings(
            text,
            findings.get(path, []),
            placeholders,
            pii_types,
        )
        replacements[path] = sanitized
        redactions += count
        if sanitized.strip():
            text_sink.append(sanitized)
    return (
        _replace_text_leaves(value, replacements, field_name=field_name),
        redactions,
        chunks_processed,
    )

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

    placeholders: dict[tuple[str, str], str] = {}
    pii_types: set[str] = set()
    texts: list[str] = []
    if preserves_private_meeting_context(action):
        sanitized = value
        redactions = 0
        comprehend_chunks = 0
        texts.extend(text for _path, text in _text_leaves(value) if text.strip())
        pii_mode = "preserved-private-context"
    elif pii_screening_enabled():
        sanitized, redactions, comprehend_chunks = _sanitize(
            value,
            placeholders,
            pii_types,
            texts,
            block_high_risk=normalized_source == "INPUT",
        )
        pii_mode = "redacted"
    else:
        sanitized = value
        redactions = 0
        comprehend_chunks = 0
        texts.extend(text for _path, text in _text_leaves(value) if text.strip())
        pii_mode = "disabled"
    guardrail_chunks = _apply_guardrail(texts, normalized_source)
    return sanitized, {
        "source": normalized_source,
        "policyResult": "passed",
        "redactionCount": redactions,
        "piiTypes": sorted(pii_types),
        "piiMode": pii_mode,
        "comprehendChunks": comprehend_chunks,
        "guardrailChunks": guardrail_chunks,
    }
