from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

import boto3
from botocore.config import Config
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer


REGION = os.getenv("AWS_REGION", "us-east-1")
PROJECT_TABLE = os.getenv("PROJECT_TABLE", "")
ARTIFACT_BUCKET = os.getenv("ARTIFACT_BUCKET", "")
JOB_QUEUE_URL = os.getenv("JOB_QUEUE_URL", "")
DATA_KMS_KEY_ARN = os.getenv("DATA_KMS_KEY_ARN", "")
API_ORIGIN_VERIFY_SECRET_ARN = os.getenv("API_ORIGIN_VERIFY_SECRET_ARN", "")
DEMO_TENANT_ID = os.getenv("DEMO_TENANT_ID", "demo")
DEMO_ALLOWED_CLIENT_IDS = {
    item.strip()
    for item in os.getenv(
        "DEMO_ALLOWED_CLIENT_IDS",
        "apex-mutual,bluemesa-payments,northstar-health,peakcart-retail,custom-demo",
    ).split(",")
    if item.strip()
}
DEMO_PRESET_CLIENT_IDS = {
    item
    for item in DEMO_ALLOWED_CLIENT_IDS
    if item != "custom-demo"
}
ALLOWED_ORIGINS = tuple(
    item.strip()
    for item in os.getenv(
        "ALLOWED_ORIGINS",
        "https://pilarprep.app",
    ).split(",")
    if item.strip()
)
ALLOW_LOCAL_IDENTITY = os.getenv("ALLOW_LOCAL_IDENTITY", "false").lower() == "true"
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "3600"))
MAX_INPUT_BYTES = int(os.getenv("MAX_INPUT_BYTES", "180000"))
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
AI_ACTIONS = {
    "brief.generate",
    "brief.refine",
    "handoff.generate",
    "catchup.generate",
    "meeting.process",
}
CONTROL_ACTIONS = {"brief.approve", "meeting.approve"}
EVIDENCE_ACTIONS = {
    "evidence.ingest",
    "evidence.delete",
    "evidence.reindex",
}
ACTIONS = AI_ACTIONS | CONTROL_ACTIONS | EVIDENCE_ACTIONS
AUDIENCE_ROLES = {
    "Sales",
    "Solutions Architect",
    "Executive",
    "PM",
    "Engineer",
    "New member",
}
MODEL_PREFERENCES = {"nova-pro", "nova-micro", "claude-sonnet-4.6"}
REFINEMENT_TARGETS = {
    "businessCase",
    "technical",
    "executive",
    "stakeholders",
    "gameplan",
    "objections",
}

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
_AWS_CLIENTS: dict[str, Any] = {}
_API_ORIGIN_VERIFY_SECRET: str | None = None


class AuthorizationError(PermissionError):
    pass



class ScopeAuthorizationError(AuthorizationError):
    """A trusted identity attempted to leave its authorized data scope."""

def aws_client(service_name: str):
    cached = _AWS_CLIENTS.get(service_name)
    if cached is not None:
        return cached
    if service_name == "bedrock-agentcore":
        read_timeout = int(
            os.getenv("AGENT_RUNTIME_READ_TIMEOUT_SECONDS", "300")
        )
        client = boto3.client(
            service_name,
            region_name=REGION,
            config=Config(
                connect_timeout=5,
                read_timeout=read_timeout,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
    elif service_name == "s3":
        client = boto3.client(
            service_name,
            region_name=REGION,
            config=Config(signature_version="s3v4"),
        )
    else:
        client = boto3.client(service_name, region_name=REGION)
    _AWS_CLIENTS[service_name] = client
    return client


def clear_aws_client_cache() -> None:
    """Reset warm clients for isolated tests; production invocations reuse them."""
    global _API_ORIGIN_VERIFY_SECRET
    _API_ORIGIN_VERIFY_SECRET = None
    _AWS_CLIENTS.clear()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field} must contain 1-64 lowercase letters, numbers, or hyphens"
        )
    return value


