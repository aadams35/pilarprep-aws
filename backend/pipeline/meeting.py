from __future__ import annotations

import hashlib
import json
import logging
import os
from urllib.parse import unquote_plus
from decimal import Decimal
from typing import Any, Mapping

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from pipeline.state import (
    ARTIFACT_BUCKET,
    PROJECT_TABLE,
    aws_client,
    dynamodb_client_request_token,
    job_key,
    now_epoch,
    now_iso,
    project_artifact_prefix,
    project_partition_key,
    require_identifier,
    s3_encryption_args,
    stable_identifier,
)
from pipeline.meeting_contracts import (
    SCENARIO_ID,
    TRANSCRIPT_PREFIX,
    MeetingConflictError,
    accepted_changes,
    assert_public_demo_scope,
    transcript_evidence,
)


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

MEETING_EVIDENCE_BUCKET = os.getenv("MEETING_EVIDENCE_BUCKET", "")
LIVE_AI_ENABLED = os.getenv("LIVE_AI_ENABLED", "true").lower() == "true"
MEETING_TTL_SECONDS = int(os.getenv("MEETING_TTL_SECONDS", "172800"))
CONTINUATION_LEASE_SECONDS = int(
    os.getenv("CONTINUATION_LEASE_SECONDS", "300")
)
_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()


def _continuation_key(job_name: str) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": f"TRANSCRIBE#{job_name}"},
        "sortKey": {"S": "MEETING#CONTINUATION"},
    }


def _upload_key(
    scope: Mapping[str, str], upload_id: str
) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": f"MEETING#UPLOAD#{upload_id}"},
    }


def _proposal_key(
    scope: Mapping[str, str], meeting_id: str
) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": f"MEETING#{meeting_id}#PROPOSAL"},
    }


def _latest_key(
    scope: Mapping[str, str], meeting_id: str
) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": f"MEETING#{meeting_id}#LATEST"},
    }


def _deserialize(item: object) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        return {}
    return {
        key: _DESERIALIZER.deserialize(value)
        for key, value in item.items()
        if isinstance(value, Mapping)
    }


def _normalize_etag(value: object) -> str:
    return str(value or "").strip().strip("\"")


def _audio_scope_prefix(scope: Mapping[str, str]) -> str:
    return "audio/uploads/{}/{}/{}/".format(
        scope["tenantId"],
        scope["clientId"],
        scope["projectId"],
    )


def _scope_from_audio_key(object_key: str) -> tuple[dict[str, str], str]:
    parts = object_key.split("/")
    if (
        len(parts) != 7
        or parts[0] != "audio"
        or parts[1] != "uploads"
        or not parts[6].startswith("meeting.")
    ):
        raise PermissionError("The scanned object is outside the meeting upload boundary")
    scope = {
        "tenantId": require_identifier(parts[2], "tenantId"),
        "clientId": require_identifier(parts[3], "clientId"),
        "projectId": require_identifier(parts[4], "projectId"),
    }
    upload_id = require_identifier(parts[5], "uploadId")
    return scope, upload_id


def _load_audio_upload(
    scope: Mapping[str, str], upload_id: str
) -> dict[str, Any]:
    return _deserialize(
        aws_client("dynamodb").get_item(
            TableName=PROJECT_TABLE,
            Key=_upload_key(scope, upload_id),
            ConsistentRead=True,
        ).get("Item")
    )


def _assert_upload_scope(
    item: Mapping[str, Any],
    scope: Mapping[str, str],
    *,
    upload_id: str,
    scenario_id: str,
    meeting_id: str,
) -> None:
    if not item:
        raise MeetingConflictError("Upload the meeting audio before processing it.")
    if (
        item.get("tenantId") != scope["tenantId"]
        or item.get("clientId") != scope["clientId"]
        or item.get("projectScopeId") != scope["projectId"]
        or item.get("ownerId") != scope["userId"]
        or item.get("sessionId") != scope["sessionId"]
        or item.get("uploadId") != upload_id
        or item.get("scenarioId") != scenario_id
        or item.get("meetingId") != meeting_id
    ):
        raise MeetingConflictError("The selected meeting audio is outside this session.")
    object_key = str(item.get("objectKey") or "")
    media_format = str(item.get("mediaFormat") or "")
    if (
        not object_key.startswith(_audio_scope_prefix(scope))
        or object_key.split("/")[-2] != upload_id
        or media_format not in {"mp3", "wav", "mp4"}
    ):
        raise MeetingConflictError("The meeting audio upload is invalid.")


def _object_identity(
    object_key: str,
    *,
    version_id: str,
) -> tuple[str, str, int]:
    request: dict[str, Any] = {
        "Bucket": MEETING_EVIDENCE_BUCKET,
        "Key": object_key,
        "ObjectAttributes": ["ETag", "ObjectSize"],
    }
    if version_id:
        request["VersionId"] = version_id
    response = aws_client("s3").get_object_attributes(**request)
    return (
        str(response.get("VersionId") or version_id),
        _normalize_etag(response.get("ETag")),
        int(response.get("ObjectSize") or 0),
    )


def _scan_tag(object_key: str, version_id: str) -> str:
    request: dict[str, Any] = {
        "Bucket": MEETING_EVIDENCE_BUCKET,
        "Key": object_key,
    }
    if version_id:
        request["VersionId"] = version_id
    response = aws_client("s3").get_object_tagging(**request)
    tags = {
        str(item.get("Key") or ""): str(item.get("Value") or "")
        for item in response.get("TagSet", [])
        if isinstance(item, Mapping)
    }
    return tags.get("GuardDutyMalwareScanStatus", "")


def _verify_clean_audio(item: Mapping[str, Any]) -> tuple[str, str, str]:
    object_key = str(item.get("objectKey") or "")
    version_id = str(item.get("scanVersionId") or "")
    expected_etag = _normalize_etag(item.get("scanETag"))
    expected_size = int(item.get("expectedSizeBytes") or 0)
    if (
        not object_key
        or not version_id
        or not expected_etag
        or item.get("scanTagVerified") is not True
    ):
        raise MeetingConflictError(
            "This upload predates verified audio scanning. Remove it and upload again."
        )
    try:
        actual_version, actual_etag, actual_size = _object_identity(
            object_key,
            version_id="",
        )
        scan_tag = _scan_tag(object_key, version_id)
    except ClientError as exc:
        raise MeetingConflictError(
            "The audio security status could not be verified. Remove it and upload again."
        ) from exc
    if (
        actual_version != version_id
        or actual_etag != expected_etag
        or actual_size != expected_size
        or scan_tag != "NO_THREATS_FOUND"
    ):
        raise MeetingConflictError(
            "The audio security status changed. Remove it and upload again."
        )
    return object_key, str(item.get("mediaFormat") or ""), version_id


