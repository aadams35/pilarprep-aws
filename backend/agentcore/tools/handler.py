from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

try:
    from common.contracts import empty_project_state, normalize_project_update, require_string
    from common.identifiers import (
        project_artifact_prefix,
        project_partition_key,
        require_identifier,
    )
    from common.security import assert_event_scope, verify_scope_token
    from tools.docx import handoff_docx_bytes
except ModuleNotFoundError:
    from agentcore.common.contracts import (
        empty_project_state,
        normalize_project_update,
        require_string,
    )
    from agentcore.common.identifiers import (
        project_artifact_prefix,
        project_partition_key,
        require_identifier,
    )
    from agentcore.common.security import assert_event_scope, verify_scope_token
    from agentcore.tools.docx import handoff_docx_bytes


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

REGION = os.getenv("AWS_REGION", "us-east-1")
ARTIFACT_BUCKET = os.getenv("ARTIFACT_BUCKET", "")
PROJECT_TABLE = os.getenv("PROJECT_TABLE", "")
SCOPE_SECRET_ARN = os.getenv("SCOPE_SECRET_ARN", "")
DATA_KMS_KEY_ARN = os.getenv("DATA_KMS_KEY_ARN", "")
DEMO_TENANT_ID = os.getenv("DEMO_TENANT_ID", "demo")
DEMO_LEGACY_CLIENT_ID = os.getenv("DEMO_LEGACY_CLIENT_ID", "bluemesa-payments")
DEMO_ALLOWED_CLIENT_IDS = {
    item.strip()
    for item in os.getenv("DEMO_ALLOWED_CLIENT_IDS", DEMO_LEGACY_CLIENT_ID).split(",")
    if item.strip()
}
ALLOW_LEGACY_DEMO_BRIEF = os.getenv("ALLOW_LEGACY_DEMO_BRIEF", "false").lower() == "true"

_SECRET: str | None = None
_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()



def _s3_artifact_args(scope: Mapping[str, str]) -> dict[str, str]:
    if DATA_KMS_KEY_ARN:
        arguments = {
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": DATA_KMS_KEY_ARN,
        }
    else:
        arguments = {"ServerSideEncryption": "AES256"}
    tenant_id = str(scope.get("tenantId") or "")
    if tenant_id == DEMO_TENANT_ID or tenant_id.startswith("guest-"):
        arguments["Tagging"] = "RetentionClass=guest-temporary"
    return arguments

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _idempotency_expiry() -> str:
    return str(int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp()))


def _client(service_name: str):
    if service_name == "s3":
        return boto3.client(
            service_name,
            region_name=REGION,
            config=Config(signature_version="s3v4"),
        )
    return boto3.client(service_name, region_name=REGION)


def _scope_secret() -> str:
    global _SECRET
    if _SECRET:
        return _SECRET
    if not SCOPE_SECRET_ARN:
        raise RuntimeError("SCOPE_SECRET_ARN is not configured")
    response = _client("secretsmanager").get_secret_value(SecretId=SCOPE_SECRET_ARN)
    value = response.get("SecretString")
    if not isinstance(value, str):
        raise RuntimeError("AgentCore scope secret must be a string")
    _SECRET = value
    return value


def _tool_name(event: Mapping[str, Any], context: Any) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None)
    raw_name = custom.get("bedrockAgentCoreToolName") if isinstance(custom, Mapping) else None
    if not raw_name:
        raw_name = event.get("_toolName") or event.get("toolName")
    if not isinstance(raw_name, str):
        raise ValueError("AgentCore Gateway did not provide a tool name")
    return raw_name.rsplit("___", 1)[-1]


def _authorized_scope(event: Mapping[str, Any]) -> dict[str, str]:
    scope = verify_scope_token(event.get("scopeToken"), _scope_secret())
    assert_event_scope(event, scope)
    return scope


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _deserialize_map(value: Mapping[str, Any]) -> dict[str, Any]:
    return _json_safe({key: _DESERIALIZER.deserialize(item) for key, item in value.items()})


def _read_json_object(key: str) -> dict[str, Any] | None:
    if not ARTIFACT_BUCKET:
        raise RuntimeError("ARTIFACT_BUCKET is not configured")
    try:
        response = _client("s3").get_object(Bucket=ARTIFACT_BUCKET, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None
        raise
    raw = response["Body"].read()
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Stored artifact is not a JSON object")
    return parsed


def _latest_brief_state(scope: Mapping[str, str]) -> dict[str, Any]:
    if not PROJECT_TABLE:
        raise RuntimeError("PROJECT_TABLE is not configured")
    item = _client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key={
            "projectId": {"S": project_partition_key(dict(scope))},
            "sortKey": {"S": "BRIEF#LATEST"},
        },
        ConsistentRead=True,
    ).get("Item")
    return _deserialize_map(item) if isinstance(item, Mapping) else {}


