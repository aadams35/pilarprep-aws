from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from botocore.exceptions import ClientError

from shared import content_safety

from pipeline.state import (
    ARTIFACT_BUCKET,
    JOB_QUEUE_URL,
    JOB_TTL_SECONDS,
    PROJECT_TABLE,
    aws_client,
    client_directory_key,
    deserialize_item,
    dynamodb_client_request_token,
    idempotency_key,
    job_key,
    job_object_prefix,
    metric,
    now_epoch,
    now_iso,
    project_artifact_prefix,
    project_partition_key,
    require_identifier,
    s3_encryption_args,
    s3_artifact_args,
    serialize,
    stable_identifier,
)


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

AGENT_RUNTIME_ARN = os.getenv("AGENT_RUNTIME_ARN", "")
SCOPE_SECRET_ARN = os.getenv("SCOPE_SECRET_ARN", "")
KNOWLEDGE_BASE_ID = os.getenv("KNOWLEDGE_BASE_ID", "")
WORKER_TIMEOUT_SECONDS = int(os.getenv("WORKER_TIMEOUT_SECONDS", "180"))
MAX_RECEIVE_COUNT = int(os.getenv("MAX_RECEIVE_COUNT", "3"))
RETRY_VISIBILITY_SECONDS = int(os.getenv("RETRY_VISIBILITY_SECONDS", "5"))
_SCOPE_SECRET: str | None = None
_BRIEF_APP: Any | None = None
_MEETING_APP: Any | None = None
_EVIDENCE_APP: Any | None = None
_HANDOFF_TOOLS_APP: Any | None = None

LEGACY_BLUE_MESA_ADDITIONAL_DIRECTION = (
    "Treat BlueMesa as an existing AWS customer. Make payroll integration, "
    "mixed API and encrypted-file interfaces, idempotency, reconciliation, "
    "data privacy, retention, partner certification, cutover, and recovery "
    "evidence explicit. The existing ledger replacement is out of scope."
)
CURRENT_BLUE_MESA_ADDITIONAL_DIRECTION = (
    "BlueMesa is an existing AWS customer. The engagement focuses on payroll "
    "integration across mixed API and encrypted-file interfaces, including "
    "idempotency, reconciliation, data privacy, retention, partner "
    "certification, cutover, and recovery evidence. Replacing the existing "
    "ledger is outside scope."
)


class NonRetryableJobError(ValueError):
    """User-correctable job conflict that should not be retried by SQS."""


def _normalize_legacy_demo_context(
    scope: Mapping[str, str],
    payload: Mapping[str, Any],
    *,
    action: str,
) -> dict[str, Any]:
    normalized = dict(payload)
    if (
        scope.get("clientId") == "bluemesa-payments"
        and scope.get("projectId") == "bluemesa-payments"
        and normalized.get("additionalDirection")
        == LEGACY_BLUE_MESA_ADDITIONAL_DIRECTION
    ):
        normalized["additionalDirection"] = CURRENT_BLUE_MESA_ADDITIONAL_DIRECTION
        metric("LegacyDemoContextNormalized", Action=action)
    return normalized


def _elapsed_since_iso_ms(value: object) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        started = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))


def _screen_ai_payload(
    value: object,
    *,
    source: str,
    action: str,
    trace_id: str,
) -> tuple[object, dict[str, object]]:
    try:
        sanitized, diagnostics = content_safety.screen_payload(
            value,
            source=source,
            action=action,
            trace_id=trace_id,
        )
    except content_safety.GuardrailIntervention as exc:
        metric(f"{source.title()}GuardrailInterventions", Action=action)
        raise NonRetryableJobError(
            "PilarPrep could not process part of the supplied content. Describe "
            "customer facts and desired outcomes without instructions to ignore, "
            "override, or reveal AI behavior."
        ) from exc
    except content_safety.ContentSafetyConfigurationError as exc:
        metric("ContentSafetyConfigurationErrors", Action=action, Source=source)
        raise NonRetryableJobError(
            "AI content-safety controls are unavailable."
        ) from exc
    return sanitized, diagnostics


def _client_error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code") or "")


def _brief_module():
    global _BRIEF_APP
    if _BRIEF_APP is None:
        from bedrock import brief_generator as app

        _BRIEF_APP = app
    return _BRIEF_APP


def _meeting_module():
    global _MEETING_APP
    if _MEETING_APP is None:
        from pipeline import meeting

        _MEETING_APP = meeting
    return _MEETING_APP


def _evidence_module():
    global _EVIDENCE_APP
    if _EVIDENCE_APP is None:
        from pipeline import evidence

        _EVIDENCE_APP = evidence
    return _EVIDENCE_APP


def _handoff_tools_module():
    global _HANDOFF_TOOLS_APP
    if _HANDOFF_TOOLS_APP is None:
        from agentcore.tools import handler as app

        _HANDOFF_TOOLS_APP = app
    return _HANDOFF_TOOLS_APP



def _load_secret() -> str:
    global _SCOPE_SECRET
    if _SCOPE_SECRET:
        return _SCOPE_SECRET
    if not SCOPE_SECRET_ARN:
        raise RuntimeError("AgentCore scope signing is not configured")
    value = aws_client("secretsmanager").get_secret_value(
        SecretId=SCOPE_SECRET_ARN
    ).get("SecretString")
    if not isinstance(value, str) or len(value) < 32:
        raise RuntimeError("AgentCore scope signing secret is unavailable")
    _SCOPE_SECRET = value
    return value


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _tenant_hash(scope: Mapping[str, str]) -> str:
    tenant_id = str(scope.get("tenantId") or "unknown")
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:12]