def _reconcile_verified_clean_scan(
    scope: Mapping[str, str], upload_id: str, item: Mapping[str, Any]
) -> bool:
    object_key = str(item.get("objectKey") or "")
    expected_size = int(item.get("expectedSizeBytes") or 0)
    if not object_key or expected_size <= 0:
        return False
    try:
        version_id, etag, actual_size = _object_identity(object_key, version_id="")
        managed_tag = _scan_tag(object_key, version_id)
    except ClientError:
        return False
    if (
        not version_id
        or not etag
        or actual_size != expected_size
        or managed_tag != "NO_THREATS_FOUND"
    ):
        return False
    timestamp = now_iso()
    try:
        aws_client("dynamodb").update_item(
            TableName=PROJECT_TABLE,
            Key=_upload_key(scope, upload_id),
            UpdateExpression=(
                "SET #status = :clean, scanBucketName = :bucket, "
                "scanVersionId = :versionId, scanETag = :etag, "
                "scanStatus = :completed, scanResultStatus = :cleanResult, "
                "scanTagVerified = :verified, scanSource = :source, "
                "scannedAt = :updatedAt, updatedAt = :updatedAt"
            ),
            ConditionExpression=(
                "#status = :pending AND objectKey = :objectKey "
                "AND expectedSizeBytes = :expectedSize"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":pending": {"S": "pending_scan"},
                ":clean": {"S": "clean"},
                ":bucket": {"S": MEETING_EVIDENCE_BUCKET},
                ":versionId": {"S": version_id},
                ":etag": {"S": etag},
                ":completed": {"S": "COMPLETED"},
                ":cleanResult": {"S": "NO_THREATS_FOUND"},
                ":verified": {"BOOL": True},
                ":source": {"S": "guardduty-managed-tag-reconciliation"},
                ":objectKey": {"S": object_key},
                ":expectedSize": {"N": str(actual_size)},
                ":updatedAt": {"S": timestamp},
            },
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != (
            "ConditionalCheckFailedException"
        ):
            raise
        return False

def _defer_for_scan(
    scope: Mapping[str, str],
    upload_id: str,
    item: Mapping[str, Any],
    *,
    job_id: str,
    input_key: str,
    input_version: str,
    trace_id: str,
    approved_packet_version: int,
) -> bool:
    if item.get("waitingJobId") == job_id:
        return True
    if item.get("waitingJobId"):
        raise MeetingConflictError("This upload already has a processing request.")
    timestamp = now_iso()
    token = dynamodb_client_request_token(
        "waitscan", [scope["tenantId"], scope["clientId"], upload_id, job_id]
    )
    try:
        aws_client("dynamodb").transact_write_items(
            ClientRequestToken=token,
            TransactItems=[
                {
                    "Update": {
                        "TableName": PROJECT_TABLE,
                        "Key": _upload_key(scope, upload_id),
                        "UpdateExpression": (
                            "SET waitingJobId = :jobId, waitingInputKey = :inputKey, "
                            "waitingInputVersion = :inputVersion, waitingTraceId = :traceId, "
                            "waitingApprovedPacketVersion = :packetVersion, "
                            "waitingRequestedAt = :updatedAt, updatedAt = :updatedAt"
                        ),
                        "ConditionExpression": (
                            "#status = :pending AND objectKey = :objectKey "
                            "AND attribute_not_exists(waitingJobId)"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":pending": {"S": "pending_scan"},
                            ":objectKey": {"S": str(item.get("objectKey") or "")},
                            ":jobId": {"S": job_id},
                            ":inputKey": {"S": input_key},
                            ":inputVersion": {"S": input_version},
                            ":traceId": {"S": trace_id},
                            ":packetVersion": {"N": str(approved_packet_version)},
                            ":updatedAt": {"S": timestamp},
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": PROJECT_TABLE,
                        "Key": job_key(scope, job_id),
                        "UpdateExpression": (
                            "SET #status = :waiting, #phase = :waiting, "
                            "updatedAt = :updatedAt REMOVE leaseExpiresAt"
                        ),
                        "ConditionExpression": "#status = :running",
                        "ExpressionAttributeNames": {
                            "#status": "status",
                            "#phase": "phase",
                        },
                        "ExpressionAttributeValues": {
                            ":running": {"S": "running"},
                            ":waiting": {"S": "waiting_for_scan"},
                            ":updatedAt": {"S": timestamp},
                        },
                    }
                },
            ],
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {
            "TransactionCanceledException",
            "ConditionalCheckFailedException",
            "IdempotentParameterMismatchException",
        }:
            raise
        current = _load_audio_upload(scope, upload_id)
        return (
            current.get("status") == "pending_scan"
            and current.get("waitingJobId") == job_id
        )


def _resolve_audio_upload(
    scope: Mapping[str, str],
    inputs: Mapping[str, Any],
    scenario_id: str,
    meeting_id: str,
    *,
    job_id: str,
    input_key: str,
    input_version: str,
    trace_id: str,
    approved_packet_version: int,
) -> tuple[str, str, str] | None:
    upload_id = require_identifier(inputs.get("audioUploadId"), "audioUploadId")
    for _attempt in range(3):
        item = _load_audio_upload(scope, upload_id)
        _assert_upload_scope(
            item,
            scope,
            upload_id=upload_id,
            scenario_id=scenario_id,
            meeting_id=meeting_id,
        )
        status = str(item.get("status") or "")
        if status == "pending_scan":
            if _reconcile_verified_clean_scan(scope, upload_id, item):
                continue
            if _defer_for_scan(
                scope,
                upload_id,
                item,
                job_id=job_id,
                input_key=input_key,
                input_version=input_version,
                trace_id=trace_id,
                approved_packet_version=approved_packet_version,
            ):
                return None
            continue
        if status == "blocked":
            raise MeetingConflictError(
                "This audio upload was blocked. Remove it and upload a new file."
            )
        if status == "scan_failed":
            raise MeetingConflictError(
                "The audio malware scan could not complete. Remove it and upload again."
            )
        if status not in {"clean", "processing"}:
            raise MeetingConflictError(
                "This upload has no verified clean scan. Remove it and upload again."
            )
        if status == "processing" and item.get("processingJobId") != job_id:
            raise MeetingConflictError("This upload is already being processed.")
        object_key, media_format, _version_id = _verify_clean_audio(item)
        if status == "clean":
            try:
                aws_client("dynamodb").update_item(
                    TableName=PROJECT_TABLE,
                    Key=_upload_key(scope, upload_id),
                    UpdateExpression=(
                        "SET #status = :processing, processingJobId = :jobId, "
                        "updatedAt = :updatedAt"
                    ),
                    ConditionExpression="#status = :clean AND scanTagVerified = :verified",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":clean": {"S": "clean"},
                        ":processing": {"S": "processing"},
                        ":jobId": {"S": job_id},
                        ":verified": {"BOOL": True},
                        ":updatedAt": {"S": now_iso()},
                    },
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                continue
        return upload_id, object_key, media_format
    raise MeetingConflictError("The audio security state changed. Try processing again.")


def _scan_event_record_key(
    scope: Mapping[str, str],
    event_id: str,
    bucket_name: str,
    object_key: str,
    version_id: str,
    etag: str,
) -> dict[str, dict[str, str]]:
    digest = stable_identifier(
        "scan-event",
        [event_id, bucket_name, object_key, version_id, etag],
        40,
    )
    return {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": "MALWARE_SCAN#EVENT#" + digest},
    }


def handle_guardduty_scan_event(
    event: Mapping[str, Any], *, final_attempt: bool = False
) -> dict[str, Any]:
    if event.get("source") != "aws.guardduty" or event.get("detail-type") != (
        "GuardDuty Malware Protection Object Scan Result"
    ):
        raise ValueError("Unexpected GuardDuty event type")
    expected_account = os.getenv("EXPECTED_AWS_ACCOUNT_ID", "").strip()
    expected_region = os.getenv("AWS_REGION", "").strip()
    if expected_account and event.get("account") != expected_account:
        raise PermissionError("GuardDuty event account did not match")
    if expected_region and event.get("region") != expected_region:
        raise PermissionError("GuardDuty event region did not match")
    event_id = str(event.get("id") or "")
    if not event_id or len(event_id) > 128:
        raise ValueError("GuardDuty event ID is invalid")
    detail = event.get("detail")
    if not isinstance(detail, Mapping) or detail.get("resourceType") != "S3_OBJECT":
        raise ValueError("GuardDuty object scan detail is invalid")
    object_details = detail.get("s3ObjectDetails")
    result_details = detail.get("scanResultDetails")
    if not isinstance(object_details, Mapping) or not isinstance(result_details, Mapping):
        raise ValueError("GuardDuty scan result is incomplete")
    bucket_name = str(object_details.get("bucketName") or "")
    if bucket_name != MEETING_EVIDENCE_BUCKET:
        raise PermissionError("GuardDuty event bucket did not match")
    object_key = unquote_plus(str(object_details.get("objectKey") or ""))
    if not object_key.startswith("audio/uploads/"):
        raise PermissionError("GuardDuty event key is outside the upload prefix")
    event_etag = _normalize_etag(object_details.get("eTag"))
    version_id = str(object_details.get("versionId") or "")
    if not event_etag or not version_id:
        raise ValueError("GuardDuty object identity is incomplete")
    parsed_scope, upload_id = _scope_from_audio_key(object_key)
    item = _load_audio_upload(parsed_scope, upload_id)
    if not item:
        raise LookupError("GuardDuty event has no matching upload record")
    scope = {
        **parsed_scope,
        "userId": require_identifier(item.get("ownerId"), "userId"),
        "sessionId": require_identifier(item.get("sessionId"), "sessionId"),
    }
    _assert_upload_scope(
        item,
        scope,
        upload_id=upload_id,
        scenario_id=str(item.get("scenarioId") or ""),
        meeting_id=str(item.get("meetingId") or ""),
    )
    if str(item.get("objectKey") or "") != object_key:
        raise PermissionError("GuardDuty object key did not match the upload record")
    actual_version, actual_etag, actual_size = _object_identity(
        object_key,
        version_id="",
    )
    if (
        actual_version != version_id
        or actual_etag != event_etag
        or actual_size != int(item.get("expectedSizeBytes") or 0)
    ):
        raise PermissionError("GuardDuty object identity did not match the upload")
    scan_status = str(detail.get("scanStatus") or "")
    result_status = str(result_details.get("scanResultStatus") or "")
    managed_tag = _scan_tag(object_key, version_id)
    tag_verified = managed_tag == result_status and bool(result_status)
    if (
        scan_status == "COMPLETED"
        and result_status in {"NO_THREATS_FOUND", "THREATS_FOUND"}
        and not tag_verified
        and not final_attempt
    ):
        raise RuntimeError("The managed malware scan tag is not available yet")
    if (
        scan_status == "COMPLETED"
        and result_status == "NO_THREATS_FOUND"
        and tag_verified
    ):
        upload_status = "clean"
    elif (
        scan_status == "COMPLETED"
        and result_status == "THREATS_FOUND"
        and tag_verified
    ):
        upload_status = "blocked"
    else:
        upload_status = "scan_failed"
    timestamp = now_iso()
    event_key = _scan_event_record_key(
        scope,
        event_id,
        bucket_name,
        object_key,
        version_id,
        event_etag,
    )
    scan_event_item = {
        **event_key,
        "entityType": {"S": "MALWARE_SCAN_EVENT"},
        "eventId": {"S": event_id},
        "uploadId": {"S": upload_id},
        "scanStatus": {"S": scan_status or "UNKNOWN"},
        "scanResultStatus": {"S": result_status or "UNKNOWN"},
        "resolvedUploadStatus": {"S": upload_status},
        "tagVerified": {"BOOL": tag_verified},
        "createdAt": {"S": timestamp},
        "expiresAt": {"N": str(now_epoch() + MEETING_TTL_SECONDS)},
    }
    token = dynamodb_client_request_token(
        "scan",
        [event_id, bucket_name, object_key, version_id, event_etag],
    )
    duplicate = False
    try:
        aws_client("dynamodb").transact_write_items(
            ClientRequestToken=token,
            TransactItems=[
                {
                    "Put": {
                        "TableName": PROJECT_TABLE,
                        "Item": scan_event_item,
                        "ConditionExpression": "attribute_not_exists(projectId)",
                    }
                },
                {
                    "Update": {
                        "TableName": PROJECT_TABLE,
                        "Key": _upload_key(scope, upload_id),
                        "UpdateExpression": (
                            "SET #status = :status, scanBucketName = :bucket, "
                            "scanVersionId = :versionId, scanETag = :etag, "
                            "scanStatus = :scanStatus, scanResultStatus = :resultStatus, "
                            "scanTagVerified = :tagVerified, scanEventId = :eventId, "
                            "scannedAt = :updatedAt, updatedAt = :updatedAt"
                        ),
                        "ConditionExpression": (
                            "#status = :pending AND objectKey = :objectKey "
                            "AND expectedSizeBytes = :expectedSize"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":pending": {"S": "pending_scan"},
                            ":status": {"S": upload_status},
                            ":bucket": {"S": bucket_name},
                            ":versionId": {"S": version_id},
                            ":etag": {"S": event_etag},
                            ":scanStatus": {"S": scan_status or "UNKNOWN"},
                            ":resultStatus": {"S": result_status or "UNKNOWN"},
                            ":tagVerified": {"BOOL": tag_verified},
                            ":eventId": {"S": event_id},
                            ":objectKey": {"S": object_key},
                            ":expectedSize": {"N": str(actual_size)},
                            ":updatedAt": {"S": timestamp},
                        },
                    }
                },
            ],
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {
            "TransactionCanceledException",
            "ConditionalCheckFailedException",
            "IdempotentParameterMismatchException",
        }:
            raise
        duplicate = True
    current = _load_audio_upload(scope, upload_id)
    if not current:
        raise LookupError("The meeting audio upload disappeared during scan processing")
    current_status = str(current.get("status") or "")
    if (
        current_status == "processing"
        and current.get("scanTagVerified") is True
        and current.get("scanResultStatus") == "NO_THREATS_FOUND"
    ):
        current_status = "clean"
    if current_status == "pending_scan":
        raise RuntimeError("The GuardDuty scan state was not persisted")
    return {
        "outcome": current_status or upload_status,
        "duplicate": duplicate,
        "upload": current,
        "scope": scope,
    }


def _waiting_pointer(
    scope: Mapping[str, str], upload: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "tenantId": scope["tenantId"],
        "clientId": scope["clientId"],
        "projectId": scope["projectId"],
        "userId": scope["userId"],
        "sessionId": scope["sessionId"],
        "jobId": require_identifier(upload.get("waitingJobId"), "jobId"),
        "action": "meeting.process",
        "inputKey": str(upload.get("waitingInputKey") or ""),
        "inputVersion": str(upload.get("waitingInputVersion") or ""),
        "traceId": str(upload.get("waitingTraceId") or ""),
    }


def claim_waiting_scan_process(
    scan_result: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, Any]] | None:
    upload = scan_result.get("upload")
    scope = scan_result.get("scope")
    if not isinstance(upload, Mapping) or not isinstance(scope, Mapping):
        raise ValueError("GuardDuty scan result context is invalid")
    if not upload.get("waitingJobId"):
        return None
    pointer = _waiting_pointer(scope, upload)
    job_id = pointer["jobId"]
    status = str(upload.get("status") or "")
    if status == "processing" and upload.get("processingJobId") == job_id:
        job = _deserialize(
            aws_client("dynamodb").get_item(
                TableName=PROJECT_TABLE,
                Key=job_key(scope, job_id),
                ConsistentRead=True,
            ).get("Item")
        )
        return (dict(scope), pointer) if job.get("status") == "running" else None
    if status != "clean":
        return None
    timestamp = now_iso()
    token = dynamodb_client_request_token(
        "scanclaim", [scope["tenantId"], scope["clientId"], str(upload.get("uploadId")), job_id]
    )
    try:
        aws_client("dynamodb").transact_write_items(
            ClientRequestToken=token,
            TransactItems=[
                {
                    "Update": {
                        "TableName": PROJECT_TABLE,
                        "Key": _upload_key(scope, str(upload.get("uploadId"))),
                        "UpdateExpression": (
                            "SET #status = :processing, processingJobId = :jobId, "
                            "updatedAt = :updatedAt"
                        ),
                        "ConditionExpression": (
                            "#status = :clean AND waitingJobId = :jobId "
                            "AND scanTagVerified = :verified"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":clean": {"S": "clean"},
                            ":processing": {"S": "processing"},
                            ":jobId": {"S": job_id},
                            ":verified": {"BOOL": True},
                            ":updatedAt": {"S": timestamp},
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": PROJECT_TABLE,
                        "Key": job_key(scope, job_id),
                        "UpdateExpression": (
                            "SET #status = :running, #phase = :running, "
                            "updatedAt = :updatedAt, leaseExpiresAt = :lease"
                        ),
                        "ConditionExpression": (
                            "#status = :waiting AND ownerId = :ownerId "
                            "AND sessionId = :sessionId"
                        ),
                        "ExpressionAttributeNames": {
                            "#status": "status",
                            "#phase": "phase",
                        },
                        "ExpressionAttributeValues": {
                            ":waiting": {"S": "waiting_for_scan"},
                            ":running": {"S": "running"},
                            ":ownerId": {"S": scope["userId"]},
                            ":sessionId": {"S": scope["sessionId"]},
                            ":updatedAt": {"S": timestamp},
                            ":lease": {"N": str(now_epoch() + CONTINUATION_LEASE_SECONDS)},
                        },
                    }
                },
            ],
        )
        return dict(scope), pointer
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {
            "TransactionCanceledException",
            "ConditionalCheckFailedException",
            "IdempotentParameterMismatchException",
        }:
            raise
        current = _load_audio_upload(scope, str(upload.get("uploadId")))
        if (
            current.get("status") == "processing"
            and current.get("processingJobId") == job_id
        ):
            job = _deserialize(
                aws_client("dynamodb").get_item(
                    TableName=PROJECT_TABLE,
                    Key=job_key(scope, job_id),
                    ConsistentRead=True,
                ).get("Item")
            )
            if job.get("status") == "running":
                return dict(scope), pointer
        return None


def fail_waiting_scan_process(scan_result: Mapping[str, Any]) -> None:
    upload = scan_result.get("upload")
    scope = scan_result.get("scope")
    if (
        not isinstance(upload, Mapping)
        or not isinstance(scope, Mapping)
        or not upload.get("waitingJobId")
    ):
        return
    job_id = require_identifier(upload.get("waitingJobId"), "jobId")
    outcome = str(scan_result.get("outcome") or "scan_failed")
    message = (
        "This audio upload was blocked. Remove it and upload a new file."
        if outcome == "blocked"
        else "The audio malware scan could not complete. Remove it and upload again."
    )
    try:
        aws_client("dynamodb").update_item(
            TableName=PROJECT_TABLE,
            Key=job_key(scope, job_id),
            UpdateExpression=(
                "SET #status = :failed, #phase = :failed, updatedAt = :updatedAt, "
                "#error = :error, errorType = :errorType REMOVE leaseExpiresAt"
            ),
            ConditionExpression="#status = :waiting",
            ExpressionAttributeNames={
                "#status": "status",
                "#phase": "phase",
                "#error": "error",
            },
            ExpressionAttributeValues={
                ":waiting": {"S": "waiting_for_scan"},
                ":failed": {"S": "failed"},
                ":updatedAt": {"S": now_iso()},
                ":error": {"S": message},
                ":errorType": {"S": "AudioSecurityScanRejected"},
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def _dynamodb_safe(value: object) -> object:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {key: _dynamodb_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dynamodb_safe(item) for item in value]
    return value


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        integral = value.to_integral_value()
        return int(integral) if value == integral else float(value)
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _typed(value: object) -> dict[str, Any]:
    return _SERIALIZER.serialize(_dynamodb_safe(value))


def _scope_from_item(item: Mapping[str, Any]) -> dict[str, str]:
    return {
        field: require_identifier(item.get(field), field)
        for field in (
            "tenantId",
            "clientId",
            "projectScopeId",
            "userId",
            "sessionId",
        )
    } | {"projectId": require_identifier(item.get("projectScopeId"), "projectId")}


def continuation_scope(item: Mapping[str, Any]) -> dict[str, str]:
    return _scope_from_item(item)


def set_job_phase(
    scope: Mapping[str, str], job_id: str, status: str, phase: str
) -> None:
    _status_job(scope, job_id, status=status, phase=phase)


def _status_job(
    scope: Mapping[str, str],
    job_id: str,
    *,
    status: str,
    phase: str,
    extra: Mapping[str, object] | None = None,
) -> None:
    names = {"#status": "status", "#phase": "phase"}
    values: dict[str, dict[str, Any]] = {
        ":status": {"S": status},
        ":phase": {"S": phase},
        ":updatedAt": {"S": now_iso()},
    }
    sets = [
        "#status = :status",
        "#phase = :phase",
        "updatedAt = :updatedAt",
    ]
    for index, (name, value) in enumerate((extra or {}).items()):
        placeholder = f"#extra{index}"
        value_placeholder = f":extra{index}"
        names[placeholder] = name
        values[value_placeholder] = _typed(value)
        sets.append(f"{placeholder} = {value_placeholder}")
    aws_client("dynamodb").update_item(
        TableName=PROJECT_TABLE,
        Key=job_key(scope, job_id),
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _transcript_output_key(job_name: str) -> str:
    return f"{TRANSCRIPT_PREFIX}full-{job_name}.json"


def start_transcription(
    scope: Mapping[str, str],
    document: Mapping[str, Any],
    *,
    job_id: str,
    input_key: str,
    input_version: str,
    trace_id: str,
    approved_packet_version: int,
) -> dict[str, Any]:
    if not LIVE_AI_ENABLED:
        raise MeetingConflictError("Live meeting AI is temporarily disabled")
    if not MEETING_EVIDENCE_BUCKET:
        raise RuntimeError("Meeting evidence storage is not configured")
    inputs = document.get("input")
    if not isinstance(inputs, Mapping):
        raise ValueError("Meeting input is missing")
    scenario_id = str(inputs.get("scenarioId") or "")
    assert_public_demo_scope(scope, scenario_id)
    meeting_id = require_identifier(inputs.get("meetingId"), "meetingId")
    expected_version = int(inputs.get("expectedApprovedPacketVersion") or 0)
    if expected_version != approved_packet_version:
        raise MeetingConflictError(
            "The approved brief changed before meeting processing; reload it."
        )
    resolved_upload = _resolve_audio_upload(
        scope,
        inputs,
        scenario_id,
        meeting_id,
        job_id=job_id,
        input_key=input_key,
        input_version=input_version,
        trace_id=trace_id,
        approved_packet_version=approved_packet_version,
    )
    if resolved_upload is None:
        return {
            "deferred": True,
            "waitingForScan": True,
            "meetingId": meeting_id,
        }
    audio_upload_id, audio_key, media_format = resolved_upload
    job_name = f"pillarprep-{job_id}"
    output_key = _transcript_output_key(job_name)
    timestamp = now_iso()
    continuation = {
        **_continuation_key(job_name),
        "entityType": {"S": "MEETING_CONTINUATION"},
        "action": {"S": "meeting.process"},
        "status": {"S": "pending"},
        "scenarioId": {"S": scenario_id},
        "meetingId": {"S": meeting_id},
        "jobId": {"S": job_id},
        "traceId": {"S": trace_id},
        "inputKey": {"S": input_key},
        "inputVersion": {"S": input_version},
        "audioKey": {"S": audio_key},
        "audioUploadId": {"S": audio_upload_id},
        "mediaFormat": {"S": media_format},
        "outputKey": {"S": output_key},
        "transcriptMode": {"S": "full-private"},
        "requestedAt": {"S": str(document.get("createdAt") or timestamp)},
        "expectedApprovedPacketVersion": {"N": str(expected_version)},
        "tenantId": {"S": scope["tenantId"]},
        "clientId": {"S": scope["clientId"]},
        "projectScopeId": {"S": scope["projectId"]},
        "userId": {"S": scope["userId"]},
        "sessionId": {"S": scope["sessionId"]},
        "createdAt": {"S": timestamp},
        "updatedAt": {"S": timestamp},
        "expiresAt": {"N": str(now_epoch() + MEETING_TTL_SECONDS)},
    }
    try:
        aws_client("dynamodb").put_item(
            TableName=PROJECT_TABLE,
            Item=continuation,
            ConditionExpression="attribute_not_exists(projectId)",
        )
    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            != "ConditionalCheckFailedException"
        ):
            raise

    request: dict[str, Any] = {
        "TranscriptionJobName": job_name,
        "LanguageCode": "en-US",
        "MediaFormat": media_format,
        "Media": {
            "MediaFileUri": (
                f"s3://{MEETING_EVIDENCE_BUCKET}/{audio_key}"
            )
        },
        "OutputBucketName": MEETING_EVIDENCE_BUCKET,
        "OutputKey": output_key,
        "Settings": {
            "ShowSpeakerLabels": True,
            "MaxSpeakerLabels": 6,
        },
        "Tags": [
            {"Key": "Project", "Value": "PilarPrep"},
            {"Key": "ScenarioId", "Value": SCENARIO_ID},
            {"Key": "DataClassification", "Value": "private-meeting"},
        ],
    }
    try:
        aws_client("transcribe").start_transcription_job(**request)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConflictException":
            aws_client("dynamodb").update_item(
                TableName=PROJECT_TABLE,
                Key=_continuation_key(job_name),
                UpdateExpression="SET #status = :failed, updatedAt = :updatedAt",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":failed": {"S": "failed"},
                    ":updatedAt": {"S": now_iso()},
                },
            )
            raise
        existing = aws_client("transcribe").get_transcription_job(
            TranscriptionJobName=job_name
        ).get("TranscriptionJob", {})
        if existing.get("TranscriptionJobStatus") == "FAILED":
            raise RuntimeError("The existing transcription job failed")

    _status_job(
        scope,
        job_id,
        status="transcribing",
        phase="transcribing",
        extra={
            "transcriptionJobName": job_name,
            "scenarioId": scenario_id,
            "meetingId": meeting_id,
        },
    )
    return {
        "deferred": True,
        "transcriptionJobName": job_name,
        "meetingId": meeting_id,
    }


def load_continuation(job_name: object) -> dict[str, Any]:
    safe_name = require_identifier(
        str(job_name).replace(".", "-").replace("_", "-"),
        "transcriptionJobName",
    )
    if safe_name != job_name:
        raise ValueError("Unexpected transcription job name")
    item = aws_client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key=_continuation_key(safe_name),
        ConsistentRead=True,
    ).get("Item")
    continuation = _deserialize(item)
    if not continuation:
        raise LookupError("Transcription continuation was not found")
    return continuation


def claim_continuation(job_name: str) -> dict[str, Any] | None:
    lease = now_epoch() + CONTINUATION_LEASE_SECONDS
    try:
        response = aws_client("dynamodb").update_item(
            TableName=PROJECT_TABLE,
            Key=_continuation_key(job_name),
            UpdateExpression=(
                "SET #status = :processing, leaseExpiresAt = :lease, "
                "updatedAt = :updatedAt"
            ),
            ConditionExpression=(
                "#status = :pending OR "
                "(#status = :processing AND leaseExpiresAt < :now)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":pending": {"S": "pending"},
                ":processing": {"S": "processing"},
                ":lease": {"N": str(lease)},
                ":now": {"N": str(now_epoch())},
                ":updatedAt": {"S": now_iso()},
            },
            ReturnValues="ALL_NEW",
        )
        return _deserialize(response.get("Attributes"))
    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            == "ConditionalCheckFailedException"
        ):
            return None
        raise


def reset_continuation(job_name: str, error: Exception) -> None:
    aws_client("dynamodb").update_item(
        TableName=PROJECT_TABLE,
        Key=_continuation_key(job_name),
        UpdateExpression=(
            "SET #status = :pending, updatedAt = :updatedAt, "
            "lastErrorType = :errorType REMOVE leaseExpiresAt"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":pending": {"S": "pending"},
            ":updatedAt": {"S": now_iso()},
            ":errorType": {"S": type(error).__name__},
        },
    )


def complete_continuation(job_name: str, *, result_key: str = "") -> None:
    expression = (
        "SET #status = :complete, updatedAt = :updatedAt"
        + (", resultKey = :resultKey" if result_key else "")
        + " REMOVE leaseExpiresAt"
    )
    values = {
        ":complete": {"S": "complete"},
        ":updatedAt": {"S": now_iso()},
    }
    if result_key:
        values[":resultKey"] = {"S": result_key}
    aws_client("dynamodb").update_item(
        TableName=PROJECT_TABLE,
        Key=_continuation_key(job_name),
        UpdateExpression=expression,
        ConditionExpression="#status = :processing",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            **values,
            ":processing": {"S": "processing"},
        },
    )


def fail_continuation(
    continuation: Mapping[str, Any],
    job_name: str,
    message: str,
) -> None:
    scope = _scope_from_item(continuation)
    job_id = require_identifier(continuation.get("jobId"), "jobId")
    aws_client("dynamodb").update_item(
        TableName=PROJECT_TABLE,
        Key=_continuation_key(job_name),
        UpdateExpression=(
            "SET #status = :failed, updatedAt = :updatedAt, "
            "#error = :error REMOVE leaseExpiresAt"
        ),
        ExpressionAttributeNames={"#status": "status", "#error": "error"},
        ExpressionAttributeValues={
            ":failed": {"S": "failed"},
            ":updatedAt": {"S": now_iso()},
            ":error": {"S": message[:500]},
        },
    )
    _status_job(
        scope,
        job_id,
        status="failed",
        phase="failed",
        extra={"error": message[:500]},
    )


def read_transcript(continuation: Mapping[str, Any]) -> dict[str, Any]:
    output_key = str(continuation.get("outputKey") or "")
    if (
        continuation.get("transcriptMode") != "full-private"
        or not output_key.startswith(TRANSCRIPT_PREFIX + "full-pillarprep-")
        or not output_key.endswith(".json")
    ):
        raise PermissionError("Only a scoped private transcript may be processed")
    raw = (
        aws_client("s3")
        .get_object(Bucket=MEETING_EVIDENCE_BUCKET, Key=output_key)["Body"]
        .read()
    )
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, Mapping):
        raise ValueError("Transcribe output is invalid")
    return transcript_evidence(parsed)


def persist_proposal(
    scope: Mapping[str, str],
    continuation: Mapping[str, Any],
    transcript: Mapping[str, Any],
    analysis_result: Mapping[str, Any],
) -> dict[str, Any]:
    meeting_id = require_identifier(continuation.get("meetingId"), "meetingId")
    scenario_id = require_identifier(
        continuation.get("scenarioId"), "scenarioId"
    )
    assert_public_demo_scope(scope, scenario_id)
    analysis = analysis_result.get("analysis")
    review_items = analysis_result.get("reviewItems")
    if not isinstance(analysis, Mapping) or not isinstance(review_items, list):
        raise ValueError("Meeting analysis result is incomplete")
    job_id = require_identifier(continuation.get("jobId"), "jobId")
    digest = hashlib.sha256(
        json.dumps(analysis, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    proposal_id = stable_identifier("proposal", [job_id, digest], 24)
    base_version = int(
        continuation.get("expectedApprovedPacketVersion") or 0
    )
    timestamp = now_iso()
    analysis_metadata = analysis_result.get("metadata")
    analysis_metadata = (
        dict(analysis_metadata)
        if isinstance(analysis_metadata, Mapping)
        else {}
    )
    proposal = {
        "proposalId": proposal_id,
        "scenarioId": scenario_id,
        "meetingId": meeting_id,
        "status": "proposed",
        "baseBriefVersion": base_version,
        "analysis": dict(analysis),
        "reviewItems": review_items,
        "retrieval": dict(analysis_result.get("retrieval") or {}),
        "model": dict(analysis_result.get("model") or {}),
        "safety": dict(analysis_metadata.get("safety") or {}),
        "createdAt": timestamp,
        "traceId": continuation.get("traceId"),
    }
    prefix = f"{project_artifact_prefix(scope)}/meeting/{meeting_id}"
    transcript_key = (
        "transcripts/private/{}/{}/{}/{}/latest.json".format(
            scope["tenantId"],
            scope["clientId"],
            scope["projectId"],
            meeting_id,
        )
    )
    proposal_key = f"{prefix}/proposal/latest.json"
    s3 = aws_client("s3")
    s3.put_object(
        Bucket=MEETING_EVIDENCE_BUCKET,
        Key=transcript_key,
        Body=json.dumps(transcript, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
        **s3_encryption_args(),
    )
    s3.put_object(
        Bucket=ARTIFACT_BUCKET,
        Key=proposal_key,
        Body=json.dumps(proposal, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
        **s3_encryption_args(),
    )
    aws_client("dynamodb").put_item(
        TableName=PROJECT_TABLE,
        Item={
            **_proposal_key(scope, meeting_id),
            "entityType": {"S": "MEETING_PROPOSAL"},
            "proposalId": {"S": proposal_id},
            "scenarioId": {"S": scenario_id},
            "meetingId": {"S": meeting_id},
            "status": {"S": "proposed"},
            "baseBriefVersion": {"N": str(base_version)},
            "proposal": _typed(proposal),
            "proposalArtifactKey": {"S": proposal_key},
            "transcriptArtifactKey": {"S": transcript_key},
            "createdAt": {"S": timestamp},
            "updatedAt": {"S": timestamp},
            "expiresAt": {"N": str(now_epoch() + MEETING_TTL_SECONDS)},
        },
    )
    return {
        "provider": "agentcore-strands",
        "action": "meeting.process",
        "status": "review-ready",
        "scenarioId": scenario_id,
        "meetingId": meeting_id,
        "proposalId": proposal_id,
        "baseBriefVersion": base_version,
        "transcript": dict(transcript),
        "analysis": dict(analysis),
        "reviewItems": review_items,
        "citations": list(analysis.get("citations") or []),
        "metadata": {
            "proposalArtifactKey": proposal_key,
            "transcriptArtifactKey": transcript_key,
            "transcriptStorage": "private-meeting-evidence",
            "traceId": continuation.get("traceId"),
            "retrieval": proposal["retrieval"],
            "model": proposal["model"],
            "safety": proposal["safety"],
            "syntheticDemo": True,
            "writesApplied": False,
        },
    }


def load_proposal(
    scope: Mapping[str, str],
    meeting_id: str,
    proposal_id: str,
) -> dict[str, Any]:
    item = aws_client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key=_proposal_key(scope, meeting_id),
        ConsistentRead=True,
    ).get("Item")
    stored = _deserialize(item)
    proposal = stored.get("proposal")
    if (
        not isinstance(proposal, Mapping)
        or stored.get("proposalId") != proposal_id
        or proposal.get("scenarioId") != SCENARIO_ID
    ):
        raise MeetingConflictError("Meeting proposal is unavailable or stale")
    return dict(proposal)


def review_proposal(
    scope: Mapping[str, str],
    document: Mapping[str, Any],
    *,
    current_approved_version: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    inputs = document.get("input")
    if not isinstance(inputs, Mapping):
        raise ValueError("Meeting approval input is missing")
    assert_public_demo_scope(scope, inputs.get("scenarioId"))
    meeting_id = require_identifier(inputs.get("meetingId"), "meetingId")
    proposal_id = require_identifier(inputs.get("proposalId"), "proposalId")
    expected_version = int(inputs.get("expectedApprovedPacketVersion") or 0)
    if expected_version != current_approved_version:
        raise MeetingConflictError(
            "The approved brief changed during meeting review; reload the latest packet."
        )
    proposal = load_proposal(scope, meeting_id, proposal_id)
    if int(proposal.get("baseBriefVersion") or 0) != expected_version:
        raise MeetingConflictError(
            "This meeting proposal was created from a different brief version."
        )
    dispositions = inputs.get("dispositions")
    if not isinstance(dispositions, list):
        raise ValueError("Meeting review decisions are missing")
    accepted, rejected = accepted_changes(proposal, dispositions, now_iso())
    return proposal, accepted, rejected


def _clock_label(seconds: object) -> str:
    try:
        value = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        value = 0
    return f"{value // 60:02d}:{value % 60:02d}"


def approved_meeting_notes(
    proposal: Mapping[str, Any], accepted: list[Mapping[str, Any]]
) -> str:
    analysis = proposal.get("analysis")
    summary = (
        str(analysis.get("meetingSummary") or "").strip()
        if isinstance(analysis, Mapping)
        else ""
    )
    lines = [
        "APPROVED SYNTHETIC MEETING EVIDENCE",
        f"Meeting: {proposal.get('meetingId')}",
        f"Scenario: {proposal.get('scenarioId')}",
    ]
    if summary:
        lines.extend(["", "Meeting summary:", summary])
    lines.extend(["", "Human-approved changes:"])
    for item in accepted:
        statement = str(item.get("proposedUpdate") or "").strip()
        evidence = str(item.get("evidenceText") or "").strip()
        speaker = str(item.get("speaker") or "Speaker").strip()
        timestamp = _clock_label(item.get("timestampStart"))
        category = str(item.get("category") or "Update").strip()
        owner = str(item.get("owner") or "").strip()
        lines.append(f"- [{category}] {statement}")
        lines.append(f"  Evidence: {speaker} at {timestamp}: {evidence}")
        if owner:
            lines.append(f"  Owner: {owner}")
    lines.extend(
        [
            "",
            "Only the changes listed above were approved. Rejected proposals are audit history, not project facts.",
        ]
    )
    return "\n".join(lines)


def _approval_key(
    scope: Mapping[str, str], meeting_id: str, approval_id: str
) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": f"MEETING#{meeting_id}#APPROVED#{approval_id}"},
    }


def finalize_approval(
    scope: Mapping[str, str],
    document: Mapping[str, Any],
    proposal: Mapping[str, Any],
    accepted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    handoff: Mapping[str, Any],
) -> dict[str, Any]:
    inputs = document.get("input")
    if not isinstance(inputs, Mapping):
        raise ValueError("Meeting approval input is missing")
    meeting_id = require_identifier(proposal.get("meetingId"), "meetingId")
    proposal_id = require_identifier(proposal.get("proposalId"), "proposalId")
    base_version = int(proposal.get("baseBriefVersion") or 0)
    timestamp = now_iso()
    approval_digest = hashlib.sha256(
        json.dumps(
            _json_safe({
                "proposalId": proposal_id,
                "accepted": accepted,
                "rejected": rejected,
                "baseBriefVersion": base_version,
            }),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    approval_id = stable_identifier(
        "meeting-approval", [proposal_id, approval_digest], 28
    )
    dynamodb = aws_client("dynamodb")
    previous_latest = _deserialize(
        dynamodb.get_item(
            TableName=PROJECT_TABLE,
            Key=_latest_key(scope, meeting_id),
            ConsistentRead=True,
        ).get("Item")
    )
    previous_approval_id = str(previous_latest.get("approvalId") or "")
    if previous_approval_id and previous_latest.get("status") != "approved":
        raise MeetingConflictError(
            "The current meeting approval pointer is not in an approvable state."
        )
    prefix = f"{project_artifact_prefix(scope)}/meeting/{meeting_id}/approved"
    immutable_key = f"{prefix}/{approval_id}.json"
    latest_key = f"{prefix}/latest.json"
    handoff_metadata = handoff.get("metadata")
    handoff_metadata = (
        dict(handoff_metadata) if isinstance(handoff_metadata, Mapping) else {}
    )
    approval = {
        "approvalId": approval_id,
        "proposalId": proposal_id,
        "scenarioId": SCENARIO_ID,
        "meetingId": meeting_id,
        "status": "approved",
        "baseBriefVersion": base_version,
        "approvedAt": timestamp,
        "approvedBy": scope["userId"],
        "acceptedChanges": _json_safe(accepted),
        "rejectedChanges": _json_safe(rejected),
        "handoff": {
            "artifactKey": handoff_metadata.get("artifactKey"),
            "docxArtifactKey": handoff_metadata.get("docxArtifactKey"),
            "provider": handoff.get("provider"),
        },
        "audit": {
            "allItemsReviewed": True,
            "acceptedCount": len(accepted),
            "rejectedCount": len(rejected),
            "sourceProposalCreatedAt": proposal.get("createdAt"),
            "traceId": proposal.get("traceId"),
        },
    }
    if previous_approval_id:
        approval["supersedesApprovalId"] = previous_approval_id
    body = json.dumps(approval, separators=(",", ":")).encode("utf-8")
    s3 = aws_client("s3")
    for key in (immutable_key, latest_key):
        s3.put_object(
            Bucket=ARTIFACT_BUCKET,
            Key=key,
            Body=body,
            ContentType="application/json",
            **s3_encryption_args(),
        )

    latest_item = {
        **_latest_key(scope, meeting_id),
        "entityType": {"S": "MEETING_APPROVAL_LATEST"},
        "scenarioId": {"S": SCENARIO_ID},
        "meetingId": {"S": meeting_id},
        "approvalId": {"S": approval_id},
        "proposalId": {"S": proposal_id},
        "status": {"S": "approved"},
        "baseBriefVersion": {"N": str(base_version)},
        "approvedAt": {"S": timestamp},
        "approvedArtifactKey": {"S": latest_key},
        "immutableApprovalArtifactKey": {"S": immutable_key},
        "acceptedCount": {"N": str(len(accepted))},
        "rejectedCount": {"N": str(len(rejected))},
    }
    if previous_approval_id:
        latest_item["supersedesApprovalId"] = {"S": previous_approval_id}
    immutable_item = {
        **_approval_key(scope, meeting_id, approval_id),
        **{key: value for key, value in latest_item.items() if key not in {"projectId", "sortKey"}},
        "entityType": {"S": "MEETING_APPROVAL_AUDIT"},
        "approval": _typed(approval),
    }
    try:
        dynamodb.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": PROJECT_TABLE,
                        "Key": _proposal_key(scope, meeting_id),
                        "UpdateExpression": (
                            "SET #status = :approved, approvalId = :approvalId, "
                            "approvedAt = :approvedAt REMOVE expiresAt"
                        ),
                        "ConditionExpression": (
                            "proposalId = :proposalId AND #status = :proposed "
                            "AND baseBriefVersion = :baseVersion"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":approved": {"S": "approved"},
                            ":proposed": {"S": "proposed"},
                            ":proposalId": {"S": proposal_id},
                            ":approvalId": {"S": approval_id},
                            ":approvedAt": {"S": timestamp},
                            ":baseVersion": {"N": str(base_version)},
                        },
                    }
                },
                *(
                    [
                        {
                            "Update": {
                                "TableName": PROJECT_TABLE,
                                "Key": _approval_key(
                                    scope, meeting_id, previous_approval_id
                                ),
                                "UpdateExpression": (
                                    "SET #status = :superseded, "
                                    "supersededAt = :supersededAt, "
                                    "supersededBy = :supersededBy"
                                ),
                                "ConditionExpression": "#status = :approved",
                                "ExpressionAttributeNames": {"#status": "status"},
                                "ExpressionAttributeValues": {
                                    ":approved": {"S": "approved"},
                                    ":superseded": {"S": "superseded"},
                                    ":supersededAt": {"S": timestamp},
                                    ":supersededBy": {"S": approval_id},
                                },
                            }
                        }
                    ]
                    if previous_approval_id
                    else []
                ),
                {
                    "Put": {
                        "TableName": PROJECT_TABLE,
                        "Item": immutable_item,
                        "ConditionExpression": "attribute_not_exists(projectId)",
                    }
                },
                {
                    "Put": {
                        "TableName": PROJECT_TABLE,
                        "Item": latest_item,
                        "ConditionExpression": (
                            "approvalId = :previousApprovalId "
                            "AND #status = :approved"
                            if previous_approval_id
                            else "attribute_not_exists(projectId)"
                        ),
                        **(
                            {
                                "ExpressionAttributeNames": {
                                    "#status": "status"
                                },
                                "ExpressionAttributeValues": {
                                    ":previousApprovalId": {
                                        "S": previous_approval_id
                                    },
                                    ":approved": {"S": "approved"},
                                },
                            }
                            if previous_approval_id
                            else {}
                        ),
                    }
                },
            ]
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "TransactionCanceledException",
            "ConditionalCheckFailedException",
        }:
            raise MeetingConflictError(
                "This meeting proposal was already reviewed or became stale."
            ) from exc
        raise

    result = dict(handoff)
    metadata = result.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    metadata.update(
        {
            "meetingApprovalId": approval_id,
            "meetingProposalId": proposal_id,
            "meetingId": meeting_id,
            "meetingApprovalStatus": "approved",
            "meetingApprovedAt": timestamp,
            "meetingAcceptedChangeCount": len(accepted),
            "meetingRejectedChangeCount": len(rejected),
            "meetingApprovalArtifactKey": latest_key,
        }
    )
    result["metadata"] = metadata
    result["meetingApproval"] = {
        "approvalId": approval_id,
        "proposalId": proposal_id,
        "meetingId": meeting_id,
        "status": "approved",
        "approvedAt": timestamp,
        "acceptedCount": len(accepted),
        "rejectedCount": len(rejected),
        "supersedesApprovalId": previous_approval_id or None,
    }
    return result