def _validate_approved_brief(
    scope: Mapping[str, str],
    document: Mapping[str, Any],
    approved_version: int,
) -> None:
    stored_scope = document.get("scope")
    if isinstance(stored_scope, Mapping) and any(
        stored_scope.get(field) != scope[field]
        for field in ("tenantId", "clientId", "projectId")
    ):
        raise PermissionError("Approved brief scope does not match the authorized project")
    metadata = document.get("response", {}).get("metadata", {})
    packet_version = int(
        document.get("packetVersion")
        or (metadata.get("packetVersion") if isinstance(metadata, Mapping) else 0)
        or 0
    )
    approval_status = document.get("approvalStatus") or (
        metadata.get("approvalStatus") if isinstance(metadata, Mapping) else None
    )
    if packet_version != approved_version or approval_status != "approved":
        raise ValueError("Approved brief pointer does not match the stored packet")


def get_latest_brief(scope: Mapping[str, str]) -> dict[str, Any]:
    latest = _latest_brief_state(scope)
    approved_version = int(latest.get("approvedPacketVersion") or 0)
    scoped_key = str(latest.get("approvedArtifactKey") or "")
    immutable_key = (
        f"{project_artifact_prefix(dict(scope))}/brief/approved/"
        f"v{approved_version:06d}/packet.json"
    )
    scoped_latest_key = f"{project_artifact_prefix(dict(scope))}/brief/latest.json"
    document = None
    source = "approved-pointer"
    if scoped_key:
        if not approved_version or scoped_key not in {
            immutable_key,
            scoped_latest_key,
        }:
            raise PermissionError("Approved brief pointer is outside the authorized project")
        if scoped_key == scoped_latest_key:
            source = "scoped-latest-pointer"
        document = _read_json_object(scoped_key)
        if document is not None:
            _validate_approved_brief(scope, document, approved_version)

    if (
        document is None
        and ALLOW_LEGACY_DEMO_BRIEF
        and scope["tenantId"] == DEMO_TENANT_ID
        and scope["clientId"] in DEMO_ALLOWED_CLIENT_IDS
    ):
        scoped_key = f"clients/{scope['clientId']}/brief/latest.json"
        document = _read_json_object(scoped_key)
        source = "legacy-demo"

    if document is None:
        raise LookupError("No approved brief exists for this authorized project")

    response = document.get("response") if isinstance(document.get("response"), dict) else document
    request = document.get("request") if isinstance(document.get("request"), dict) else {}
    packet_version = document.get("packetVersion") or document.get("briefVersion")
    return {
        "brief": response,
        "requestContext": request,
        "metadata": {
            "artifactKey": scoped_key,
            "storedAt": document.get("approvedAt") or document.get("storedAt"),
            "briefVersion": packet_version,
            "packetVersion": packet_version,
            "approvalStatus": document.get("approvalStatus"),
            "source": source,
        },
    }


def get_project_state(scope: Mapping[str, str]) -> dict[str, Any]:
    if not PROJECT_TABLE:
        raise RuntimeError("PROJECT_TABLE is not configured")
    key = project_partition_key(dict(scope))
    response = _client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key={"projectId": {"S": key}, "sortKey": {"S": "PROJECT#STATE"}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not isinstance(item, Mapping):
        return {**empty_project_state(), "stateKey": "PROJECT#STATE"}

    state_value = item.get("state")
    state = (
        _DESERIALIZER.deserialize(state_value)
        if isinstance(state_value, Mapping)
        else empty_project_state()
    )
    if not isinstance(state, dict):
        state = empty_project_state()
    state["version"] = int(item.get("version", {}).get("N", "0"))
    state["updatedAt"] = item.get("updatedAt", {}).get("S", "")
    state["stateKey"] = "PROJECT#STATE"
    return _json_safe(state)


def _idempotency_sort_key(tool_name: str, idempotency_key: str) -> str:
    return f"IDEMPOTENCY#{tool_name}#{idempotency_key}"


def _idempotency_exists(scope: Mapping[str, str], tool_name: str, key: str) -> bool:
    response = _client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key={
            "projectId": {"S": project_partition_key(dict(scope))},
            "sortKey": {"S": _idempotency_sort_key(tool_name, key)},
        },
        ConsistentRead=True,
        ProjectionExpression="projectId",
    )
    return bool(response.get("Item"))