def require_string(
    value: object,
    field: str,
    *,
    minimum: int = 1,
    maximum: int = 20_000,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if len(normalized) < minimum or len(normalized) > maximum:
        raise ValueError(f"{field} must contain {minimum}-{maximum} characters")
    return normalized


def optional_string(value: object, field: str, maximum: int = 20_000) -> str:
    if value in (None, ""):
        return ""
    return require_string(value, field, maximum=maximum)


def slugify(value: object, fallback: str = "client") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return normalized[:64].rstrip("-") or fallback


def stable_identifier(prefix: str, parts: list[str], length: int = 40) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def dynamodb_client_request_token(prefix: str, parts: list[str]) -> str:
    digest_length = 36 - len(prefix) - 1
    if digest_length < 1:
        raise ValueError("DynamoDB client request token prefix is too long")
    return stable_identifier(prefix, parts, digest_length)


def project_partition_key(scope: Mapping[str, str]) -> str:
    return (
        f"TENANT#{scope['tenantId']}|CLIENT#{scope['clientId']}|"
        f"PROJECT#{scope['projectId']}"
    )


def project_artifact_prefix(scope: Mapping[str, str]) -> str:
    return (
        f"tenants/{scope['tenantId']}/clients/{scope['clientId']}/"
        f"projects/{scope['projectId']}"
    )


def job_object_prefix(scope: Mapping[str, str], job_id: str) -> str:
    return (
        f"jobs/{scope['tenantId']}/{scope['clientId']}/"
        f"{scope['projectId']}/{job_id}"
    )


def job_key(scope: Mapping[str, str], job_id: str) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": f"JOB#{job_id}"},
    }


def idempotency_key(
    scope: Mapping[str, str], value: str
) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": f"IDEMPOTENCY#JOB#{value}"},
    }


def client_directory_key(scope: Mapping[str, str]) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": f"TENANT#{scope['tenantId']}"},
        "sortKey": {"S": f"CLIENT#{scope['clientId']}"},
    }


def identity_tenant_id(prefix: str, subject: str) -> str:
    """Create a non-reversible tenant boundary from a trusted identity."""
    return stable_identifier(prefix, [subject], length=40)


def s3_encryption_args() -> dict[str, str]:
    """Use a configured customer-managed key without weakening local defaults."""
    if DATA_KMS_KEY_ARN:
        return {
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": DATA_KMS_KEY_ARN,
        }
    return {"ServerSideEncryption": "AES256"}



def s3_artifact_args(scope: Mapping[str, str]) -> dict[str, str]:
    """Apply encryption and an explicit retention class to scoped artifacts."""
    arguments = s3_encryption_args()
    tenant_id = str(scope.get("tenantId") or "")
    if tenant_id == DEMO_TENANT_ID or tenant_id.startswith("guest-"):
        arguments["Tagging"] = "RetentionClass=guest-temporary"
    return arguments

def _header(event: Mapping[str, Any], name: str) -> str:
    headers = event.get("headers")
    if not isinstance(headers, Mapping):
        return ""
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value or "")
    return ""


def allowed_origin(event: Mapping[str, Any]) -> str:
    origin = _header(event, "origin")
    if origin and origin in ALLOWED_ORIGINS:
        return origin
    return ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else "https://pilarprep.app"


def assert_secure_request(event: Mapping[str, Any]) -> None:
    forwarded = _header(event, "x-forwarded-proto").lower()
    if forwarded and forwarded != "https":
        raise AuthorizationError("HTTPS is required")
    origin = _header(event, "origin")
    if origin.startswith("http://"):
        local = origin.startswith("http://127.0.0.1") or origin.startswith(
            "http://localhost"
        )
        if not (ALLOW_LOCAL_IDENTITY and local):
            raise AuthorizationError("HTTPS is required")
    if origin and origin not in ALLOWED_ORIGINS:
        local = origin.startswith("http://127.0.0.1") or origin.startswith(
            "http://localhost"
        )
        if not (ALLOW_LOCAL_IDENTITY and local):
            raise AuthorizationError("Origin is not allowed")


