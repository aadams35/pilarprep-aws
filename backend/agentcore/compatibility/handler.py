from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

import boto3
from botocore.config import Config

from common.contracts import validate_router_request
from common.identifiers import project_partition_key, require_identifier, stable_identifier
from common.security import sign_scope_token


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

REGION = os.getenv("AWS_REGION", "us-east-1")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")
AGENT_RUNTIME_ARN = os.getenv("AGENT_RUNTIME_ARN", "")
FALLBACK_FUNCTION_ARN = os.getenv("FALLBACK_FUNCTION_ARN", "")
SCOPE_SECRET_ARN = os.getenv("SCOPE_SECRET_ARN", "")
PROJECT_TABLE = os.getenv("PROJECT_TABLE", "")
AGENT_WORKER_FUNCTION = os.getenv("AGENT_WORKER_FUNCTION", "")
AGENT_RUNTIME_READ_TIMEOUT_SECONDS = int(
    os.getenv("AGENT_RUNTIME_READ_TIMEOUT_SECONDS", "540")
)
JOB_TTL_MINUTES = 60
MAX_JOB_RESULT_BYTES = 350 * 1024
DEMO_TENANT_ID = os.getenv("DEMO_TENANT_ID", "demo")
DEMO_ALLOWED_CLIENT_IDS = {
    item.strip()
    for item in os.getenv("DEMO_ALLOWED_CLIENT_IDS", "bluemesa-payments").split(",")
    if item.strip()
}
ALLOW_LOCAL_IDENTITY = os.getenv("ALLOW_LOCAL_IDENTITY", "false").lower() == "true"

_SECRET: str | None = None


class AuthorizationError(PermissionError):
    pass


def _client(service_name: str):
    if service_name == "bedrock-agentcore":
        return boto3.client(
            service_name,
            region_name=REGION,
            config=Config(
                connect_timeout=5,
                read_timeout=AGENT_RUNTIME_READ_TIMEOUT_SECONDS,
                retries={"max_attempts": 0, "mode": "standard"},
            ),
        )
    return boto3.client(service_name, region_name=REGION)


def _response(status_code: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": ALLOWED_ORIGIN,
            "cache-control": "no-store",
        },
        "body": json.dumps(body, separators=(",", ":")),
    }


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = event.get("body", event)
    if isinstance(raw, str):
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        parsed = json.loads(raw)
    else:
        parsed = raw
    if not isinstance(parsed, dict):
        raise ValueError("Request body must be a JSON object")
    return parsed


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


def _csv_claim(claims: Mapping[str, Any], key: str) -> set[str]:
    value = claims.get(key)
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    return set()


def _derive_scope(
    event: Mapping[str, Any], request: Mapping[str, Any]
) -> dict[str, str]:
    request_context = event.get("requestContext")
    authorizer = (
        request_context.get("authorizer", {})
        if isinstance(request_context, Mapping)
        else {}
    )
    jwt = authorizer.get("jwt") if isinstance(authorizer, Mapping) else None
    claims = jwt.get("claims") if isinstance(jwt, Mapping) else None

    client_id = require_identifier(request.get("clientId"), "clientId")
    project_id = require_identifier(request.get("projectId"), "projectId")
    session_id = require_identifier(request.get("sessionId"), "sessionId")

    if isinstance(claims, Mapping):
        tenant_id = require_identifier(claims.get("custom:tenantId"), "tenantId claim")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AuthorizationError("Authenticated identity is missing a subject claim")
        allowed_clients = _csv_claim(claims, "custom:clientIds")
        allowed_projects = _csv_claim(claims, "custom:projectIds")
        if client_id not in allowed_clients:
            raise AuthorizationError("Identity is not assigned to this client")
        if allowed_projects and project_id not in allowed_projects:
            raise AuthorizationError("Identity is not assigned to this project")
        user_id = stable_identifier("user", [subject])
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
            raise AuthorizationError("IAM identity was not available to the AgentCore router")
        if client_id not in DEMO_ALLOWED_CLIENT_IDS:
            raise AuthorizationError("The demo identity is not assigned to this client")
        tenant_id = require_identifier(DEMO_TENANT_ID, "DEMO_TENANT_ID")
        user_id = stable_identifier("user", [principal])

    supplied_tenant = request.get("tenantId")
    supplied_user = request.get("userId")
    if supplied_tenant not in (None, "", tenant_id):
        raise AuthorizationError("Browser tenantId does not match authenticated scope")
    if supplied_user not in (None, "", user_id):
        raise AuthorizationError("Browser userId does not match authenticated scope")

    return {
        "tenantId": tenant_id,
        "clientId": client_id,
        "projectId": project_id,
        "userId": user_id,
        "sessionId": session_id,
    }