def save_project_update(
    scope: Mapping[str, str],
    update: object,
    *,
    expected_version: object,
    idempotency_key: object,
    confirm_write: object,
) -> dict[str, Any]:
    if confirm_write is not True:
        raise PermissionError("Project-state writes require explicit confirmation")
    if not isinstance(expected_version, int) or expected_version < 0:
        raise ValueError("expectedVersion must be a non-negative integer")
    key = require_identifier(idempotency_key, "idempotencyKey")
    validated_update = normalize_project_update(update)
    partition_key = project_partition_key(dict(scope))
    timestamp = _now()

    transact = [
        {
            "Put": {
                "TableName": PROJECT_TABLE,
                "Item": {
                    "projectId": {"S": partition_key},
                    "sortKey": {"S": _idempotency_sort_key("save_project_update", key)},
                    "createdAt": {"S": timestamp},
                    "expiresAt": {"N": _idempotency_expiry()},
                    "tool": {"S": "save_project_update"},
                },
                "ConditionExpression": "attribute_not_exists(projectId)",
            }
        },
        {
            "Update": {
                "TableName": PROJECT_TABLE,
                "Key": {
                    "projectId": {"S": partition_key},
                    "sortKey": {"S": "PROJECT#STATE"},
                },
                "UpdateExpression": (
                    "SET #state = :state, #version = :nextVersion, updatedAt = :updatedAt, "
                    "updatedBy = :updatedBy, tenantId = :tenantId, clientId = :clientId, "
                    "projectScopeId = :projectScopeId"
                ),
                "ConditionExpression": "attribute_not_exists(#version) OR #version = :expectedVersion",
                "ExpressionAttributeNames": {"#state": "state", "#version": "version"},
                "ExpressionAttributeValues": {
                    ":state": _SERIALIZER.serialize(validated_update),
                    ":nextVersion": {"N": str(expected_version + 1)},
                    ":expectedVersion": {"N": str(expected_version)},
                    ":updatedAt": {"S": timestamp},
                    ":updatedBy": {"S": scope["userId"]},
                    ":tenantId": {"S": scope["tenantId"]},
                    ":clientId": {"S": scope["clientId"]},
                    ":projectScopeId": {"S": scope["projectId"]},
                },
            }
        },
    ]

    try:
        _client("dynamodb").transact_write_items(TransactItems=transact)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            if _idempotency_exists(scope, "save_project_update", key):
                return {**get_project_state(scope), "idempotent": True}
            raise RuntimeError("Project state changed; reload before saving this update") from exc
        raise

    return {
        **validated_update,
        "version": expected_version + 1,
        "updatedAt": timestamp,
        "stateKey": "PROJECT#STATE",
        "idempotent": False,
    }


def _purge_prefix(s3: Any, prefix: str, keep_versions: set[tuple[str, str]]) -> None:
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
            for collection in (page.get("Versions", []), page.get("DeleteMarkers", []))
            for item in collection
            if item.get("Key")
            and item.get("VersionId")
            and (item["Key"], item["VersionId"]) not in keep_versions
        ]
        if objects:
            s3.delete_objects(
                Bucket=ARTIFACT_BUCKET, Delete={"Objects": objects, "Quiet": True}
            )
        if not page.get("IsTruncated"):
            break
        key_marker = page.get("NextKeyMarker")
        version_marker = page.get("NextVersionIdMarker")


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


def _handoff_result(
    s3: Any,
    json_key: str,
    docx_key: str,
    *,
    timestamp: str = "",
    idempotent: bool,
    download_filename: str,
) -> dict[str, Any]:
    result = {
        "artifactKey": json_key,
        "docxArtifactKey": docx_key,
        "docxDownloadUrl": s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": ARTIFACT_BUCKET,
                "Key": docx_key,
                "ResponseContentDisposition": _content_disposition(download_filename),
            },
            ExpiresIn=3600,
        ),
        "artifactRetention": "latest-only",
        "idempotent": idempotent,
    }
    if timestamp:
        result["storedAt"] = timestamp
    return result


