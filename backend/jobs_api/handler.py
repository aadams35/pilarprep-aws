from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from pipeline import evidence as evidence_store
from pipeline.meeting_contracts import (
    DEFAULT_AUDIO_KEY,
    SCENARIO_ID,
    assert_public_demo_scope,
)
from botocore.exceptions import ClientError

from pipeline.state import (
    ACTIONS,
    AI_ACTIONS,
    ARTIFACT_BUCKET,
    JOB_QUEUE_URL,
    JOB_TTL_SECONDS,
    PROJECT_TABLE,
    AuthorizationError,
    ScopeAuthorizationError,
    assert_api_origin_verification,
    assert_secure_request,
    aws_client,
    client_directory_key,
    derive_list_identity,
    derive_scope,
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
    read_json_body,
    require_identifier,
    require_string,
    response,
    s3_encryption_args,
    slugify,
    stable_identifier,
    validate_job_request,
)


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
MAX_RECEIVE_COUNT = int(os.getenv("MAX_RECEIVE_COUNT", "3"))
JOB_DLQ_URL = os.getenv("JOB_DLQ_URL", "")
MEETING_EVIDENCE_BUCKET = os.getenv("MEETING_EVIDENCE_BUCKET", "")
MEETING_AUDIO_MAX_BYTES = int(os.getenv("MEETING_AUDIO_MAX_BYTES", str(25 * 1024 * 1024)))
MEETING_AUDIO_UPLOAD_TTL_SECONDS = int(os.getenv("MEETING_AUDIO_UPLOAD_TTL_SECONDS", "172800"))
MAX_REPLAY_COUNT = max(1, int(os.getenv("MAX_REPLAY_COUNT", "1")))
MAX_TOTAL_ATTEMPTS = max(3, int(os.getenv("MAX_TOTAL_ATTEMPTS", "6")))
QUARANTINE_VISIBILITY_SECONDS = 43_200
GUEST_HOURLY_AI_LIMIT = max(1, int(os.getenv("GUEST_HOURLY_AI_LIMIT", "20")))
GUEST_DAILY_AI_LIMIT = max(1, int(os.getenv("GUEST_DAILY_AI_LIMIT", "200")))
AUTH_USER_DAILY_AI_LIMIT = max(1, int(os.getenv("AUTH_USER_DAILY_AI_LIMIT", "100")))
AUTH_TENANT_DAILY_AI_LIMIT = max(1, int(os.getenv("AUTH_TENANT_DAILY_AI_LIMIT", "500")))
CLAUDE_DAILY_AI_LIMIT = max(1, int(os.getenv("CLAUDE_DAILY_AI_LIMIT", "5")))
GENERATION_ENABLED = os.getenv("GENERATION_ENABLED", "true").lower() == "true"
GUEST_ALLOWED_MODELS = {
    value.strip()
    for value in os.getenv(
        "GUEST_ALLOWED_MODELS", "nova-micro,nova-pro"
    ).split(",")
    if value.strip()
}
AUTH_ALLOWED_MODELS = {
    value.strip()
    for value in os.getenv(
        "AUTH_ALLOWED_MODELS", "nova-micro,nova-pro,claude-sonnet-4.6"
    ).split(",")
    if value.strip()
}
HOURLY_WINDOW_SECONDS = 3600
DAILY_WINDOW_SECONDS = 86400


@dataclass(frozen=True)
class UsageQuotaWindow:
    kind: str
    label: str
    key: str
    limit: int
    duration: int
    window_start: int

    @property
    def resets_at(self) -> int:
        return self.window_start + self.duration


class UsageQuotaExceeded(RuntimeError):
    def __init__(self, window: UsageQuotaWindow, current_epoch: int):
        self.retry_after_seconds = max(1, window.resets_at - current_epoch)
        minutes = (self.retry_after_seconds + 59) // 60
        hours, minutes = divmod(minutes, 60)
        wait = []
        if hours:
            wait.append(f"{hours} hour" + ("s" if hours != 1 else ""))
        if minutes:
            wait.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
        period = "hour" if window.duration == HOURLY_WINDOW_SECONDS else "day"
        super().__init__(
            f"{window.label} AI usage limit reached ({window.limit} requests per {period}). "
            f"Try again in {' '.join(wait)}. Your saved work is unchanged."
        )
        self.quota = {
            "kind": window.kind,
            "limit": window.limit,
            "windowSeconds": window.duration,
            "resetsAt": datetime.fromtimestamp(window.resets_at, timezone.utc).isoformat(),
        }


class GenerationDisabled(RuntimeError):
    pass


def _usage_update(
    scope: Mapping[str, str],
    *,
    key: str,
    window_start: int,
    expires_at: int,
    limit: int,
) -> dict[str, Any]:
    return {
        "Update": {
            "TableName": PROJECT_TABLE,
            "Key": {
                "projectId": {"S": f"TENANT#{scope['tenantId']}"},
                "sortKey": {"S": key},
            },
            "UpdateExpression": (
                "SET entityType = if_not_exists(entityType, :entityType), "
                "windowStart = if_not_exists(windowStart, :windowStart), "
                "expiresAt = :expiresAt, limitValue = :limit ADD requestCount :one"
            ),
            "ConditionExpression": (
                "attribute_not_exists(requestCount) OR requestCount < :limit"
            ),
            "ExpressionAttributeValues": {
                ":entityType": {"S": "AI_USAGE_QUOTA"},
                ":windowStart": {"N": str(window_start)},
                ":expiresAt": {"N": str(expires_at)},
                ":one": {"N": "1"},
                ":limit": {"N": str(limit)},
            },
        }
    }


