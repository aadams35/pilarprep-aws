from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import socket
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from botocore.exceptions import ClientError

from pipeline.state import (
    PROJECT_TABLE,
    aws_client,
    deserialize_item,
    now_iso,
    metric,
    project_partition_key,
    require_identifier,
    require_string,
    s3_encryption_args,
    serialize,
    slugify,
)


EVIDENCE_BUCKET = os.getenv("MEETING_EVIDENCE_BUCKET", "")
KNOWLEDGE_BASE_ID = os.getenv("KNOWLEDGE_BASE_ID", "")
KNOWLEDGE_BASE_DATA_SOURCE_ID = os.getenv(
    "KNOWLEDGE_BASE_DATA_SOURCE_ID", ""
)
ALLOWED_DOCUMENT_TYPES = {
    "architecture",
    "business-objective",
    "company-profile",
    "compliance",
    "constraints-risks",
    "customer-notes",
    "meeting-notes",
    "policy",
    "requirements",
    "stakeholder-profile",
    "technical-inventory",
}
ALLOWED_EXTENSIONS = {".csv", ".docx", ".html", ".json", ".md", ".pdf", ".txt"}
MAX_DOCUMENT_BYTES = 5_000_000
ALLOWED_CONTENT_TYPES = {
    "application/json",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/csv",
    "text/html",
    "text/markdown",
    "text/plain",
}


class EvidenceConflictError(ValueError):
    """The requested document mutation conflicts with durable evidence state."""


class EvidenceScopeError(PermissionError):
    """Retrieved evidence did not match the server-authorized client scope."""


def _is_guest(scope: Mapping[str, str]) -> bool:
    tenant_id = str(scope.get("tenantId") or "")
    return tenant_id == "demo" or tenant_id.startswith("guest-")


def _retrieval_filters(
    scope: Mapping[str, str],
) -> tuple[list[dict[str, Any]] | None, str]:
    if _is_guest(scope):
        if scope.get("clientId") != "bluemesa-payments":
            return None, "guest-no-private-rag"
        return [
            {"equals": {"key": "scenarioId", "value": "blue-mesa-payments"}},
            {"equals": {"key": "approved", "value": True}},
            {"equals": {"key": "visibility", "value": "public-demo"}},
        ], "public-demo"
    return [
        {"equals": {"key": "tenantId", "value": scope["tenantId"]}},
        {"equals": {"key": "clientId", "value": scope["clientId"]}},
        {"equals": {"key": "projectId", "value": scope["projectId"]}},
        {"equals": {"key": "approved", "value": True}},
        {"equals": {"key": "status", "value": "approved"}},
        {"equals": {"key": "visibility", "value": "tenant-private"}},
    ], "tenant-private"


def _assert_retrieval_scope(
    scope: Mapping[str, str], metadata: Mapping[str, Any]
) -> None:
    expected = (
        {
            "scenarioId": "blue-mesa-payments",
            "approved": True,
            "visibility": "public-demo",
        }
        if _is_guest(scope)
        else {
            "tenantId": scope["tenantId"],
            "clientId": scope["clientId"],
            "projectId": scope["projectId"],
            "approved": True,
            "status": "approved",
            "visibility": "tenant-private",
        }
    )
    if any(metadata.get(key) != value for key, value in expected.items()):
        metric("RagCrossScopeAttempts", Action="brief.generate")
        raise EvidenceScopeError(
            "Retrieved evidence escaped the authorized client scope"
        )