def _clean_string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _job_key(scope: Mapping[str, str], job_id: str) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": project_partition_key(dict(scope))},
        "sortKey": {"S": f"AGENTJOB#{job_id}"},
    }


def _job_value(item: object, name: str) -> str:
    value = item.get(name) if isinstance(item, Mapping) else None
    if not isinstance(value, Mapping):
        return ""
    return _clean_string(value.get("S"))


def _update_agent_job(
    scope: Mapping[str, str],
    job_id: str,
    status: str,
    *,
    result: Mapping[str, Any] | None = None,
    error: str = "",
) -> None:
    if not PROJECT_TABLE:
        raise RuntimeError("Agent job storage is not configured")

    names = {"#status": "status"}
    values = {
        ":status": {"S": status},
        ":updatedAt": {"S": datetime.now(timezone.utc).isoformat()},
        ":ownerId": {"S": scope["userId"]},
        ":sessionId": {"S": scope["sessionId"]},
    }
    assignments = ["#status = :status", "updatedAt = :updatedAt"]

    if result is not None:
        result_json = json.dumps(result, separators=(",", ":"))
        if len(result_json.encode("utf-8")) > MAX_JOB_RESULT_BYTES:
            raise ValueError("Agent result is too large for the job store")
        values[":resultJson"] = {"S": result_json}
        assignments.append("resultJson = :resultJson")

    if error:
        names["#error"] = "error"
        values[":error"] = {"S": _clean_string(error)[:500]}
        assignments.append("#error = :error")

    _client("dynamodb").update_item(
        TableName=PROJECT_TABLE,
        Key=_job_key(scope, job_id),
        UpdateExpression="SET " + ", ".join(assignments),
        ConditionExpression="ownerId = :ownerId AND sessionId = :sessionId",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _get_agent_job(scope: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, Any]:
    job_id = require_identifier(payload.get("jobId"), "jobId")
    if not PROJECT_TABLE:
        return _response(503, {"error": "Agent job storage is not configured"})

    item = _client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key=_job_key(scope, job_id),
        ConsistentRead=True,
    ).get("Item")

    if (
        not item
        or _job_value(item, "ownerId") != scope["userId"]
        or _job_value(item, "sessionId") != scope["sessionId"]
    ):
        return _response(404, {"error": "Agent job not found"})

    status = _job_value(item, "status") or "queued"
    if status == "complete":
        try:
            result = json.loads(_job_value(item, "resultJson"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return _response(500, {"error": "Agent job result is unavailable"})
        if not isinstance(result, dict):
            return _response(500, {"error": "Agent job result is unavailable"})
        return _response(200, result)

    if status == "failed":
        return _response(
            500,
            {
                "error": "Project AI could not complete this request.",
                "jobId": job_id,
                "status": status,
                "traceId": _job_value(item, "traceId"),
            },
        )

    return _response(
        202,
        {
            "jobId": job_id,
            "clientId": scope["clientId"],
            "projectId": scope["projectId"],
            "status": status,
            "pollAfterMs": 1500,
        },
    )

def _read_runtime_response(response: Mapping[str, Any]) -> dict[str, Any]:
    stream = response.get("response") or response.get("body") or response.get("payload")
    if hasattr(stream, "read"):
        raw = stream.read()
    elif isinstance(stream, (bytes, bytearray, str)):
        raw = stream
    elif stream is not None:
        chunks: list[bytes] = []
        for event in stream:
            chunk = event.get("chunk", {}).get("bytes") if isinstance(event, Mapping) else None
            if isinstance(chunk, bytes):
                chunks.append(chunk)
        raw = b"".join(chunks)
    else:
        raise RuntimeError("AgentCore Runtime returned no response body")

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("AgentCore Runtime returned an invalid response")
    return parsed


def _invoke_runtime(runtime_payload: Mapping[str, Any], runtime_session_id: str):
    if not AGENT_RUNTIME_ARN:
        raise RuntimeError("AGENT_RUNTIME_ARN is not configured")
    response = _client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        runtimeSessionId=runtime_session_id,
        qualifier="DEFAULT",
        contentType="application/json",
        accept="application/json",
        traceId=runtime_payload["traceId"],
        payload=json.dumps(runtime_payload, separators=(",", ":")).encode("utf-8"),
    )
    return _read_runtime_response(response)


def _invoke_fallback(request: Mapping[str, Any], reason: str) -> dict[str, Any]:
    if not FALLBACK_FUNCTION_ARN:
        raise RuntimeError("AgentCore failed and no Lambda fallback is configured")
    brief_request = dict(request["briefRequest"])
    brief_request.update(
        {
            "mode": "project",
            "modelPreference": request["modelPreference"],
            "meetingNotes": request["meetingNotes"],
            "role": request["audienceRole"],
            "prompt": request["focus"],
            "approvedBrief": request.get("approvedBrief"),
        }
    )
    response = _client("lambda").invoke(
        FunctionName=FALLBACK_FUNCTION_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps({"body": json.dumps(brief_request)}).encode("utf-8"),
    )
    if response.get("FunctionError"):
        raise RuntimeError("The existing Lambda fallback also failed")
    payload = json.loads(response["Payload"].read())
    if not isinstance(payload, dict):
        raise RuntimeError("Lambda fallback returned an invalid response")
    status_code = int(payload.get("statusCode", 500))
    body = payload.get("body", "{}")
    parsed = json.loads(body) if isinstance(body, str) else body
    if status_code >= 400 or not isinstance(parsed, dict):
        raise RuntimeError("Lambda fallback could not generate the project handoff")
    metadata = parsed.get("metadata") if isinstance(parsed.get("metadata"), dict) else {}
    parsed["metadata"] = {
        **metadata,
        "agentMode": "lambda-fallback",
        "fallbackUsed": True,
        "fallbackReason": reason[:240],
        "memoryUsed": False,
        "gatewayUsed": False,
    }
    return parsed


def _run_agent_request(
    request: Mapping[str, Any],
    scope: Mapping[str, str],
    runtime_session_id: str,
    trace_id: str,
) -> dict[str, Any]:
    try:
        runtime_payload = {
            **request,
            "scope": dict(scope),
            "scopeToken": sign_scope_token(_scope_secret(), dict(scope), ttl_seconds=600),
            "traceId": trace_id,
        }
        result = _invoke_runtime(runtime_payload, runtime_session_id)
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        result["metadata"] = {
            **metadata,
            "agentSessionId": runtime_session_id,
            "agentTraceId": trace_id,
            "agentMode": "agentcore",
            "fallbackUsed": False,
        }
        return result
    except Exception as exc:
        LOGGER.warning(
            json.dumps(
                {
                    "event": "agentcore_runtime_fallback",
                    "traceId": trace_id,
                    "errorType": type(exc).__name__,
                }
            )
        )
        if request["action"] == "generate_catchup":
            raise RuntimeError(
                "Role-aware catch-up is temporarily unavailable; the approved brief and project state remain unchanged."
            ) from exc
        try:
            return _invoke_fallback(request, type(exc).__name__)
        except Exception as fallback_exc:
            LOGGER.error(
                json.dumps(
                    {
                        "event": "agentcore_fallback_failure",
                        "traceId": trace_id,
                        "errorType": type(fallback_exc).__name__,
                    }
                )
            )
            raise RuntimeError(
                "Project AI is temporarily unavailable; the approved brief remains unchanged."
            ) from fallback_exc


def _start_agent_job(
    request: Mapping[str, Any],
    scope: Mapping[str, str],
    runtime_session_id: str,
    trace_id: str,
) -> dict[str, Any]:
    if not PROJECT_TABLE or not AGENT_WORKER_FUNCTION:
        return _response(503, {"error": "Asynchronous AgentCore execution is not configured"})

    job_id = str(uuid4())
    now = datetime.now(timezone.utc)
    expires_at = int((now + timedelta(minutes=JOB_TTL_MINUTES)).timestamp())
    created = False
    dispatch_stage = "create_job_record"

    try:
        _client("dynamodb").put_item(
            TableName=PROJECT_TABLE,
            Item={
                **_job_key(scope, job_id),
                "ownerId": {"S": scope["userId"]},
                "sessionId": {"S": scope["sessionId"]},
                "clientId": {"S": scope["clientId"]},
                "action": {"S": request["action"]},
                "traceId": {"S": trace_id},
                "status": {"S": "queued"},
                "createdAt": {"S": now.isoformat()},
                "updatedAt": {"S": now.isoformat()},
                "expiresAt": {"N": str(expires_at)},
            },
            ConditionExpression="attribute_not_exists(projectId) AND attribute_not_exists(sortKey)",
        )
        created = True
        dispatch_stage = "invoke_worker"
        invocation = _client("lambda").invoke(
            FunctionName=AGENT_WORKER_FUNCTION,
            InvocationType="Event",
            Payload=json.dumps(
                {
                    "jobId": job_id,
                    "request": dict(request),
                    "scope": dict(scope),
                    "runtimeSessionId": runtime_session_id,
                    "traceId": trace_id,
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        if invocation.get("StatusCode") != 202:
            raise RuntimeError("Agent worker did not accept the job")
    except Exception as exc:
        error_response = getattr(exc, "response", {})
        error_details = (
            error_response.get("Error", {})
            if isinstance(error_response, Mapping)
            else {}
        )
        error_code = (
            str(error_details.get("Code") or "")
            if isinstance(error_details, Mapping)
            else ""
        )
        if created:
            try:
                _update_agent_job(
                    scope,
                    job_id,
                    "failed",
                    error=f"Unable to start AgentCore execution: {type(exc).__name__}",
                )
            except Exception:
                LOGGER.exception("Unable to record failed AgentCore job dispatch")
        LOGGER.error(
            json.dumps(
                {
                    "event": "agentcore_job_dispatch_failure",
                    "traceId": trace_id,
                    "stage": dispatch_stage,
                    "errorCode": error_code,
                    "errorType": type(exc).__name__,
                }
            )
        )
        return _response(502, {"error": "Unable to start AgentCore execution"})

    LOGGER.info(
        json.dumps(
            {
                "event": "agentcore_job_queued",
                "action": request["action"],
                "tenantId": scope["tenantId"],
                "clientId": scope["clientId"],
                "projectId": scope["projectId"],
                "jobId": job_id,
                "traceId": trace_id,
            }
        )
    )
    return _response(
        202,
        {
            "jobId": job_id,
            "clientId": scope["clientId"],
            "projectId": scope["projectId"],
            "status": "queued",
            "pollAfterMs": 1500,
        },
    )


def worker_handler(event: object, _context: Any) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ValueError("Invalid AgentCore worker event")

    job_id = require_identifier(event.get("jobId"), "jobId")
    request_value = event.get("request")
    scope_value = event.get("scope")
    runtime_session_id = require_identifier(
        event.get("runtimeSessionId"), "runtimeSessionId"
    )
    trace_id = require_identifier(event.get("traceId"), "traceId")
    if not isinstance(request_value, Mapping) or not isinstance(scope_value, Mapping):
        raise ValueError("Invalid AgentCore worker payload")

    request = validate_router_request(request_value)
    scope = {
        field: require_identifier(scope_value.get(field), field)
        for field in ("tenantId", "clientId", "projectId", "userId", "sessionId")
    }
    for field in ("clientId", "projectId", "sessionId"):
        if request[field] != scope[field]:
            raise AuthorizationError("Agent worker request does not match its trusted scope")

    try:
        _update_agent_job(scope, job_id, "running")
        result = _run_agent_request(request, scope, runtime_session_id, trace_id)
        _update_agent_job(scope, job_id, "complete", result=result)
        LOGGER.info(
            json.dumps(
                {
                    "event": "agentcore_job_complete",
                    "jobId": job_id,
                    "traceId": trace_id,
                    "provider": result.get("provider"),
                    "fallbackUsed": bool(
                        isinstance(result.get("metadata"), Mapping)
                        and result["metadata"].get("fallbackUsed")
                    ),
                }
            )
        )
        return {"jobId": job_id, "status": "complete"}
    except Exception as exc:
        LOGGER.error(
            json.dumps(
                {
                    "event": "agentcore_job_failure",
                    "jobId": job_id,
                    "traceId": trace_id,
                    "errorType": type(exc).__name__,
                }
            )
        )
        try:
            _update_agent_job(scope, job_id, "failed", error=type(exc).__name__)
        except Exception:
            LOGGER.exception("Unable to record failed AgentCore worker job")
        return {"jobId": job_id, "status": "failed"}


def handler(event: object, _context: Any) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        return _response(400, {"error": "Invalid API Gateway event"})

    try:
        raw_request = _payload(event)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _response(400, {"error": "Invalid JSON payload"})

    operation = _clean_string(raw_request.get("operation"))
    if operation:
        if operation != "getAgentJob":
            return _response(400, {"error": "Unsupported operation"})
        try:
            scope = _derive_scope(event, raw_request)
            return _get_agent_job(scope, raw_request)
        except AuthorizationError as exc:
            return _response(403, {"error": str(exc)})
        except ValueError as exc:
            return _response(400, {"error": str(exc)})
        except Exception as exc:
            LOGGER.error(
                json.dumps(
                    {
                        "event": "agentcore_job_poll_failure",
                        "errorType": type(exc).__name__,
                    }
                )
            )
            return _response(502, {"error": "Unable to read AgentCore job status"})

    try:
        request = validate_router_request(raw_request)
        scope = _derive_scope(event, request)
    except AuthorizationError as exc:
        return _response(403, {"error": str(exc)})
    except ValueError as exc:
        return _response(400, {"error": str(exc)})

    runtime_session_id = stable_identifier(
        "runtime-session",
        [
            scope["tenantId"],
            scope["clientId"],
            scope["projectId"],
            scope["userId"],
            scope["sessionId"],
        ],
        length=48,
    )
    trace_id = stable_identifier(
        "trace", [runtime_session_id, request["idempotencyKey"]], length=32
    )
    return _start_agent_job(request, scope, runtime_session_id, trace_id)