def create_handoff_packet(
    scope: Mapping[str, str],
    packet: object,
    *,
    audience: object,
    idempotency_key: object,
    confirm_write: object,
) -> dict[str, Any]:
    if confirm_write is not True:
        raise PermissionError("Handoff artifact writes require explicit confirmation")
    if not isinstance(packet, Mapping):
        raise ValueError("packet must be an object")
    if not packet.get("projectAnswer") or not packet.get("projectArtifacts"):
        raise ValueError("packet requires projectAnswer and projectArtifacts")
    role = require_string(audience, "audience", maximum=32)
    key = require_identifier(idempotency_key, "idempotencyKey")
    prefix = f"{project_artifact_prefix(dict(scope))}/handoff/"
    json_key = f"{prefix}latest.json"
    docx_key = f"{prefix}latest.docx"
    s3 = _client("s3")
    packet_version = packet.get("metadata", {}).get("packetVersion") if isinstance(packet.get("metadata"), Mapping) else packet.get("packetVersion")
    download_filename = _artifact_download_filename(
        packet.get("company") or scope["clientId"], "handoff", packet_version
    )
    if _idempotency_exists(scope, "create_handoff_packet", key):
        return _handoff_result(
            s3,
            json_key,
            docx_key,
            idempotent=True,
            download_filename=download_filename,
        )

    timestamp = _now()
    document = {
        "scope": {
            "tenantId": scope["tenantId"],
            "clientId": scope["clientId"],
            "projectId": scope["projectId"],
        },
        "audience": role,
        "storedAt": timestamp,
        "packet": dict(packet),
    }
    json_result = s3.put_object(
        Bucket=ARTIFACT_BUCKET,
        Key=json_key,
        Body=json.dumps(document, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
        **_s3_artifact_args(scope),
    )
    docx_result = s3.put_object(
        Bucket=ARTIFACT_BUCKET,
        Key=docx_key,
        Body=handoff_docx_bytes(packet, scope),
        ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ContentDisposition=_content_disposition(download_filename),
        **_s3_artifact_args(scope),
    )
    version_pairs = {
        (json_key, json_result.get("VersionId")) if isinstance(json_result, dict) else None,
        (docx_key, docx_result.get("VersionId")) if isinstance(docx_result, dict) else None,
    }
    keep_versions = {
        (object_key, version)
        for pair in version_pairs
        if pair is not None
        for object_key, version in [pair]
        if isinstance(version, str) and version and version != "null"
    }
    if len(keep_versions) == 2:
        _purge_prefix(s3, prefix, keep_versions)
    try:
        _client("dynamodb").put_item(
            TableName=PROJECT_TABLE,
            Item={
                "projectId": {"S": project_partition_key(dict(scope))},
                "sortKey": {"S": _idempotency_sort_key("create_handoff_packet", key)},
                "createdAt": {"S": timestamp},
                "expiresAt": {"N": _idempotency_expiry()},
                "tool": {"S": "create_handoff_packet"},
                "artifactKey": {"S": json_key},
                "docxArtifactKey": {"S": docx_key},
            },
            ConditionExpression="attribute_not_exists(projectId)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return _handoff_result(
            s3,
            json_key,
            docx_key,
            idempotent=True,
            download_filename=download_filename,
        )

    return _handoff_result(
        s3,
        json_key,
        docx_key,
        timestamp=timestamp,
        idempotent=False,
        download_filename=download_filename,
    )

def generate_catchup(
    scope: Mapping[str, str], audience_role: object, focus: object
) -> dict[str, Any]:
    role = require_string(audience_role, "audienceRole", maximum=32)
    focus_text = "" if focus in (None, "") else require_string(focus, "focus", maximum=500)
    role_lenses = {
        "Sales": ["business outcomes", "stakeholders", "next customer commitment"],
        "Solutions Architect": ["confirmed customer context", "business scenario and urgency", "ranked Well-Architected priorities", "architecture assumptions and unknowns", "required evidence", "RTO/RPO and compliance validation", "AWS options with rationale", "risks and decision gates", "next technical session"],
        "Executive": ["business risk", "decisions", "success measures"],
        "PM": ["owners", "actions", "milestones", "open questions"],
        "Engineer": ["technical assumptions", "risks", "first build steps"],
        "New member": ["why the project exists", "current state", "where to start"],
    }
    return {
        "audienceRole": role,
        "focus": focus_text,
        "recommendedLenses": role_lenses.get(role, role_lenses["New member"]),
        "sources": [
            f"{project_partition_key(dict(scope))}#BRIEF#LATEST.approvedArtifactKey",
            f"{project_partition_key(dict(scope))}#PROJECT#STATE",
        ],
    }


def handler(event: object, context: Any) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ValueError("Tool input must be a JSON object")
    tool_name = _tool_name(event, context)
    scope = _authorized_scope(event)
    LOGGER.info(
        json.dumps(
            {
                "event": "agentcore_tool_invocation",
                "tool": tool_name,
                "tenantId": scope["tenantId"],
                "clientId": scope["clientId"],
                "projectId": scope["projectId"],
            }
        )
    )

    if tool_name == "get_latest_brief":
        return get_latest_brief(scope)
    if tool_name == "get_project_state":
        return get_project_state(scope)
    if tool_name == "save_project_update":
        return save_project_update(
            scope,
            event.get("update"),
            expected_version=event.get("expectedVersion"),
            idempotency_key=event.get("idempotencyKey"),
            confirm_write=event.get("confirmWrite"),
        )
    if tool_name == "create_handoff_packet":
        return create_handoff_packet(
            scope,
            event.get("packet"),
            audience=event.get("audience"),
            idempotency_key=event.get("idempotencyKey"),
            confirm_write=event.get("confirmWrite"),
        )
    if tool_name == "generate_catchup":
        return generate_catchup(scope, event.get("audienceRole"), event.get("focus"))
    raise ValueError(f"Unsupported AgentCore tool: {tool_name}")