def retrieve_for_brief(
    scope: Mapping[str, str],
    query: str,
    *,
    retrieval_client: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    filters, mode = _retrieval_filters(scope)
    if not KNOWLEDGE_BASE_ID or filters is None:
        return [], {
            "enabled": False,
            "mode": mode,
            "resultCount": 0,
        }
    client = retrieval_client or aws_client("bedrock-agent-runtime")
    response = client.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={
            "text": (str(query or "customer research and discovery")[:1000])
        },
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": 6,
                "filter": {"andAll": filters},
            }
        },
    )
    sources = []
    for result in response.get("retrievalResults", []):
        if not isinstance(result, Mapping):
            continue
        metadata = result.get("metadata")
        if not isinstance(metadata, Mapping):
            raise EvidenceScopeError(
                "Retrieved evidence omitted authorization metadata"
            )
        _assert_retrieval_scope(scope, metadata)
        content = result.get("content")
        excerpt = (
            str(content.get("text") or "").strip()
            if isinstance(content, Mapping)
            else ""
        )
        if not excerpt:
            continue
        title = str(
            metadata.get("sourceTitle") or "Approved customer evidence"
        )[:240]
        source_seed = str(metadata.get("documentId") or title)
        digest = hashlib.sha256(source_seed.encode("utf-8")).hexdigest()[:12]
        sources.append(
            {
                "sourceId": f"src-rag-{digest}",
                "label": title,
                "sourceTitle": title,
                "sourceType": str(
                    metadata.get("documentType")
                    or "approved-customer-evidence"
                )[:80],
                "sourceLocation": "private-knowledge-base",
                "capturedAt": str(
                    metadata.get("approvedAt")
                    or metadata.get("uploadedAt")
                    or ""
                )[:80],
                "freshness": "approved-evidence",
                "approvedBy": str(
                    metadata.get("source") or "workspace-reviewer"
                )[:80],
                "evidenceSnippet": excerpt[:1200],
                "accessScope": mode,
                "lifecycleStatus": "active",
                "relevanceScore": round(float(result.get("score") or 0), 4),
            }
        )
    metric(
        "BriefRagRetrievals",
        value=max(1, len(sources)),
        Action="brief.generate",
        Mode=mode,
    )
    return sources, {
        "enabled": True,
        "mode": mode,
        "resultCount": len(sources),
        "maxResults": 6,
    }


def evidence_record_key(
    scope: Mapping[str, str], document_id: str
) -> dict[str, dict[str, str]]:
    return {
        "projectId": {"S": project_partition_key(scope)},
        "sortKey": {"S": f"EVIDENCE#{document_id}"},
    }


def _safe_filename(value: object) -> str:
    filename = require_string(value, "input.fileName", maximum=180)
    lowered = filename.lower()
    extension = next(
        (item for item in ALLOWED_EXTENSIONS if lowered.endswith(item)),
        "",
    )
    if not extension:
        raise ValueError(
            "Evidence files must be PDF, DOCX, TXT, Markdown, JSON, CSV, or HTML"
        )
    stem = slugify(filename[: -len(extension)], "evidence")
    return f"{stem}{extension}"


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_public_https_url(value: object) -> str:
    url = require_string(value, "input.sourceUrl", maximum=2048)
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Approved source URLs must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("Approved source URLs cannot include credentials")
    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or 443,
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise ValueError("The approved source URL could not be resolved") from exc
    if not addresses:
        raise ValueError("The approved source URL could not be resolved")
    for address in addresses:
        try:
            candidate = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("The approved source resolved to an invalid address") from exc
        if not candidate.is_global:
            raise ValueError("Approved source URLs must resolve to a public address")
    return url


def _read_approved_url(value: object) -> tuple[bytes, str, str]:
    current = _validate_public_https_url(value)
    opener = build_opener(_NoRedirectHandler())
    for redirect_count in range(4):
        request = Request(
            current,
            headers={
                "Accept": ", ".join(sorted(ALLOWED_CONTENT_TYPES)),
                "User-Agent": "PilarPrep-approved-source/1.0",
            },
        )
        try:
            response = opener.open(request, timeout=8)
        except HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise ValueError("The approved source URL could not be retrieved") from exc
            location = exc.headers.get("Location")
            if not location or redirect_count >= 3:
                raise ValueError("The approved source URL exceeded the redirect limit") from exc
            current = _validate_public_https_url(urljoin(current, location))
            continue
        content_type = str(response.headers.get_content_type() or "").lower()
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise ValueError("The approved source URL returned an unsupported content type")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "The approved source returned an invalid content length"
                ) from exc
            if declared_length < 0 or declared_length > MAX_DOCUMENT_BYTES:
                raise ValueError("The approved source exceeds the 5 MB limit")
        body = response.read(MAX_DOCUMENT_BYTES + 1)
        if len(body) > MAX_DOCUMENT_BYTES:
            raise ValueError("The approved source exceeds the 5 MB limit")
        if len(body) < 20:
            raise ValueError("The approved source did not contain enough content")
        return body, content_type, current
    raise ValueError("The approved source URL exceeded the redirect limit")