def _consume_usage_quota(
    scope: Mapping[str, str], action: str, model_preference: str
) -> None:
    if action not in AI_ACTIONS:
        return
    if not GENERATION_ENABLED:
        raise GenerationDisabled("AI generation is temporarily disabled")
    allowed_models = (
        GUEST_ALLOWED_MODELS
        if scope.get("identityType") == "guest"
        else AUTH_ALLOWED_MODELS
    )
    if model_preference not in allowed_models:
        raise AuthorizationError("This account cannot use the selected model")

    current_epoch = now_epoch()
    hour = current_epoch // HOURLY_WINDOW_SECONDS
    day = current_epoch // DAILY_WINDOW_SECONDS
    user_id = scope["userId"]
    windows: list[UsageQuotaWindow] = []
    if scope.get("identityType") == "guest":
        windows.append(
            UsageQuotaWindow(
                "guest_hourly", "Demo hourly",
                f"USAGE#USER#{user_id}#HOUR#{hour}", GUEST_HOURLY_AI_LIMIT,
                HOURLY_WINDOW_SECONDS, hour * HOURLY_WINDOW_SECONDS,
            )
        )
        daily_limit = GUEST_DAILY_AI_LIMIT
        daily_kind, daily_label = "guest_daily", "Demo daily"
    else:
        daily_limit = AUTH_USER_DAILY_AI_LIMIT
        daily_kind, daily_label = "user_daily", "Account daily"
        windows.append(
            UsageQuotaWindow(
                "tenant_daily", "Workspace daily", f"USAGE#TENANT#DAY#{day}",
                AUTH_TENANT_DAILY_AI_LIMIT, DAILY_WINDOW_SECONDS,
                day * DAILY_WINDOW_SECONDS,
            )
        )
    windows.append(
        UsageQuotaWindow(
            daily_kind, daily_label, f"USAGE#USER#{user_id}#DAY#{day}",
            daily_limit, DAILY_WINDOW_SECONDS, day * DAILY_WINDOW_SECONDS,
        )
    )
    if model_preference == "claude-sonnet-4.6":
        windows.append(
            UsageQuotaWindow(
                "claude_daily", "Claude daily", f"USAGE#USER#{user_id}#MODEL#CLAUDE#DAY#{day}",
                CLAUDE_DAILY_AI_LIMIT, DAILY_WINDOW_SECONDS, day * DAILY_WINDOW_SECONDS,
            )
        )
    updates = [
        _usage_update(
            scope, key=window.key, window_start=window.window_start,
            expires_at=current_epoch + (2 * window.duration), limit=window.limit,
        )
        for window in windows
    ]
    dynamodb = aws_client("dynamodb")
    try:
        dynamodb.transact_write_items(TransactItems=updates)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "ConditionalCheckFailedException",
            "TransactionCanceledException",
        }:
            reasons = exc.response.get("CancellationReasons")
            exhausted = [
                windows[index]
                for index, reason in enumerate(reasons or [])
                if index < len(windows) and isinstance(reason, Mapping)
                and reason.get("Code") == "ConditionalCheckFailed"
            ]
            # Missing cancellation details must not turn a transient failure into a quota claim.
            if not reasons:
                for window, update in zip(windows, updates):
                    item = dynamodb.get_item(
                        TableName=PROJECT_TABLE, Key=update["Update"]["Key"],
                        ProjectionExpression="requestCount", ConsistentRead=True,
                    ).get("Item", {})
                    if int(item.get("requestCount", {}).get("N", "0")) >= window.limit:
                        exhausted.append(window)
            if exhausted:
                window = max(exhausted, key=lambda value: value.resets_at)
                metric("UsageQuotaExceeded", Action=action)
                LOGGER.info(json.dumps({
                    "event": "ai_usage_limit_reached", "action": action,
                    "quotaKind": window.kind, "limit": window.limit,
                    "resetsAt": window.resets_at,
                }))
                raise UsageQuotaExceeded(window, current_epoch) from exc
        raise


def _route_model(
    scope: Mapping[str, str],
    action: str,
    inputs: dict[str, Any],
) -> str:
    if action not in AI_ACTIONS:
        return str(inputs.get("modelPreference") or "nova-pro")
    requested = str(inputs.get("modelPreference") or "nova-pro")
    quality_tier = str(inputs.get("qualityTier") or "standard")
    user_tier = str(scope.get("userTier") or "guest")

    if action == "catchup.generate" and quality_tier != "premium":
        selected = "nova-micro"
        reason = "Concise catch-up is a bounded, low-risk helper task"
    elif (
        user_tier == "premium"
        and action in {"brief.generate", "brief.refine", "handoff.generate"}
        and (quality_tier == "premium" or requested == "claude-sonnet-4.6")
    ):
        selected = "claude-sonnet-4.6"
        reason = "Premium final-quality reasoning was authorized by trusted identity"
    else:
        selected = "nova-pro"
        reason = "Nova Pro is the standard model for complete customer packets"

    inputs["requestedModelPreference"] = requested
    inputs["modelPreference"] = selected
    inputs["modelRouting"] = {
        "selectedModel": selected,
        "requestedModel": requested,
        "qualityTier": quality_tier,
        "userTier": user_tier,
        "reason": reason,
        "serverSelected": True,
    }
    metric(
        "ModelRoutes",
        Action=action,
        RequestedModel=requested,
        SelectedModel=selected,
        UserTier=user_tier,
    )
    return selected


def _jwt_claims(event: Mapping[str, Any]) -> Mapping[str, Any]:
    request_context = event.get("requestContext")
    authorizer = (
        request_context.get("authorizer", {})
        if isinstance(request_context, Mapping)
        else {}
    )
    jwt = authorizer.get("jwt") if isinstance(authorizer, Mapping) else None
    claims = jwt.get("claims") if isinstance(jwt, Mapping) else None
    return claims if isinstance(claims, Mapping) else {}


def _claim_set(value: object) -> set[str]:
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    if not isinstance(value, str):
        return set()
    stripped = value.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return {str(item).strip() for item in parsed if str(item).strip()}
    return {item.strip() for item in stripped.split(",") if item.strip()}


def _require_operator(event: Mapping[str, Any]) -> str:
    claims = _jwt_claims(event)
    if "PilarPrepOperators" not in _claim_set(claims.get("cognito:groups")):
        raise AuthorizationError("This operation is not available")
    subject = require_string(claims.get("sub"), "subject claim", maximum=240)
    return stable_identifier("user", [subject])