def _scope_token(scope: Mapping[str, str]) -> str:
    issued_at = int(time.time())
    payload = {
        **{
            field: scope[field]
            for field in (
                "tenantId",
                "clientId",
                "projectId",
                "userId",
                "sessionId",
            )
        },
        "iat": issued_at,
        "exp": issued_at + 600,
        "v": 1,
    }
    header = {"alg": "HS256", "typ": "PPSCOPE", "v": 1}
    encoded_header = _b64(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    encoded_payload = _b64(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{encoded_header}.{encoded_payload}"
    signature = hmac.new(
        _load_secret().encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{_b64(signature)}"


def _scope(message: Mapping[str, Any]) -> dict[str, str]:
    return {
        field: require_identifier(message.get(field), field)
        for field in (
            "tenantId",
            "clientId",
            "projectId",
            "userId",
            "sessionId",
        )
    }


def _job_item(scope: Mapping[str, str], job_id: str) -> dict[str, Any]:
    item = aws_client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key=job_key(scope, job_id),
        ConsistentRead=True,
    ).get("Item")
    return deserialize_item(item)


def _claim_job(scope: Mapping[str, str], job_id: str, receive_count: int) -> bool:
    lease = now_epoch() + WORKER_TIMEOUT_SECONDS + 60
    try:
        aws_client("dynamodb").update_item(
            TableName=PROJECT_TABLE,
            Key=job_key(scope, job_id),
            UpdateExpression=(
                "SET #status = :running, updatedAt = :updatedAt, "
                "retryCount = :retryCount, leaseExpiresAt = :lease"
            ),
            ConditionExpression=(
                "#status = :queued OR "
                "(#status IN (:running, :validating, :saving) "
                "AND leaseExpiresAt < :now)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":queued": {"S": "queued"},
                ":running": {"S": "running"},
                ":validating": {"S": "validating"},
                ":saving": {"S": "saving"},
                ":updatedAt": {"S": now_iso()},
                ":retryCount": {"N": str(max(0, receive_count - 1))},
                ":lease": {"N": str(lease)},
                ":now": {"N": str(now_epoch())},
            },
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        existing = _job_item(scope, job_id)
        if not existing:
            metric(
                "OrphanedQueueMessages",
                Action="unknown",
            )
            return False
        if existing.get("status") in {
            "complete",
            "failed",
            "running",
            "validating",
            "saving",
            "waiting_for_scan",
            "transcribing",
            "screening",
            "analyzing",
            "review-ready",
            "approved",
        }:
            metric(
                "DuplicateDeliveries",
                Action=str(existing.get("action") or "unknown"),
            )
            return False
        raise


def _set_job_phase(
    scope: Mapping[str, str], job_id: str, phase: str
) -> None:
    if phase not in {"validating", "saving"}:
        raise ValueError("Unsupported active job phase")
    aws_client("dynamodb").update_item(
        TableName=PROJECT_TABLE,
        Key=job_key(scope, job_id),
        UpdateExpression=(
            "SET #status = :phase, phase = :phase, updatedAt = :updatedAt"
        ),
        ConditionExpression="#status IN (:running, :validating, :saving)",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":phase": {"S": phase},
            ":running": {"S": "running"},
            ":validating": {"S": "validating"},
            ":saving": {"S": "saving"},
            ":updatedAt": {"S": now_iso()},
        },
    )


def _load_input(
    message: Mapping[str, Any], scope: Mapping[str, str]
) -> dict[str, Any]:
    input_key = str(message.get("inputKey") or "")
    expected_prefix = f"{job_object_prefix(scope, str(message['jobId']))}/"
    if not input_key.startswith(expected_prefix):
        raise PermissionError("Job input pointer is outside the authorized scope")
    body = aws_client("s3").get_object(
        Bucket=ARTIFACT_BUCKET, Key=input_key
    )["Body"].read()
    document = json.loads(body.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Job input is invalid")
    if document.get("inputVersion") != message.get("inputVersion"):
        raise ValueError("Job input version does not match its queue pointer")
    if document.get("action") != message.get("action"):
        raise ValueError("Job action does not match its queue pointer")
    stored_scope = document.get("scope")
    if not isinstance(stored_scope, Mapping) or any(
        stored_scope.get(field) != scope[field]
        for field in (
            "tenantId",
            "clientId",
            "projectId",
            "userId",
            "sessionId",
        )
    ):
        raise PermissionError("Job input scope is invalid")
    return document


def _purge_noncurrent_versions(
    s3: Any, prefix: str, keep_versions: set[tuple[str, str]], *, target_keys: set[str] | None = None
) -> None:
    if target_keys is None:
        target_keys = {key for key, _version_id in keep_versions}
    key_marker: str | None = None
    version_marker: str | None = None
    while True:
        request: dict[str, Any] = {"Bucket": ARTIFACT_BUCKET, "Prefix": prefix}
        if key_marker:
            request["KeyMarker"] = key_marker
        if version_marker:
            request["VersionIdMarker"] = version_marker
        page = s3.list_object_versions(**request)
        objects = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for collection in (
                page.get("Versions", []),
                page.get("DeleteMarkers", []),
            )
            for item in collection
            if item.get("Key")
            and item.get("VersionId")
            and item["Key"] in target_keys
            and (item["Key"], item["VersionId"]) not in keep_versions
        ]
        if objects:
            deleted = s3.delete_objects(
                Bucket=ARTIFACT_BUCKET,
                Delete={"Objects": objects, "Quiet": True},
            ) or {}
            if deleted.get("Errors"):
                raise RuntimeError("Some superseded artifact versions could not be deleted")
        if not page.get("IsTruncated"):
            return
        key_marker = page.get("NextKeyMarker")
        version_marker = page.get("NextVersionIdMarker")


def _cleanup_replaced_artifacts(scope: Mapping[str, str], old_keys: list[str], keep_keys: set[str]) -> None:
    obsolete = {key for key in old_keys if key and key not in keep_keys}
    if not obsolete:
        return
    root = project_artifact_prefix(scope)
    allowed = (f"{root}/brief/draft/", f"{root}/handoff/")
    try:
        if any(not key.startswith(allowed) or not key.endswith(("/latest.json", "/latest.docx")) for key in obsolete):
            raise ValueError("Artifact cleanup is outside the mutable packet scope")
        s3 = aws_client("s3")
        for key in obsolete:
            _purge_noncurrent_versions(s3, key, set(), target_keys={key})
    except Exception:
        # Persistence has already committed; cleanup must never turn that success into a retry.
        LOGGER.warning("Superseded artifact cleanup needs attention")
        metric("ArtifactCleanupFailures")


def _artifact_download_filename(company: object, artifact_type: str, version: object) -> str:
    clean_company = " ".join(str(company or "Client").split())
    clean_company = "".join(
        char for char in clean_company if char.isascii() and (char.isalnum() or char in " ._-")
    ).strip(" ._-") or "Client"
    clean_type = "Handoff" if artifact_type == "handoff" else "Brief"
    try:
        clean_version = int(version)
    except (TypeError, ValueError):
        clean_version = 1
    return f"{clean_company} - {clean_type} - v{max(1, clean_version)}.docx"


def _content_disposition(filename: str) -> str:
    safe_name = filename.replace("\\", "").replace('"', "")
    return f'attachment; filename="{safe_name}"'


def _write_packet_pair(
    scope: Mapping[str, str],
    prefix: str,
    document: Mapping[str, Any],
    docx_bytes: bytes,
    *,
    download_filename: str,
) -> tuple[str, str, str]:
    json_key = f"{prefix}latest.json"
    docx_key = f"{prefix}latest.docx"
    s3 = aws_client("s3")
    json_result = s3.put_object(
        Bucket=ARTIFACT_BUCKET,
        Key=json_key,
        Body=json.dumps(document, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
        **s3_artifact_args(scope),
    )
    docx_result = s3.put_object(
        Bucket=ARTIFACT_BUCKET,
        Key=docx_key,
        Body=docx_bytes,
        ContentType=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        ContentDisposition=_content_disposition(download_filename),
        **s3_artifact_args(scope),
    )
    keep_versions = {
        (json_key, str(json_result.get("VersionId"))),
        (docx_key, str(docx_result.get("VersionId"))),
    }
    keep_versions = {
        pair
        for pair in keep_versions
        if pair[1] and pair[1] != "None" and pair[1] != "null"
    }
    if len(keep_versions) == 2:
        _purge_noncurrent_versions(s3, prefix, keep_versions)
    download_url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": ARTIFACT_BUCKET,
            "Key": docx_key,
            "ResponseContentDisposition": _content_disposition(download_filename),
        },
        ExpiresIn=3600,
    )
    return json_key, docx_key, download_url


def _put_immutable_object(
    s3: Any,
    scope: Mapping[str, str],
    *,
    key: str,
    body: bytes,
    content_type: str,
    content_disposition: str = "",
) -> str:
    digest = hashlib.sha256(body).hexdigest()
    request: dict[str, Any] = {
        "Bucket": ARTIFACT_BUCKET,
        "Key": key,
        "Body": body,
        "ContentType": content_type,
        "IfNoneMatch": "*",
        "Metadata": {"content-sha256": digest},
        **s3_artifact_args(scope),
    }
    if content_disposition:
        request["ContentDisposition"] = content_disposition
    try:
        s3.put_object(**request)
    except ClientError as exc:
        if _client_error_code(exc) not in {
            "PreconditionFailed",
            "ConditionalRequestConflict",
        }:
            raise
        existing = s3.get_object(Bucket=ARTIFACT_BUCKET, Key=key)["Body"].read()
        if hashlib.sha256(existing).hexdigest() != digest:
            raise NonRetryableJobError(
                "An immutable approved artifact already exists with different content"
            ) from exc
    return digest


def _write_approved_packet_pair(
    scope: Mapping[str, str],
    version: int,
    document: Mapping[str, Any],
    docx_bytes: bytes,
    *,
    download_filename: str,
) -> tuple[str, str, str, str, str]:
    prefix = (
        f"{project_artifact_prefix(scope)}/brief/approved/"
        f"v{version:06d}/"
    )
    json_key = f"{prefix}packet.json"
    docx_key = f"{prefix}packet.docx"
    s3 = aws_client("s3")
    json_body = json.dumps(
        document, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    json_digest = _put_immutable_object(
        s3,
        scope,
        key=json_key,
        body=json_body,
        content_type="application/json",
    )
    docx_digest = _put_immutable_object(
        s3,
        scope,
        key=docx_key,
        body=docx_bytes,
        content_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        content_disposition=_content_disposition(download_filename),
    )
    download_url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": ARTIFACT_BUCKET,
            "Key": docx_key,
            "ResponseContentDisposition": _content_disposition(download_filename),
        },
        ExpiresIn=3600,
    )
    return json_key, docx_key, download_url, json_digest, docx_digest


def _brief_latest_key(scope: Mapping[str, str]) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": "BRIEF#LATEST"},
    }


def _current_draft_response(
    scope: Mapping[str, str], latest: Mapping[str, Any]
) -> dict[str, Any]:
    draft_key = str(latest.get("draftArtifactKey") or "")
    expected_prefix = f"{project_artifact_prefix(scope)}/brief/draft/"
    if not draft_key:
        raise NonRetryableJobError(
            "The current brief draft could not be loaded; generate the brief again."
        )
    if not draft_key.startswith(expected_prefix):
        raise PermissionError("Draft brief is outside the authorized scope")
    document = json.loads(
        aws_client("s3")
        .get_object(Bucket=ARTIFACT_BUCKET, Key=draft_key)["Body"]
        .read()
        .decode("utf-8")
    )
    if not isinstance(document, dict):
        raise NonRetryableJobError("The current brief draft is invalid")
    if int(document.get("packetVersion") or 0) != int(
        latest.get("packetVersion") or 0
    ):
        raise NonRetryableJobError(
            "The current brief draft version is inconsistent; reload the latest packet."
        )
    response = document.get("response")
    if not isinstance(response, dict):
        raise NonRetryableJobError("The current brief draft is invalid")
    return response