def _document_body(
    inputs: Mapping[str, Any], filename: str
) -> tuple[bytes, str, str]:
    source_url = str(inputs.get("sourceUrl") or "").strip()
    if source_url:
        body, content_type, final_url = _read_approved_url(source_url)
        return body, content_type, final_url

    content_base64 = str(inputs.get("contentBase64") or "").strip()
    if content_base64:
        try:
            body = base64.b64decode(content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("The evidence upload is not valid base64") from exc
        if len(body) > MAX_DOCUMENT_BYTES:
            raise ValueError("Evidence content exceeds 5 MB")
        if len(body) < 20:
            raise ValueError("Evidence content is too short")
        if filename.endswith(".pdf") and not body.startswith(b"%PDF-"):
            raise ValueError("The uploaded PDF signature is invalid")
        if filename.endswith(".docx") and not body.startswith(b"PK\x03\x04"):
            raise ValueError("The uploaded DOCX signature is invalid")
        content_type = str(inputs.get("contentType") or "").split(";", 1)[0].lower()
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise ValueError("The evidence upload has an unsupported content type")
        return body, content_type, "protected-workspace-object"

    content = require_string(
        inputs.get("content"),
        "input.content",
        minimum=20,
        maximum=MAX_DOCUMENT_BYTES,
    )
    body = content.encode("utf-8")
    if len(body) > MAX_DOCUMENT_BYTES:
        raise ValueError("Evidence content exceeds 5 MB")
    return body, "text/plain; charset=utf-8", "protected-workspace-object"


def _document_keys(
    scope: Mapping[str, str],
    document_id: str,
    filename: str,
) -> tuple[str, str]:
    prefix = (
        f"evidence/tenants/{scope['tenantId']}/clients/{scope['clientId']}/"
        f"projects/{scope['projectId']}/documents/{document_id}"
    )
    document_key = f"{prefix}/{filename}"
    return document_key, f"{document_key}.metadata.json"


def _record(
    scope: Mapping[str, str], document_id: str
) -> dict[str, Any]:
    item = aws_client("dynamodb").get_item(
        TableName=PROJECT_TABLE,
        Key=evidence_record_key(scope, document_id),
        ConsistentRead=True,
    ).get("Item")
    return deserialize_item(item)


def _public_record(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "documentId",
            "fileName",
            "sourceTitle",
            "documentType",
            "source",
            "approvalStatus",
            "status",
            "version",
            "checksumSha256",
            "createdAt",
            "updatedAt",
            "approvedAt",
            "ingestionJobId",
            "ingestionStatus",
            "failureReasons",
            "sourceId",
            "sourceType",
            "sourceLocation",
            "capturedAt",
            "freshness",
            "approvedBy",
            "accessScope",
            "lifecycleStatus",
        )
        if item.get(key) not in (None, "", [])
    }