def assert_api_origin_verification(event: Mapping[str, Any]) -> None:
    """Require CloudFront's secret header on authenticated workspace routes."""
    request_context = event.get("requestContext")
    http = (
        request_context.get("http", {})
        if isinstance(request_context, Mapping)
        else {}
    )
    path = str(http.get("path") or event.get("rawPath") or event.get("path") or "")
    if not path.startswith("/workspace/") or not API_ORIGIN_VERIFY_SECRET_ARN:
        return
    global _API_ORIGIN_VERIFY_SECRET
    if _API_ORIGIN_VERIFY_SECRET is None:
        value = aws_client("secretsmanager").get_secret_value(
            SecretId=API_ORIGIN_VERIFY_SECRET_ARN
        ).get("SecretString")
        if not isinstance(value, str) or len(value) < 32:
            raise RuntimeError("API origin verification is unavailable")
        _API_ORIGIN_VERIFY_SECRET = value
    supplied = _header(event, "x-pilarprep-origin-verify")
    if not supplied or not hmac.compare_digest(
        supplied.encode("utf-8"),
        _API_ORIGIN_VERIFY_SECRET.encode("utf-8"),
    ):
        raise AuthorizationError("This operation is not available")


def response(
    event: Mapping[str, Any], status_code: int, body: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": allowed_origin(event),
            "access-control-allow-headers": (
                "accept,authorization,content-type,x-amz-content-sha256,"
                "x-amz-date,x-amz-security-token"
            ),
            "access-control-allow-methods": "GET,POST,OPTIONS",
            "cache-control": "no-store",
            "vary": "origin",
            "x-content-type-options": "nosniff",
        },
        "body": json.dumps(body, separators=(",", ":"), default=str),
    }