def _write_brief_draft(
    scope: Mapping[str, str],
    payload: Mapping[str, Any],
    generated: dict[str, Any],
    *,
    action: str,
    job_id: str,
    input_version: str,
) -> dict[str, Any]:
    generated_metadata = generated.setdefault("metadata", {})
    if not isinstance(generated_metadata, dict):
        generated_metadata = {}
        generated["metadata"] = generated_metadata

    current_packet_version: int | None = None
    if action == "brief.generate":
        latest = _brief_latest(scope)
        raw_current_version = latest.get("packetVersion")
        if raw_current_version is not None:
            current_packet_version = int(raw_current_version)
        approved_version = int(latest.get("approvedPacketVersion") or 0)
        version_floor = max(current_packet_version or 0, approved_version)
        packet_version = version_floor + 1 if version_floor else 1
    elif action == "brief.refine":
        packet_version = int(payload.get("baseBriefVersion") or 0) + 1
    else:
        raise NonRetryableJobError(f"Unsupported brief action: {action}")
    generated_metadata["packetVersion"] = packet_version

    timestamp = now_iso()
    # Competing writes must not share objects before the conditional latest-pointer update.
    prefix = f"{project_artifact_prefix(scope)}/brief/draft/{require_identifier(job_id, 'jobId')}/{uuid4().hex}/"
    stored_request = {
        key: value
        for key, value in payload.items()
        if key
        not in {"previousBrief", "approvedBrief", "_pipelineManagedPersistence"}
    }
    document = {
        "scope": {
            "tenantId": scope["tenantId"],
            "clientId": scope["clientId"],
            "projectId": scope["projectId"],
        },
        "approvalStatus": "stale" if action == "brief.refine" else "draft",
        "request": stored_request,
        "response": generated,
        "storedAt": timestamp,
        "packetVersion": packet_version,
        "sourceJobId": job_id,
        "inputVersion": input_version,
    }
    metadata = {
        "projectId": scope["projectId"],
        "clientId": scope["clientId"],
        "packetVersion": packet_version,
        "artifactRetention": "latest-only",
    }
    docx = _brief_module()._brief_docx_bytes(dict(payload), generated, metadata)
    artifact_key, docx_key, download_url = _write_packet_pair(
        scope,
        prefix,
        document,
        docx,
        download_filename=_artifact_download_filename(
            payload.get("company"), "brief", packet_version
        ),
    )

    values = {
        ":entity": {"S": "BRIEF_LATEST"},
        ":company": {"S": str(payload.get("company") or "")},
        ":industry": {"S": str(payload.get("industry") or "")},
        ":meetingType": {"S": str(payload.get("meetingType") or "")},
        ":packetVersion": {"N": str(packet_version)},
        ":draftArtifactKey": {"S": artifact_key},
        ":draftDocxArtifactKey": {"S": docx_key},
        ":approvalStatus": {
            "S": "stale" if action == "brief.refine" else "draft"
        },
        ":updatedAt": {"S": timestamp},
        ":jobId": {"S": job_id},
        ":precallStatus": {"S": "stale" if action == "brief.refine" else "idle"},
    }
    request: dict[str, Any] = {
        "TableName": PROJECT_TABLE,
        "Key": _brief_latest_key(scope),
        "UpdateExpression": (
            "SET entityType = :entity, company = :company, industry = :industry, "
            "meetingType = :meetingType, packetVersion = :packetVersion, "
            "draftArtifactKey = :draftArtifactKey, "
            "draftDocxArtifactKey = :draftDocxArtifactKey, "
            "approvalStatus = :approvalStatus, updatedAt = :updatedAt, "
            "sourceJobId = :jobId, precallHandoffStatus = :precallStatus"
        ),
        "ExpressionAttributeValues": values,
        "ReturnValues": "ALL_OLD",
    }
    if action == "brief.generate":
        if current_packet_version is None:
            request["ConditionExpression"] = "attribute_not_exists(packetVersion)"
        else:
            request["ConditionExpression"] = "packetVersion = :baseVersion"
            values[":baseVersion"] = {"N": str(current_packet_version)}
    elif action == "brief.refine":
        request["ConditionExpression"] = (
            "attribute_not_exists(packetVersion) OR packetVersion = :baseVersion"
        )
        values[":baseVersion"] = {"N": str(payload.get("baseBriefVersion"))}
    try:
        saved = aws_client("dynamodb").update_item(**request) or {}
    except ClientError as exc:
        if _client_error_code(exc) == "ConditionalCheckFailedException":
            _cleanup_replaced_artifacts(scope, [artifact_key, docx_key], set())
            if action == "brief.refine":
                message = (
                    "The brief changed before refinement; reload the latest packet "
                    "and apply feedback again."
                )
            else:
                message = (
                    "Another brief generation completed first; reload the latest "
                    "packet before generating again."
                )
            raise NonRetryableJobError(message) from exc
        raise
    previous = deserialize_item(saved.get("Attributes"))
    _cleanup_replaced_artifacts(scope, [str(previous.get("draftArtifactKey") or ""), str(previous.get("draftDocxArtifactKey") or "")], {artifact_key, docx_key})
    generated.setdefault("metadata", {}).update(
        {
            "projectId": scope["projectId"],
            "clientId": scope["clientId"],
            "artifactKey": artifact_key,
            "docxArtifactKey": docx_key,
            "docxDownloadUrl": download_url,
            "artifactRetention": "latest-only",
            "stateKey": "BRIEF#LATEST",
            "packetVersion": packet_version,
            "approvalStatus": (
                "stale" if action == "brief.refine" else "draft"
            ),
        }
    )
    return generated


def _run_brief(
    scope: Mapping[str, str], document: Mapping[str, Any], job_id: str
) -> dict[str, Any]:
    action = str(document["action"])
    payload = dict(document["input"])
    payload.update(
        {
            "mode": "prebrief",
            "tenantId": scope["tenantId"],
            "clientId": scope["clientId"],
            "projectId": scope["projectId"],
            "_pipelineManagedPersistence": True,
        }
    )
    payload = _normalize_legacy_demo_context(scope, payload, action=action)
    latest: dict[str, Any] = {}
    authoritative_previous: dict[str, Any] | None = None
    if action == "brief.refine":
        latest = _brief_latest(scope)
        current_version = latest.get("packetVersion")
        base_version = payload.get("baseBriefVersion")
        if current_version is not None and int(current_version) != int(
            base_version
        ):
            raise NonRetryableJobError(
                "The brief changed before refinement; reload the latest packet and apply feedback again."
            )
        if current_version is not None:
            authoritative_previous = _current_draft_response(scope, latest)
            payload.pop("previousBrief", None)
    screened_payload, input_safety = _screen_ai_payload(
        payload,
        source="INPUT",
        action=action,
        trace_id=job_id,
    )
    if not isinstance(screened_payload, Mapping):
        raise NonRetryableJobError("The normalized brief input is invalid")
    payload = dict(screened_payload)
    if authoritative_previous is not None:
        payload["previousBrief"] = authoritative_previous
    brief_app = _brief_module()
    validation_error = brief_app._validate_brief_payload(payload)
    if validation_error:
        raise NonRetryableJobError(validation_error)

    retrieval_terms = [
        payload.get("company"),
        payload.get("industry"),
        payload.get("meetingType"),
        payload.get("context"),
        payload.get("companyValues"),
        payload.get("additionalDirection"),
        payload.get("meetingNotes"),
        payload.get("feedbackNotes"),
        " ".join(str(item) for item in payload.get("pillars", [])[:6]),
    ]
    retrieval_query = "\n".join(
        str(value).strip()
        for value in retrieval_terms
        if isinstance(value, str) and value.strip()
    )[:1000]
    approved_sources, retrieval_metadata = _evidence_module().retrieve_for_brief(
        scope,
        retrieval_query,
    )
    payload["approvedEvidenceSources"] = approved_sources

    brief_app._resolve_model_id(payload)
    try:
        generated = brief_app._generate_brief(payload)
    except ValueError as exc:
        if action == "brief.refine":
            raise NonRetryableJobError(str(exc)) from exc
        raise
    if generated.get("provider") != "bedrock" or generated.get("metadata", {}).get(
        "fallbackUsed"
    ):
        raise RuntimeError("Bedrock did not produce a live model result")
    _set_job_phase(scope, job_id, "validating")
    screened_result, output_safety = _screen_ai_payload(
        generated,
        source="OUTPUT",
        action=action,
        trace_id=job_id,
    )
    if not isinstance(screened_result, Mapping):
        raise NonRetryableJobError("The generated brief output is invalid")
    generated = dict(screened_result)
    generated.setdefault("metadata", {}).update(
        {
            "modelRouting": payload.get("modelRouting", {}),
            "promptVersion": os.getenv("BRIEF_PROMPT_VERSION", "2026-08-21.1"),
            "safety": {
                "input": input_safety,
                "output": output_safety,
            },
            "rag": retrieval_metadata,
        }
    )
    _set_job_phase(scope, job_id, "saving")
    return _write_brief_draft(
        scope,
        payload,
        generated,
        action=action,
        job_id=job_id,
        input_version=str(document.get("inputVersion") or ""),
    )


def _brief_latest(scope: Mapping[str, str]) -> dict[str, Any]:
    item = aws_client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key=_brief_latest_key(scope),
        ConsistentRead=True,
    ).get("Item")
    return deserialize_item(item)


def _upsert_client_directory(
    scope: Mapping[str, str],
    *,
    brief: Mapping[str, Any] | None = None,
    handoff: Mapping[str, Any] | None = None,
) -> None:
    timestamp = now_iso()
    names: dict[str, str] = {}
    values: dict[str, Any] = {
        ":entity": {"S": "CLIENT"},
        ":clientId": {"S": scope["clientId"]},
        ":projectScopeId": {"S": scope["projectId"]},
        ":ownerId": {"S": scope["userId"]},
        ":updatedAt": {"S": timestamp},
    }
    assignments = [
        "entityType = :entity",
        "clientId = :clientId",
        "projectScopeId = :projectScopeId",
        "ownerId = :ownerId",
        "updatedAt = :updatedAt",
    ]
    if brief:
        names["#company"] = "company"
        values.update(
            {
                ":company": {
                    "S": str(brief.get("company") or scope["clientId"])
                },
                ":latestApprovedAt": {
                    "S": str(brief.get("approvedAt") or timestamp)
                },
                ":approvedPacketVersion": {
                    "N": str(brief.get("approvedPacketVersion") or 1)
                },
                ":approvedArtifactKey": {
                    "S": str(brief.get("approvedArtifactKey") or "")
                },
            }
        )
        assignments.extend(
            [
                "#company = :company",
                "latestApprovedAt = :latestApprovedAt",
                "approvedPacketVersion = :approvedPacketVersion",
                "approvedArtifactKey = :approvedArtifactKey",
            ]
        )
    if handoff:
        values.update(
            {
                ":latestHandoffAt": {
                    "S": str(handoff.get("updatedAt") or timestamp)
                },
                ":handoffArtifactKey": {
                    "S": str(handoff.get("artifactKey") or "")
                },
            }
        )
        assignments.extend(
            [
                "latestHandoffAt = :latestHandoffAt",
                "handoffArtifactKey = :handoffArtifactKey",
            ]
        )
    request: dict[str, Any] = {
        "TableName": PROJECT_TABLE,
        "Key": client_directory_key(scope),
        "UpdateExpression": "SET " + ", ".join(assignments),
        "ExpressionAttributeValues": values,
    }
    if names:
        request["ExpressionAttributeNames"] = names
    aws_client("dynamodb").update_item(**request)