def _put_record(
    scope: Mapping[str, str],
    item: Mapping[str, Any],
    *,
    allow_deleted: bool,
) -> None:
    values = {
        **evidence_record_key(scope, str(item["documentId"])),
        **{
            key: serialize(value)
            for key, value in item.items()
            if value is not None
        },
    }
    arguments: dict[str, Any] = {
        "TableName": PROJECT_TABLE,
        "Item": values,
        "ConditionExpression": "attribute_not_exists(sortKey)",
    }
    if allow_deleted:
        arguments.update(
            {
                "ConditionExpression": (
                    "attribute_not_exists(sortKey) OR #status = :deleted"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {":deleted": {"S": "DELETED"}},
            }
        )
    aws_client("dynamodb").put_item(**arguments)


def _start_sync(document_id: str, operation: str) -> dict[str, str]:
    if not KNOWLEDGE_BASE_ID or not KNOWLEDGE_BASE_DATA_SOURCE_ID:
        raise RuntimeError("The tenant Knowledge Base data source is not configured")
    try:
        response = aws_client("bedrock-agent").start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=KNOWLEDGE_BASE_DATA_SOURCE_ID,
            description=(
                f"PilarPrep {operation} for approved evidence {document_id}"
            )[:200],
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "")
        if code == "ConflictException":
            return {
                "ingestionStatus": "WAITING_FOR_SYNC",
                "ingestionJobId": "",
            }
        raise
    job = response.get("ingestionJob")
    if not isinstance(job, Mapping):
        raise RuntimeError("Bedrock did not return an ingestion job")
    job_id = require_string(
        job.get("ingestionJobId"), "ingestionJobId", maximum=80
    )
    return {
        "ingestionStatus": str(job.get("status") or "STARTING"),
        "ingestionJobId": job_id,
    }


def _update_status(
    scope: Mapping[str, str],
    document_id: str,
    *,
    status: str,
    ingestion_status: str,
    ingestion_job_id: str = "",
    failure_reasons: list[str] | None = None,
) -> None:
    values: dict[str, dict[str, Any]] = {
        ":status": {"S": status},
        ":ingestion": {"S": ingestion_status},
        ":updated": {"S": now_iso()},
    }
    update = (
        "SET #status = :status, ingestionStatus = :ingestion, "
        "ingestionJobId = :job, updatedAt = :updated"
    )
    values[":job"] = {"S": ingestion_job_id}
    if failure_reasons:
        update += ", failureReasons = :reasons"
        values[":reasons"] = serialize(failure_reasons[:5])
    aws_client("dynamodb").update_item(
        TableName=PROJECT_TABLE,
        Key=evidence_record_key(scope, document_id),
        UpdateExpression=update,
        ConditionExpression=(
            "tenantId = :tenant AND clientId = :client AND projectScopeId = :project"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            **values,
            ":tenant": {"S": scope["tenantId"]},
            ":client": {"S": scope["clientId"]},
            ":project": {"S": scope["projectId"]},
        },
    )