def _dlq_pointer(body: str) -> tuple[dict[str, str], dict[str, Any]]:
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("DLQ message is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("DLQ message must contain a job pointer")
    if value.get("source") == "aws.transcribe":
        raise ValueError(
            "Transcribe continuation events require the meeting recovery runbook"
        )
    action = require_string(value.get("action"), "action", maximum=32)
    if action not in ACTIONS:
        raise ValueError("DLQ message has an unsupported action")
    scope = {
        field: require_identifier(value.get(field), field)
        for field in ("tenantId", "clientId", "projectId", "userId", "sessionId")
    }
    job_id = require_identifier(value.get("jobId"), "jobId")
    input_version = require_identifier(value.get("inputVersion"), "inputVersion")
    trace_id = require_identifier(value.get("traceId"), "traceId")
    input_key = str(value.get("inputKey") or "")
    expected_prefix = f"{job_object_prefix(scope, job_id)}/"
    if not input_key.startswith(expected_prefix) or not input_key.endswith(
        "/input.json"
    ):
        raise ValueError("DLQ input pointer is outside its declared scope")
    return scope, {
        "action": action,
        "jobId": job_id,
        "inputVersion": input_version,
        "traceId": trace_id,
        "inputKey": input_key,
        "body": body,
    }


def _quarantine_dlq_message(
    *,
    message_id: str,
    body_hash: str,
    reason: str,
    operator_id: str,
) -> None:
    timestamp = now_iso()
    aws_client("dynamodb").update_item(
        TableName=PROJECT_TABLE,
        Key={
            "projectId": {"S": "OPERATIONS#DLQ"},
            "sortKey": {"S": f"QUARANTINE#{body_hash}"},
        },
        UpdateExpression=(
            "SET entityType = :entity, firstSeenAt = if_not_exists(firstSeenAt, "
            ":seen), lastSeenAt = :seen, quarantineReason = :reason, "
            "lastReviewedBy = :operator, messageId = :messageId "
            "ADD observationCount :one"
        ),
        ExpressionAttributeValues={
            ":entity": {"S": "DLQ_QUARANTINE"},
            ":seen": {"S": timestamp},
            ":reason": {"S": reason[:240]},
            ":operator": {"S": operator_id},
            ":messageId": {"S": message_id[:128] or "unknown"},
            ":one": {"N": "1"},
        },
    )
    metric("DlqMessagesQuarantined", Reason=slugify(reason, "invalid")[:64])


def _mark_replay_dispatch(
    scope: Mapping[str, str],
    job_id: str,
    token: str,
    replay_number: int,
    *,
    succeeded: bool,
) -> None:
    status = "sent" if succeeded else "dispatch-failed"
    timestamp = now_iso()
    dynamodb = aws_client("dynamodb")
    job_update: dict[str, Any] = {
        "TableName": PROJECT_TABLE,
        "Key": job_key(scope, job_id),
        "UpdateExpression": (
            "SET replayDispatchStatus = :dispatch, replayDispatchUpdatedAt = :now"
            + ("" if succeeded else ", #status = :failed")
        ),
        "ConditionExpression": "lastReplayToken = :token",
        "ExpressionAttributeValues": {
            ":dispatch": {"S": status},
            ":now": {"S": timestamp},
            ":token": {"S": token},
            **({":failed": {"S": "failed"}} if not succeeded else {}),
        },
    }
    if not succeeded:
        job_update["ExpressionAttributeNames"] = {"#status": "status"}
    dynamodb.update_item(**job_update)
    dynamodb.update_item(
        TableName=PROJECT_TABLE,
        Key={
            "projectId": {"S": project_partition_key(scope)},
            "sortKey": {
                "S": f"DLQ#REPLAY#{job_id}#{replay_number:02d}"
            },
        },
        UpdateExpression=(
            "SET dispatchStatus = :dispatch, dispatchUpdatedAt = :now"
        ),
        ExpressionAttributeValues={
            ":dispatch": {"S": status},
            ":now": {"S": timestamp},
        },
    )


def _prepare_dlq_replay(
    scope: Mapping[str, str],
    pointer: Mapping[str, Any],
    *,
    operator_id: str,
    reason: str,
    token: str,
) -> tuple[str, int]:
    dynamodb = aws_client("dynamodb")
    job_id = str(pointer["jobId"])
    item = dynamodb.get_item(
        TableName=PROJECT_TABLE,
        Key=job_key(scope, job_id),
        ConsistentRead=True,
    ).get("Item")
    job = deserialize_item(item)
    if not job:
        return "quarantine: job record not found", 0
    if any(
        (
            job.get("ownerId") != scope["userId"],
            job.get("clientId") != scope["clientId"],
            job.get("projectScopeId") != scope["projectId"],
            job.get("action") != pointer["action"],
            job.get("inputKey") != pointer["inputKey"],
            job.get("inputVersion") != pointer["inputVersion"],
        )
    ):
        return "quarantine: job pointer does not match durable state", 0
    status = str(job.get("status") or "")
    if status in {"complete", "approved", "review-ready"}:
        return "acknowledge: job already completed", int(job.get("replayCount") or 0)
    if (
        status in {"queued", "running", "transcribing", "analyzing"}
        and job.get("lastReplayToken") == token
    ):
        return "acknowledge: replay already dispatched", int(
            job.get("replayCount") or 0
        )
    if status != "failed":
        return f"quarantine: job status {status or 'unknown'} is not replayable", 0

    replay_count = int(job.get("replayCount") or 0)
    retry_count = int(job.get("retryCount") or 0)
    if (
        replay_count >= MAX_REPLAY_COUNT
        or retry_count + (replay_count * MAX_RECEIVE_COUNT)
        >= MAX_TOTAL_ATTEMPTS
    ):
        return "quarantine: maximum total attempts reached", replay_count
    dispatch_retry = (
        job.get("lastReplayToken") == token
        and job.get("replayDispatchStatus") == "dispatch-failed"
    )
    next_replay = replay_count if dispatch_retry else replay_count + 1
    timestamp = now_iso()
    update_expression = (
        "SET #status = :queued, phase = :queued, updatedAt = :now, "
        "lastReplayToken = :token, lastReplayAt = :now, "
        "lastReplayedBy = :operator, replayReason = :reason, "
        "replayDispatchStatus = :pending"
    )
    if not dispatch_retry:
        update_expression += (
            ", replayCount = if_not_exists(replayCount, :zero) + :one"
        )
    audit_item = {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": f"DLQ#REPLAY#{job_id}#{next_replay:02d}"},
        "entityType": {"S": "DLQ_REPLAY_AUDIT"},
        "jobId": {"S": job_id},
        "action": {"S": str(pointer["action"])},
        "traceId": {"S": str(pointer["traceId"])},
        "requestedAt": {"S": timestamp},
        "requestedBy": {"S": operator_id},
        "reason": {"S": reason},
        "replayCount": {"N": str(next_replay)},
        "messageHash": {"S": token},
        "dispatchStatus": {"S": "pending"},
    }
    if dispatch_retry:
        dynamodb.update_item(
            TableName=PROJECT_TABLE,
            Key=job_key(scope, job_id),
            UpdateExpression=update_expression,
            ConditionExpression=(
                "#status = :failed AND lastReplayToken = :token "
                "AND replayDispatchStatus = :dispatchFailed"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":queued": {"S": "queued"},
                ":failed": {"S": "failed"},
                ":now": {"S": timestamp},
                ":token": {"S": token},
                ":operator": {"S": operator_id},
                ":reason": {"S": reason},
                ":pending": {"S": "pending"},
                ":dispatchFailed": {"S": "dispatch-failed"},
            },
        )
    else:
        dynamodb.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": PROJECT_TABLE,
                        "Key": job_key(scope, job_id),
                        "UpdateExpression": update_expression,
                        "ConditionExpression": (
                            "#status = :failed AND "
                            "(attribute_not_exists(replayCount) OR "
                            "replayCount < :maxReplay)"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":queued": {"S": "queued"},
                            ":failed": {"S": "failed"},
                            ":now": {"S": timestamp},
                            ":token": {"S": token},
                            ":operator": {"S": operator_id},
                            ":reason": {"S": reason},
                            ":pending": {"S": "pending"},
                            ":zero": {"N": "0"},
                            ":one": {"N": "1"},
                            ":maxReplay": {"N": str(MAX_REPLAY_COUNT)},
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": PROJECT_TABLE,
                        "Item": audit_item,
                        "ConditionExpression": "attribute_not_exists(projectId)",
                    }
                },
            ],
            ClientRequestToken=dynamodb_client_request_token(
                "dlq-replay", [project_partition_key(scope), job_id, token]
            ),
        )
    return "replay", next_replay