def _precall_handoff_state(
    scope: Mapping[str, str],
    *,
    status: str,
    job_id: str,
    source_version: int,
    error: str = "",
) -> bool:
    values: dict[str, dict[str, str]] = {
        ":status": {"S": status},
        ":jobId": {"S": job_id},
        ":version": {"N": str(source_version)},
        ":updatedAt": {"S": now_iso()},
    }
    expression = (
        "SET precallHandoffStatus = :status, precallHandoffJobId = :jobId, "
        "precallHandoffSourceVersion = :version, "
        "precallHandoffUpdatedAt = :updatedAt"
    )
    names: dict[str, str] = {}
    if error:
        expression += ", precallHandoffError = :error"
        values[":error"] = {"S": error[:500]}
    else:
        expression += " REMOVE precallHandoffError"
    try:
        aws_client("dynamodb").update_item(
            TableName=PROJECT_TABLE,
            Key=_brief_latest_key(scope),
            UpdateExpression=expression,
            ConditionExpression=(
                "approvalStatus = :approved AND approvedPacketVersion = :version"
            ),
            ExpressionAttributeValues={
                **values,
                ":approved": {"S": "approved"},
            },
            **({"ExpressionAttributeNames": names} if names else {}),
        )
    except ClientError as exc:
        if _client_error_code(exc) == "ConditionalCheckFailedException":
            LOGGER.info(
                json.dumps(
                    {
                        "event": "precall_handoff_status_ignored",
                        "jobId": job_id,
                        "sourceVersion": source_version,
                        "reason": "approved brief changed",
                    }
                )
            )
            return False
        raise
    return True


def _approve_brief(
    scope: Mapping[str, str], document: Mapping[str, Any], job_id: str
) -> dict[str, Any]:
    expected_version = int(document["input"]["packetVersion"])
    latest = _brief_latest(scope)
    if int(latest.get("packetVersion") or 0) != expected_version:
        raise NonRetryableJobError(
            "The brief changed before approval; review the latest version."
        )
    prior_approved_version = int(latest.get("approvedPacketVersion") or 0)
    approval_version = expected_version
    if (
        latest.get("approvalStatus") != "approved"
        and prior_approved_version >= expected_version
    ):
        approval_version = prior_approved_version + 1
        metric("ApprovalVersionCollisionRecovered", Action="brief.approve")
        LOGGER.warning(
            json.dumps(
                {
                    "event": "approval_version_collision_recovered",
                    "jobId": job_id,
                    "requestedPacketVersion": expected_version,
                    "approvedPacketVersion": approval_version,
                }
            )
        )
    draft_key = str(latest.get("draftArtifactKey") or "")
    draft_docx_key = str(latest.get("draftDocxArtifactKey") or "")
    expected_prefix = f"{project_artifact_prefix(scope)}/brief/draft/"
    if not draft_key.startswith(expected_prefix) or not draft_docx_key.startswith(
        expected_prefix
    ):
        raise PermissionError("Draft brief is outside the authorized scope")
    s3 = aws_client("s3")
    draft_document = json.loads(
        s3.get_object(Bucket=ARTIFACT_BUCKET, Key=draft_key)["Body"]
        .read()
        .decode("utf-8")
    )
    generated = draft_document.get("response")
    if not isinstance(generated, dict):
        raise ValueError("Draft brief is invalid")
    approved_at = str(
        document.get("createdAt")
        or draft_document.get("storedAt")
        or now_iso()
    )
    approved_prefix = (
        f"{project_artifact_prefix(scope)}/brief/approved/"
        f"v{approval_version:06d}/"
    )
    expected_artifact_key = f"{approved_prefix}packet.json"
    expected_docx_key = f"{approved_prefix}packet.docx"
    metadata = generated.setdefault("metadata", {})
    metadata.pop("docxDownloadUrl", None)
    metadata.pop("precallHandoffJobId", None)
    metadata.pop("precallHandoffError", None)
    metadata.update(
        {
            "projectId": scope["projectId"],
            "clientId": scope["clientId"],
            "packetVersion": approval_version,
            "approvedPacketVersion": approval_version,
            "approvalStatus": "approved",
            "approvedAt": approved_at,
            "artifactKey": expected_artifact_key,
            "docxArtifactKey": expected_docx_key,
            "stateKey": "BRIEF#LATEST",
            "artifactRetention": "immutable-approved",
            "precallHandoffStatus": "idle",
            "precallHandoffSourceVersion": approval_version,
        }
    )
    refinement_history = {
        key: metadata.get(key)
        for key in (
            "baseBriefVersion",
            "refinementTarget",
            "changedSectionIds",
            "changedPassageIds",
            "appliedFeedback",
            "supersededFacts",
            "contradictionValidationPassed",
        )
        if metadata.get(key) not in (None, "", [])
    }
    approval_audit = {
        "approverId": scope["userId"],
        "approvedAt": approved_at,
        "packetVersion": approval_version,
        "sourcePacketVersion": expected_version,
        "sourceJobId": job_id,
        "modelId": metadata.get("modelId"),
        "modelProfile": metadata.get("modelProfile"),
        "promptVersion": metadata.get("promptVersion") or "brief-contract-v1",
        "inputVersion": draft_document.get("inputVersion"),
        "refinementHistory": refinement_history,
        "evidenceCitations": list(generated.get("citations") or []),
    }
    approved_document = {
        **draft_document,
        "packetVersion": approval_version,
        "approvalStatus": "approved",
        "approvedAt": approved_at,
        "approvedBy": scope["userId"],
        "approvalJobId": job_id,
        "approvalAudit": approval_audit,
    }
    if approval_version == expected_version:
        docx_bytes = s3.get_object(
            Bucket=ARTIFACT_BUCKET, Key=draft_docx_key
        )["Body"].read()
    else:
        docx_bytes = _brief_module()._brief_docx_bytes(
            dict(approved_document.get("request") or {}),
            generated,
            {
                "projectId": scope["projectId"],
                "clientId": scope["clientId"],
                "packetVersion": approval_version,
                "artifactRetention": "immutable-approved",
            },
        )
    artifact_key, docx_key, download_url, json_digest, docx_digest = (
        _write_approved_packet_pair(
            scope,
            approval_version,
            approved_document,
            docx_bytes,
            download_filename=_artifact_download_filename(
                approved_document.get("request", {}).get("company"),
                "brief",
                approval_version,
            ),
        )
    )
    if artifact_key != expected_artifact_key or docx_key != expected_docx_key:
        raise RuntimeError("Approved artifact writer returned unexpected keys")
    audit_key = {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": f"BRIEF#APPROVAL#v{approval_version:06d}"},
    }
    audit_item = {
        **audit_key,
        "entityType": {"S": "BRIEF_APPROVAL_AUDIT"},
        "approvalStatus": {"S": "approved"},
        "packetVersion": {"N": str(approval_version)},
        "approvedAt": {"S": approved_at},
        "approverId": {"S": scope["userId"]},
        "sourceJobId": {"S": job_id},
        "artifactKey": {"S": artifact_key},
        "docxArtifactKey": {"S": docx_key},
        "artifactSha256": {"S": json_digest},
        "docxSha256": {"S": docx_digest},
        "approval": serialize(approval_audit),
    }
    try:
        aws_client("dynamodb").transact_write_items(
            TransactItems=[
                {"Update": {
                    "TableName": PROJECT_TABLE,
                    "Key": _brief_latest_key(scope),
                    "UpdateExpression": (
                        "SET packetVersion = :approvedVersion, "
                        "approvalStatus = :approved, "
                        "approvedPacketVersion = :approvedVersion, "
                        "approvedArtifactKey = :artifactKey, "
                        "approvedDocxArtifactKey = :docxKey, "
                        "approvedAt = :approvedAt, approvedBy = :approvedBy, "
                        "approvalJobId = :jobId, updatedAt = :updatedAt, "
                        "precallHandoffStatus = :handoffIdle, "
                        "precallHandoffSourceVersion = :approvedVersion "
                        "REMOVE precallHandoffJobId, precallHandoffError, "
                        "precallHandoffUpdatedAt"
                    ),
                    "ConditionExpression": (
                        "packetVersion = :expectedVersion AND "
                        "draftArtifactKey = :draftArtifactKey"
                    ),
                    "ExpressionAttributeValues": {
                        ":approved": {"S": "approved"},
                        ":approvedVersion": {"N": str(approval_version)},
                        ":expectedVersion": {"N": str(expected_version)},
                        ":artifactKey": {"S": artifact_key},
                        ":docxKey": {"S": docx_key},
                        ":approvedAt": {"S": approved_at},
                        ":approvedBy": {"S": scope["userId"]},
                        ":jobId": {"S": job_id},
                        ":updatedAt": {"S": approved_at},
                        ":draftArtifactKey": {"S": draft_key},
                        ":handoffIdle": {"S": "idle"},
                    },
                }},
                {"Put": {
                    "TableName": PROJECT_TABLE,
                    "Item": audit_item,
                    "ConditionExpression": "attribute_not_exists(projectId)",
                }},
            ],
            ClientRequestToken=dynamodb_client_request_token(
                "brief-approval", [project_partition_key(scope), job_id]
            ),
        )
    except ClientError as exc:
        if _client_error_code(exc) in {
            "ConditionalCheckFailedException",
            "TransactionCanceledException",
        }:
            current = _brief_latest(scope)
            if (
                current.get("approvalJobId") == job_id
                and int(current.get("approvedPacketVersion") or 0)
                == approval_version
                and current.get("approvedArtifactKey") == artifact_key
            ):
                metadata["docxDownloadUrl"] = download_url
                return generated
            raise NonRetryableJobError("The brief changed before approval; review the latest version.") from exc
        raise
    _upsert_client_directory(
        scope,
        brief={
            "company": latest.get("company"),
            "approvedAt": approved_at,
            "approvedPacketVersion": approval_version,
            "approvedArtifactKey": artifact_key,
        },
    )
    metadata["docxDownloadUrl"] = download_url
    return generated


def _read_runtime_response(response: Mapping[str, Any]) -> dict[str, Any]:
    stream = (
        response.get("response")
        or response.get("body")
        or response.get("payload")
    )
    if hasattr(stream, "read"):
        raw = stream.read()
    elif isinstance(stream, (bytes, bytearray, str)):
        raw = stream
    elif stream is not None:
        chunks = []
        for event in stream:
            chunk = (
                event.get("chunk", {}).get("bytes")
                if isinstance(event, Mapping)
                else None
            )
            if isinstance(chunk, bytes):
                chunks.append(chunk)
        raw = b"".join(chunks)
    else:
        raise RuntimeError("AgentCore Runtime returned no response")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("AgentCore Runtime returned invalid JSON")
    if parsed.get("errorCode") == "AGENT_CONTEXT_TOO_LARGE" and parsed.get("retryable") is False:
        raise NonRetryableJobError(
            "This request contains too much context to process. Shorten the meeting "
            "notes or customer inputs and try again. The approved brief has not changed."
        )
    return parsed