def ingest_document(
    scope: Mapping[str, str],
    inputs: Mapping[str, Any],
    *,
    source_job_id: str,
) -> dict[str, Any]:
    if not EVIDENCE_BUCKET or not PROJECT_TABLE:
        raise RuntimeError("Tenant evidence storage is not configured")
    if _is_guest(scope):
        raise EvidenceScopeError("Sign in before adding private customer evidence")
    document_id = require_identifier(inputs.get("documentId"), "input.documentId")
    existing = _record(scope, document_id)
    if existing and existing.get("sourceJobId") == source_job_id:
        return _public_record(existing)
    if existing and existing.get("status") != "DELETED":
        raise EvidenceConflictError(
            "This evidence document already exists; delete it before replacing it"
        )

    filename = _safe_filename(inputs.get("fileName"))
    source_title = require_string(
        inputs.get("sourceTitle"), "input.sourceTitle", maximum=240
    )
    document_type = require_string(
        inputs.get("documentType"), "input.documentType", maximum=64
    )
    if document_type not in ALLOWED_DOCUMENT_TYPES:
        raise ValueError("input.documentType is not supported")
    content_body, content_type, source_location = _document_body(inputs, filename)

    timestamp = now_iso()
    version = int(existing.get("version") or 0) + 1
    document_key, metadata_key = _document_keys(
        scope, document_id, filename
    )
    checksum = hashlib.sha256(content_body).hexdigest()
    source_id = f"src-doc-{checksum[:12]}"
    source_type = str(inputs.get("sourceType") or document_type)[:80]
    approved_by = str(inputs.get("approvedBy") or scope["userId"])[:120]
    metadata = {
        "tenantId": scope["tenantId"],
        "clientId": scope["clientId"],
        "projectId": scope["projectId"],
        "documentId": document_id,
        "documentType": document_type,
        "sourceTitle": source_title,
        "source": str(inputs.get("source") or "customer-upload")[:80],
        "approved": True,
        "status": "approved",
        "visibility": "tenant-private",
        "version": version,
        "uploadedAt": timestamp,
        "contentTrust": "untrusted-evidence",
        "sourceId": source_id,
        "sourceType": source_type,
        "sourceLocation": source_location if source_location.startswith("https://") else document_key,
        "capturedAt": timestamp,
        "freshness": "current",
        "approvedBy": approved_by,
        "accessScope": "tenant-private",
        "lifecycleStatus": "active",
    }
    sidecar = json.dumps(
        {"metadataAttributes": metadata},
        separators=(",", ":"),
    ).encode("utf-8")
    s3 = aws_client("s3")
    s3.put_object(
        Bucket=EVIDENCE_BUCKET,
        Key=document_key,
        Body=content_body,
        ContentType=content_type,
        Metadata={
            "document-id": document_id,
            "checksum-sha256": checksum,
            "approval-status": "approved",
        },
        **s3_encryption_args(),
    )
    s3.put_object(
        Bucket=EVIDENCE_BUCKET,
        Key=metadata_key,
        Body=sidecar,
        ContentType="application/json",
        **s3_encryption_args(),
    )

    record = {
        "entityType": "EVIDENCE_DOCUMENT",
        "tenantId": scope["tenantId"],
        "clientId": scope["clientId"],
        "projectScopeId": scope["projectId"],
        "ownerId": scope["userId"],
        "documentId": document_id,
        "fileName": filename,
        "sourceTitle": source_title,
        "documentType": document_type,
        "source": metadata["source"],
        "approvalStatus": "approved",
        "status": "STORED",
        "version": version,
        "checksumSha256": checksum,
        "objectKey": document_key,
        "metadataKey": metadata_key,
        "sourceJobId": source_job_id,
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "approvedAt": timestamp,
        "sourceId": source_id,
        "sourceType": source_type,
        "sourceLocation": source_location if source_location.startswith("https://") else document_key,
        "capturedAt": timestamp,
        "freshness": "current",
        "approvedBy": approved_by,
        "accessScope": "tenant-private",
        "lifecycleStatus": "active",
    }
    try:
        _put_record(scope, record, allow_deleted=bool(existing))
    except ClientError as exc:
        current = _record(scope, document_id)
        if current.get("sourceJobId") == source_job_id:
            return _public_record(current)
        raise EvidenceConflictError(
            "A concurrent evidence upload used this document ID"
        ) from exc

    sync = _start_sync(document_id, "ingestion")
    status = (
        "INGESTING"
        if sync["ingestionStatus"] != "WAITING_FOR_SYNC"
        else "INGESTION_PENDING"
    )
    _update_status(
        scope,
        document_id,
        status=status,
        ingestion_status=sync["ingestionStatus"],
        ingestion_job_id=sync["ingestionJobId"],
    )
    return _public_record(
        {
            **record,
            **sync,
            "status": status,
            "updatedAt": now_iso(),
        }
    )