def _replay_dlq(event: Mapping[str, Any]) -> dict[str, Any]:
    operator_id = _require_operator(event)
    if not JOB_DLQ_URL or not JOB_QUEUE_URL:
        return response(event, 503, {"error": "DLQ recovery is not configured"})
    body = read_json_body(event)
    reason = require_string(body.get("reason"), "reason", minimum=8, maximum=240)
    max_messages = body.get("maxMessages", 1)
    if isinstance(max_messages, bool) or not isinstance(max_messages, int):
        raise ValueError("maxMessages must be an integer")
    if max_messages < 1 or max_messages > 10:
        raise ValueError("maxMessages must be between 1 and 10")
    sqs = aws_client("sqs")
    received = sqs.receive_message(
        QueueUrl=JOB_DLQ_URL,
        MaxNumberOfMessages=max_messages,
        WaitTimeSeconds=1,
        VisibilityTimeout=120,
        AttributeNames=["ApproximateReceiveCount"],
    ).get("Messages", [])
    outcomes: list[dict[str, Any]] = []
    for message in received:
        raw = str(message.get("Body") or "")
        message_id = str(message.get("MessageId") or "")
        receipt = str(message.get("ReceiptHandle") or "")
        token = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        try:
            scope, pointer = _dlq_pointer(raw)
            decision, replay_number = _prepare_dlq_replay(
                scope,
                pointer,
                operator_id=operator_id,
                reason=reason,
                token=token,
            )
        except (ValueError, AuthorizationError) as exc:
            decision = f"quarantine: {str(exc)}"
            scope = {}
            pointer = {"jobId": "", "action": ""}
            replay_number = 0
        if decision.startswith("acknowledge:"):
            sqs.delete_message(QueueUrl=JOB_DLQ_URL, ReceiptHandle=receipt)
            outcomes.append(
                {
                    "messageId": message_id,
                    "jobId": pointer.get("jobId"),
                    "status": "acknowledged",
                    "reason": decision.split(": ", 1)[1],
                }
            )
            continue
        if decision.startswith("quarantine:"):
            quarantine_reason = decision.split(": ", 1)[1]
            _quarantine_dlq_message(
                message_id=message_id,
                body_hash=token,
                reason=quarantine_reason,
                operator_id=operator_id,
            )
            if receipt:
                sqs.change_message_visibility(
                    QueueUrl=JOB_DLQ_URL,
                    ReceiptHandle=receipt,
                    VisibilityTimeout=QUARANTINE_VISIBILITY_SECONDS,
                )
            outcomes.append(
                {
                    "messageId": message_id,
                    "jobId": pointer.get("jobId"),
                    "status": "quarantined",
                    "reason": quarantine_reason,
                }
            )
            continue
        try:
            sqs.send_message(QueueUrl=JOB_QUEUE_URL, MessageBody=raw)
            _mark_replay_dispatch(
                scope,
                str(pointer["jobId"]),
                token,
                replay_number,
                succeeded=True,
            )
            sqs.delete_message(QueueUrl=JOB_DLQ_URL, ReceiptHandle=receipt)
            metric("DlqMessagesReplayed", Action=str(pointer["action"]))
            outcomes.append(
                {
                    "messageId": message_id,
                    "jobId": pointer["jobId"],
                    "status": "replayed",
                    "replayCount": replay_number,
                }
            )
        except Exception:
            _mark_replay_dispatch(
                scope,
                str(pointer["jobId"]),
                token,
                replay_number,
                succeeded=False,
            )
            LOGGER.exception(
                json.dumps(
                    {
                        "event": "dlq_replay_dispatch_failed",
                        "jobId": pointer["jobId"],
                        "messageId": message_id,
                    }
                )
            )
            outcomes.append(
                {
                    "messageId": message_id,
                    "jobId": pointer["jobId"],
                    "status": "dispatch-failed",
                }
            )
    return response(
        event,
        200,
        {
            "received": len(received),
            "results": outcomes,
            "automaticReplay": False,
        },
    )




def _route(event: Mapping[str, Any]) -> tuple[str, str]:
    request_context = event.get("requestContext")
    http = (
        request_context.get("http", {})
        if isinstance(request_context, Mapping)
        else {}
    )
    method = str(http.get("method") or event.get("httpMethod") or "").upper()
    path = str(http.get("path") or event.get("rawPath") or event.get("path") or "")
    return method, path.rstrip("/") or "/"


def _path_parameter(event: Mapping[str, Any], name: str) -> str:
    values = event.get("pathParameters")
    return str(values.get(name) or "") if isinstance(values, Mapping) else ""


def _query(event: Mapping[str, Any]) -> dict[str, str]:
    values = event.get("queryStringParameters")
    if not isinstance(values, Mapping):
        return {}
    return {str(key): str(value or "") for key, value in values.items()}


def _scope_from_query(event: Mapping[str, Any], *, client_id: str = "") -> dict[str, str]:
    query = _query(event)
    supplied_client = client_id or query.get("clientId", "")
    return derive_scope(
        event,
        {
            "clientId": supplied_client,
            "projectId": query.get("projectId") or supplied_client,
            "sessionId": query.get("sessionId", ""),
        },
    )


def _meeting_upload_key(scope: Mapping[str, str], upload_id: str) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": f"MEETING#UPLOAD#{upload_id}"},
    }


def _require_authenticated_workspace(scope: Mapping[str, str]) -> None:
    if scope.get("identityType") != "authenticated":
        raise AuthorizationError(
            "Meeting audio requires a verified PilarPrep workspace"
        )