def _approved_document(
    scope: Mapping[str, str], *, require_current: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    latest = _brief_latest(scope)
    approved_key = str(latest.get("approvedArtifactKey") or "")
    approved_version = int(latest.get("approvedPacketVersion") or 0)
    if not approved_key or not approved_version:
        raise LookupError("No approved brief exists for this project")
    if require_current and (
        latest.get("approvalStatus") != "approved"
        or int(latest.get("packetVersion") or 0) != approved_version
    ):
        raise ValueError("The latest brief must be approved before handoff")
    prefix = f"{project_artifact_prefix(scope)}/brief/"
    if not approved_key.startswith(prefix):
        raise PermissionError("Approved brief is outside the authorized scope")
    document = json.loads(
        aws_client("s3")
        .get_object(Bucket=ARTIFACT_BUCKET, Key=approved_key)["Body"]
        .read()
        .decode("utf-8")
    )
    if not isinstance(document, dict) or not isinstance(
        document.get("response"), dict
    ):
        raise ValueError("Approved brief artifact is invalid")
    return latest, document


def _run_agent(
    scope: Mapping[str, str], document: Mapping[str, Any]
) -> dict[str, Any]:
    if not AGENT_RUNTIME_ARN:
        raise RuntimeError("AgentCore Runtime is not configured")
    pipeline_action = str(document["action"])
    legacy_action = (
        "create_handoff"
        if pipeline_action == "handoff.generate"
        else "generate_catchup"
    )
    latest, approved_document = _approved_document(
        scope, require_current=pipeline_action == "handoff.generate"
    )
    inputs = document["input"]
    if pipeline_action == "handoff.generate":
        expected_version = int(inputs["expectedApprovedPacketVersion"])
        approved_version = int(latest.get("approvedPacketVersion") or 0)
        if expected_version != approved_version:
            raise NonRetryableJobError(
                "The approved brief changed before handoff; reload the latest packet."
            )
    approved = approved_document["response"]
    request_context = approved_document.get("request")
    request_context = (
        dict(request_context) if isinstance(request_context, Mapping) else {}
    )
    request_context.update(
        {
            "mode": "project",
            "modelPreference": inputs.get("modelPreference", "nova-pro"),
            "meetingNotes": inputs.get(
                "meetingNotes", request_context.get("meetingNotes", "")
            ),
            "role": inputs.get("audienceRole", "PM"),
            "prompt": inputs.get("focus", ""),
            "approvedBrief": approved,
        }
    )
    idempotency = require_identifier(
        document.get("idempotencyKey"), "idempotencyKey"
    )
    agent_session_id = stable_identifier(
        "agent-session",
        [
            scope[field]
            for field in (
                "tenantId",
                "clientId",
                "projectId",
                "userId",
                "sessionId",
            )
        ],
        48,
    )
    runtime_session_id = stable_identifier(
        "runtime-invocation",
        [agent_session_id, idempotency],
        48,
    )
    trace_id = stable_identifier("trace", [runtime_session_id, idempotency], 32)
    runtime_payload = {
        "action": legacy_action,
        "clientId": scope["clientId"],
        "projectId": scope["projectId"],
        "sessionId": scope["sessionId"],
        "audienceRole": inputs.get("audienceRole", "PM"),
        "focus": inputs.get("focus", ""),
        "meetingNotes": inputs.get("meetingNotes", ""),
        "modelPreference": inputs.get("modelPreference", "nova-pro"),
        "confirmWrite": pipeline_action == "handoff.generate",
        "idempotencyKey": idempotency,
        "approvedBrief": approved,
        "briefRequest": request_context,
        "scope": dict(scope),
        "scopeToken": _scope_token(scope),
        "traceId": trace_id,
    }
    screened_payload, input_safety = _screen_ai_payload(
        runtime_payload,
        source="INPUT",
        action=pipeline_action,
        trace_id=trace_id,
    )
    if not isinstance(screened_payload, Mapping):
        raise NonRetryableJobError("The normalized agent input is invalid")
    runtime_payload = dict(screened_payload)
    runtime_response = aws_client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        runtimeSessionId=runtime_session_id,
        qualifier="DEFAULT",
        contentType="application/json",
        accept="application/json",
        traceId=trace_id,
        payload=json.dumps(runtime_payload, separators=(",", ":")).encode("utf-8"),
    )
    result = _read_runtime_response(runtime_response)
    if result.get("provider") != "agentcore":
        raise RuntimeError("AgentCore did not produce the requested result")
    screened_result, output_safety = _screen_ai_payload(
        result,
        source="OUTPUT",
        action=pipeline_action,
        trace_id=trace_id,
    )
    if not isinstance(screened_result, Mapping):
        raise NonRetryableJobError("The AgentCore output is invalid")
    result = dict(screened_result)
    result.setdefault("metadata", {}).update(
        {
            "agentSessionId": agent_session_id,
            "agentRuntimeSessionId": runtime_session_id,
            "agentTraceId": trace_id,
            "agentMode": "agentcore",
            "fallbackUsed": False,
            "approvedPacketVersion": latest.get("approvedPacketVersion"),
            "modelRouting": inputs.get("modelRouting", {}),
            "promptVersion": os.getenv("AGENT_PROMPT_VERSION", "2026-08-21.1"),
            "safety": {
                "input": input_safety,
                "output": output_safety,
            },
        }
    )
    if pipeline_action == "catchup.generate":
        # Other users can legitimately advance project state during this read-only request.
        read_tools = {"get_latest_brief", "get_project_state", "retrieve_authorized_evidence", "generate_catchup"}
        used_tools = result.get("metadata", {}).get("toolCalls", [])
        if not isinstance(used_tools, list) or any(not isinstance(tool, str) or tool not in read_tools for tool in used_tools):
            raise RuntimeError("Read-only catch-up reported a write-capable or unknown tool")
    if pipeline_action == "handoff.generate":
        metadata = (
            result.get("metadata")
            if isinstance(result.get("metadata"), Mapping)
            else {}
        )
        _record_latest_handoff(scope, latest, metadata, assembly="agentcore")
    return result


def _invoke_meeting_runtime(
    scope: Mapping[str, str],
    document: Mapping[str, Any],
    transcript: Mapping[str, Any],
    approved_document: Mapping[str, Any],
    approved_version: int,
    trace_id: str,
    *,
    repair_reason: str = "",
) -> dict[str, Any]:
    if not AGENT_RUNTIME_ARN:
        raise RuntimeError("AgentCore Runtime is not configured")
    inputs = document["input"]
    approved = approved_document["response"]
    brief_request = approved_document.get("request")
    if not isinstance(brief_request, Mapping):
        brief_request = {}
    idempotency = require_identifier(
        document.get("idempotencyKey"), "idempotencyKey"
    )
    runtime_session_id = stable_identifier(
        "runtime-session",
        [
            scope["tenantId"],
            scope["clientId"],
            scope["projectId"],
            scope["userId"],
            scope["sessionId"],
            str(inputs["meetingId"]),
        ],
        48,
    )
    payload = {
        "action": "analyze_meeting",
        "clientId": scope["clientId"],
        "projectId": scope["projectId"],
        "sessionId": scope["sessionId"],
        "audienceRole": "Solutions Architect",
        "focus": "Compare the meeting with the approved prebrief.",
        "meetingNotes": "",
        "modelPreference": "nova-pro",
        "confirmWrite": False,
        "idempotencyKey": idempotency,
        "approvedBrief": approved,
        "briefRequest": dict(brief_request),
        "scenarioId": inputs["scenarioId"],
        "meetingId": inputs["meetingId"],
        "briefVersion": approved_version,
        "knowledgeBaseId": KNOWLEDGE_BASE_ID,
        "meetingTranscript": dict(transcript),
        "repairReason": repair_reason,
        "scope": dict(scope),
        "scopeToken": _scope_token(scope),
        "traceId": trace_id,
    }
    screened_payload, input_safety = _screen_ai_payload(
        payload,
        source="INPUT",
        action="meeting.process",
        trace_id=trace_id,
    )
    if not isinstance(screened_payload, Mapping):
        raise NonRetryableJobError("The normalized meeting input is invalid")
    payload = dict(screened_payload)
    response = aws_client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        runtimeSessionId=runtime_session_id,
        qualifier="DEFAULT",
        contentType="application/json",
        accept="application/json",
        traceId=trace_id,
        payload=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )
    result = _read_runtime_response(response)
    if result.get("provider") != "agentcore-strands":
        raise RuntimeError("AgentCore did not return meeting analysis")
    screened_result, output_safety = _screen_ai_payload(
        result,
        source="OUTPUT",
        action="meeting.process",
        trace_id=trace_id,
    )
    if not isinstance(screened_result, Mapping):
        raise NonRetryableJobError("The meeting analysis output is invalid")
    result = dict(screened_result)
    result.setdefault("metadata", {}).setdefault("safety", {}).update(
        {
            "input": input_safety,
            "output": output_safety,
        }
    )
    return result