def read_json_body(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = event.get("body", {})
    if isinstance(raw, str):
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        if len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError("Request body is too large")
        parsed = json.loads(raw or "{}")
    else:
        parsed = raw
    if not isinstance(parsed, dict):
        raise ValueError("Request body must be a JSON object")
    return parsed


def _csv_claim(claims: Mapping[str, Any], key: str) -> set[str]:
    value = claims.get(key)
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    return set()


def derive_scope(
    event: Mapping[str, Any], request: Mapping[str, Any]
) -> dict[str, str]:
    client_id = require_identifier(request.get("clientId"), "clientId")
    project_id = require_identifier(request.get("projectId"), "projectId")
    session_id = require_identifier(
        request.get("sessionId") or "browser-session", "sessionId"
    )
    request_context = event.get("requestContext")
    authorizer = (
        request_context.get("authorizer", {})
        if isinstance(request_context, Mapping)
        else {}
    )
    jwt = authorizer.get("jwt") if isinstance(authorizer, Mapping) else None
    claims = jwt.get("claims") if isinstance(jwt, Mapping) else None

    if isinstance(claims, Mapping):
        subject = require_string(claims.get("sub"), "subject claim", maximum=240)
        claimed_tenant = claims.get("custom:tenantId")
        tenant_id = (
            require_identifier(claimed_tenant, "tenantId claim")
            if claimed_tenant
            else identity_tenant_id("personal", subject)
        )
        allowed_clients = _csv_claim(claims, "custom:clientIds")
        effective_clients = allowed_clients or DEMO_ALLOWED_CLIENT_IDS
        allowed_projects = _csv_claim(claims, "custom:projectIds")
        if client_id not in effective_clients:
            raise ScopeAuthorizationError("Identity is not assigned to this client")
        if allowed_projects and project_id not in allowed_projects:
            raise ScopeAuthorizationError("Identity is not assigned to this project")
        user_id = stable_identifier("user", [subject])
        identity_type = "authenticated"
        user_tier = (
            "premium"
            if "PilarPrepPremium" in _csv_claim(claims, "cognito:groups")
            else "standard"
        )
    else:
        iam = authorizer.get("iam") if isinstance(authorizer, Mapping) else None
        cognito_identity = (
            iam.get("cognitoIdentity") if isinstance(iam, Mapping) else None
        )
        principal = ""
        if isinstance(cognito_identity, Mapping):
            principal = str(cognito_identity.get("identityId") or "")
        if not principal and isinstance(iam, Mapping):
            principal = str(iam.get("userArn") or iam.get("callerId") or "")
        if not principal and ALLOW_LOCAL_IDENTITY:
            principal = str(event.get("localIdentity") or "local-test-user")
        if not principal:
            raise AuthorizationError("IAM identity was not available")
        if client_id not in DEMO_ALLOWED_CLIENT_IDS:
            raise ScopeAuthorizationError("Identity is not assigned to this client")
        user_id = stable_identifier("user", [principal])
        tenant_id = identity_tenant_id("guest", principal)
        if project_id == client_id:
            pass
        else:
            raise ScopeAuthorizationError("Guest projects must match their client scope")
        identity_type = "guest"
        user_tier = "guest"

    return {
        "tenantId": tenant_id,
        "clientId": client_id,
        "projectId": project_id,
        "userId": user_id,
        "sessionId": session_id,
        "identityType": identity_type,
        "userTier": user_tier,
    }


def derive_list_identity(event: Mapping[str, Any]) -> dict[str, Any]:
    request_context = event.get("requestContext")
    authorizer = (
        request_context.get("authorizer", {})
        if isinstance(request_context, Mapping)
        else {}
    )
    jwt = authorizer.get("jwt") if isinstance(authorizer, Mapping) else None
    claims = jwt.get("claims") if isinstance(jwt, Mapping) else None
    if isinstance(claims, Mapping):
        subject = require_string(claims.get("sub"), "subject claim", maximum=240)
        claimed_tenant = claims.get("custom:tenantId")
        tenant_id = (
            require_identifier(claimed_tenant, "tenantId claim")
            if claimed_tenant
            else identity_tenant_id("personal", subject)
        )
        allowed_clients = _csv_claim(claims, "custom:clientIds")
        return {
            "tenantId": tenant_id,
            "userId": stable_identifier("user", [subject]),
            "allowedClients": allowed_clients or set(DEMO_ALLOWED_CLIENT_IDS),
            "identityType": "authenticated",
        }

    iam = authorizer.get("iam") if isinstance(authorizer, Mapping) else None
    cognito_identity = iam.get("cognitoIdentity") if isinstance(iam, Mapping) else None
    principal = ""
    if isinstance(cognito_identity, Mapping):
        principal = str(cognito_identity.get("identityId") or "")
    if not principal and isinstance(iam, Mapping):
        principal = str(iam.get("userArn") or iam.get("callerId") or "")
    if not principal and ALLOW_LOCAL_IDENTITY:
        principal = str(event.get("localIdentity") or "local-test-user")
    if not principal:
        raise AuthorizationError("IAM identity was not available")
    return {
        "tenantId": identity_tenant_id("guest", principal),
        "userId": stable_identifier("user", [principal]),
        "allowedClients": set(DEMO_ALLOWED_CLIENT_IDS),
        "identityType": "guest",
    }


def validate_job_request(payload: object) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("Request body must be a JSON object")
    action = require_string(payload.get("action"), "action", maximum=32)
    if action not in ACTIONS:
        raise ValueError("Unsupported job action")
    request_value = payload.get("input")
    if not isinstance(request_value, Mapping):
        raise ValueError("input must be an object")
    request = dict(request_value)
    model_preference = str(request.get("modelPreference") or "nova-pro")
    if model_preference == "default":
        model_preference = "nova-pro"
    if model_preference not in MODEL_PREFERENCES:
        raise ValueError(
            "modelPreference must be nova-pro, nova-micro, or claude-sonnet-4.6"
        )
    request["modelPreference"] = model_preference
    quality_tier = str(request.get("qualityTier") or "standard")
    if quality_tier not in {"fast", "standard", "premium"}:
        raise ValueError(
            "input.qualityTier must be fast, standard, or premium"
        )
    request["qualityTier"] = quality_tier

    if action in {"brief.generate", "brief.refine"}:
        require_string(request.get("company"), "input.company", maximum=160)
        if action == "brief.refine":
            refinement_target = require_string(
                request.get("refinementTarget"),
                "input.refinementTarget",
                maximum=32,
            )
            if refinement_target not in REFINEMENT_TARGETS:
                raise ValueError("input.refinementTarget is not supported")
            if not isinstance(request.get("previousBrief"), Mapping):
                raise ValueError("brief.refine requires input.previousBrief")
    elif action == "brief.approve":
        version = request.get("packetVersion")
        if not isinstance(version, int) or version < 1:
            raise ValueError("brief.approve requires a positive input.packetVersion")
    elif action == "meeting.process":
        scenario_id = require_identifier(
            request.get("scenarioId"), "input.scenarioId"
        )
        if scenario_id != "blue-mesa-payments":
            raise AuthorizationError(
                "Meeting processing is limited to the Blue Mesa public demo"
            )
        request["meetingId"] = require_identifier(
            request.get("meetingId"), "input.meetingId"
        )
        request["audioUploadId"] = require_identifier(
            request.get("audioUploadId"), "input.audioUploadId"
        )
        request.pop("audioKey", None)
        expected_version = request.get("expectedApprovedPacketVersion")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise ValueError(
                "meeting.process requires a positive "
                "input.expectedApprovedPacketVersion"
            )
        request.pop("enablePiiRedaction", None)
    elif action == "meeting.approve":
        scenario_id = require_identifier(
            request.get("scenarioId"), "input.scenarioId"
        )
        if scenario_id != "blue-mesa-payments":
            raise AuthorizationError(
                "Meeting approval is limited to the Blue Mesa public demo"
            )
        request["meetingId"] = require_identifier(
            request.get("meetingId"), "input.meetingId"
        )
        request["proposalId"] = require_identifier(
            request.get("proposalId"), "input.proposalId"
        )
        expected_version = request.get("expectedApprovedPacketVersion")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise ValueError(
                "meeting.approve requires a positive "
                "input.expectedApprovedPacketVersion"
            )
        dispositions = request.get("dispositions")
        if not isinstance(dispositions, list) or not dispositions:
            raise ValueError("meeting.approve requires reviewed input.dispositions")
        if len(dispositions) > 80:
            raise ValueError("meeting.approve accepts at most 80 review dispositions")
        normalized_dispositions = []
        seen_ids: set[str] = set()
        for index, disposition in enumerate(dispositions):
            if not isinstance(disposition, Mapping):
                raise ValueError(
                    f"input.dispositions[{index}] must be an object"
                )
            item_id = require_identifier(
                disposition.get("id"), f"input.dispositions[{index}].id"
            )
            if item_id in seen_ids:
                raise ValueError("input.dispositions contains duplicate ids")
            seen_ids.add(item_id)
            decision = require_string(
                disposition.get("decision"),
                f"input.dispositions[{index}].decision",
                maximum=16,
            )
            if decision not in {"accepted", "edited", "rejected"}:
                raise ValueError(
                    "Review decisions must be accepted, edited, or rejected"
                )
            edited_statement = optional_string(
                disposition.get("editedStatement"),
                f"input.dispositions[{index}].editedStatement",
                2_000,
            )
            if decision == "edited" and not edited_statement:
                raise ValueError("Edited review items require editedStatement")
            normalized_dispositions.append(
                {
                    "id": item_id,
                    "decision": decision,
                    "editedStatement": edited_statement,
                }
            )
        request["dispositions"] = normalized_dispositions
    elif action in EVIDENCE_ACTIONS:
        request["documentId"] = require_identifier(
            request.get("documentId"), "input.documentId"
        )
        if action == "evidence.ingest":
            request["fileName"] = require_string(
                request.get("fileName"), "input.fileName", maximum=180
            )
            request["sourceTitle"] = require_string(
                request.get("sourceTitle"), "input.sourceTitle", maximum=240
            )
            request["documentType"] = require_string(
                request.get("documentType"), "input.documentType", maximum=64
            )
            content = optional_string(
                request.get("content"), "input.content", 120_000
            )
            content_base64 = optional_string(
                request.get("contentBase64"),
                "input.contentBase64",
                6_700_000,
            )
            source_url = optional_string(
                request.get("sourceUrl"), "input.sourceUrl", 2_048
            )
            supplied_sources = [
                value
                for value in (content, content_base64, source_url)
                if value
            ]
            if len(supplied_sources) != 1:
                raise ValueError(
                    "evidence.ingest requires exactly one of input.content, "
                    "input.contentBase64, or input.sourceUrl"
                )
            if content and len(content) < 20:
                raise ValueError("input.content must be at least 20 characters")
            request["content"] = content
            request["contentBase64"] = content_base64
            request["contentType"] = optional_string(
                request.get("contentType"), "input.contentType", 160
            )
            request["sourceUrl"] = source_url
            request["source"] = optional_string(
                request.get("source"), "input.source", 80
            )
            request["sourceType"] = optional_string(
                request.get("sourceType"), "input.sourceType", 80
            )
            request["approvedBy"] = optional_string(
                request.get("approvedBy"), "input.approvedBy", 120
            )
    else:
        audience = require_string(
            request.get("audienceRole", "PM"), "input.audienceRole", maximum=32
        )
        if audience not in AUDIENCE_ROLES:
            raise ValueError("input.audienceRole is not supported")
        request["audienceRole"] = audience
        request["focus"] = optional_string(request.get("focus"), "input.focus", 500)
        request["meetingNotes"] = optional_string(
            request.get("meetingNotes"), "input.meetingNotes", 20_000
        )
        if action == "handoff.generate":
            approved_version = request.get("expectedApprovedPacketVersion")
            if (
                isinstance(approved_version, bool)
                or not isinstance(approved_version, int)
                or approved_version < 1
            ):
                raise ValueError(
                    "handoff.generate requires a positive "
                    "input.expectedApprovedPacketVersion"
                )

    return {
        "action": action,
        "clientId": require_identifier(payload.get("clientId"), "clientId"),
        "projectId": require_identifier(payload.get("projectId"), "projectId"),
        "sessionId": require_identifier(payload.get("sessionId"), "sessionId"),
        "idempotencyKey": require_identifier(
            payload.get("idempotencyKey"), "idempotencyKey"
        ),
        "input": request,
    }


def deserialize_item(item: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        return {}

    def safe(value: Any) -> Any:
        if isinstance(value, Decimal):
            return int(value) if value % 1 == 0 else float(value)
        if isinstance(value, list):
            return [safe(entry) for entry in value]
        if isinstance(value, dict):
            return {key: safe(entry) for key, entry in value.items()}
        return value

    return safe({key: _DESERIALIZER.deserialize(value) for key, value in item.items()})


def serialize(value: Any) -> dict[str, Any]:
    return _SERIALIZER.serialize(value)


def metric(name: str, value: float = 1, unit: str = "Count", **dimensions: str) -> None:
    metric_dimensions = {"Service": "JobsPipeline", **dimensions}
    dimension_sets = [["Service"]]
    if len(metric_dimensions) > 1:
        dimension_sets.append(list(metric_dimensions.keys()))
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "PilarPrep",
                            "Dimensions": dimension_sets,
                            "Metrics": [{"Name": name, "Unit": unit}],
                        }
                    ],
                },
                name: value,
                **metric_dimensions,
            }
        )
    )