def _create_meeting_audio_upload(event: Mapping[str, Any]) -> dict[str, Any]:
    if not (PROJECT_TABLE and MEETING_EVIDENCE_BUCKET):
        return response(event, 503, {"error": "Meeting audio upload is not configured"})
    payload = read_json_body(event)
    scope = derive_scope(event, payload)
    _require_authenticated_workspace(scope)
    scenario_id = require_identifier(payload.get("scenarioId"), "scenarioId")
    assert_public_demo_scope(scope, scenario_id)
    if payload.get("consentAcknowledged") is not True:
        raise ValueError(
            "Confirm that you are authorized to process this recording"
        )
    meeting_id = require_identifier(payload.get("meetingId"), "meetingId")
    file_name = require_string(payload.get("fileName"), "fileName", maximum=180)
    content_type = require_string(payload.get("contentType"), "contentType", maximum=100).lower()
    size_bytes = int(payload.get("sizeBytes") or 0)
    if size_bytes < 1 or size_bytes > MEETING_AUDIO_MAX_BYTES:
        raise ValueError("Meeting audio must be between 1 byte and 25 MB")
    extension = os.path.splitext(file_name)[1].lower().lstrip(".")
    allowed = {
        "mp3": ({"audio/mpeg", "audio/mp3"}, "mp3"),
        "wav": ({"audio/wav", "audio/x-wav", "audio/wave"}, "wav"),
        "m4a": ({"audio/mp4", "audio/x-m4a", "video/mp4"}, "mp4"),
    }
    if extension not in allowed or content_type not in allowed[extension][0]:
        raise ValueError("Upload an MP3, WAV, or M4A audio file")
    upload_id = str(uuid4())
    safe_name = f"meeting.{extension}"
    object_key = (
        f"audio/uploads/{scope['tenantId']}/{scope['clientId']}/"
        f"{scope['projectId']}/{upload_id}/{safe_name}"
    )
    timestamp = now_iso()
    aws_client("dynamodb").put_item(
        TableName=PROJECT_TABLE,
        Item={
            **_meeting_upload_key(scope, upload_id),
            "entityType": {"S": "MEETING_AUDIO_UPLOAD"},
            "uploadId": {"S": upload_id},
            "scenarioId": {"S": scenario_id},
            "meetingId": {"S": meeting_id},
            "tenantId": {"S": scope["tenantId"]},
            "clientId": {"S": scope["clientId"]},
            "projectScopeId": {"S": scope["projectId"]},
            "ownerId": {"S": scope["userId"]},
            "sessionId": {"S": scope["sessionId"]},
            "objectKey": {"S": object_key},
            "fileName": {"S": file_name},
            "contentType": {"S": content_type},
            "mediaFormat": {"S": allowed[extension][1]},
            "expectedSizeBytes": {"N": str(size_bytes)},
            "status": {"S": "pending_scan"},
            "consentAcknowledged": {"BOOL": True},
            "consentVersion": {"S": "2026-08-27"},
            "consentedAt": {"S": timestamp},
            "createdAt": {"S": timestamp},
            "updatedAt": {"S": timestamp},
            "expiresAt": {"N": str(now_epoch() + MEETING_AUDIO_UPLOAD_TTL_SECONDS)},
        },
        ConditionExpression="attribute_not_exists(projectId)",
    )
    encryption = s3_encryption_args()
    fields = {"Content-Type": content_type}
    conditions: list[object] = [
        {"Content-Type": content_type},
        ["content-length-range", 1, MEETING_AUDIO_MAX_BYTES],
    ]
    if encryption.get("ServerSideEncryption"):
        fields["x-amz-server-side-encryption"] = encryption["ServerSideEncryption"]
        conditions.append({
            "x-amz-server-side-encryption": encryption["ServerSideEncryption"]
        })
    if encryption.get("SSEKMSKeyId"):
        fields["x-amz-server-side-encryption-aws-kms-key-id"] = encryption["SSEKMSKeyId"]
        conditions.append({
            "x-amz-server-side-encryption-aws-kms-key-id": encryption["SSEKMSKeyId"]
        })
    upload = aws_client("s3").generate_presigned_post(
        Bucket=MEETING_EVIDENCE_BUCKET,
        Key=object_key,
        Fields=fields,
        Conditions=conditions,
        ExpiresIn=900,
    )
    return response(
        event,
        200,
        {
            "uploadId": upload_id,
            "uploadUrl": upload["url"],
            "uploadFields": upload["fields"],
            "expiresIn": 900,
            "fileName": file_name,
            "sizeBytes": size_bytes,
            "contentType": content_type,
            "scenarioId": SCENARIO_ID,
            "status": "pending_scan",
            "consentVersion": "2026-08-27",
        },
    )


def _get_meeting_audio_upload(
    event: Mapping[str, Any], upload_id: str
) -> dict[str, Any]:
    if not (PROJECT_TABLE and MEETING_EVIDENCE_BUCKET):
        return response(event, 503, {"error": "Meeting audio upload is not configured"})
    safe_upload_id = require_identifier(upload_id, "uploadId")
    scope = _scope_from_query(event)
    _require_authenticated_workspace(scope)
    item = deserialize_item(
        aws_client("dynamodb").get_item(
            TableName=PROJECT_TABLE,
            Key=_meeting_upload_key(scope, safe_upload_id),
            ConsistentRead=True,
        ).get("Item")
    )
    if (
        not item
        or item.get("entityType") != "MEETING_AUDIO_UPLOAD"
        or item.get("ownerId") != scope["userId"]
        or item.get("sessionId") != scope["sessionId"]
        or item.get("tenantId") != scope["tenantId"]
        or item.get("clientId") != scope["clientId"]
        or item.get("projectScopeId") != scope["projectId"]
    ):
        return response(event, 404, {"error": "Audio upload not found"})
    status = str(item.get("status") or "pending_scan")
    if status not in {
        "pending_scan",
        "clean",
        "blocked",
        "scan_failed",
        "processing",
    }:
        status = "scan_failed"
    return response(
        event,
        200,
        {
            "uploadId": safe_upload_id,
            "status": status,
            "updatedAt": item.get("updatedAt") or item.get("createdAt"),
        },
    )


def _get_demo_meeting_audio(event: Mapping[str, Any]) -> dict[str, Any]:
    if not MEETING_EVIDENCE_BUCKET:
        return response(event, 503, {"error": "Meeting audio is not configured"})
    scope = _scope_from_query(event)
    _require_authenticated_workspace(scope)
    assert_public_demo_scope(scope, SCENARIO_ID)
    s3 = aws_client("s3")
    try:
        s3.head_object(Bucket=MEETING_EVIDENCE_BUCKET, Key=DEFAULT_AUDIO_KEY)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            raise LookupError(
                "The BlueMesa demo recording is not available"
            ) from exc
        raise
    file_name = "PilarPrep-BlueMesa-Discovery-Meeting.mp3"
    download_url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": MEETING_EVIDENCE_BUCKET,
            "Key": DEFAULT_AUDIO_KEY,
            "ResponseContentType": "audio/mpeg",
            "ResponseContentDisposition": (
                f'attachment; filename="{file_name}"'
            ),
        },
        ExpiresIn=900,
    )
    return response(
        event,
        200,
        {
            "downloadUrl": download_url,
            "fileName": file_name,
            "expiresIn": 900,
            "scenarioId": SCENARIO_ID,
        },
    )


def _get_idempotent_job(scope: Mapping[str, str], key: str) -> str:
    item = aws_client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key=idempotency_key(scope, key),
        ConsistentRead=True,
        ProjectionExpression="jobId",
    ).get("Item")
    return str(item.get("jobId", {}).get("S") or "") if isinstance(item, Mapping) else ""