def delete_document(
    scope: Mapping[str, str],
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    document_id = require_identifier(inputs.get("documentId"), "input.documentId")
    record = _record(scope, document_id)
    if not record:
        raise EvidenceConflictError("The evidence document does not exist")
    if record.get("status") == "DELETED":
        return _public_record(record)
    if record.get("status") == "DELETING" and record.get("ingestionJobId"):
        return _public_record(record)

    object_keys = [
        str(record.get("objectKey") or ""),
        str(record.get("metadataKey") or ""),
    ]
    objects = [{"Key": key} for key in object_keys if key]
    if objects:
        aws_client("s3").delete_objects(
            Bucket=EVIDENCE_BUCKET,
            Delete={"Objects": objects, "Quiet": True},
        )

    _update_status(
        scope,
        document_id,
        status="DELETION_PENDING",
        ingestion_status="START_REQUESTED",
    )
    sync = _start_sync(document_id, "deletion")
    status = (
        "DELETING"
        if sync["ingestionStatus"] != "WAITING_FOR_SYNC"
        else "DELETION_PENDING"
    )
    _update_status(
        scope,
        document_id,
        status=status,
        ingestion_status=sync["ingestionStatus"],
        ingestion_job_id=sync["ingestionJobId"],
    )
    return _public_record(
        {
            **record,
            **sync,
            "status": status,
            "updatedAt": now_iso(),
        }
    )


def reindex_document(
    scope: Mapping[str, str],
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    document_id = require_identifier(inputs.get("documentId"), "input.documentId")
    record = _record(scope, document_id)
    if not record or record.get("status") == "DELETED":
        raise EvidenceConflictError("The evidence document does not exist")

    sync = _start_sync(document_id, "re-index")
    status = (
        "INGESTING"
        if sync["ingestionStatus"] != "WAITING_FOR_SYNC"
        else "INGESTION_PENDING"
    )
    _update_status(
        scope,
        document_id,
        status=status,
        ingestion_status=sync["ingestionStatus"],
        ingestion_job_id=sync["ingestionJobId"],
    )
    return _public_record(
        {
            **record,
            **sync,
            "status": status,
            "updatedAt": now_iso(),
        }
    )


def _refresh_ingestion_status(
    scope: Mapping[str, str], record: Mapping[str, Any]
) -> dict[str, Any]:
    ingestion_job_id = str(record.get("ingestionJobId") or "")
    if not ingestion_job_id or record.get("status") not in {
        "INGESTING",
        "DELETING",
    }:
        return dict(record)

    try:
        response = aws_client("bedrock-agent").get_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=KNOWLEDGE_BASE_DATA_SOURCE_ID,
            ingestionJobId=ingestion_job_id,
        )
    except ClientError:
        metric("RagIngestionStatusFailures", Action="evidence.status")
        return {
            **record,
            "ingestionStatus": "STATUS_CHECK_FAILED",
        }
    job = response.get("ingestionJob")
    if not isinstance(job, Mapping):
        return dict(record)

    ingestion_status = str(
        job.get("status") or record.get("ingestionStatus") or ""
    )
    failure_reasons = [
        str(reason)[:300]
        for reason in job.get("failureReasons", [])
        if str(reason).strip()
    ]
    current_status = str(record.get("status") or "")
    next_status = current_status
    if ingestion_status == "COMPLETE":
        next_status = "DELETED" if current_status == "DELETING" else "AVAILABLE"
    elif ingestion_status in {"FAILED", "STOPPED"}:
        next_status = (
            "DELETION_FAILED"
            if current_status == "DELETING"
            else "INGESTION_FAILED"
        )

    if (
        next_status != current_status
        or ingestion_status != record.get("ingestionStatus")
        or failure_reasons != record.get("failureReasons", [])
    ):
        _update_status(
            scope,
            str(record["documentId"]),
            status=next_status,
            ingestion_status=ingestion_status,
            ingestion_job_id=ingestion_job_id,
            failure_reasons=failure_reasons,
        )
    return {
        **record,
        "status": next_status,
        "ingestionStatus": ingestion_status,
        "failureReasons": failure_reasons,
        "updatedAt": now_iso(),
    }


def list_documents(scope: Mapping[str, str]) -> list[dict[str, Any]]:
    if not PROJECT_TABLE:
        raise RuntimeError("Tenant evidence storage is not configured")
    result = aws_client("dynamodb").query(
        TableName=PROJECT_TABLE,
        KeyConditionExpression=(
            "projectId = :project AND begins_with(sortKey, :evidence)"
        ),
        ExpressionAttributeValues={
            ":project": {"S": project_partition_key(scope)},
            ":evidence": {"S": "EVIDENCE#"},
        },
        ConsistentRead=True,
        Limit=100,
    )
    records = [
        deserialize_item(item)
        for item in result.get("Items", [])
        if isinstance(item, Mapping)
    ]
    refreshed = [
        _refresh_ingestion_status(scope, record)
        for record in records
    ]
    visible = [
        _public_record(record)
        for record in refreshed
        if record.get("status") != "DELETED"
    ]
    return sorted(
        visible,
        key=lambda item: str(item.get("updatedAt") or ""),
        reverse=True,
    )