def _run_meeting_analysis(
    scope: Mapping[str, str],
    document: Mapping[str, Any],
    transcript: Mapping[str, Any],
    approved_document: Mapping[str, Any],
    approved_version: int,
    trace_id: str,
) -> dict[str, Any]:
    from pipeline.meeting_contracts import (
        compare_meeting_to_brief,
        validate_analysis,
    )

    error: Exception | None = None
    for attempt in range(2):
        raw = _invoke_meeting_runtime(
            scope,
            document,
            transcript,
            approved_document,
            approved_version,
            trace_id,
            repair_reason=str(error or ""),
        )
        try:
            analysis = validate_analysis(raw.get("analysis"), transcript)
            review_items = compare_meeting_to_brief(
                str(document["input"]["scenarioId"]),
                str(document["input"]["meetingId"]),
                approved_version,
                approved_document["response"],
                analysis,
            )
            if not review_items:
                raise ValueError("Meeting analysis proposed no reviewable changes")
            return {
                **raw,
                "analysis": analysis,
                "reviewItems": review_items,
            }
        except ValueError as exc:
            error = exc
            LOGGER.warning(
                json.dumps(
                    {
                        "event": "meeting_analysis_validation_retry",
                        "attempt": attempt + 1,
                        "jobId": document.get("idempotencyKey"),
                        "errorType": type(exc).__name__,
                        "reason": str(exc)[:240],
                    }
                )
            )
    raise NonRetryableJobError(
        "Meeting analysis remained incomplete or contradictory after repair"
    ) from error


def _latest_handoff_packet(
    scope: Mapping[str, str],
    approved_document: Mapping[str, Any],
    approved_version: int,
) -> dict[str, Any]:
    item = deserialize_item(
        aws_client("dynamodb")
        .get_item(
            TableName=PROJECT_TABLE,
            Key={
                "projectId": {"S": project_partition_key(scope)},
                "sortKey": {"S": "HANDOFF#LATEST"},
            },
            ConsistentRead=True,
        )
        .get("Item")
    )
    artifact_key = str(item.get("artifactKey") or "")
    expected_prefix = f"{project_artifact_prefix(scope)}/handoff/"
    if (
        artifact_key.startswith(expected_prefix)
        and int(item.get("sourceBriefVersion") or 0) == approved_version
    ):
        stored = json.loads(
            aws_client("s3")
            .get_object(Bucket=ARTIFACT_BUCKET, Key=artifact_key)["Body"]
            .read()
            .decode("utf-8")
        )
        packet = stored.get("packet") if isinstance(stored, Mapping) else None
        if isinstance(packet, Mapping):
            return dict(packet)
    approved = approved_document.get("response")
    if not isinstance(approved, Mapping):
        raise ValueError("The approved brief cannot seed the meeting handoff")
    return dict(approved)


def _record_latest_handoff(
    scope: Mapping[str, str],
    latest: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    assembly: str,
) -> None:
    timestamp = now_iso()
    replaced = aws_client("dynamodb").put_item(
        TableName=PROJECT_TABLE,
        Item={
            "projectId": {"S": project_partition_key(scope)},
            "sortKey": {"S": "HANDOFF#LATEST"},
            "entityType": {"S": "HANDOFF_LATEST"},
            "company": {"S": str(latest.get("company") or "")},
            "artifactKey": {"S": str(metadata.get("artifactKey") or "")},
            "docxArtifactKey": {
                "S": str(metadata.get("docxArtifactKey") or "")
            },
            "updatedAt": {"S": timestamp},
            "sourceBriefVersion": {
                "N": str(latest.get("approvedPacketVersion") or 0)
            },
            "provider": {"S": "agentcore"},
            "assembly": {"S": assembly},
        },
        ReturnValues="ALL_OLD",
    ) or {}
    previous = deserialize_item(replaced.get("Attributes"))
    _cleanup_replaced_artifacts(scope, [str(previous.get("artifactKey") or ""), str(previous.get("docxArtifactKey") or "")], {str(metadata.get("artifactKey") or ""), str(metadata.get("docxArtifactKey") or "")})
    _upsert_client_directory(
        scope,
        handoff={
            "updatedAt": timestamp,
            "artifactKey": metadata.get("artifactKey"),
        },
    )


def _promote_approved_meeting(
    scope: Mapping[str, str],
    document: Mapping[str, Any],
    latest: Mapping[str, Any],
    approved_document: Mapping[str, Any],
    proposal: Mapping[str, Any],
    accepted: list[dict[str, Any]],
) -> dict[str, Any]:
    from pipeline import handoff_promotion

    trace_id = stable_identifier(
        "meeting-approval",
        [
            scope["tenantId"],
            scope["clientId"],
            scope["projectId"],
            str(document.get("idempotencyKey") or ""),
        ],
        32,
    )
    screened, input_safety = _screen_ai_payload(
        accepted,
        source="INPUT",
        action="meeting.approve",
        trace_id=trace_id,
    )
    if not isinstance(screened, list) or not all(
        isinstance(item, Mapping) for item in screened
    ):
        raise NonRetryableJobError("The reviewed meeting updates are invalid")
    screened_accepted = [dict(item) for item in screened]
    approved_version = int(latest.get("approvedPacketVersion") or 0)
    base_packet = _latest_handoff_packet(
        scope, approved_document, approved_version
    )
    packet = handoff_promotion.promote_handoff(
        base_packet,
        proposal,
        screened_accepted,
        company=str(latest.get("company") or scope["clientId"]),
        packet_version=approved_version,
    )
    tools = _handoff_tools_module()
    current_state = tools.get_project_state(scope)
    artifacts = packet.get("projectArtifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("The promoted handoff has no project artifacts")
    update = handoff_promotion.project_update(
        current_state,
        screened_accepted,
        artifacts,
        meeting_id=str(proposal.get("meetingId") or "customer-meeting"),
    )
    idempotency = stable_identifier(
        "meeting-promotion",
        [str(document.get("idempotencyKey") or "")],
        40,
    )
    saved_state = tools.save_project_update(
        scope,
        update,
        expected_version=int(current_state.get("version") or 0),
        idempotency_key=idempotency,
        confirm_write=True,
    )
    metadata_value = packet.get("metadata")
    metadata = (
        dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    )
    metadata.update(
        {
            "stateKey": saved_state.get("stateKey", "PROJECT#STATE"),
            "projectVersion": int(saved_state.get("version") or 0),
            "toolCalls": [
                "get_latest_brief",
                "get_project_state",
                "promote_human_approved_meeting",
                "save_project_update",
                "create_handoff_packet",
            ],
            "safety": {"input": input_safety},
        }
    )
    packet["metadata"] = metadata
    artifact = tools.create_handoff_packet(
        scope,
        packet,
        audience="Solutions Architect",
        idempotency_key=idempotency,
        confirm_write=True,
    )
    metadata.update(
        {
            "artifactKey": artifact.get("artifactKey"),
            "docxArtifactKey": artifact.get("docxArtifactKey"),
            "docxDownloadUrl": artifact.get("docxDownloadUrl"),
            "artifactRetention": artifact.get(
                "artifactRetention", "latest-only"
            ),
        }
    )
    packet["metadata"] = metadata
    _record_latest_handoff(
        scope,
        latest,
        metadata,
        assembly="human-approved-meeting-promotion",
    )
    return packet


def _approve_meeting(
    scope: Mapping[str, str],
    document: Mapping[str, Any],
) -> dict[str, Any]:
    meeting = _meeting_module()
    latest, approved_document = _approved_document(
        scope, require_current=True
    )
    current_version = int(latest.get("approvedPacketVersion") or 0)
    proposal, accepted, rejected = meeting.review_proposal(
        scope,
        document,
        current_approved_version=current_version,
    )
    handoff = _promote_approved_meeting(
        scope,
        document,
        latest,
        approved_document,
        proposal,
        accepted,
    )
    return meeting.finalize_approval(
        scope,
        document,
        proposal,
        accepted,
        rejected,
        handoff,
    )



def _run_evidence(
    scope: Mapping[str, str],
    document: Mapping[str, Any],
    job_id: str,
) -> dict[str, Any]:
    evidence = _evidence_module()
    action = str(document.get("action") or "")
    inputs = document.get("input")
    if not isinstance(inputs, Mapping):
        raise ValueError("Evidence job input is missing")
    try:
        if action == "evidence.ingest":
            return evidence.ingest_document(
                scope,
                inputs,
                source_job_id=job_id,
            )
        if action == "evidence.delete":
            return evidence.delete_document(scope, inputs)
        if action == "evidence.reindex":
            return evidence.reindex_document(scope, inputs)
    except evidence.EvidenceConflictError as exc:
        raise NonRetryableJobError(str(exc)) from exc
    raise ValueError("Unsupported evidence action")


def _action_idempotency_key(
    scope: Mapping[str, str], idempotency: str
) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": f"IDEMPOTENCY#ACTION#{idempotency}"},
    }


def _existing_action_result(
    scope: Mapping[str, str], idempotency: str
) -> str:
    item = aws_client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key=_action_idempotency_key(scope, idempotency),
        ConsistentRead=True,
        ProjectionExpression="resultKey",
    ).get("Item")
    return (
        str(item.get("resultKey", {}).get("S") or "")
        if isinstance(item, Mapping)
        else ""
    )