def _start_job(event: Mapping[str, Any]) -> dict[str, Any]:
    if not (PROJECT_TABLE and ARTIFACT_BUCKET and JOB_QUEUE_URL):
        return response(event, 503, {"error": "The job pipeline is not configured"})
    payload = validate_job_request(read_json_body(event))
    scope = derive_scope(event, payload)
    if (
        payload["action"] in {"meeting.process", "meeting.approve"}
        and scope.get("identityType") != "authenticated"
    ):
        raise AuthorizationError(
            "Meeting intelligence requires a verified PilarPrep workspace"
        )
    if (
        payload["action"].startswith("evidence.")
        and scope.get("identityType") != "authenticated"
    ):
        raise AuthorizationError("Evidence management requires a verified workspace")
    selected_model = _route_model(scope, payload["action"], payload["input"])
    existing_job = _get_idempotent_job(scope, payload["idempotencyKey"])
    if existing_job:
        metric("JobIdempotencyHit", Action=payload["action"])
        return response(
            event,
            202,
            {
                "jobId": existing_job,
                "clientId": scope["clientId"],
                "projectId": scope["projectId"],
                "status": "queued",
                "pollAfterMs": 1500,
                "idempotent": True,
            },
        )
    _consume_usage_quota(
        scope,
        payload["action"],
        selected_model,
    )

    job_id = str(uuid4())
    input_version = str(uuid4())
    trace_id = stable_identifier(
        "trace",
        [
            scope["tenantId"],
            scope["clientId"],
            scope["projectId"],
            payload["idempotencyKey"],
        ],
        length=32,
    )
    timestamp = now_iso()
    expires_at = now_epoch() + JOB_TTL_SECONDS
    input_key = f"{job_object_prefix(scope, job_id)}/input.json"
    input_document = {
        "inputVersion": input_version,
        "action": payload["action"],
        "scope": dict(scope),
        "idempotencyKey": payload["idempotencyKey"],
        "input": payload["input"],
        "createdAt": timestamp,
    }
    input_body = json.dumps(input_document, separators=(",", ":")).encode("utf-8")
    if len(input_body) > 240_000:
        raise ValueError("Validated job input is too large")

    s3 = aws_client("s3")
    s3.put_object(
        Bucket=ARTIFACT_BUCKET,
        Key=input_key,
        Body=input_body,
        ContentType="application/json",
        **s3_encryption_args(),
        Metadata={"input-version": input_version, "job-id": job_id},
    )

    dynamodb = aws_client("dynamodb")
    try:
        dynamodb.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": PROJECT_TABLE,
                        "Item": {
                            **job_key(scope, job_id),
                            "entityType": {"S": "JOB"},
                            "jobId": {"S": job_id},
                            "tenantId": {"S": scope["tenantId"]},
                            "clientId": {"S": scope["clientId"]},
                            "projectScopeId": {"S": scope["projectId"]},
                            "ownerId": {"S": scope["userId"]},
                            "sessionId": {"S": scope["sessionId"]},
                            "action": {"S": payload["action"]},
                            "status": {"S": "queued"},
                            "traceId": {"S": trace_id},
                            "inputKey": {"S": input_key},
                            "inputVersion": {"S": input_version},
                            "retryCount": {"N": "0"},
                            "createdAt": {"S": timestamp},
                            "updatedAt": {"S": timestamp},
                            "expiresAt": {"N": str(expires_at)},
                        },
                        "ConditionExpression": (
                            "attribute_not_exists(projectId) AND attribute_not_exists(sortKey)"
                        ),
                    }
                },
                {
                    "Put": {
                        "TableName": PROJECT_TABLE,
                        "Item": {
                            **idempotency_key(scope, payload["idempotencyKey"]),
                            "entityType": {"S": "IDEMPOTENCY"},
                            "jobId": {"S": job_id},
                            "action": {"S": payload["action"]},
                            "createdAt": {"S": timestamp},
                            "expiresAt": {"N": str(expires_at + 6 * 86400)},
                        },
                        "ConditionExpression": "attribute_not_exists(projectId)",
                    }
                },
            ],
            ClientRequestToken=dynamodb_client_request_token(
                "job", [project_partition_key(scope), payload["idempotencyKey"]]
            ),
        )
    except ClientError as exc:
        s3.delete_object(Bucket=ARTIFACT_BUCKET, Key=input_key)
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            existing_job = _get_idempotent_job(scope, payload["idempotencyKey"])
            if existing_job:
                return response(
                    event,
                    202,
                    {
                        "jobId": existing_job,
                        "clientId": scope["clientId"],
                        "projectId": scope["projectId"],
                        "status": "queued",
                        "pollAfterMs": 1500,
                        "idempotent": True,
                    },
                )
        raise

    message = {
        "action": payload["action"],
        "jobId": job_id,
        "tenantId": scope["tenantId"],
        "clientId": scope["clientId"],
        "projectId": scope["projectId"],
        "userId": scope["userId"],
        "sessionId": scope["sessionId"],
        "traceId": trace_id,
        "inputVersion": input_version,
        "inputKey": input_key,
    }
    try:
        aws_client("sqs").send_message(
            QueueUrl=JOB_QUEUE_URL,
            MessageBody=json.dumps(message, separators=(",", ":")),
        )
    except Exception as exc:
        dynamodb.update_item(
            TableName=PROJECT_TABLE,
            Key=job_key(scope, job_id),
            UpdateExpression="SET #status = :failed, updatedAt = :updatedAt, #error = :error",
            ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues={
                ":failed": {"S": "failed"},
                ":updatedAt": {"S": now_iso()},
                ":error": {"S": "The job could not be queued"},
            },
        )
        LOGGER.error(
            json.dumps(
                {
                    "event": "job_dispatch_failed",
                    "jobId": job_id,
                    "traceId": trace_id,
                    "errorType": type(exc).__name__,
                }
            )
        )
        return response(event, 503, {"error": "The job could not be queued"})

    metric("JobsQueued", Action=payload["action"])
    return response(
        event,
        202,
        {
            "jobId": job_id,
            "clientId": scope["clientId"],
            "projectId": scope["projectId"],
            "status": "queued",
            "pollAfterMs": 1500,
            "idempotent": False,
        },
    )


def _reconcile_expired_final_attempt(
    dynamodb: Any,
    scope: Mapping[str, str],
    job_id: str,
    job: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        job.get("status") not in {"running", "validating", "saving"}
        or int(job.get("retryCount") or 0) < MAX_RECEIVE_COUNT - 1
        or int(job.get("leaseExpiresAt") or 0) >= now_epoch()
    ):
        return dict(job)
    try:
        result = dynamodb.update_item(
            TableName=PROJECT_TABLE,
            Key=job_key(scope, job_id),
            UpdateExpression=(
                "SET #status = :failed, updatedAt = :updatedAt, "
                "#error = :error, errorType = :errorType "
                "REMOVE leaseExpiresAt"
            ),
            ConditionExpression=(
                "#status IN (:running, :validating, :saving) "
                "AND retryCount >= :lastRetry "
                "AND leaseExpiresAt < :now"
            ),
            ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues={
                ":failed": {"S": "failed"},
                ":running": {"S": "running"},
                ":validating": {"S": "validating"},
                ":saving": {"S": "saving"},
                ":lastRetry": {"N": str(MAX_RECEIVE_COUNT - 1)},
                ":now": {"N": str(now_epoch())},
                ":updatedAt": {"S": now_iso()},
                ":error": {"S": "The AI job timed out after its final retry"},
                ":errorType": {"S": "WorkerHardTimeout"},
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != (
            "ConditionalCheckFailedException"
        ):
            raise
        return dict(job)
    reconciled = deserialize_item(result.get("Attributes"))
    metric("TerminalJobsReconciled", Action=str(job.get("action") or "unknown"))
    return reconciled or dict(job)


def _get_job(event: Mapping[str, Any], job_id: str) -> dict[str, Any]:
    scope = _scope_from_query(event)
    dynamodb = aws_client("dynamodb")
    item = dynamodb.get_item(
        TableName=PROJECT_TABLE,
        Key=job_key(scope, require_identifier(job_id, "jobId")),
        ConsistentRead=True,
    ).get("Item")
    job = deserialize_item(item)
    if (
        not job
        or job.get("ownerId") != scope["userId"]
        or job.get("clientId") != scope["clientId"]
        or job.get("projectScopeId") != scope["projectId"]
    ):
        return response(event, 404, {"error": "Job not found"})

    job = _reconcile_expired_final_attempt(dynamodb, scope, job_id, job)
    status = str(job.get("status") or "queued")
    envelope: dict[str, Any] = {
        "jobId": job_id,
        "clientId": scope["clientId"],
        "projectId": scope["projectId"],
        "action": job.get("action"),
        "status": status,
        "retryCount": int(job.get("retryCount") or 0),
        "traceId": job.get("traceId"),
        "phase": job.get("phase") or status,
        "pollAfterMs": 1500,
    }
    if status in {
        "queued",
        "running",
        "validating",
        "saving",
        "waiting_for_scan",
        "transcribing",
        "screening",
        "analyzing",
    }:
        return response(event, 202, envelope)
    if status == "failed":
        envelope["error"] = job.get("error") or "The AI job failed"
        return response(event, 200, envelope)
    result_key = str(job.get("resultKey") or "")
    if status not in {"complete", "review-ready", "approved"} or not result_key:
        return response(event, 500, {"error": "Job result is unavailable"})
    result_object = aws_client("s3").get_object(
        Bucket=ARTIFACT_BUCKET, Key=result_key
    )
    result = json.loads(result_object["Body"].read().decode("utf-8"))
    envelope["result"] = result
    return response(event, 200, envelope)


def _list_clients(event: Mapping[str, Any]) -> dict[str, Any]:
    identity = derive_list_identity(event)
    allowed = set(identity["allowedClients"])
    result = aws_client("dynamodb").query(
        TableName=PROJECT_TABLE,
        KeyConditionExpression="projectId = :tenant AND begins_with(sortKey, :client)",
        ExpressionAttributeValues={
            ":tenant": {"S": f"TENANT#{identity['tenantId']}"},
            ":client": {"S": "CLIENT#"},
        },
        ConsistentRead=True,
    )
    stored = {
        item.get("clientId"): item
        for raw in result.get("Items", [])
        for item in [deserialize_item(raw)]
        if (
            item.get("clientId") in allowed
            and item.get("ownerId") in {None, "", identity["userId"]}
        )
    }
    clients = []
    for client_id in sorted(allowed):
        item = stored.get(client_id, {})
        clients.append(
            {
                "clientId": client_id,
                "projectId": item.get("projectScopeId") or client_id,
                "company": item.get("company") or client_id.replace("-", " ").title(),
                "latestApprovedAt": item.get("latestApprovedAt"),
                "latestHandoffAt": item.get("latestHandoffAt"),
                "approvedPacketVersion": item.get("approvedPacketVersion"),
                "hasApprovedBrief": bool(item.get("approvedArtifactKey")),
                "hasHandoff": bool(item.get("handoffArtifactKey")),
            }
        )
    clients.sort(
        key=lambda item: str(
            item.get("latestHandoffAt") or item.get("latestApprovedAt") or ""
        ),
        reverse=True,
    )
    return response(event, 200, {"clients": clients})


def _list_evidence(event: Mapping[str, Any]) -> dict[str, Any]:
    scope = _scope_from_query(event)
    if scope.get("identityType") != "authenticated":
        raise AuthorizationError("Evidence management requires a verified workspace")
    return response(
        event,
        200,
        {"documents": evidence_store.list_documents(scope)},
    )



def _read_latest_approved(
    event: Mapping[str, Any], client_id: str
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    scope = _scope_from_query(event, client_id=client_id)
    metadata_item = aws_client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key={
            "projectId": {"S": project_partition_key(scope)},
            "sortKey": {"S": "BRIEF#LATEST"},
        },
        ConsistentRead=True,
    ).get("Item")
    metadata = deserialize_item(metadata_item)
    artifact_key = str(metadata.get("approvedArtifactKey") or "")
    if not artifact_key:
        raise LookupError("No approved brief exists for this client")
    expected_prefix = f"{project_artifact_prefix(scope)}/brief/"
    if not artifact_key.startswith(expected_prefix):
        raise ScopeAuthorizationError("Stored packet is outside the authorized project")
    stored = aws_client("s3").get_object(
        Bucket=ARTIFACT_BUCKET, Key=artifact_key
    )
    document = json.loads(stored["Body"].read().decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Stored packet is invalid")
    return scope, metadata, document


def _get_latest(event: Mapping[str, Any], client_id: str) -> dict[str, Any]:
    scope, metadata, document = _read_latest_approved(
        event, require_identifier(client_id, "clientId")
    )
    stored_packet = document.get("response") or document
    if not isinstance(stored_packet, dict):
        raise ValueError("Stored packet is invalid")
    packet = dict(stored_packet)
    packet_metadata = dict(packet.get("metadata") or {})
    # Signed links are transient; keep the approved object and its digest unchanged.
    packet_metadata.pop("docxDownloadUrl", None)
    if metadata.get("approvedDocxArtifactKey"):
        download = _artifact_download(scope, metadata, "brief", "docx")
        packet_metadata["docxDownloadUrl"] = download["downloadUrl"]
    packet["metadata"] = packet_metadata
    return response(
        event,
        200,
        {
            "clientId": scope["clientId"],
            "projectId": scope["projectId"],
            "packetVersion": metadata.get("approvedPacketVersion"),
            "approvedAt": metadata.get("approvedAt"),
            "packet": packet,
            "requestContext": document.get("request") or {},
        },
    )


def _get_current(event: Mapping[str, Any], client_id: str) -> dict[str, Any]:
    scope = _scope_from_query(
        event, client_id=require_identifier(client_id, "clientId")
    )
    metadata_item = aws_client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key={
            "projectId": {"S": project_partition_key(scope)},
            "sortKey": {"S": "BRIEF#LATEST"},
        },
        ConsistentRead=True,
    ).get("Item")
    metadata = deserialize_item(metadata_item)
    packet_version = int(metadata.get("packetVersion") or 0)
    if packet_version < 1:
        raise LookupError("No current brief exists for this client")

    is_approved = (
        metadata.get("approvalStatus") == "approved"
        and int(metadata.get("approvedPacketVersion") or 0) == packet_version
    )
    artifact_key = str(
        metadata.get("approvedArtifactKey" if is_approved else "draftArtifactKey")
        or ""
    )
    docx_key = str(
        metadata.get(
            "approvedDocxArtifactKey" if is_approved else "draftDocxArtifactKey"
        )
        or ""
    )
    expected_prefix = f"{project_artifact_prefix(scope)}/brief/"
    if not artifact_key or not artifact_key.startswith(expected_prefix):
        raise ScopeAuthorizationError("Stored packet is outside the authorized project")
    if docx_key and not docx_key.startswith(expected_prefix):
        raise ScopeAuthorizationError("Stored packet is outside the authorized project")

    stored = aws_client("s3").get_object(
        Bucket=ARTIFACT_BUCKET, Key=artifact_key
    )
    document = json.loads(stored["Body"].read().decode("utf-8"))
    stored_packet = document.get("response") if isinstance(document, dict) else None
    if not isinstance(stored_packet, dict):
        raise ValueError("Stored packet is invalid")
    packet = dict(stored_packet)
    packet_metadata = dict(packet.get("metadata") or {})
    packet_metadata.pop("docxDownloadUrl", None)
    packet_metadata.update(
        {
            "clientId": scope["clientId"],
            "projectId": scope["projectId"],
            "packetVersion": packet_version,
            "approvedPacketVersion": metadata.get("approvedPacketVersion"),
            "approvalStatus": metadata.get("approvalStatus") or "draft",
            "approvedAt": metadata.get("approvedAt"),
        }
    )
    if docx_key:
        packet_metadata["docxDownloadUrl"] = aws_client("s3").generate_presigned_url(
            "get_object",
            Params={
                "Bucket": ARTIFACT_BUCKET,
                "Key": docx_key,
                "ResponseContentDisposition": _content_disposition(
                    _artifact_download_filename(
                        metadata.get("company") or scope["clientId"],
                        "brief",
                        packet_version,
                    )
                ),
            },
            ExpiresIn=900,
        )
    packet["metadata"] = packet_metadata
    return response(
        event,
        200,
        {
            "clientId": scope["clientId"],
            "projectId": scope["projectId"],
            "packetVersion": packet_version,
            "approvalStatus": packet_metadata["approvalStatus"],
            "packet": packet,
            "requestContext": document.get("request") or {},
        },
    )


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


def _get_artifact(event: Mapping[str, Any], artifact_type: str) -> dict[str, Any]:
    if artifact_type not in {"brief", "handoff"}:
        raise ValueError("artifactType must be brief or handoff")
    scope = _scope_from_query(event)
    item = aws_client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key={
            "projectId": {"S": project_partition_key(scope)},
            "sortKey": {
                "S": "BRIEF#LATEST" if artifact_type == "brief" else "HANDOFF#LATEST"
            },
        },
        ConsistentRead=True,
    ).get("Item")
    metadata = deserialize_item(item)
    format_name = _query(event).get("format", "docx")
    if format_name not in {"json", "docx"}:
        raise ValueError("format must be json or docx")
    return response(event, 200, _artifact_download(scope, metadata, artifact_type, format_name))


def _artifact_download(
    scope: Mapping[str, str], metadata: Mapping[str, Any], artifact_type: str, format_name: str
) -> dict[str, Any]:
    if artifact_type == "brief":
        key_name = "approvedDocxArtifactKey" if format_name == "docx" else "approvedArtifactKey"
    else:
        key_name = "docxArtifactKey" if format_name == "docx" else "artifactKey"
    artifact_key = str(metadata.get(key_name) or "")
    if not artifact_key:
        raise LookupError(f"No {artifact_type} {format_name} artifact exists")
    expected_prefix = f"{project_artifact_prefix(scope)}/{artifact_type}/"
    if not artifact_key.startswith(expected_prefix):
        raise ScopeAuthorizationError("Stored artifact is outside the authorized project")
    params = {"Bucket": ARTIFACT_BUCKET, "Key": artifact_key}
    if format_name == "docx":
        version = (
            metadata.get("approvedPacketVersion")
            if artifact_type == "brief"
            else metadata.get("sourceBriefVersion")
        )
        params["ResponseContentDisposition"] = _content_disposition(
            _artifact_download_filename(
                metadata.get("company") or scope["clientId"], artifact_type, version
            )
        )
    url = aws_client("s3").generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=900,
    )
    return {
        "artifactType": artifact_type,
        "format": format_name,
        "artifactKey": artifact_key,
        "downloadUrl": url,
        "expiresIn": 900,
    }