def _store_result(
    scope: Mapping[str, str],
    job_id: str,
    document: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    final_status: str = "complete",
) -> str:
    result_key = f"{job_object_prefix(scope, job_id)}/result.json"
    metadata = result.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    safety = metadata.get("safety")
    safety = safety if isinstance(safety, Mapping) else {}
    input_safety = safety.get("input") or safety.get("transcriptInput")
    input_safety = input_safety if isinstance(input_safety, Mapping) else {}
    output_safety = safety.get("output")
    output_safety = output_safety if isinstance(output_safety, Mapping) else {}
    input_validation_outcome = str(
        input_safety.get("policyResult") or "not_recorded"
    )[:64]
    output_validation_outcome = str(
        output_safety.get("policyResult") or "not_recorded"
    )[:64]
    aws_client("s3").put_object(
        Bucket=ARTIFACT_BUCKET,
        Key=result_key,
        Body=json.dumps(result, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
        **s3_encryption_args(),
    )
    timestamp = now_iso()
    idempotency = require_identifier(
        document.get("idempotencyKey"), "idempotencyKey"
    )
    try:
        aws_client("dynamodb").put_item(
            TableName=PROJECT_TABLE,
            Item={
                **_action_idempotency_key(scope, idempotency),
                "entityType": {"S": "IDEMPOTENCY"},
                "action": {"S": str(document["action"])},
                "jobId": {"S": job_id},
                "resultKey": {"S": result_key},
                "createdAt": {"S": timestamp},
                "expiresAt": {"N": str(now_epoch() + 7 * 86400)},
            },
            ConditionExpression="attribute_not_exists(projectId)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != (
            "ConditionalCheckFailedException"
        ):
            raise
    aws_client("dynamodb").update_item(
        TableName=PROJECT_TABLE,
        Key=job_key(scope, job_id),
        UpdateExpression=(
            "SET #status = :final, resultKey = :resultKey, "
            "updatedAt = :updatedAt, completedAt = :updatedAt, "
            "inputValidationOutcome = :inputValidationOutcome, "
            "outputValidationOutcome = :outputValidationOutcome "
            "REMOVE leaseExpiresAt, #error, piiScreeningOutcome"
        ),
        ConditionExpression=(
            "#status IN (:running, :validating, :saving, :analyzing)"
        ),
        ExpressionAttributeNames={"#status": "status", "#error": "error"},
        ExpressionAttributeValues={
            ":final": {"S": final_status},
            ":running": {"S": "running"},
            ":validating": {"S": "validating"},
            ":saving": {"S": "saving"},
            ":analyzing": {"S": "analyzing"},
            ":resultKey": {"S": result_key},
            ":updatedAt": {"S": timestamp},
            ":inputValidationOutcome": {"S": input_validation_outcome},
            ":outputValidationOutcome": {"S": output_validation_outcome},
        },
    )
    return result_key


def _complete_from_existing(
    scope: Mapping[str, str], job_id: str, result_key: str
) -> None:
    aws_client("dynamodb").update_item(
        TableName=PROJECT_TABLE,
        Key=job_key(scope, job_id),
        UpdateExpression=(
            "SET #status = :complete, resultKey = :resultKey, "
            "updatedAt = :updatedAt, completedAt = :updatedAt "
            "REMOVE leaseExpiresAt, #error"
        ),
        ExpressionAttributeNames={"#status": "status", "#error": "error"},
        ExpressionAttributeValues={
            ":complete": {"S": "complete"},
            ":resultKey": {"S": result_key},
            ":updatedAt": {"S": now_iso()},
        },
    )


def _record_terminal_failure(
    scope: Mapping[str, str],
    job_id: str,
    error: Exception,
) -> None:
    aws_client("dynamodb").update_item(
        TableName=PROJECT_TABLE,
        Key=job_key(scope, job_id),
        UpdateExpression=(
            "SET #status = :failed, retryCount = :retryCount, "
            "updatedAt = :updatedAt, #error = :error, errorType = :errorType "
            "REMOVE leaseExpiresAt"
        ),
        ExpressionAttributeNames={"#status": "status", "#error": "error"},
        ExpressionAttributeValues={
            ":failed": {"S": "failed"},
            ":retryCount": {"N": "0"},
            ":updatedAt": {"S": now_iso()},
            ":error": {"S": str(error)},
            ":errorType": {"S": type(error).__name__},
        },
    )


def _record_failure(
    scope: Mapping[str, str],
    job_id: str,
    receive_count: int,
    error: Exception,
) -> None:
    terminal = receive_count >= MAX_RECEIVE_COUNT
    safe_error = (
        "The AI job failed after its final retry"
        if terminal
        else "The AI job will be retried"
    )
    aws_client("dynamodb").update_item(
        TableName=PROJECT_TABLE,
        Key=job_key(scope, job_id),
        UpdateExpression=(
            "SET #status = :status, retryCount = :retryCount, "
            "updatedAt = :updatedAt, #error = :error, errorType = :errorType "
            "REMOVE leaseExpiresAt"
        ),
        ExpressionAttributeNames={"#status": "status", "#error": "error"},
        ExpressionAttributeValues={
            ":status": {"S": "failed" if terminal else "queued"},
            ":retryCount": {"N": str(receive_count)},
            ":updatedAt": {"S": now_iso()},
            ":error": {"S": safe_error},
            ":errorType": {"S": type(error).__name__},
        },
    )

def _process_guardduty_record(
    _record: Mapping[str, Any],
    event: Mapping[str, Any],
) -> None:
    meeting = _meeting_module()
    receive_count = int(
        (_record.get("attributes") or {}).get("ApproximateReceiveCount", "1")
    )
    scan_result = meeting.handle_guardduty_scan_event(
        event,
        final_attempt=receive_count >= MAX_RECEIVE_COUNT,
    )
    outcome = str(scan_result.get("outcome") or "scan_failed")
    detail = event.get("detail")
    scan_status = (
        str(detail.get("scanStatus") or "")
        if isinstance(detail, Mapping)
        else ""
    )
    metric("GuardDutyEvent1Deliveries", Action="meeting.process")
    if scan_result.get("duplicate") is True:
        metric("DuplicateGuardDutyEvents", Action="meeting.process")
    if outcome == "clean":
        metric("GuardDutyCleanScans", Action="meeting.process")
        claimed = meeting.claim_waiting_scan_process(scan_result)
        if claimed is None:
            return
        scope, pointer = claimed
        job_id = require_identifier(pointer.get("jobId"), "jobId")
        try:
            document = _load_input(pointer, scope)
            if document.get("action") != "meeting.process":
                raise NonRetryableJobError("The waiting meeting job is invalid")
            latest, _approved = _approved_document(scope, require_current=True)
            started = meeting.start_transcription(
                scope,
                document,
                job_id=job_id,
                input_key=str(pointer.get("inputKey") or ""),
                input_version=str(pointer.get("inputVersion") or ""),
                trace_id=str(pointer.get("traceId") or ""),
                approved_packet_version=int(
                    latest.get("approvedPacketVersion") or 0
                ),
            )
            if started.get("waitingForScan") is True:
                raise NonRetryableJobError(
                    "The verified scan could not resume meeting processing"
                )
            metric("MeetingTranscriptionStarted", Action="meeting.process")
        except (NonRetryableJobError, meeting.MeetingConflictError) as exc:
            meeting.set_job_phase(
                scope,
                job_id,
                status="failed",
                phase="failed",
            )
            metric("JobsFailed", Action="meeting.process")
            return
        return
    if outcome == "blocked":
        metric("GuardDutyBlockedScans", Action="meeting.process")
    elif scan_status == "SKIPPED":
        metric("GuardDutySkippedScans", Action="meeting.process")
    else:
        metric("GuardDutyFailedScans", Action="meeting.process")
    meeting.fail_waiting_scan_process(scan_result)


def _process_transcribe_record(
    record: Mapping[str, Any],
    event: Mapping[str, Any],
) -> None:
    detail = event.get("detail")
    if not isinstance(detail, Mapping):
        raise ValueError("Transcribe completion detail is missing")
    job_name = str(detail.get("TranscriptionJobName") or "")
    job_status = str(detail.get("TranscriptionJobStatus") or "")
    meeting = _meeting_module()
    continuation = meeting.claim_continuation(job_name)
    if continuation is None:
        metric("DuplicateDeliveries", Action="meeting.process")
        return
    scope = meeting.continuation_scope(continuation)
    job_id = require_identifier(continuation.get("jobId"), "jobId")
    receive_count = int(
        (record.get("attributes") or {}).get("ApproximateReceiveCount", "1")
    )
    if job_status == "FAILED":
        reason = str(detail.get("FailureReason") or "")
        meeting.fail_continuation(
            continuation,
            job_name,
            "Meeting transcription failed"
            + (f": {reason[:240]}" if reason else "."),
        )
        metric("JobsFailed", Action="meeting.process")
        return
    if job_status != "COMPLETED":
        meeting.reset_continuation(
            job_name,
            ValueError("Unexpected Transcribe completion state"),
        )
        raise ValueError("Unexpected Transcribe completion state")
    try:
        meeting.set_job_phase(
            scope,
            job_id,
            status="screening",
            phase="screening",
        )
        input_pointer = {
            **continuation,
            "action": str(continuation.get("action") or "meeting.process"),
        }
        document = _load_input(input_pointer, scope)
        latest, approved_document = _approved_document(
            scope, require_current=True
        )
        approved_version = int(
            latest.get("approvedPacketVersion") or 0
        )
        if approved_version != int(
            continuation.get("expectedApprovedPacketVersion") or 0
        ):
            raise NonRetryableJobError(
                "The approved brief changed while the meeting was transcribed."
            )
        transcript = meeting.read_transcript(continuation)
        trace_id = str(continuation.get("traceId") or "")
        screened_context, transcript_safety = _screen_ai_payload(
            {
                "document": document,
                "transcript": transcript,
            },
            source="INPUT",
            action="meeting.process",
            trace_id=trace_id,
        )
        if not isinstance(screened_context, Mapping):
            raise NonRetryableJobError("The normalized meeting context is invalid")
        document = screened_context["document"]
        transcript = screened_context["transcript"]
        if not all(
            isinstance(value, Mapping)
            for value in (document, transcript, approved_document)
        ):
            raise NonRetryableJobError("The normalized meeting context is invalid")
        meeting.set_job_phase(
            scope,
            job_id,
            status="analyzing",
            phase="analyzing",
        )
        analysis = _run_meeting_analysis(
            scope,
            document,
            transcript,
            approved_document,
            approved_version,
            trace_id,
        )
        analysis.setdefault("metadata", {}).setdefault("safety", {})[
            "transcriptInput"
        ] = transcript_safety
        result = meeting.persist_proposal(
            scope,
            continuation,
            transcript,
            analysis,
        )
        result_key = _store_result(
            scope,
            job_id,
            document,
            result,
            final_status="review-ready",
        )
        meeting.complete_continuation(
            job_name,
            result_key=result_key,
        )
        meeting_latency_ms = _elapsed_since_iso_ms(
            continuation.get("requestedAt") or continuation.get("createdAt")
        )
        if meeting_latency_ms is not None:
            metric(
                "MeetingEndToEndLatencyMs",
                meeting_latency_ms,
                unit="Milliseconds",
                Action="meeting.process",
            )
        metric("JobsCompleted", Action="meeting.process")
    except (NonRetryableJobError, meeting.MeetingConflictError) as exc:
        meeting.fail_continuation(
            continuation,
            job_name,
            str(exc),
        )
        metric("JobsFailed", Action="meeting.process")
    except Exception as exc:
        if receive_count >= MAX_RECEIVE_COUNT:
            meeting.fail_continuation(
                continuation,
                job_name,
                "Meeting analysis failed after its final retry.",
            )
        else:
            meeting.reset_continuation(job_name, exc)
        LOGGER.exception(
            json.dumps(
                {
                    "event": "meeting_continuation_failure",
                    "jobId": job_id,
                    "transcriptionJobName": job_name,
                    "receiveCount": receive_count,
                    "errorType": type(exc).__name__,
                }
            )
        )
        _record_failure(scope, job_id, receive_count, exc)
        raise

def _process_record(record: Mapping[str, Any]) -> None:
    worker_started = time.perf_counter()
    message = json.loads(str(record.get("body") or "{}"))
    if not isinstance(message, Mapping):
        raise ValueError("Queue message must be an object")
    if (
        message.get("source") == "aws.guardduty"
        and message.get("detail-type")
        == "GuardDuty Malware Protection Object Scan Result"
    ):
        _process_guardduty_record(record, message)
        return
    if (
        message.get("source") == "aws.transcribe"
        and message.get("detail-type") == "Transcribe Job State Change"
    ):
        metric("TranscribeEvent2Deliveries", Action="meeting.process")
        _process_transcribe_record(record, message)
        return
    scope = _scope(message)
    job_id = require_identifier(message.get("jobId"), "jobId")
    action = str(message.get("action") or "unknown")
    automatic_handoff = False
    source_brief_version = 0
    record_attributes = record.get("attributes") or {}
    receive_count = int(record_attributes.get("ApproximateReceiveCount", "1"))
    sent_timestamp = int(record_attributes.get("SentTimestamp", "0") or 0)
    queue_wait_ms = (
        max(0, int(time.time() * 1000) - sent_timestamp)
        if sent_timestamp > 0
        else 0
    )
    if not _claim_job(scope, job_id, receive_count):
        return
    try:
        document = _load_input(message, scope)
        idempotency = require_identifier(
            document.get("idempotencyKey"), "idempotencyKey"
        )
        existing_result = _existing_action_result(scope, idempotency)
        if existing_result:
            _complete_from_existing(scope, job_id, existing_result)
            return
        action = str(document["action"])
        job_input = (
            document.get("input")
            if isinstance(document.get("input"), Mapping)
            else {}
        )
        automatic_handoff = (
            action == "handoff.generate" and job_input.get("automatic") is True
        )
        source_brief_version = int(
            job_input.get("expectedApprovedPacketVersion") or 0
        )
        if automatic_handoff:
            _precall_handoff_state(
                scope,
                status="preparing",
                job_id=job_id,
                source_version=source_brief_version,
            )
        if action in {"brief.generate", "brief.refine"}:
            result = _run_brief(scope, document, job_id)
        elif action == "brief.approve":
            result = _approve_brief(scope, document, job_id)
        elif action == "meeting.process":
            meeting = _meeting_module()
            try:
                latest, _approved = _approved_document(
                    scope, require_current=True
                )
                transcription_state = meeting.start_transcription(
                    scope,
                    document,
                    job_id=job_id,
                    input_key=str(message.get("inputKey") or ""),
                    input_version=str(message.get("inputVersion") or ""),
                    trace_id=str(message.get("traceId") or ""),
                    approved_packet_version=int(
                        latest.get("approvedPacketVersion") or 0
                    ),
                )
            except meeting.MeetingConflictError as exc:
                raise NonRetryableJobError(str(exc)) from exc
            if transcription_state.get("waitingForScan") is True:
                metric("MeetingJobsWaitingForScan", Action=action)
            else:
                metric("MeetingTranscriptionStarted", Action=action)
            return
        elif action == "meeting.approve":
            meeting = _meeting_module()
            try:
                result = _approve_meeting(scope, document)
            except meeting.MeetingConflictError as exc:
                raise NonRetryableJobError(str(exc)) from exc
        elif action in {"handoff.generate", "catchup.generate"}:
            agent_started = time.perf_counter()
            try:
                result = _run_agent(scope, document)
            except Exception:
                metric("AgentCoreFailures", Action=action)
                raise
            agent_latency_ms = int(
                (time.perf_counter() - agent_started) * 1000
            )
            metric("AgentCoreInvocations", Action=action)
            metric(
                "AgentCoreLatencyMs",
                agent_latency_ms,
                unit="Milliseconds",
                Action=action,
            )
            agent_metadata = (
                result.get("metadata")
                if isinstance(result, Mapping)
                and isinstance(result.get("metadata"), Mapping)
                else {}
            )
            if agent_metadata.get("fallbackUsed") is True:
                metric("AgentCoreFallbacks", Action=action)
            _set_job_phase(scope, job_id, "validating")
        elif action.startswith("evidence."):
            result = _run_evidence(scope, document, job_id)
        else:
            raise ValueError("Unsupported job action")
        if action not in {"brief.generate", "brief.refine"}:
            _set_job_phase(scope, job_id, "saving")
        _store_result(
            scope,
            job_id,
            document,
            result,
            final_status=(
                "approved" if action == "meeting.approve" else "complete"
            ),
        )
        if automatic_handoff:
            _precall_handoff_state(
                scope,
                status="ready",
                job_id=job_id,
                source_version=source_brief_version,
            )
        metric("JobsCompleted", Action=action)
        result_metadata = (
            result.get("metadata")
            if isinstance(result, Mapping)
            and isinstance(result.get("metadata"), Mapping)
            else {}
        )
        job_input = (
            document.get("input")
            if isinstance(document.get("input"), Mapping)
            else {}
        )
        routing = (
            job_input.get("modelRouting")
            if isinstance(job_input.get("modelRouting"), Mapping)
            else {}
        )
        selected_model = str(
            routing.get("selectedModel")
            or result_metadata.get("modelId")
            or job_input.get("modelPreference")
            or "none"
        )
        total_latency_ms = int((time.perf_counter() - worker_started) * 1000)
        model_latency_ms = int(result_metadata.get("latencyMs") or 0)
        LOGGER.info(
            json.dumps(
                {
                    "event": "ai_worker_completed",
                    "traceId": message.get("traceId"),
                    "jobId": job_id,
                    "tenantHash": _tenant_hash(scope),
                    "action": action,
                    "model": selected_model,
                    "queueWaitMs": queue_wait_ms,
                    "modelLatencyMs": model_latency_ms,
                    "validationAndPersistenceLatencyMs": max(
                        0, total_latency_ms - model_latency_ms
                    ),
                    "totalLatencyMs": total_latency_ms,
                    "retryCount": max(0, receive_count - 1),
                    "inputTokens": int(result_metadata.get("inputTokens") or 0),
                    "outputTokens": int(result_metadata.get("outputTokens") or 0),
                    "estimatedModelCostUsd": float(
                        result_metadata.get("estimatedModelCostUsd") or 0
                    ),
                    "finalStatus": "complete",
                },
                separators=(",", ":"),
            )
        )
        metric(
            "JobTotalLatencyMs",
            total_latency_ms,
            unit="Milliseconds",
            Action=action,
            Model=selected_model,
        )
        metric(
            "JobQueueWaitMs",
            queue_wait_ms,
            unit="Milliseconds",
            Action=action,
        )
    except NonRetryableJobError as exc:
        if automatic_handoff and source_brief_version > 0:
            _precall_handoff_state(
                scope,
                status="failed",
                job_id=job_id,
                source_version=source_brief_version,
                error="The pre-call handoff could not be generated.",
            )
        _record_terminal_failure(scope, job_id, exc)
        LOGGER.warning(
            json.dumps(
                {
                    "event": "ai_worker_non_retryable_failure",
                    "jobId": job_id,
                    "tenantHash": _tenant_hash(scope),
                    "traceId": message.get("traceId"),
                    "receiveCount": receive_count,
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                }
            )
        )
        metric("JobsFailed", Action=str(message.get("action") or "unknown"))
        return
    except Exception as exc:
        if (
            automatic_handoff
            and source_brief_version > 0
            and receive_count >= MAX_RECEIVE_COUNT
        ):
            _precall_handoff_state(
                scope,
                status="failed",
                job_id=job_id,
                source_version=source_brief_version,
                error="The pre-call handoff failed after its final retry.",
            )
        _record_failure(scope, job_id, receive_count, exc)
        LOGGER.exception(
            json.dumps(
                {
                    "event": "ai_worker_failure",
                    "jobId": job_id,
                    "tenantHash": _tenant_hash(scope),
                    "traceId": message.get("traceId"),
                    "receiveCount": receive_count,
                    "errorType": type(exc).__name__,
                }
            )
        )
        metric("JobsFailed", Action=str(message.get("action") or "unknown"))
        raise


def handler(event: object, _context: Any) -> dict[str, Any]:
    if not isinstance(event, Mapping) or not isinstance(event.get("Records"), list):
        raise ValueError("The unified worker requires an SQS event")
    failures = []
    for record in event["Records"]:
        message_id = (
            str(record.get("messageId") or "")
            if isinstance(record, Mapping)
            else ""
        )
        try:
            if not isinstance(record, Mapping):
                raise ValueError("SQS record is invalid")
            _process_record(record)
        except Exception:
            if isinstance(record, Mapping):
                receipt_handle = str(record.get("receiptHandle") or "")
                if JOB_QUEUE_URL and receipt_handle:
                    try:
                        aws_client("sqs").change_message_visibility(
                            QueueUrl=JOB_QUEUE_URL,
                            ReceiptHandle=receipt_handle,
                            VisibilityTimeout=RETRY_VISIBILITY_SECONDS,
                        )
                    except Exception:
                        LOGGER.exception(
                            json.dumps(
                                {
                                    "event": "retry_visibility_change_failed",
                                    "messageId": message_id,
                                }
                            )
                        )
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