def handler(event: object, _context: Any) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        return {"statusCode": 400, "body": '{"error":"Invalid request"}'}
    try:
        assert_secure_request(event)
        assert_api_origin_verification(event)
        method, path = _route(event)
        if method == "OPTIONS":
            return response(event, 204, {})
        if method == "POST" and path == "/workspace/operations/dlq/replay":
            return _replay_dlq(event)
        if method == "GET" and path == "/workspace/meeting-audio/demo":
            return _get_demo_meeting_audio(event)
        if method == "POST" and path.endswith("/meeting-audio/uploads"):
            return _create_meeting_audio_upload(event)
        if method == "GET" and "/meeting-audio/uploads/" in path:
            upload_id = (
                _path_parameter(event, "uploadId")
                or path.rsplit("/", 1)[-1]
            )
            return _get_meeting_audio_upload(event, upload_id)
        if method == "POST" and path.endswith("/jobs"):
            return _start_job(event)
        if method == "GET" and "/jobs/" in path:
            return _get_job(event, _path_parameter(event, "jobId") or path.rsplit("/", 1)[-1])
        if method == "GET" and path.endswith("/clients"):
            return _list_clients(event)
        if method == "GET" and path == "/workspace/evidence":
            return _list_evidence(event)
        if method == "GET" and path.endswith("/current") and "/clients/" in path:
            client_id = _path_parameter(event, "clientId") or path.split("/clients/", 1)[1].split("/", 1)[0]
            return _get_current(event, client_id)
        if method == "GET" and path.endswith("/latest") and "/clients/" in path:
            client_id = _path_parameter(event, "clientId") or path.split("/clients/", 1)[1].split("/", 1)[0]
            return _get_latest(event, client_id)
        if method == "GET" and "/artifacts/" in path:
            artifact_type = _path_parameter(event, "artifactType") or path.rsplit("/", 1)[-1]
            return _get_artifact(event, artifact_type)
        return response(event, 404, {"error": "Route not found"})
    except UsageQuotaExceeded as exc:
        result = response(
            event,
            429,
            {
                "error": str(exc),
                "errorCode": "AI_USAGE_LIMIT",
                "retryAfterSeconds": exc.retry_after_seconds,
                "quota": exc.quota,
            },
        )
        result["headers"]["retry-after"] = str(exc.retry_after_seconds)
        return result
    except GenerationDisabled as exc:
        return response(event, 503, {"error": str(exc)})
    except ScopeAuthorizationError:
        metric("UnauthorizedRequests")
        metric("CrossScopeAuthorizationAttempts")
        return response(event, 403, {"error": "This resource is not available"})
    except PermissionError:
        metric("UnauthorizedRequests")
        metric("CrossScopeAuthorizationAttempts")
        return response(event, 403, {"error": "This resource is not available"})
    except AuthorizationError as exc:
        metric("UnauthorizedRequests")
        return response(event, 403, {"error": str(exc)})
    except LookupError as exc:
        return response(event, 404, {"error": str(exc)})
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        return response(event, 400, {"error": str(exc)})
    except Exception as exc:
        LOGGER.exception(
            json.dumps({"event": "jobs_api_error", "errorType": type(exc).__name__})
        )
        metric("JobsApiErrors", ErrorType=type(exc).__name__)
        return response(event, 500, {"error": "The PilarPrep job service is unavailable"})
