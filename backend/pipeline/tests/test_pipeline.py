from __future__ import annotations

import json
import sys
import types
import unittest
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))


class TypeSerializer:
    def serialize(self, value):
        if value is None:
            return {"NULL": True}
        if isinstance(value, bool):
            return {"BOOL": value}
        if isinstance(value, str):
            return {"S": value}
        if isinstance(value, (int, float, Decimal)):
            return {"N": str(value)}
        if isinstance(value, list):
            return {"L": [self.serialize(item) for item in value]}
        if isinstance(value, dict):
            return {"M": {key: self.serialize(item) for key, item in value.items()}}
        raise TypeError(f"Unsupported test value: {type(value).__name__}")


class TypeDeserializer:
    def deserialize(self, value):
        if "NULL" in value:
            return None
        if "BOOL" in value:
            return value["BOOL"]
        if "S" in value:
            return value["S"]
        if "N" in value:
            number = value["N"]
            return float(number) if "." in number else int(number)
        if "L" in value:
            return [self.deserialize(item) for item in value["L"]]
        if "M" in value:
            return {
                key: self.deserialize(item) for key, item in value["M"].items()
            }
        raise TypeError("Unsupported DynamoDB test value")


fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda *args, **kwargs: None
fake_dynamodb = types.ModuleType("boto3.dynamodb")
fake_dynamodb_types = types.ModuleType("boto3.dynamodb.types")
fake_dynamodb_types.TypeSerializer = TypeSerializer
fake_dynamodb_types.TypeDeserializer = TypeDeserializer
sys.modules.setdefault("boto3", fake_boto3)
sys.modules.setdefault("boto3.dynamodb", fake_dynamodb)
sys.modules.setdefault("boto3.dynamodb.types", fake_dynamodb_types)

fake_botocore = types.ModuleType("botocore")
fake_exceptions = types.ModuleType("botocore.exceptions")
fake_config = types.ModuleType("botocore.config")


class ClientError(Exception):
    def __init__(self, response, operation_name="test"):
        super().__init__(operation_name)
        self.response = response


fake_exceptions.ClientError = ClientError


class Config:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


fake_config.Config = Config
sys.modules.setdefault("botocore", fake_botocore)
sys.modules.setdefault("botocore.exceptions", fake_exceptions)
sys.modules.setdefault("botocore.config", fake_config)

from jobs_api import handler as api  # noqa: E402
from ai_worker import handler as worker  # noqa: E402
from pipeline import (  # noqa: E402
    state as common,
    evidence,
    handoff_promotion,
    meeting,
    meeting_contracts,
)
from shared import content_safety  # noqa: E402


SCOPE = {
    "tenantId": common.identity_tenant_id("guest", "us-east-1:identity"),
    "clientId": "apex-mutual",
    "projectId": "apex-mutual",
    "userId": common.stable_identifier("user", ["us-east-1:identity"]),
    "sessionId": "session-demo-0001",
    "identityType": "guest",
}


BLUE_SCOPE = {
    "tenantId": common.identity_tenant_id("guest", "us-east-1:identity"),
    "clientId": "bluemesa-payments",
    "projectId": "bluemesa-payments",
    "userId": common.stable_identifier("user", ["us-east-1:identity"]),
    "sessionId": "session-blue-mesa",
    "identityType": "guest",
}

AUTH_BLUE_SCOPE = {
    "tenantId": common.identity_tenant_id("personal", "workspace-user-1"),
    "clientId": "bluemesa-payments",
    "projectId": "bluemesa-payments",
    "userId": common.stable_identifier("user", ["workspace-user-1"]),
    "sessionId": "session-blue-mesa",
    "identityType": "authenticated",
}

def iam_event(method="POST", path="/jobs", *, body=None, query=None):
    return {
        "rawPath": path,
        "body": json.dumps(body or {}),
        "headers": {
            "origin": "https://pilarprep.app",
            "x-forwarded-proto": "https",
        },
        "queryStringParameters": query,
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {
                "iam": {
                    "cognitoIdentity": {
                        "identityId": "us-east-1:identity",
                        "identityPoolId": "us-east-1:pool",
                    }
                }
            },
        },
    }


def jwt_event(
    method="POST",
    path="/workspace/jobs",
    *,
    body=None,
    query=None,
    claims=None,
):
    event = iam_event(method, path, body=body, query=query)
    event["requestContext"]["authorizer"] = {
        "jwt": {
            "claims": claims
            or {
                "sub": "workspace-user-1",
                "email": "owner@example.com",
                "email_verified": "true",
            }
        }
    }
    return event


def generation_request():
    return {
        "action": "brief.generate",
        "clientId": "apex-mutual",
        "projectId": "apex-mutual",
        "sessionId": "session-demo-0001",
        "idempotencyKey": "generate-demo-0001",
        "input": {
            "company": "Apex Mutual",
            "context": "Customer-approved modernization context.",
            "modelPreference": "nova-pro",
        },
    }


class CommonContractTests(unittest.TestCase):
    def setUp(self):
        common.clear_aws_client_cache()

    def tearDown(self):
        common.clear_aws_client_cache()

    def test_metric_emits_aggregate_and_detailed_dimension_sets(self):
        with patch("builtins.print") as emit:
            common.metric("JobsCompleted", Action="brief.generate")

        payload = json.loads(emit.call_args.args[0])
        metric_definition = payload["_aws"]["CloudWatchMetrics"][0]
        self.assertEqual(
            metric_definition["Dimensions"],
            [["Service"], ["Service", "Action"]],
        )
        self.assertEqual(payload["Service"], "JobsPipeline")
        self.assertEqual(payload["Action"], "brief.generate")

    def test_artifact_args_use_kms_and_expire_only_guest_work(self):
        key_arn = "arn:aws:kms:us-east-1:111122223333:key/test"
        with patch.object(common, "DATA_KMS_KEY_ARN", key_arn):
            guest = common.s3_artifact_args({"tenantId": "guest-identity"})
            workspace = common.s3_artifact_args({"tenantId": "tenant-acme"})

        self.assertEqual(guest["ServerSideEncryption"], "aws:kms")
        self.assertEqual(guest["SSEKMSKeyId"], key_arn)
        self.assertEqual(guest["Tagging"], "RetentionClass=guest-temporary")
        self.assertNotIn("Tagging", workspace)

    def test_dynamodb_client_request_token_is_bounded_and_stable(self):
        token = common.dynamodb_client_request_token(
            "brief-approval", ["tenant", "job-approval"]
        )

        self.assertEqual(len(token), 36)
        self.assertEqual(
            token,
            common.dynamodb_client_request_token(
                "brief-approval", ["tenant", "job-approval"]
            ),
        )

    def test_agentcore_client_has_a_deadline_before_lambda_timeout(self):
        with patch.object(common.boto3, "client", return_value=object()) as client:
            common.aws_client("bedrock-agentcore")

        config = client.call_args.kwargs["config"]
        self.assertEqual(config.connect_timeout, 5)
        self.assertEqual(config.read_timeout, 300)
        self.assertEqual(config.retries["max_attempts"], 2)

    def test_s3_client_forces_sigv4_for_kms_presigned_downloads(self):
        with patch.object(common.boto3, "client", return_value=object()) as client:
            common.aws_client("s3")

        config = client.call_args.kwargs["config"]
        self.assertEqual(config.signature_version, "s3v4")

    def test_warm_invocations_reuse_service_clients(self):
        service_client = object()
        with patch.object(
            common.boto3, "client", return_value=service_client
        ) as client:
            first = common.aws_client("dynamodb")
            second = common.aws_client("dynamodb")

        self.assertIs(first, service_client)
        self.assertIs(second, service_client)
        client.assert_called_once()

    def test_production_rejects_http(self):
        event = iam_event()
        event["headers"]["origin"] = "http://pilarprep.app"
        with self.assertRaises(common.AuthorizationError):
            common.assert_secure_request(event)

    def test_workspace_route_requires_cloudfront_origin_verification(self):
        secret = "a" * 48
        event = jwt_event()
        secrets = types.SimpleNamespace(
            get_secret_value=lambda **_kwargs: {"SecretString": secret}
        )

        with (
            patch.object(
                common,
                "API_ORIGIN_VERIFY_SECRET_ARN",
                "arn:aws:secretsmanager:us-east-1:111122223333:secret:test",
            ),
            patch.object(common, "aws_client", return_value=secrets),
        ):
            with self.assertRaises(common.AuthorizationError):
                common.assert_api_origin_verification(event)

            event["headers"]["x-pilarprep-origin-verify"] = "wrong"
            with self.assertRaises(common.AuthorizationError):
                common.assert_api_origin_verification(event)

            event["headers"]["x-pilarprep-origin-verify"] = secret
            common.assert_api_origin_verification(event)

    def test_guest_route_does_not_load_origin_verification_secret(self):
        with (
            patch.object(common, "API_ORIGIN_VERIFY_SECRET_ARN", "configured"),
            patch.object(common, "aws_client", side_effect=AssertionError("unexpected")),
        ):
            common.assert_api_origin_verification(iam_event())

    def test_unsigned_request_is_rejected(self):
        event = iam_event()
        event["requestContext"]["authorizer"] = {}
        with self.assertRaises(common.AuthorizationError):
            common.derive_scope(event, generation_request())

    def test_demo_iam_identity_cannot_select_unassigned_client(self):
        request = {
            **generation_request(),
            "clientId": "another-customer",
            "projectId": "another-customer",
        }
        with self.assertRaises(common.AuthorizationError):
            common.derive_scope(iam_event(body=request), request)

    def test_guest_identity_derives_a_private_tenant(self):
        first = common.derive_scope(iam_event(body=generation_request()), generation_request())
        other_event = iam_event(body=generation_request())
        other_event["requestContext"]["authorizer"]["iam"]["cognitoIdentity"][
            "identityId"
        ] = "us-east-1:another-identity"
        second = common.derive_scope(other_event, generation_request())

        self.assertEqual(first["identityType"], "guest")
        self.assertNotEqual(first["tenantId"], second["tenantId"])
        self.assertNotEqual(first["userId"], second["userId"])

    def test_guest_session_change_does_not_change_tenant_or_owner(self):
        first_request = generation_request()
        second_request = {
            **generation_request(),
            "sessionId": "replacement-session",
        }
        first = common.derive_scope(iam_event(body=first_request), first_request)
        second = common.derive_scope(iam_event(body=second_request), second_request)

        self.assertEqual(first["tenantId"], second["tenantId"])
        self.assertEqual(first["userId"], second["userId"])

    def test_guest_cannot_select_a_different_project_scope(self):
        request = {
            **generation_request(),
            "projectId": "another-project",
        }
        with self.assertRaises(common.AuthorizationError):
            common.derive_scope(iam_event(body=request), request)

    def test_authenticated_user_without_tenant_claim_gets_personal_tenant(self):
        request = generation_request()
        scope = common.derive_scope(jwt_event(body=request), request)

        self.assertEqual(scope["identityType"], "authenticated")
        self.assertEqual(
            scope["tenantId"],
            common.identity_tenant_id("personal", "workspace-user-1"),
        )

    def test_authenticated_claims_restrict_client_and_project(self):
        request = generation_request()
        claims = {
            "sub": "workspace-user-1",
            "custom:tenantId": "acme",
            "custom:clientIds": "bluemesa-payments",
            "custom:projectIds": "bluemesa-payments",
        }
        with self.assertRaises(common.AuthorizationError):
            common.derive_scope(jwt_event(body=request, claims=claims), request)

    def test_generation_accepts_claude_sonnet_46(self):
        request = generation_request()
        request["input"] = {
            **request["input"],
            "modelPreference": "claude-sonnet-4.6",
        }

        validated = common.validate_job_request(request)
        self.assertEqual(validated["input"]["modelPreference"], "claude-sonnet-4.6")

    def test_refinement_requires_a_complete_previous_target(self):
        request = {
            **generation_request(),
            "action": "brief.refine",
            "input": {
                "company": "Apex Mutual",
                "refinementTarget": "technical",
                "modelPreference": "nova-pro",
            },
        }
        with self.assertRaisesRegex(ValueError, "previousBrief"):
            common.validate_job_request(request)

    def test_refinement_rejects_an_unsupported_target(self):
        request = {
            **generation_request(),
            "action": "brief.refine",
            "input": {
                "company": "Apex Mutual",
                "refinementTarget": "packet",
                "previousBrief": {"technical": ["prior"]},
                "modelPreference": "nova-pro",
            },
        }
        with self.assertRaisesRegex(ValueError, "not supported"):
            common.validate_job_request(request)

    def test_handoff_requires_the_approved_packet_version(self):
        request = {
            **generation_request(),
            "action": "handoff.generate",
            "input": {
                "audienceRole": "Solutions Architect",
                "modelPreference": "nova-pro",
            },
        }
        with self.assertRaisesRegex(ValueError, "expectedApprovedPacketVersion"):
            common.validate_job_request(request)

    def test_server_model_router_uses_task_and_trusted_tier(self):
        guest = {**SCOPE, "userTier": "guest"}
        guest_input = {
            "modelPreference": "claude-sonnet-4.6",
            "qualityTier": "premium",
        }
        selected = api._route_model(guest, "brief.generate", guest_input)
        self.assertEqual(selected, "nova-pro")
        self.assertTrue(guest_input["modelRouting"]["serverSelected"])

        catchup_input = {
            "modelPreference": "nova-pro",
            "qualityTier": "standard",
        }
        selected = api._route_model(
            {**SCOPE, "userTier": "standard"},
            "catchup.generate",
            catchup_input,
        )
        self.assertEqual(selected, "nova-micro")

        premium_input = {
            "modelPreference": "claude-sonnet-4.6",
            "qualityTier": "premium",
        }
        selected = api._route_model(
            {**SCOPE, "userTier": "premium"},
            "brief.refine",
            premium_input,
        )
        self.assertEqual(selected, "claude-sonnet-4.6")

    def test_evidence_ingestion_contract_is_bounded(self):
        request = {
            **generation_request(),
            "action": "evidence.ingest",
            "input": {
                "documentId": "approved-architecture",
                "fileName": "current-state.md",
                "sourceTitle": "Customer-approved current state",
                "documentType": "architecture",
                "content": "The approved architecture evidence is customer supplied.",
            },
        }
        validated = common.validate_job_request(request)
        self.assertEqual(validated["input"]["documentId"], "approved-architecture")
        self.assertEqual(validated["input"]["qualityTier"], "standard")

    def test_evidence_ingestion_accepts_one_bounded_source_mode(self):
        url_request = {
            **generation_request(),
            "action": "evidence.ingest",
            "input": {
                "documentId": "approved-values-page",
                "fileName": "values.html",
                "sourceTitle": "Customer-approved values page",
                "documentType": "company-profile",
                "sourceUrl": "https://example.com/company/values",
                "sourceType": "approved-public-url",
            },
        }
        upload_request = {
            **generation_request(),
            "action": "evidence.ingest",
            "input": {
                "documentId": "approved-requirements",
                "fileName": "requirements.pdf",
                "sourceTitle": "Customer-approved requirements",
                "documentType": "requirements",
                "contentBase64": "JVBERi0xLjQK" * 3,
                "contentType": "application/pdf",
                "sourceType": "uploaded-customer-document",
            },
        }

        validated_url = common.validate_job_request(url_request)
        validated_upload = common.validate_job_request(upload_request)

        self.assertEqual(
            validated_url["input"]["sourceUrl"],
            "https://example.com/company/values",
        )
        self.assertEqual(
            validated_upload["input"]["contentType"],
            "application/pdf",
        )
        self.assertFalse(validated_url["input"]["content"])

    def test_evidence_ingestion_rejects_missing_or_multiple_source_modes(self):
        base_input = {
            "documentId": "ambiguous-source",
            "fileName": "source.md",
            "sourceTitle": "Ambiguous source",
            "documentType": "requirements",
        }
        missing = {
            **generation_request(),
            "action": "evidence.ingest",
            "input": base_input,
        }
        multiple = {
            **generation_request(),
            "action": "evidence.ingest",
            "input": {
                **base_input,
                "content": "Customer-approved source content for the packet.",
                "sourceUrl": "https://example.com/source",
            },
        }

        with self.assertRaisesRegex(ValueError, "exactly one"):
            common.validate_job_request(missing)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            common.validate_job_request(multiple)


    def test_guest_cannot_create_an_evidence_job(self):
        request = {
            **generation_request(),
            "action": "evidence.ingest",
            "input": {
                "documentId": "private-architecture",
                "fileName": "architecture.md",
                "sourceTitle": "Private customer architecture",
                "documentType": "architecture",
                "content": (
                    "This customer-approved architecture must remain private "
                    "to the authenticated workspace."
                ),
            },
        }
        with (
            patch.object(api, "PROJECT_TABLE", "state-table"),
            patch.object(api, "ARTIFACT_BUCKET", "artifact-bucket"),
            patch.object(api, "JOB_QUEUE_URL", "https://sqs.example/jobs"),
            patch.object(
                api,
                "aws_client",
                side_effect=AssertionError("authorization must happen before AWS"),
            ),
            self.assertRaisesRegex(
                common.AuthorizationError,
                "verified workspace",
            ),
        ):
            api._start_job(iam_event(body=request))


class EvidenceStorageTests(unittest.TestCase):
    def test_private_rag_retrieval_uses_exact_scope_filters(self):
        calls = []

        class FakeRetrieval:
            def retrieve(self, **kwargs):
                calls.append(kwargs)
                return {
                    "retrievalResults": [
                        {
                            "score": 0.91,
                            "content": {"text": "Approved AWS current-state evidence."},
                            "metadata": {
                                "tenantId": "tenant-acme",
                                "clientId": "apex-mutual",
                                "projectId": "apex-mutual",
                                "approved": True,
                                "status": "approved",
                                "visibility": "tenant-private",
                                "documentId": "current-state",
                                "sourceTitle": "Approved current state",
                                "documentType": "architecture",
                            },
                        }
                    ]
                }

        scope = {
            **SCOPE,
            "tenantId": "tenant-acme",
            "identityType": "authenticated",
        }
        with patch.object(evidence, "KNOWLEDGE_BASE_ID", "kb-123"):
            sources, metadata = evidence.retrieve_for_brief(
                scope,
                "current architecture",
                retrieval_client=FakeRetrieval(),
            )

        filters = calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]["andAll"]
        self.assertIn(
            {"equals": {"key": "tenantId", "value": "tenant-acme"}},
            filters,
        )
        self.assertIn(
            {"equals": {"key": "clientId", "value": "apex-mutual"}},
            filters,
        )
        self.assertEqual(metadata["mode"], "tenant-private")
        self.assertEqual(sources[0]["accessScope"], "tenant-private")

    def test_public_demo_rag_is_limited_to_blue_mesa(self):
        calls = []

        class FakeRetrieval:
            def retrieve(self, **kwargs):
                calls.append(kwargs)
                return {
                    "retrievalResults": [
                        {
                            "score": 0.8,
                            "content": {"text": "Synthetic Blue Mesa evidence."},
                            "metadata": {
                                "scenarioId": "blue-mesa-payments",
                                "approved": True,
                                "visibility": "public-demo",
                                "documentId": "demo-overview",
                                "sourceTitle": "Blue Mesa overview",
                            },
                        }
                    ]
                }

        with patch.object(evidence, "KNOWLEDGE_BASE_ID", "kb-123"):
            sources, metadata = evidence.retrieve_for_brief(
                BLUE_SCOPE,
                "payment platform",
                retrieval_client=FakeRetrieval(),
            )
            custom_sources, custom_metadata = evidence.retrieve_for_brief(
                SCOPE,
                "private custom scenario",
                retrieval_client=types.SimpleNamespace(
                    retrieve=lambda **_kwargs: (_ for _ in ()).throw(
                        AssertionError("guest custom scenarios cannot query RAG")
                    )
                ),
            )

        filters = calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]["andAll"]
        self.assertIn(
            {"equals": {"key": "scenarioId", "value": "blue-mesa-payments"}},
            filters,
        )
        self.assertEqual(metadata["mode"], "public-demo")
        self.assertEqual(sources[0]["accessScope"], "public-demo")
        self.assertEqual(custom_sources, [])
        self.assertEqual(custom_metadata["mode"], "guest-no-private-rag")

    def test_rag_rejects_any_result_outside_the_authorized_scope(self):
        class FakeRetrieval:
            def retrieve(self, **_kwargs):
                return {
                    "retrievalResults": [
                        {
                            "content": {"text": "Evidence from another customer."},
                            "metadata": {
                                "tenantId": "tenant-other",
                                "clientId": "other-client",
                                "projectId": "other-project",
                                "approved": True,
                                "status": "approved",
                                "visibility": "tenant-private",
                            },
                        }
                    ]
                }

        scope = {
            **SCOPE,
            "tenantId": "tenant-acme",
            "identityType": "authenticated",
        }
        with (
            patch.object(evidence, "KNOWLEDGE_BASE_ID", "kb-123"),
            patch.object(evidence, "metric"),
            self.assertRaises(evidence.EvidenceScopeError),
        ):
            evidence.retrieve_for_brief(
                scope,
                "current architecture",
                retrieval_client=FakeRetrieval(),
            )

    def test_approved_url_validation_blocks_private_network_targets(self):
        with patch.object(
            evidence.socket,
            "getaddrinfo",
            return_value=[
                (evidence.socket.AF_INET, evidence.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
            ],
        ):
            with self.assertRaisesRegex(ValueError, "public address"):
                evidence._validate_public_https_url("https://localhost/source")

        with self.assertRaisesRegex(ValueError, "HTTPS"):
            evidence._validate_public_https_url("http://example.com/source")

    def test_tenant_evidence_writes_scoped_document_and_metadata(self):
        calls = {"s3": [], "ddb": [], "ingestion": []}

        class FakeS3:
            def put_object(self, **kwargs):
                calls["s3"].append(kwargs)

        class FakeDynamo:
            def get_item(self, **_kwargs):
                return {}

            def put_item(self, **kwargs):
                calls["ddb"].append(("put", kwargs))

            def update_item(self, **kwargs):
                calls["ddb"].append(("update", kwargs))

        class FakeBedrock:
            def start_ingestion_job(self, **kwargs):
                calls["ingestion"].append(kwargs)
                return {
                    "ingestionJob": {
                        "ingestionJobId": "ingestion-123",
                        "status": "STARTING",
                    }
                }

        clients = {
            "s3": FakeS3(),
            "dynamodb": FakeDynamo(),
            "bedrock-agent": FakeBedrock(),
        }
        scope = {
            **SCOPE,
            "tenantId": "tenant-acme",
            "userId": "user-owner",
            "identityType": "authenticated",
        }
        with (
            patch.object(evidence, "EVIDENCE_BUCKET", "private-evidence"),
            patch.object(evidence, "PROJECT_TABLE", "state-table"),
            patch.object(evidence, "KNOWLEDGE_BASE_ID", "kb-123"),
            patch.object(evidence, "KNOWLEDGE_BASE_DATA_SOURCE_ID", "ds-123"),
            patch.object(evidence, "aws_client", side_effect=lambda name: clients[name]),
        ):
            result = evidence.ingest_document(
                scope,
                {
                    "documentId": "approved-architecture",
                    "fileName": "Current State.md",
                    "sourceTitle": "Customer-approved current state",
                    "documentType": "architecture",
                    "content": "The customer-approved architecture is already on AWS.",
                },
                source_job_id="job-evidence-1",
            )

        self.assertEqual(result["status"], "INGESTING")
        self.assertEqual(len(calls["s3"]), 2)
        prefix = (
            "evidence/tenants/tenant-acme/clients/apex-mutual/"
            "projects/apex-mutual/documents/approved-architecture/"
        )
        self.assertTrue(calls["s3"][0]["Key"].startswith(prefix))
        self.assertTrue(calls["s3"][1]["Key"].endswith(".metadata.json"))
        metadata = json.loads(calls["s3"][1]["Body"])
        attributes = metadata["metadataAttributes"]
        self.assertEqual(attributes["tenantId"], "tenant-acme")
        self.assertEqual(attributes["clientId"], "apex-mutual")
        self.assertEqual(attributes["projectId"], "apex-mutual")
        self.assertEqual(attributes["visibility"], "tenant-private")
        self.assertIs(attributes["approved"], True)
        self.assertTrue(attributes["sourceId"].startswith("src-doc-"))
        self.assertEqual(attributes["sourceType"], "architecture")
        self.assertEqual(attributes["accessScope"], "tenant-private")
        self.assertEqual(attributes["lifecycleStatus"], "active")
        self.assertEqual(len(calls["ingestion"]), 1)

    def test_evidence_paths_change_across_tenants(self):
        first, _ = evidence._document_keys(
            {**SCOPE, "tenantId": "tenant-one"},
            "architecture",
            "current.md",
        )
        second, _ = evidence._document_keys(
            {**SCOPE, "tenantId": "tenant-two"},
            "architecture",
            "current.md",
        )
        self.assertNotEqual(first, second)
        self.assertIn("/tenant-one/", first)
        self.assertIn("/tenant-two/", second)


    def test_delete_waits_for_a_knowledge_base_sync_before_completion(self):
        record = {
            "documentId": "approved-architecture",
            "objectKey": "evidence/tenants/tenant-a/document.md",
            "metadataKey": "evidence/tenants/tenant-a/document.md.metadata.json",
            "status": "AVAILABLE",
            "version": 1,
        }

        class FakeS3:
            def __init__(self):
                self.calls = []

            def delete_objects(self, **kwargs):
                self.calls.append(kwargs)

        s3 = FakeS3()
        with (
            patch.object(evidence, "EVIDENCE_BUCKET", "private-evidence"),
            patch.object(evidence, "_record", return_value=record),
            patch.object(evidence, "_update_status") as update,
            patch.object(
                evidence,
                "_start_sync",
                return_value={
                    "ingestionStatus": "WAITING_FOR_SYNC",
                    "ingestionJobId": "",
                },
            ),
            patch.object(evidence, "aws_client", return_value=s3),
        ):
            result = evidence.delete_document(
                {**SCOPE, "identityType": "authenticated"},
                {"documentId": "approved-architecture"},
            )

        self.assertEqual(result["status"], "DELETION_PENDING")
        self.assertEqual(len(s3.calls), 1)
        self.assertEqual(update.call_count, 2)
        self.assertNotEqual(result["status"], "DELETED")

    def test_completed_deletion_sync_marks_the_record_deleted(self):
        with (
            patch.object(evidence, "KNOWLEDGE_BASE_ID", "kb-123"),
            patch.object(evidence, "KNOWLEDGE_BASE_DATA_SOURCE_ID", "ds-123"),
            patch.object(evidence, "_update_status") as update,
            patch.object(
                evidence,
                "aws_client",
                return_value=types.SimpleNamespace(
                    get_ingestion_job=lambda **_kwargs: {
                        "ingestionJob": {"status": "COMPLETE"}
                    }
                ),
            ),
        ):
            result = evidence._refresh_ingestion_status(
                {**SCOPE, "identityType": "authenticated"},
                {
                    "documentId": "approved-architecture",
                    "status": "DELETING",
                    "ingestionStatus": "IN_PROGRESS",
                    "ingestionJobId": "ingestion-delete-1",
                },
            )

        self.assertEqual(result["status"], "DELETED")
        self.assertEqual(update.call_args.kwargs["status"], "DELETED")


class InfrastructureSecurityTests(unittest.TestCase):
    def test_worker_can_retrieve_only_from_the_deployed_knowledge_base(self):
        pipeline = (
            BACKEND_ROOT.parent / "infrastructure" / "jobs-pipeline.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("bedrock:Retrieve", pipeline)
        self.assertIn("BlueMesaKnowledgeBase.KnowledgeBaseArn", pipeline)

    def test_frontend_and_meeting_buckets_are_private_and_tls_only(self):
        frontend = (
            BACKEND_ROOT.parent / "infrastructure" / "frontend.yaml"
        ).read_text(encoding="utf-8")
        pipeline = (
            BACKEND_ROOT.parent / "infrastructure" / "jobs-pipeline.yaml"
        ).read_text(encoding="utf-8")

        for template in (frontend, pipeline):
            self.assertIn("BlockPublicAcls: true", template)
            self.assertIn("BlockPublicPolicy: true", template)
            self.assertIn("IgnorePublicAcls: true", template)
            self.assertIn("RestrictPublicBuckets: true", template)
            self.assertIn("aws:SecureTransport", template)
            self.assertNotIn("WebsiteConfiguration:", template)

        self.assertIn("AWS::CloudFront::OriginAccessControl", frontend)
        self.assertIn("SigningBehavior: always", frontend)
        self.assertIn("ViewerProtocolPolicy: redirect-to-https", frontend)
        self.assertIn("FrontendResponseHeaders:", frontend)
        self.assertIn('headers["content-security-policy"]', frontend)
        self.assertIn('headers["strict-transport-security"]', frontend)
        self.assertIn("CognitoLoginDomainName:", frontend)
        self.assertIn("https://${CognitoLoginDomainName}", frontend)
        self.assertNotIn("67f7725c-6f97-4210-82d7-5512b31e9d03", frontend)
        self.assertIn("AWS::WAFv2::WebACL", frontend)
        self.assertIn("RateBasedStatement:", frontend)
        self.assertIn("Name: RateLimitPublicDemo", frontend)
        self.assertIn("MeetingEvidenceBucketPolicy:", pipeline)
        self.assertIn(
            "transcripts/private/*/bluemesa-payments/bluemesa-payments/"
            "blue-mesa-discovery/latest.json",
            pipeline,
        )
        agent_contracts = (
            BACKEND_ROOT / "agentcore" / "common" / "contracts.py"
        ).read_text(encoding="utf-8")
        agent_security = (
            BACKEND_ROOT / "agentcore" / "common" / "security.py"
        ).read_text(encoding="utf-8")
        self.assertIn("from .identifiers import", agent_contracts)
        self.assertIn("from .security import", agent_contracts)
        self.assertIn("from .identifiers import", agent_security)

    def test_every_blue_mesa_source_has_consistent_approved_metadata(self):
        corpus = BACKEND_ROOT.parent / "data" / "blue-mesa-evidence"
        documents = sorted(
            list(corpus.glob("*.md")) +
            list(corpus.glob("*.txt"))
        )
        self.assertEqual(len(documents), 18)

        for document in documents:
            self.assertLessEqual(
                document.stat().st_size,
                1600,
                f"Evidence document is too large for S3 Vectors metadata: {document.name}",
            )
            sidecar = Path(str(document) + ".metadata.json")
            self.assertTrue(sidecar.exists(), f"Missing metadata for {document.name}")
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))[
                "metadataAttributes"
            ]
            self.assertEqual(metadata["scenarioId"], "blue-mesa-payments")
            self.assertIs(metadata["approved"], True)
            self.assertEqual(metadata["visibility"], "public-demo")
            self.assertIsInstance(metadata["version"], int)
            self.assertGreaterEqual(metadata["version"], 1)
            self.assertTrue(metadata["documentType"])
            self.assertTrue(metadata["sourceTitle"])

    def test_workspace_routes_use_jwt_and_guest_policy_is_narrow(self):
        pipeline = (
            BACKEND_ROOT.parent / "infrastructure" / "jobs-pipeline.yaml"
        ).read_text(encoding="utf-8")
        frontend = (
            BACKEND_ROOT.parent / "infrastructure" / "frontend.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("Type: AWS::Cognito::UserPool", pipeline)
        self.assertIn("AutoVerifiedAttributes: [email]", pipeline)
        self.assertIn("WorkspaceJwtAuthorizer:", pipeline)
        self.assertIn("Path: /workspace/jobs", pipeline)
        self.assertIn("Authorizer: WorkspaceJwtAuthorizer", pipeline)
        self.assertNotIn(
            '${JobsApi}/*/*/*"',
            pipeline,
        )
        self.assertIn('${JobsApi}/*/POST/jobs"', pipeline)
        self.assertIn("GUEST_HOURLY_AI_LIMIT", pipeline)
        self.assertNotIn("DEMO_SESSION_AI_LIMIT", pipeline)
        self.assertIn("Path: /workspace/operations/dlq/replay", pipeline)
        self.assertIn("GroupName: PilarPrepOperators", pipeline)
        self.assertIn("Sid: OperateOnlyPilarPrepDeadLetterQueue", pipeline)
        self.assertIn("MAX_REPLAY_COUNT", pipeline)
        self.assertIn("Type: AWS::SNS::Topic", pipeline)
        self.assertIn("AlarmActions: [!Ref OperationsAlertTopic]", pipeline)
        self.assertIn("DATA_KMS_KEY_ARN", pipeline)
        self.assertIn("s3:PutObjectTagging", pipeline)
        self.assertIn("ApiOriginVerificationSecret:", pipeline)
        self.assertIn("API_ORIGIN_VERIFY_SECRET_ARN", pipeline)
        self.assertIn("Path: /workspace/operations/dlq/replay", pipeline)

        self.assertIn("JobsApiOriginDomainName:", frontend)
        self.assertIn("X-PilarPrep-Origin-Verify", frontend)
        self.assertIn("PathPattern: /api/*", frontend)
        self.assertIn("4135ea2d-6df8-44a3-9df3-4b5a84be39ad", frontend)
        self.assertNotIn("413f160d-6ff1-4ffb-9b7d-56a5dbac60a4", frontend)
        self.assertIn("WorkspaceApiUrl:", frontend)

        backend = (
            BACKEND_ROOT.parent / "infrastructure" / "bedrock.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("Type: AWS::KMS::Key", backend)
        self.assertIn("EnableKeyRotation: true", backend)
        self.assertIn("DeletionProtectionEnabled: true", backend)
        self.assertIn("DataEncryptionKeyArn:", backend)
        self.assertIn("indexing.s3vectors.amazonaws.com", backend)
        self.assertIn("AllowS3VectorsIndexMaintenance", backend)
        self.assertIn("kms:EncryptionContextKeys", backend)
        agentcore = (
            BACKEND_ROOT.parent / "infrastructure" / "agentcore.yaml"
        ).read_text(encoding="utf-8")
        deploy_script = (
            BACKEND_ROOT.parent / "scripts" / "deploy-agentcore.ps1"
        ).read_text(encoding="utf-8")
        jobs_deploy_script = (
            BACKEND_ROOT.parent / "scripts" / "deploy-jobs-pipeline.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("DeletionPolicy: Retain", backend)
        self.assertIn("RetentionClass", backend)
        self.assertIn("Value: guest-temporary", backend)
        self.assertIn("DataKmsKeyArn:", agentcore)
        self.assertIn("DATA_KMS_KEY_ARN", agentcore)
        self.assertIn("s3:PutObjectTagging", agentcore)
        self.assertIn(r"runtime\evidence.py", deploy_script)
        self.assertIn('"DataKmsKeyArn=$DataKmsKeyArn"', deploy_script)
        self.assertIn("MetricName: CrossScopeAuthorizationAttempts", pipeline)
        self.assertIn("MetricName: RagRetrievalFailures", pipeline)
        self.assertIn("MetricName: BriefEstimatedCostUsd", pipeline)
        self.assertIn("--template-url $templateUrl", jobs_deploy_script)
        self.assertIn("KnowledgeBaseGeneration:", pipeline)
        self.assertIn(
            'PilarPrepAuthorizedEvidence-${KnowledgeBaseGeneration}',
            pipeline,
        )
        self.assertIn(
            '"KnowledgeBaseGeneration=$KnowledgeBaseGeneration"',
            jobs_deploy_script,
        )
        self.assertIn("--s3-bucket $PackagingBucket", jobs_deploy_script)
        self.assertNotIn(
            '--template-body "file://$packagedPath"',
            jobs_deploy_script,
        )




class JobsApiTests(unittest.TestCase):
    def test_post_job_queues_only_scope_and_s3_pointer(self):
        calls = {"s3": [], "transactions": [], "messages": []}

        class FakeS3:
            def put_object(self, **kwargs):
                calls["s3"].append(kwargs)
                return {}

        class FakeDynamoDB:
            def get_item(self, **_kwargs):
                return {}

            def update_item(self, **kwargs):
                calls.setdefault("quota", []).append(kwargs)
                return {}

            def transact_write_items(self, **kwargs):
                calls["transactions"].append(kwargs)
                return {}

        class FakeSqs:
            def send_message(self, **kwargs):
                calls["messages"].append(kwargs)
                return {"MessageId": "message-1"}

        clients = {
            "s3": FakeS3(),
            "dynamodb": FakeDynamoDB(),
            "sqs": FakeSqs(),
        }
        with (
            patch.object(api, "PROJECT_TABLE", "project-state"),
            patch.object(api, "ARTIFACT_BUCKET", "artifact-bucket"),
            patch.object(api, "JOB_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/q"),
            patch.object(api, "aws_client", side_effect=lambda name: clients[name]),
            patch.object(api, "uuid4", side_effect=["job-0001", "input-0001"]),
            patch.object(api, "metric"),
        ):
            response = api.handler(iam_event(body=generation_request()), None)

        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(len(calls["transactions"]), 2)
        quota_transaction = calls["transactions"][0]["TransactItems"]
        job_transaction = calls["transactions"][1]["TransactItems"]
        self.assertEqual(len(quota_transaction), 2)
        self.assertEqual(len(job_transaction), 2)
        queued = json.loads(calls["messages"][0]["MessageBody"])
        self.assertEqual(queued["action"], "brief.generate")
        self.assertEqual(queued["inputKey"], calls["s3"][0]["Key"])
        self.assertNotIn("input", queued)
        self.assertNotIn("company", queued)
        self.assertNotIn("context", calls["messages"][0]["MessageBody"])
        self.assertIn("requestCount", quota_transaction[0]["Update"]["UpdateExpression"])

    def test_identity_quota_returns_429_before_customer_context_is_written(self):
        class FakeDynamoDB:
            def get_item(self, **_kwargs):
                return {}

            def transact_write_items(self, **_kwargs):
                raise ClientError(
                    {
                        "Error": {
                            "Code": "TransactionCanceledException",
                            "Message": "quota reached",
                        },
                        "CancellationReasons": [
                            {"Code": "ConditionalCheckFailed"}, {"Code": "None"},
                        ],
                    },
                    "TransactWriteItems",
                )

        with (
            patch.object(api, "PROJECT_TABLE", "project-state"),
            patch.object(api, "ARTIFACT_BUCKET", "artifact-bucket"),
            patch.object(
                api,
                "JOB_QUEUE_URL",
                "https://sqs.us-east-1.amazonaws.com/q",
            ),
            patch.object(api, "aws_client", return_value=FakeDynamoDB()),
            patch.object(api, "now_epoch", return_value=3_601),
            patch.object(api, "metric"),
        ):
            response = api.handler(iam_event(body=generation_request()), None)

        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 429)
        self.assertIn("AI usage limit", body["error"])
        self.assertIn("Demo hourly", body["error"])
        self.assertEqual(body["retryAfterSeconds"], 3599)
        self.assertEqual(response["headers"]["retry-after"], "3599")
        self.assertEqual(body["errorCode"], "AI_USAGE_LIMIT")
        self.assertEqual(body["quota"]["kind"], "guest_hourly")
        self.assertEqual(body["quota"]["resetsAt"], "1970-01-01T02:00:00+00:00")

    def test_quota_reports_the_actual_daily_or_model_window(self):
        cases = [
            (SCOPE, "nova-pro", ["None", "ConditionalCheckFailed"], "guest_daily", 200),
            (SCOPE, "nova-pro", ["ConditionalCheckFailed", "ConditionalCheckFailed"], "guest_daily", 200),
            (AUTH_BLUE_SCOPE, "nova-pro", ["ConditionalCheckFailed", "None"], "tenant_daily", 500),
            (AUTH_BLUE_SCOPE, "nova-pro", ["None", "ConditionalCheckFailed"], "user_daily", 100),
            (AUTH_BLUE_SCOPE, "claude-sonnet-4.6", ["None", "None", "ConditionalCheckFailed"], "claude_daily", 5),
        ]
        for scope, model, codes, kind, limit in cases:
            with self.subTest(kind=kind, codes=codes):
                error = ClientError({
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [{"Code": code} for code in codes],
                })
                with (
                    patch.object(api, "aws_client") as client,
                    patch.object(api, "now_epoch", return_value=3601),
                    patch.object(api, "GUEST_DAILY_AI_LIMIT", 200),
                    patch.object(api, "AUTH_USER_DAILY_AI_LIMIT", 100),
                    patch.object(api, "AUTH_TENANT_DAILY_AI_LIMIT", 500),
                    patch.object(api, "CLAUDE_DAILY_AI_LIMIT", 5),
                    patch.object(api, "metric"),
                ):
                    client.return_value.transact_write_items.side_effect = error
                    with self.assertRaises(api.UsageQuotaExceeded) as raised:
                        api._consume_usage_quota(scope, "handoff.generate", model)
                self.assertEqual(raised.exception.quota["kind"], kind)
                self.assertEqual(raised.exception.quota["limit"], limit)
                self.assertEqual(raised.exception.retry_after_seconds, 82799)
                client.return_value.get_item.assert_not_called()

    def test_transaction_conflict_is_not_reported_as_exhausted_usage(self):
        error = ClientError({
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "None"}],
        })
        with patch.object(api, "aws_client") as client, patch.object(api, "metric") as metric:
            client.return_value.transact_write_items.side_effect = error
            with self.assertRaises(ClientError) as raised:
                api._consume_usage_quota(SCOPE, "handoff.generate", "nova-pro")
        self.assertIs(raised.exception, error)
        client.return_value.get_item.assert_not_called()
        metric.assert_not_called()

    def test_missing_cancellation_details_require_confirmed_exhaustion(self):
        for count in (0, 12, 20):
            with self.subTest(count=count):
                error = ClientError({"Error": {"Code": "TransactionCanceledException"}})
                with (
                    patch.object(api, "aws_client") as client,
                    patch.object(api, "now_epoch", return_value=3601),
                    patch.object(api, "GUEST_HOURLY_AI_LIMIT", 20),
                    patch.object(api, "GUEST_DAILY_AI_LIMIT", 200),
                    patch.object(api, "metric"),
                ):
                    client.return_value.transact_write_items.side_effect = error
                    client.return_value.get_item.return_value = {"Item": {"requestCount": {"N": str(count)}}}
                    expected = api.UsageQuotaExceeded if count == 20 else ClientError
                    with self.assertRaises(expected) as raised:
                        api._consume_usage_quota(SCOPE, "handoff.generate", "nova-pro")
                self.assertEqual(client.return_value.get_item.call_count, 2)
                for call in client.return_value.get_item.call_args_list:
                    self.assertTrue(call.kwargs["ConsistentRead"])
                    self.assertEqual(call.kwargs["ProjectionExpression"], "requestCount")
                if count == 20:
                    self.assertEqual(raised.exception.quota["kind"], "guest_hourly")
                    self.assertEqual(raised.exception.retry_after_seconds, 3599)
                else:
                    self.assertIs(raised.exception, error)

    def test_demo_limit_defaults_and_transactions_use_twenty_and_two_hundred(self):
        template = (BACKEND_ROOT.parent / "infrastructure/jobs-pipeline.yaml").read_text(encoding="utf-8")
        deploy = (BACKEND_ROOT.parent / "scripts/deploy-jobs-pipeline.ps1").read_text(encoding="utf-8")
        self.assertIn("GuestHourlyAiLimit:\n    Type: Number\n    Default: 20", template)
        self.assertIn("GuestDailyAiLimit:\n    Type: Number\n    Default: 200", template)
        self.assertIn("$GuestHourlyAiLimit = 20", deploy)
        self.assertIn("$GuestDailyAiLimit = 200", deploy)
        with (
            patch.object(api, "aws_client") as client,
            patch.object(api, "GUEST_HOURLY_AI_LIMIT", 20),
            patch.object(api, "GUEST_DAILY_AI_LIMIT", 200),
        ):
            api._consume_usage_quota(SCOPE, "handoff.generate", "nova-pro")
        updates = client.return_value.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual([item["Update"]["ExpressionAttributeValues"][":limit"]["N"] for item in updates], ["20", "200"])
        self.assertTrue(all("requestCount < :limit" in item["Update"]["ConditionExpression"] for item in updates))

    def test_control_actions_do_not_consume_the_ai_quota(self):
        with patch.object(api, "aws_client") as client:
            api._consume_usage_quota(SCOPE, "brief.approve", "nova-pro")
        client.assert_not_called()

    def test_changing_session_id_does_not_change_quota_keys(self):
        writes = []

        class FakeDynamoDB:
            def transact_write_items(self, **kwargs):
                writes.append(kwargs["TransactItems"])

        with (
            patch.object(api, "aws_client", return_value=FakeDynamoDB()),
            patch.object(api, "now_epoch", return_value=86_500),
        ):
            api._consume_usage_quota(SCOPE, "brief.generate", "nova-pro")
            api._consume_usage_quota(
                {**SCOPE, "sessionId": "different-session"},
                "brief.generate",
                "nova-pro",
            )

        self.assertEqual(writes[0], writes[1])

    def test_dlq_replay_requires_operator_group(self):
        event = jwt_event(
            path="/workspace/operations/dlq/replay",
            body={"reason": "Root cause has been corrected.", "maxMessages": 1},
        )
        with patch.object(api, "aws_client") as client:
            result = api.handler(event, None)
        self.assertEqual(result["statusCode"], 403)
        client.assert_not_called()

    def test_operator_replays_one_valid_failed_job_and_deletes_after_dispatch(self):
        pointer = {
            "action": "brief.generate",
            "jobId": "job-0001",
            **{key: SCOPE[key] for key in (
                "tenantId", "clientId", "projectId", "userId", "sessionId"
            )},
            "traceId": "trace-0001",
            "inputVersion": "input-0001",
            "inputKey": (
                f"jobs/{SCOPE['tenantId']}/{SCOPE['clientId']}/"
                f"{SCOPE['projectId']}/job-0001/input.json"
            ),
        }
        calls = {"transactions": [], "updates": [], "sent": [], "deleted": []}

        class FakeDynamoDB:
            def get_item(self, **_kwargs):
                return {
                    "Item": {
                        "ownerId": {"S": SCOPE["userId"]},
                        "clientId": {"S": SCOPE["clientId"]},
                        "projectScopeId": {"S": SCOPE["projectId"]},
                        "action": {"S": "brief.generate"},
                        "inputKey": {"S": pointer["inputKey"]},
                        "inputVersion": {"S": "input-0001"},
                        "status": {"S": "failed"},
                        "retryCount": {"N": "3"},
                    }
                }

            def transact_write_items(self, **kwargs):
                calls["transactions"].append(kwargs)

            def update_item(self, **kwargs):
                calls["updates"].append(kwargs)

        class FakeSqs:
            def receive_message(self, **_kwargs):
                return {
                    "Messages": [{
                        "Body": json.dumps(pointer),
                        "MessageId": "dlq-message-1",
                        "ReceiptHandle": "receipt-1",
                    }]
                }

            def send_message(self, **kwargs):
                calls["sent"].append(kwargs)

            def delete_message(self, **kwargs):
                calls["deleted"].append(kwargs)

        clients = {"dynamodb": FakeDynamoDB(), "sqs": FakeSqs()}
        event = jwt_event(
            path="/workspace/operations/dlq/replay",
            body={"reason": "The model permission issue was corrected.", "maxMessages": 1},
            claims={
                "sub": "operator-user",
                "cognito:groups": ["PilarPrepOperators"],
            },
        )
        with (
            patch.object(api, "PROJECT_TABLE", "project-state"),
            patch.object(api, "JOB_QUEUE_URL", "https://sqs.example/source"),
            patch.object(api, "JOB_DLQ_URL", "https://sqs.example/dlq"),
            patch.object(api, "MAX_REPLAY_COUNT", 1),
            patch.object(api, "MAX_TOTAL_ATTEMPTS", 6),
            patch.object(api, "aws_client", side_effect=lambda name: clients[name]),
            patch.object(api, "metric"),
        ):
            result = api.handler(event, None)

        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["results"][0]["status"], "replayed")
        self.assertFalse(body["automaticReplay"])
        self.assertEqual(len(calls["transactions"]), 1)
        replay = calls["transactions"][0]["TransactItems"]
        self.assertEqual(replay[1]["Put"]["Item"]["entityType"]["S"], "DLQ_REPLAY_AUDIT")
        self.assertEqual(len(calls["sent"]), 1)
        self.assertEqual(len(calls["deleted"]), 1)
        self.assertGreaterEqual(len(calls["updates"]), 2)

    def test_malformed_dlq_message_is_quarantined_not_deleted(self):
        calls = {"updates": [], "visibility": [], "deleted": []}

        class FakeDynamoDB:
            def update_item(self, **kwargs):
                calls["updates"].append(kwargs)

        class FakeSqs:
            def receive_message(self, **_kwargs):
                return {
                    "Messages": [{
                        "Body": "{not-json",
                        "MessageId": "poison-message",
                        "ReceiptHandle": "poison-receipt",
                    }]
                }

            def change_message_visibility(self, **kwargs):
                calls["visibility"].append(kwargs)

            def delete_message(self, **kwargs):
                calls["deleted"].append(kwargs)

        clients = {"dynamodb": FakeDynamoDB(), "sqs": FakeSqs()}
        event = jwt_event(
            path="/workspace/operations/dlq/replay",
            body={"reason": "Operator reviewed the failed queue message."},
            claims={
                "sub": "operator-user",
                "cognito:groups": '["PilarPrepOperators"]',
            },
        )
        with (
            patch.object(api, "PROJECT_TABLE", "project-state"),
            patch.object(api, "JOB_QUEUE_URL", "https://sqs.example/source"),
            patch.object(api, "JOB_DLQ_URL", "https://sqs.example/dlq"),
            patch.object(api, "aws_client", side_effect=lambda name: clients[name]),
            patch.object(api, "metric"),
        ):
            result = api.handler(event, None)

        body = json.loads(result["body"])
        self.assertEqual(body["results"][0]["status"], "quarantined")
        self.assertEqual(len(calls["updates"]), 1)
        self.assertIn(
            "DLQ_QUARANTINE",
            str(calls["updates"][0]["ExpressionAttributeValues"]),
        )
        self.assertEqual(len(calls["visibility"]), 1)
        self.assertEqual(calls["deleted"], [])

    def test_idempotent_post_does_not_write_or_enqueue(self):
        class FakeDynamoDB:
            def get_item(self, **_kwargs):
                return {"Item": {"jobId": {"S": "job-existing"}}}

        with (
            patch.object(api, "PROJECT_TABLE", "project-state"),
            patch.object(api, "ARTIFACT_BUCKET", "artifact-bucket"),
            patch.object(api, "JOB_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/q"),
            patch.object(api, "aws_client", return_value=FakeDynamoDB()) as client,
            patch.object(api, "metric"),
        ):
            response = api.handler(iam_event(body=generation_request()), None)

        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(body["jobId"], "job-existing")
        self.assertTrue(body["idempotent"])
        client.assert_called_once_with("dynamodb")

    def test_job_polling_does_not_leak_another_owners_job(self):
        class FakeDynamoDB:
            def get_item(self, **_kwargs):
                return {
                    "Item": {
                        "ownerId": {"S": "user-someone-else"},
                        "sessionId": {"S": SCOPE["sessionId"]},
                        "clientId": {"S": SCOPE["clientId"]},
                        "projectScopeId": {"S": SCOPE["projectId"]},
                        "status": {"S": "complete"},
                    }
                }

        query = {
            "clientId": SCOPE["clientId"],
            "projectId": SCOPE["projectId"],
            "sessionId": SCOPE["sessionId"],
        }
        with (
            patch.object(api, "PROJECT_TABLE", "project-state"),
            patch.object(api, "aws_client", return_value=FakeDynamoDB()),
        ):
            response = api.handler(
                iam_event("GET", "/jobs/job-0001", query=query), None
            )
        self.assertEqual(response["statusCode"], 404)

    def test_poll_reconciles_expired_final_attempt_as_failed(self):
        updates = []

        class FakeDynamoDB:
            def get_item(self, **_kwargs):
                return {
                    "Item": {
                        "ownerId": {"S": SCOPE["userId"]},
                        "sessionId": {"S": SCOPE["sessionId"]},
                        "clientId": {"S": SCOPE["clientId"]},
                        "projectScopeId": {"S": SCOPE["projectId"]},
                        "action": {"S": "catchup.generate"},
                        "status": {"S": "saving"},
                        "retryCount": {"N": "2"},
                        "leaseExpiresAt": {"N": "100"},
                    }
                }

            def update_item(self, **kwargs):
                updates.append(kwargs)
                return {
                    "Attributes": {
                        "ownerId": {"S": SCOPE["userId"]},
                        "sessionId": {"S": SCOPE["sessionId"]},
                        "clientId": {"S": SCOPE["clientId"]},
                        "projectScopeId": {"S": SCOPE["projectId"]},
                        "action": {"S": "catchup.generate"},
                        "status": {"S": "failed"},
                        "retryCount": {"N": "2"},
                        "error": {
                            "S": "The AI job timed out after its final retry"
                        },
                    }
                }

        query = {
            "clientId": SCOPE["clientId"],
            "projectId": SCOPE["projectId"],
            "sessionId": SCOPE["sessionId"],
        }
        with (
            patch.object(api, "PROJECT_TABLE", "project-state"),
            patch.object(api, "now_epoch", return_value=200),
            patch.object(api, "aws_client", return_value=FakeDynamoDB()),
            patch.object(api, "metric"),
        ):
            result = api.handler(
                iam_event("GET", "/jobs/job-0001", query=query), None
            )

        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(
            body["error"], "The AI job timed out after its final retry"
        )
        self.assertEqual(len(updates), 1)
        self.assertIn("leaseExpiresAt < :now", updates[0]["ConditionExpression"])
        self.assertIn(":saving", updates[0]["ConditionExpression"])

    def test_validating_and_saving_jobs_remain_pending(self):
        query = {
            "clientId": SCOPE["clientId"],
            "projectId": SCOPE["projectId"],
            "sessionId": SCOPE["sessionId"],
        }

        for phase in ("validating", "saving"):
            with self.subTest(phase=phase):
                class FakeDynamoDB:
                    def get_item(self, **_kwargs):
                        return {
                            "Item": {
                                "ownerId": {"S": SCOPE["userId"]},
                                "sessionId": {"S": SCOPE["sessionId"]},
                                "clientId": {"S": SCOPE["clientId"]},
                                "projectScopeId": {"S": SCOPE["projectId"]},
                                "action": {"S": "brief.generate"},
                                "status": {"S": phase},
                                "phase": {"S": phase},
                                "retryCount": {"N": "0"},
                                "leaseExpiresAt": {"N": "9999999999"},
                            }
                        }

                with (
                    patch.object(api, "PROJECT_TABLE", "project-state"),
                    patch.object(api, "aws_client", return_value=FakeDynamoDB()),
                ):
                    result = api.handler(
                        iam_event("GET", "/jobs/job-0001", query=query),
                        None,
                    )

                body = json.loads(result["body"])
                self.assertEqual(result["statusCode"], 202)
                self.assertEqual(body["status"], phase)
                self.assertEqual(body["phase"], phase)

    def test_meeting_audio_upload_returns_scoped_private_form(self):
        calls = {"items": [], "posts": []}

        class FakeDynamo:
            def put_item(self, **kwargs):
                calls["items"].append(kwargs)
                return {}

        class FakeS3:
            def generate_presigned_post(self, **kwargs):
                calls["posts"].append(kwargs)
                return {
                    "url": "https://private-upload.example",
                    "fields": {"key": kwargs["Key"]},
                }

        payload = {
            "clientId": BLUE_SCOPE["clientId"],
            "projectId": BLUE_SCOPE["projectId"],
            "sessionId": BLUE_SCOPE["sessionId"],
            "scenarioId": meeting_contracts.SCENARIO_ID,
            "meetingId": meeting_contracts.DEFAULT_MEETING_ID,
            "fileName": "blue-mesa-call.mp3",
            "contentType": "audio/mpeg",
            "sizeBytes": 4096,
            "consentAcknowledged": True,
        }
        event = jwt_event(
            path="/workspace/meeting-audio/uploads", body=payload
        )
        clients = {"dynamodb": FakeDynamo(), "s3": FakeS3()}
        with (
            patch.object(api, "PROJECT_TABLE", "project-state"),
            patch.object(api, "MEETING_EVIDENCE_BUCKET", "private-meeting-audio"),
            patch.object(api, "derive_scope", return_value=AUTH_BLUE_SCOPE),
            patch.object(api, "aws_client", side_effect=lambda name: clients[name]),
            patch.object(api, "s3_encryption_args", return_value={"ServerSideEncryption": "AES256"}),
        ):
            result = api._create_meeting_audio_upload(event)

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertTrue(body["uploadId"])
        self.assertEqual(body["uploadUrl"], "https://private-upload.example")
        key = calls["posts"][0]["Key"]
        self.assertTrue(key.startswith("audio/uploads/"))
        self.assertNotIn("audioKey", body)
        self.assertEqual(
            calls["items"][0]["Item"]["ownerId"]["S"],
            AUTH_BLUE_SCOPE["userId"],
        )
        self.assertTrue(
            calls["items"][0]["Item"]["consentAcknowledged"]["BOOL"]
        )
        self.assertEqual(body["consentVersion"], "2026-08-27")

    def test_meeting_audio_upload_requires_recording_authorization(self):
        payload = {
            "clientId": BLUE_SCOPE["clientId"],
            "projectId": BLUE_SCOPE["projectId"],
            "sessionId": BLUE_SCOPE["sessionId"],
            "scenarioId": meeting_contracts.SCENARIO_ID,
            "meetingId": meeting_contracts.DEFAULT_MEETING_ID,
            "fileName": "blue-mesa-call.mp3",
            "contentType": "audio/mpeg",
            "sizeBytes": 4096,
        }
        with (
            patch.object(api, "PROJECT_TABLE", "project-state"),
            patch.object(api, "MEETING_EVIDENCE_BUCKET", "private-meeting-audio"),
            patch.object(api, "derive_scope", return_value=AUTH_BLUE_SCOPE),
            self.assertRaisesRegex(ValueError, "authorized to process"),
        ):
            api._create_meeting_audio_upload(
                jwt_event(path="/workspace/meeting-audio/uploads", body=payload)
            )

    def test_guest_meeting_audio_upload_is_denied(self):
        payload = {
            "clientId": BLUE_SCOPE["clientId"],
            "projectId": BLUE_SCOPE["projectId"],
            "sessionId": BLUE_SCOPE["sessionId"],
            "scenarioId": meeting_contracts.SCENARIO_ID,
            "meetingId": meeting_contracts.DEFAULT_MEETING_ID,
            "fileName": "blue-mesa-call.mp3",
            "contentType": "audio/mpeg",
            "sizeBytes": 4096,
            "consentAcknowledged": True,
        }
        event = iam_event(path="/meeting-audio/uploads", body=payload)
        with (
            patch.object(api, "PROJECT_TABLE", "project-state"),
            patch.object(api, "MEETING_EVIDENCE_BUCKET", "private-meeting-audio"),
            patch.object(api, "metric"),
        ):
            result = api.handler(event, None)

        self.assertEqual(result["statusCode"], 403)
        self.assertEqual(
            json.loads(result["body"])["error"],
            "This resource is not available",
        )

    def test_authenticated_user_can_download_the_blue_mesa_demo_audio(self):
        calls = []

        class FakeS3:
            def head_object(self, **kwargs):
                calls.append(("head", kwargs))
                return {"ContentLength": 4096}

            def generate_presigned_url(self, operation, **kwargs):
                calls.append((operation, kwargs))
                return "https://private-download.example/audio"

        event = jwt_event(
            method="GET",
            path="/workspace/meeting-audio/demo",
            query={
                "clientId": AUTH_BLUE_SCOPE["clientId"],
                "projectId": AUTH_BLUE_SCOPE["projectId"],
                "sessionId": AUTH_BLUE_SCOPE["sessionId"],
            },
        )
        with (
            patch.object(api, "MEETING_EVIDENCE_BUCKET", "private-meeting-audio"),
            patch.object(api, "_scope_from_query", return_value=AUTH_BLUE_SCOPE),
            patch.object(api, "aws_client", return_value=FakeS3()),
        ):
            result = api._get_demo_meeting_audio(event)

        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(
            body["fileName"], "PilarPrep-BlueMesa-Discovery-Meeting.mp3"
        )
        self.assertEqual(body["downloadUrl"], "https://private-download.example/audio")
        self.assertEqual(calls[0][1]["Key"], meeting_contracts.DEFAULT_AUDIO_KEY)


    def test_custom_scenario_meeting_audio_is_denied_without_server_error(self):
        payload = {
            "clientId": "custom-demo",
            "projectId": "custom-demo",
            "sessionId": SCOPE["sessionId"],
            "scenarioId": meeting_contracts.SCENARIO_ID,
            "meetingId": "custom-discovery",
            "fileName": "custom-call.mp3",
            "contentType": "audio/mpeg",
            "sizeBytes": 4096,
        }
        custom_scope = {
            **AUTH_BLUE_SCOPE,
            "clientId": "custom-demo",
            "projectId": "custom-demo",
        }
        event = jwt_event(path="/workspace/meeting-audio/uploads", body=payload)
        with (
            patch.object(api, "PROJECT_TABLE", "project-state"),
            patch.object(api, "MEETING_EVIDENCE_BUCKET", "private-meeting-audio"),
            patch.object(api, "derive_scope", return_value=custom_scope),
            patch.object(api, "metric"),
        ):
            result = api.handler(event, None)

        self.assertEqual(result["statusCode"], 403)
        self.assertEqual(
            json.loads(result["body"])["error"],
            "This resource is not available",
        )


class WorkerTests(unittest.TestCase):
    def test_legacy_blue_mesa_direction_is_normalized_before_safety_screening(self):
        generated = {
            "provider": "bedrock",
            "metadata": {"fallbackUsed": False},
        }
        screened_inputs = []

        def screen(value, *, source, **_kwargs):
            if source == "INPUT":
                screened_inputs.append(json.loads(json.dumps(value)))
            return value, {"policyResult": "passed"}

        brief_module = types.SimpleNamespace(
            _validate_brief_payload=lambda _payload: None,
            _resolve_model_id=lambda _payload: "us.amazon.nova-pro-v1:0",
            _generate_brief=lambda _payload: generated,
        )
        with (
            patch.object(worker, "_brief_module", return_value=brief_module),
            patch.object(worker, "_screen_ai_payload", side_effect=screen),
            patch.object(worker, "_set_job_phase"),
            patch.object(worker, "_write_brief_draft", return_value=generated),
            patch.object(worker, "metric") as metric,
        ):
            worker._run_brief(
                BLUE_SCOPE,
                {
                    "action": "brief.generate",
                    "inputVersion": "input-legacy-blue-mesa",
                    "input": {
                        "company": "BlueMesa Payments",
                        "additionalDirection": (
                            worker.LEGACY_BLUE_MESA_ADDITIONAL_DIRECTION
                        ),
                    },
                },
                "job-legacy-blue-mesa",
            )

        self.assertEqual(len(screened_inputs), 1)
        self.assertEqual(
            screened_inputs[0]["additionalDirection"],
            worker.CURRENT_BLUE_MESA_ADDITIONAL_DIRECTION,
        )
        metric.assert_any_call(
            "LegacyDemoContextNormalized", Action="brief.generate"
        )

    def test_legacy_demo_direction_is_not_rewritten_for_another_client(self):
        normalized = worker._normalize_legacy_demo_context(
            SCOPE,
            {
                "company": "Custom Customer",
                "additionalDirection": worker.LEGACY_BLUE_MESA_ADDITIONAL_DIRECTION,
            },
            action="brief.generate",
        )

        self.assertEqual(
            normalized["additionalDirection"],
            worker.LEGACY_BLUE_MESA_ADDITIONAL_DIRECTION,
        )

    def test_edited_blue_mesa_direction_is_not_rewritten(self):
        edited = f"{worker.LEGACY_BLUE_MESA_ADDITIONAL_DIRECTION} Add a custom fact."
        normalized = worker._normalize_legacy_demo_context(
            BLUE_SCOPE,
            {"additionalDirection": edited},
            action="brief.refine",
        )

        self.assertEqual(normalized["additionalDirection"], edited)

    def test_job_claim_lease_exceeds_the_ten_minute_worker_timeout(self):
        captured = {}

        class FakeDynamo:
            def update_item(self, **kwargs):
                captured.update(kwargs)
                return {}

        with (
            patch.object(worker, "aws_client", return_value=FakeDynamo()),
            patch.object(worker, "WORKER_TIMEOUT_SECONDS", 600),
            patch.object(worker, "now_epoch", return_value=1_000),
        ):
            claimed = worker._claim_job(SCOPE, "job-with-lease", 1)

        self.assertTrue(claimed)
        self.assertEqual(
            captured["ExpressionAttributeValues"][":lease"]["N"], "1660"
        )
        self.assertIn("leaseExpiresAt < :now", captured["ConditionExpression"])
        self.assertIn(":validating", captured["ConditionExpression"])
        self.assertIn(":saving", captured["ConditionExpression"])

    def test_active_job_phase_is_persisted_without_releasing_the_lease(self):
        captured = {}

        class FakeDynamo:
            def update_item(self, **kwargs):
                captured.update(kwargs)
                return {}

        with patch.object(worker, "aws_client", return_value=FakeDynamo()):
            worker._set_job_phase(SCOPE, "job-active-phase", "validating")

        self.assertEqual(
            captured["ExpressionAttributeValues"][":phase"]["S"],
            "validating",
        )
        self.assertIn(":saving", captured["ConditionExpression"])
        self.assertNotIn("REMOVE leaseExpiresAt", captured["UpdateExpression"])
        with self.assertRaisesRegex(ValueError, "Unsupported active job phase"):
            worker._set_job_phase(SCOPE, "job-active-phase", "unknown")

    def test_orphaned_queue_message_is_acknowledged_without_processing(self):
        class FakeDynamo:
            def update_item(self, **_kwargs):
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "job record expired",
                        }
                    },
                    "UpdateItem",
                )

        with (
            patch.object(worker, "aws_client", return_value=FakeDynamo()),
            patch.object(worker, "_job_item", return_value={}),
            patch.object(worker, "metric") as metric,
        ):
            claimed = worker._claim_job(SCOPE, "expired-job", 4)

        self.assertFalse(claimed)
        metric.assert_called_once_with(
            "OrphanedQueueMessages",
            Action="unknown",
        )

    def test_approval_metadata_is_persisted_inside_latest_packet(self):
        written = {}
        transactions = []
        draft_key = (
            f"tenants/{SCOPE['tenantId']}/clients/apex-mutual/projects/apex-mutual/"
            "brief/draft/latest.json"
        )
        draft_docx_key = (
            f"tenants/{SCOPE['tenantId']}/clients/apex-mutual/projects/apex-mutual/"
            "brief/draft/latest.docx"
        )
        draft = {
            "request": {"company": "Apex Mutual"},
            "response": {
                "provider": "bedrock",
                "metadata": {
                    "approvalStatus": "stale",
                    "artifactKey": draft_key,
                    "docxArtifactKey": draft_docx_key,
                    "docxDownloadUrl": "https://expired.example/draft.docx",
                },
            },
        }

        class FakeS3:
            def get_object(self, **kwargs):
                body = (
                    json.dumps(draft).encode("utf-8")
                    if kwargs["Key"] == draft_key
                    else b"docx"
                )
                return {"Body": BytesIO(body)}

        class FakeDynamoDB:
            def transact_write_items(self, **kwargs):
                transactions.append(kwargs)

        clients = {"s3": FakeS3(), "dynamodb": FakeDynamoDB()}

        def write_pair(scope, version, document, docx_bytes, *, download_filename):
            written["document"] = json.loads(json.dumps(document))
            written["docx"] = docx_bytes
            written["download_filename"] = download_filename
            prefix = (
                f"tenants/{scope['tenantId']}/clients/apex-mutual/"
                f"projects/apex-mutual/brief/approved/v{version:06d}/"
            )
            return (
                f"{prefix}packet.json",
                f"{prefix}packet.docx",
                "https://download.example/packet.docx",
                "json-sha256",
                "docx-sha256",
            )

        with (
            patch.object(worker, "PROJECT_TABLE", "project-state"),
            patch.object(worker, "ARTIFACT_BUCKET", "artifact-bucket"),
            patch.object(
                worker,
                "_brief_latest",
                return_value={
                    "packetVersion": 2,
                    "draftArtifactKey": draft_key,
                    "draftDocxArtifactKey": draft_docx_key,
                    "company": "Apex Mutual",
                },
            ),
            patch.object(worker, "_write_approved_packet_pair", side_effect=write_pair),
            patch.object(worker, "_upsert_client_directory"),
            patch.object(worker, "aws_client", side_effect=lambda name: clients[name]),
        ):
            result = worker._approve_brief(
                SCOPE,
                {
                    "createdAt": "2026-08-22T01:00:00Z",
                    "input": {"packetVersion": 2},
                },
                "job-approval",
            )

        stored_metadata = written["document"]["response"]["metadata"]
        self.assertEqual(stored_metadata["approvalStatus"], "approved")
        self.assertEqual(stored_metadata["packetVersion"], 2)
        self.assertEqual(stored_metadata["approvedPacketVersion"], 2)
        self.assertEqual(stored_metadata["approvedAt"], "2026-08-22T01:00:00Z")
        self.assertTrue(
            stored_metadata["artifactKey"].endswith(
                "/brief/approved/v000002/packet.json"
            )
        )
        self.assertNotIn("docxDownloadUrl", stored_metadata)
        self.assertEqual(
            result["metadata"]["docxDownloadUrl"],
            "https://download.example/packet.docx",
        )
        self.assertNotIn("precallHandoffJobId", result["metadata"])
        self.assertEqual(result["metadata"]["precallHandoffStatus"], "idle")
        self.assertEqual(result["metadata"]["precallHandoffSourceVersion"], 2)
        update = transactions[0]["TransactItems"][0]["Update"]
        self.assertIn(
            "precallHandoffStatus = :handoffIdle",
            update["UpdateExpression"],
        )
        self.assertIn("REMOVE precallHandoffJobId", update["UpdateExpression"])
        self.assertEqual(
            written["download_filename"], "Apex Mutual - Brief - v2.docx"
        )
        self.assertEqual(len(transactions), 1)
        transaction = transactions[0]["TransactItems"]
        self.assertEqual(len(transaction), 2)
        audit = transaction[1]["Put"]["Item"]
        self.assertEqual(audit["entityType"]["S"], "BRIEF_APPROVAL_AUDIT")
        self.assertEqual(audit["sortKey"]["S"], "BRIEF#APPROVAL#v000002")
        self.assertEqual(audit["artifactSha256"]["S"], "json-sha256")
        self.assertEqual(audit["docxSha256"]["S"], "docx-sha256")
        self.assertEqual(audit["approverId"]["S"], SCOPE["userId"])
        self.assertIn("ClientRequestToken", transactions[0])
        self.assertLessEqual(len(transactions[0]["ClientRequestToken"]), 36)

    def test_approval_promotes_a_draft_that_collides_with_an_approved_version(self):
        written = {}
        transactions = []
        draft_key = (
            f"tenants/{SCOPE['tenantId']}/clients/apex-mutual/projects/apex-mutual/"
            "brief/draft/latest.json"
        )
        draft_docx_key = draft_key.replace("latest.json", "latest.docx")
        draft = {
            "packetVersion": 1,
            "request": {"company": "Apex Mutual"},
            "response": {
                "provider": "bedrock",
                "metadata": {"packetVersion": 1, "approvalStatus": "draft"},
            },
        }

        class FakeS3:
            def get_object(self, **kwargs):
                self.assert_draft_key(kwargs["Key"])
                return {"Body": BytesIO(json.dumps(draft).encode("utf-8"))}

            @staticmethod
            def assert_draft_key(key):
                if key != draft_key:
                    raise AssertionError(f"Unexpected S3 key: {key}")

        class FakeDynamoDB:
            def transact_write_items(self, **kwargs):
                transactions.append(kwargs)

        def write_pair(scope, version, document, docx_bytes, *, download_filename):
            written.update(
                {
                    "version": version,
                    "document": json.loads(json.dumps(document)),
                    "docx": docx_bytes,
                    "download_filename": download_filename,
                }
            )
            prefix = (
                f"tenants/{scope['tenantId']}/clients/apex-mutual/"
                f"projects/apex-mutual/brief/approved/v{version:06d}/"
            )
            return (
                f"{prefix}packet.json",
                f"{prefix}packet.docx",
                "https://download.example/packet.docx",
                "json-sha256",
                "docx-sha256",
            )

        brief_module = types.SimpleNamespace(
            _brief_docx_bytes=lambda *_args: b"rebuilt-v2-docx"
        )
        with (
            patch.object(worker, "PROJECT_TABLE", "project-state"),
            patch.object(worker, "ARTIFACT_BUCKET", "artifact-bucket"),
            patch.object(
                worker,
                "_brief_latest",
                return_value={
                    "packetVersion": 1,
                    "approvedPacketVersion": 1,
                    "approvalStatus": "draft",
                    "draftArtifactKey": draft_key,
                    "draftDocxArtifactKey": draft_docx_key,
                    "company": "Apex Mutual",
                },
            ),
            patch.object(worker, "_brief_module", return_value=brief_module),
            patch.object(worker, "_write_approved_packet_pair", side_effect=write_pair),
            patch.object(worker, "_upsert_client_directory"),
            patch.object(worker, "metric") as metric,
            patch.object(
                worker,
                "aws_client",
                side_effect=lambda name: {
                    "s3": FakeS3(),
                    "dynamodb": FakeDynamoDB(),
                }[name],
            ),
        ):
            result = worker._approve_brief(
                SCOPE,
                {
                    "createdAt": "2026-08-22T01:00:00Z",
                    "input": {"packetVersion": 1},
                },
                "job-collision-recovery",
            )

        self.assertEqual(written["version"], 2)
        self.assertEqual(written["docx"], b"rebuilt-v2-docx")
        self.assertEqual(written["document"]["packetVersion"], 2)
        self.assertEqual(
            written["document"]["approvalAudit"]["sourcePacketVersion"], 1
        )
        self.assertEqual(result["metadata"]["packetVersion"], 2)
        self.assertEqual(result["metadata"]["approvedPacketVersion"], 2)
        self.assertEqual(result["metadata"]["precallHandoffStatus"], "idle")
        self.assertNotIn("precallHandoffJobId", result["metadata"])
        update = transactions[0]["TransactItems"][0]["Update"]
        self.assertEqual(
            update["ExpressionAttributeValues"][":expectedVersion"]["N"], "1"
        )
        self.assertEqual(
            update["ExpressionAttributeValues"][":approvedVersion"]["N"], "2"
        )
        audit = transactions[0]["TransactItems"][1]["Put"]["Item"]
        self.assertEqual(audit["sortKey"]["S"], "BRIEF#APPROVAL#v000002")
        metric.assert_any_call(
            "ApprovalVersionCollisionRecovered", Action="brief.approve"
        )

    def test_immutable_artifact_conflict_is_not_retried(self):
        class FakeS3:
            def put_object(self, **_kwargs):
                raise ClientError(
                    {"Error": {"Code": "PreconditionFailed"}},
                    "PutObject",
                )

            def get_object(self, **_kwargs):
                return {"Body": BytesIO(b"different-content")}

        with (
            patch.object(worker, "ARTIFACT_BUCKET", "artifact-bucket"),
            self.assertRaisesRegex(
                worker.NonRetryableJobError,
                "immutable approved artifact",
            ),
        ):
            worker._put_immutable_object(
                FakeS3(),
                SCOPE,
                key="approved/v000001/packet.json",
                body=b"new-content",
                content_type="application/json",
            )

    def test_idempotent_approval_remains_ready_for_manual_handoff(self):
        draft_key = (
            f"tenants/{SCOPE['tenantId']}/clients/apex-mutual/projects/apex-mutual/"
            "brief/draft/latest.json"
        )
        draft_docx_key = (
            f"tenants/{SCOPE['tenantId']}/clients/apex-mutual/projects/apex-mutual/"
            "brief/draft/latest.docx"
        )
        approved_key = (
            f"tenants/{SCOPE['tenantId']}/clients/apex-mutual/projects/apex-mutual/"
            "brief/approved/v000002/packet.json"
        )
        approved_docx_key = approved_key.replace("packet.json", "packet.docx")
        draft = {
            "request": {"company": "Apex Mutual"},
            "response": {"provider": "bedrock", "metadata": {}},
        }

        class FakeS3:
            def get_object(self, **kwargs):
                body = (
                    json.dumps(draft).encode("utf-8")
                    if kwargs["Key"] == draft_key
                    else b"docx"
                )
                return {"Body": BytesIO(body)}

        class FakeDynamoDB:
            def transact_write_items(self, **_kwargs):
                raise ClientError(
                    {"Error": {"Code": "TransactionCanceledException"}}
                )

        latest = {
            "packetVersion": 2,
            "draftArtifactKey": draft_key,
            "draftDocxArtifactKey": draft_docx_key,
            "company": "Apex Mutual",
        }
        current = {
            **latest,
            "approvalJobId": "job-approval",
            "approvedPacketVersion": 2,
            "approvedArtifactKey": approved_key,
        }

        with (
            patch.object(worker, "PROJECT_TABLE", "project-state"),
            patch.object(worker, "ARTIFACT_BUCKET", "artifact-bucket"),
            patch.object(worker, "_brief_latest", side_effect=[latest, current]),
            patch.object(
                worker,
                "_write_approved_packet_pair",
                return_value=(
                    approved_key,
                    approved_docx_key,
                    "https://download.example/packet.docx",
                    "json-sha256",
                    "docx-sha256",
                ),
            ),
            patch.object(
                worker,
                "aws_client",
                side_effect=lambda name: {
                    "s3": FakeS3(),
                    "dynamodb": FakeDynamoDB(),
                }[name],
            ),
        ):
            result = worker._approve_brief(
                SCOPE,
                {
                    "createdAt": "2026-08-22T01:00:00Z",
                    "input": {"packetVersion": 2},
                },
                "job-approval",
            )

        self.assertNotIn("precallHandoffJobId", result["metadata"])
        self.assertEqual(result["metadata"]["precallHandoffStatus"], "idle")
        self.assertEqual(result["metadata"]["precallHandoffSourceVersion"], 2)
    def test_handoff_only_directory_update_omits_unused_attribute_names(self):
        calls = []

        class FakeDynamoDB:
            def update_item(self, **kwargs):
                calls.append(kwargs)
                return {}

        with (
            patch.object(worker, "PROJECT_TABLE", "project-state"),
            patch.object(worker, "aws_client", return_value=FakeDynamoDB()),
        ):
            worker._upsert_client_directory(
                SCOPE,
                handoff={
                    "updatedAt": "2026-08-13T12:00:00Z",
                    "artifactKey": "clients/apex-mutual/handoff/latest.json",
                },
            )

        self.assertEqual(len(calls), 1)
        self.assertNotIn("ExpressionAttributeNames", calls[0])
        self.assertIn("latestHandoffAt", calls[0]["UpdateExpression"])
        self.assertIn("handoffArtifactKey", calls[0]["UpdateExpression"])

    def test_latest_only_cleanup_never_deletes_nested_drafts(self):
        deleted = []

        class FakeS3:
            def list_object_versions(self, **_kwargs):
                return {
                    "Versions": [
                        {"Key": "brief/latest.json", "VersionId": "new-json"},
                        {"Key": "brief/latest.json", "VersionId": "old-json"},
                        {"Key": "brief/latest.docx", "VersionId": "new-docx"},
                        {"Key": "brief/latest.docx", "VersionId": "old-docx"},
                        {"Key": "brief/draft/latest.json", "VersionId": "draft-json"},
                    ],
                    "DeleteMarkers": [],
                    "IsTruncated": False,
                }

            def delete_objects(self, **kwargs):
                deleted.extend(kwargs["Delete"]["Objects"])

        worker._purge_noncurrent_versions(
            FakeS3(),
            "brief/",
            {
                ("brief/latest.json", "new-json"),
                ("brief/latest.docx", "new-docx"),
            },
        )
        self.assertEqual(
            deleted,
            [
                {"Key": "brief/latest.json", "VersionId": "old-json"},
                {"Key": "brief/latest.docx", "VersionId": "old-docx"},
            ],
        )

    def test_duplicate_delivery_does_not_reload_or_rewrite_job(self):
        message = {
            "action": "brief.generate",
            "jobId": "job-0001",
            **SCOPE,
            "traceId": "trace-0001",
            "inputVersion": "input-0001",
            "inputKey": "jobs/demo/apex-mutual/apex-mutual/job-0001/input.json",
        }
        record = {
            "body": json.dumps(message),
            "attributes": {"ApproximateReceiveCount": "2"},
        }
        with (
            patch.object(worker, "_claim_job", return_value=False),
            patch.object(worker, "_load_input") as load_input,
            patch.object(worker, "_store_result") as store_result,
        ):
            worker._process_record(record)
        load_input.assert_not_called()
        store_result.assert_not_called()

    def test_duplicate_transcribe_completion_does_not_run_analysis(self):
        fake_meeting = types.SimpleNamespace(
            claim_continuation=lambda _job_name: None,
            MeetingConflictError=meeting_contracts.MeetingConflictError,
        )
        event = {
            "source": "aws.transcribe",
            "detail-type": "Transcribe Job State Change",
            "detail": {
                "TranscriptionJobName": "pillarprep-job-0001",
                "TranscriptionJobStatus": "COMPLETED",
            },
        }
        record = {
            "body": json.dumps(event),
            "attributes": {"ApproximateReceiveCount": "2"},
        }
        with (
            patch.object(worker, "_meeting_module", return_value=fake_meeting),
            patch.object(worker, "_run_meeting_analysis") as analyze,
            patch.object(worker, "metric") as metric,
        ):
            worker._process_record(record)

        analyze.assert_not_called()
        metric.assert_any_call("TranscribeEvent2Deliveries", Action="meeting.process")
        metric.assert_any_call("DuplicateDeliveries", Action="meeting.process")

    def test_meeting_analysis_failure_writes_no_proposal_or_result(self):
        continuation = {
            **BLUE_SCOPE,
            "projectScopeId": BLUE_SCOPE["projectId"],
            "jobId": "job-meeting-0001",
            "inputKey": "jobs/meeting/input.json",
            "inputVersion": "input-0001",
            "expectedApprovedPacketVersion": 4,
            "traceId": "trace-meeting-0001",
        }
        calls = {"reset": [], "failed": []}
        screened_inputs = []

        def screen(value, *, source, **_kwargs):
            if source == "INPUT":
                screened_inputs.append(json.loads(json.dumps(value)))
            return value, {"source": source, "policyResult": "passed"}

        fake_meeting = types.SimpleNamespace(
            claim_continuation=lambda _job_name: continuation,
            continuation_scope=lambda _item: BLUE_SCOPE,
            set_job_phase=lambda *_args, **_kwargs: None,
            read_transcript=lambda _item: {
                "text": "Blue Mesa is already on AWS and payroll is in scope.",
                "durationSeconds": 60,
                "speakerCount": 2,
                "segments": [],
            },
            persist_proposal=lambda *_args, **_kwargs: calls.setdefault(
                "proposal", []
            ).append(True),
            complete_continuation=lambda *_args, **_kwargs: calls.setdefault(
                "complete", []
            ).append(True),
            fail_continuation=lambda *_args, **_kwargs: calls["failed"].append(
                True
            ),
            reset_continuation=lambda *args: calls["reset"].append(args),
            MeetingConflictError=meeting_contracts.MeetingConflictError,
        )
        event = {
            "source": "aws.transcribe",
            "detail-type": "Transcribe Job State Change",
            "detail": {
                "TranscriptionJobName": "pillarprep-job-meeting-0001",
                "TranscriptionJobStatus": "COMPLETED",
            },
        }
        record = {
            "body": json.dumps(event),
            "attributes": {"ApproximateReceiveCount": "1"},
        }
        with (
            patch.object(worker, "_meeting_module", return_value=fake_meeting),
            patch.object(
                worker,
                "_load_input",
                return_value={"action": "meeting.process", "input": {}},
            ),
            patch.object(
                worker,
                "_approved_document",
                return_value=(
                    {"approvedPacketVersion": 4},
                    {"response": {"technical": ["approved"]}},
                ),
            ),
            patch.object(
                worker,
                "_run_meeting_analysis",
                side_effect=RuntimeError("model failed"),
            ),
            patch.object(worker, "_screen_ai_payload", side_effect=screen),
            patch.object(worker, "_store_result") as store_result,
            patch.object(worker, "_record_failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "model failed"):
                worker._process_record(record)

        self.assertEqual(len(calls["reset"]), 1)
        self.assertNotIn("proposal", calls)
        self.assertNotIn("complete", calls)
        self.assertEqual(calls["failed"], [])
        store_result.assert_not_called()
        self.assertEqual(set(screened_inputs[0]), {"document", "transcript"})
        self.assertNotIn("approvedDocument", screened_inputs[0])

    def test_meeting_guardrail_block_stops_analysis_and_persistence(self):
        continuation = {
            **BLUE_SCOPE,
            "projectScopeId": BLUE_SCOPE["projectId"],
            "jobId": "job-meeting-blocked",
            "inputKey": "jobs/meeting/input.json",
            "inputVersion": "input-0001",
            "expectedApprovedPacketVersion": 4,
            "traceId": "trace-meeting-blocked",
        }
        failures = []
        fake_meeting = types.SimpleNamespace(
            claim_continuation=lambda _job_name: continuation,
            continuation_scope=lambda _item: BLUE_SCOPE,
            set_job_phase=lambda *_args, **_kwargs: None,
            read_transcript=lambda _item: {
                "text": "Transcript content that the configured policy blocks.",
                "durationSeconds": 60,
                "speakerCount": 1,
                "segments": [],
            },
            persist_proposal=lambda *_args, **_kwargs: self.fail(
                "Blocked content must not persist a proposal"
            ),
            fail_continuation=lambda *_args, **_kwargs: failures.append(True),
            MeetingConflictError=meeting_contracts.MeetingConflictError,
        )
        event = {
            "source": "aws.transcribe",
            "detail-type": "Transcribe Job State Change",
            "detail": {
                "TranscriptionJobName": "pillarprep-job-meeting-blocked",
                "TranscriptionJobStatus": "COMPLETED",
            },
        }
        record = {
            "body": json.dumps(event),
            "attributes": {"ApproximateReceiveCount": "1"},
        }
        with (
            patch.object(worker, "_meeting_module", return_value=fake_meeting),
            patch.object(
                worker,
                "_load_input",
                return_value={"action": "meeting.process", "input": {}},
            ),
            patch.object(
                worker,
                "_approved_document",
                return_value=(
                    {"approvedPacketVersion": 4},
                    {"response": {"technical": ["approved"]}},
                ),
            ),
            patch.object(
                worker,
                "_screen_ai_payload",
                side_effect=worker.NonRetryableJobError(
                    "Content safety check blocked the transcript"
                ),
            ),
            patch.object(worker, "_run_meeting_analysis") as analyze,
            patch.object(worker, "_store_result") as store_result,
        ):
            worker._process_record(record)

        analyze.assert_not_called()
        store_result.assert_not_called()
        self.assertEqual(failures, [True])

    def test_refinement_content_validation_failure_is_non_retryable(self):
        brief_module = types.SimpleNamespace(
            _validate_brief_payload=lambda _payload: None,
            _resolve_model_id=lambda _payload: "us.amazon.nova-pro-v1:0",
            _generate_brief=lambda _payload: (_ for _ in ()).throw(
                ValueError(
                    "Refinement could not produce a complete, contradiction-free selected brief"
                )
            ),
        )
        document = {
            "action": "brief.refine",
            "inputVersion": "input-0001",
            "input": {
                "company": "Custom Customer",
                "baseBriefVersion": 1,
                "refinementTarget": "businessCase",
            },
        }
        with (
            patch.object(worker, "_brief_module", return_value=brief_module),
            patch.object(worker, "_brief_latest", return_value={}),
        ):
            with self.assertRaisesRegex(
                worker.NonRetryableJobError,
                "contradiction-free selected brief",
            ):
                worker._run_brief(SCOPE, document, "job-refinement-validation")

    def test_refinement_rejects_stale_server_version_before_model_invocation(self):
        generated = []
        brief_module = types.SimpleNamespace(
            _validate_brief_payload=lambda _payload: None,
            _resolve_model_id=lambda _payload: "us.amazon.nova-pro-v1:0",
            _generate_brief=lambda payload: generated.append(payload),
        )
        document = {
            "action": "brief.refine",
            "inputVersion": "input-0001",
            "input": {
                "company": "Custom Customer",
                "baseBriefVersion": 1,
                "refinementTarget": "executive",
            },
        }
        with (
            patch.object(worker, "_brief_module", return_value=brief_module),
            patch.object(worker, "_brief_latest", return_value={"packetVersion": 2}),
        ):
            with self.assertRaisesRegex(
                worker.NonRetryableJobError,
                "brief changed before refinement",
            ):
                worker._run_brief(SCOPE, document, "job-stale-refinement")

        self.assertEqual(generated, [])

    def test_refinement_uses_server_draft_before_screening(self):
        authoritative = {"businessCase": {"scenario": "Trusted server draft"}}
        generated = {
            "provider": "bedrock",
            "metadata": {"packetVersion": 2, "fallbackUsed": False},
        }
        model_payloads = []
        screened_inputs = []

        def screen(value, *, source, **_kwargs):
            if source == "INPUT":
                screened_inputs.append(json.loads(json.dumps(value)))
            return value, {"source": source, "policyResult": "passed"}

        brief_module = types.SimpleNamespace(
            _validate_brief_payload=lambda _payload: None,
            _resolve_model_id=lambda _payload: "us.amazon.nova-pro-v1:0",
            _generate_brief=lambda payload: (
                model_payloads.append(payload) or generated
            ),
        )
        document = {
            "action": "brief.refine",
            "inputVersion": "input-0001",
            "input": {
                "company": "Custom Customer",
                "baseBriefVersion": 1,
                "refinementTarget": "businessCase",
                "feedbackNotes": "Emphasize the approved outcome.",
                "previousBrief": {
                    "businessCase": {"scenario": "Untrusted browser copy"}
                },
            },
        }
        with (
            patch.object(worker, "_brief_module", return_value=brief_module),
            patch.object(
                worker,
                "_brief_latest",
                return_value={
                    "packetVersion": 1,
                    "draftArtifactKey": "scoped/draft/latest.json",
                },
            ),
            patch.object(
                worker,
                "_current_draft_response",
                return_value=authoritative,
            ),
            patch.object(worker, "_screen_ai_payload", side_effect=screen),
            patch.object(worker, "_set_job_phase"),
            patch.object(worker, "_write_brief_draft", return_value=generated),
        ):
            result = worker._run_brief(
                SCOPE, document, "job-authoritative-draft"
            )

        self.assertIs(result, generated)
        self.assertNotIn("previousBrief", screened_inputs[0])
        self.assertEqual(model_payloads[0]["previousBrief"], authoritative)

    def test_refinement_can_bootstrap_missing_guest_server_state(self):
        generated = {
            "provider": "bedrock",
            "metadata": {"packetVersion": 2, "fallbackUsed": False},
        }
        brief_module = types.SimpleNamespace(
            _validate_brief_payload=lambda _payload: None,
            _resolve_model_id=lambda _payload: "us.amazon.nova-pro-v1:0",
            _generate_brief=lambda _payload: generated,
        )
        document = {
            "action": "brief.refine",
            "inputVersion": "input-0001",
            "input": {
                "company": "Custom Customer",
                "baseBriefVersion": 1,
                "refinementTarget": "executive",
            },
        }
        with (
            patch.object(worker, "_brief_module", return_value=brief_module),
            patch.object(worker, "_brief_latest", return_value={}),
            patch.object(worker, "_set_job_phase"),
            patch.object(worker, "_write_brief_draft", return_value=generated) as write,
        ):
            result = worker._run_brief(SCOPE, document, "job-bootstrap-refinement")

        self.assertIs(result, generated)
        write.assert_called_once()
        self.assertEqual(write.call_args.kwargs["action"], "brief.refine")

    def test_refinement_write_allows_missing_state_but_protects_existing_version(self):
        updates = []

        class FakeDynamoDB:
            def update_item(self, **kwargs):
                updates.append(kwargs)
                return {}

        generated = {
            "provider": "bedrock",
            "metadata": {"packetVersion": 2, "fallbackUsed": False},
        }
        brief_module = types.SimpleNamespace(
            _brief_docx_bytes=lambda *_args: b"docx",
        )
        with (
            patch.object(worker, "PROJECT_TABLE", "project-state"),
            patch.object(worker, "_brief_module", return_value=brief_module),
            patch.object(
                worker,
                "_write_packet_pair",
                return_value=(
                    "brief/draft/latest.json",
                    "brief/draft/latest.docx",
                    "https://example.test/download",
                ),
            ),
            patch.object(worker, "aws_client", return_value=FakeDynamoDB()),
        ):
            worker._write_brief_draft(
                SCOPE,
                {"company": "Custom Customer", "baseBriefVersion": 1},
                generated,
                action="brief.refine",
                job_id="job-bootstrap-refinement",
                input_version="input-0001",
            )

        self.assertEqual(len(updates), 1)
        self.assertEqual(
            updates[0]["ConditionExpression"],
            "attribute_not_exists(packetVersion) OR packetVersion = :baseVersion",
        )
        self.assertEqual(
            updates[0]["ExpressionAttributeValues"][":baseVersion"]["N"], "1"
        )

    def test_generation_uses_the_next_server_owned_packet_version(self):
        updates = []

        class FakeDynamoDB:
            def update_item(self, **kwargs):
                updates.append(kwargs)
                return {}

        generated = {
            "provider": "bedrock",
            "metadata": {"packetVersion": 1, "fallbackUsed": False},
        }
        brief_module = types.SimpleNamespace(
            _brief_docx_bytes=lambda *_args: b"docx",
        )
        with (
            patch.object(worker, "PROJECT_TABLE", "project-state"),
            patch.object(worker, "_brief_module", return_value=brief_module),
            patch.object(
                worker,
                "_brief_latest",
                return_value={
                    "packetVersion": 1,
                    "approvedPacketVersion": 1,
                    "approvalStatus": "draft",
                },
            ),
            patch.object(
                worker,
                "_write_packet_pair",
                return_value=(
                    "brief/draft/latest.json",
                    "brief/draft/latest.docx",
                    "https://example.test/download",
                ),
            ),
            patch.object(worker, "aws_client", return_value=FakeDynamoDB()),
        ):
            result = worker._write_brief_draft(
                SCOPE,
                {"company": "Custom Customer"},
                generated,
                action="brief.generate",
                job_id="job-next-version",
                input_version="input-0002",
            )

        self.assertEqual(result["metadata"]["packetVersion"], 2)
        self.assertEqual(len(updates), 1)
        self.assertEqual(
            updates[0]["ConditionExpression"],
            "packetVersion = :baseVersion",
        )
        self.assertEqual(
            updates[0]["ExpressionAttributeValues"][":baseVersion"]["N"], "1"
        )
        self.assertEqual(
            updates[0]["ExpressionAttributeValues"][":packetVersion"]["N"], "2"
        )

    def test_generation_model_value_error_remains_retryable(self):
        brief_module = types.SimpleNamespace(
            _validate_brief_payload=lambda _payload: None,
            _resolve_model_id=lambda _payload: "us.amazon.nova-pro-v1:0",
            _generate_brief=lambda _payload: (_ for _ in ()).throw(
                ValueError("temporary malformed model response")
            ),
        )
        document = {
            "action": "brief.generate",
            "inputVersion": "input-0001",
            "input": {"company": "Custom Customer"},
        }
        with patch.object(worker, "_brief_module", return_value=brief_module):
            with self.assertRaisesRegex(ValueError, "temporary malformed model response"):
                worker._run_brief(SCOPE, document, "job-generation-retry")

    def test_brief_generation_reports_validation_then_saving(self):
        generated = {
            "provider": "bedrock",
            "metadata": {"fallbackUsed": False},
        }
        brief_module = types.SimpleNamespace(
            _validate_brief_payload=lambda _payload: None,
            _resolve_model_id=lambda _payload: "us.amazon.nova-pro-v1:0",
            _generate_brief=lambda _payload: generated,
        )
        phases = []
        with (
            patch.object(worker, "_brief_module", return_value=brief_module),
            patch.object(
                worker,
                "_screen_ai_payload",
                side_effect=[
                    ({"company": "Custom Customer"}, {"policyResult": "passed"}),
                    (generated, {"policyResult": "passed"}),
                ],
            ),
            patch.object(
                worker,
                "_set_job_phase",
                side_effect=lambda _scope, _job_id, phase: phases.append(phase),
            ),
            patch.object(
                worker,
                "_write_brief_draft",
                return_value=generated,
            ),
        ):
            result = worker._run_brief(
                SCOPE,
                {
                    "action": "brief.generate",
                    "inputVersion": "input-0001",
                    "input": {"company": "Custom Customer"},
                },
                "job-generation-phases",
            )

        self.assertIs(result, generated)
        self.assertEqual(phases, ["validating", "saving"])

    def test_brief_generation_passes_tenant_scoped_retrieval_to_the_model(self):
        generated = {
            "provider": "bedrock",
            "metadata": {"fallbackUsed": False},
        }
        model_payloads = []
        approved_sources = [
            {
                "sourceId": "src-rag-current-state",
                "sourceTitle": "Approved current state",
                "evidenceSnippet": "The customer is already on AWS.",
                "accessScope": "tenant-private",
            }
        ]
        brief_module = types.SimpleNamespace(
            _validate_brief_payload=lambda _payload: None,
            _resolve_model_id=lambda _payload: "us.amazon.nova-pro-v1:0",
            _generate_brief=lambda payload: (
                model_payloads.append(json.loads(json.dumps(payload))) or generated
            ),
        )
        evidence_module = types.SimpleNamespace(
            retrieve_for_brief=lambda _scope, query: (
                approved_sources,
                {
                    "enabled": True,
                    "mode": "tenant-private",
                    "resultCount": 1,
                    "queryObserved": bool(query),
                },
            )
        )

        with (
            patch.object(worker, "_brief_module", return_value=brief_module),
            patch.object(worker, "_evidence_module", return_value=evidence_module),
            patch.object(
                worker,
                "_screen_ai_payload",
                side_effect=lambda value, **_kwargs: (
                    value,
                    {"policyResult": "passed"},
                ),
            ),
            patch.object(worker, "_set_job_phase"),
            patch.object(worker, "_write_brief_draft", return_value=generated),
        ):
            result = worker._run_brief(
                {**SCOPE, "tenantId": "tenant-acme", "identityType": "authenticated"},
                {
                    "action": "brief.generate",
                    "inputVersion": "input-rag-0001",
                    "input": {
                        "company": "Apex Mutual",
                        "industry": "Financial Services",
                        "context": "Validate the current AWS architecture.",
                    },
                },
                "job-rag-generation",
            )

        self.assertEqual(
            model_payloads[0]["approvedEvidenceSources"],
            approved_sources,
        )
        self.assertEqual(result["metadata"]["rag"]["resultCount"], 1)
        self.assertTrue(result["metadata"]["rag"]["queryObserved"])

    def test_retry_status_becomes_terminal_on_third_receive(self):
        calls = []

        class FakeDynamoDB:
            def update_item(self, **kwargs):
                calls.append(kwargs)
                return {}

        with (
            patch.object(worker, "PROJECT_TABLE", "project-state"),
            patch.object(worker, "aws_client", return_value=FakeDynamoDB()),
        ):
            worker._record_failure(SCOPE, "job-0001", 1, RuntimeError("private"))
            worker._record_failure(SCOPE, "job-0001", 3, RuntimeError("private"))

        self.assertEqual(
            calls[0]["ExpressionAttributeValues"][":status"]["S"], "queued"
        )
        self.assertEqual(
            calls[1]["ExpressionAttributeValues"][":status"]["S"], "failed"
        )
        self.assertNotIn("private", json.dumps(calls))

    def test_non_retryable_refinement_conflict_does_not_request_sqs_retry(self):
        message = {
            "tenantId": SCOPE["tenantId"],
            "clientId": SCOPE["clientId"],
            "projectId": SCOPE["projectId"],
            "userId": SCOPE["userId"],
            "sessionId": SCOPE["sessionId"],
            "jobId": "job-stale-refinement",
            "action": "brief.refine",
            "traceId": "trace-stale-refinement",
            "inputKey": "jobs/demo/apex-mutual/apex-mutual/job-stale-refinement/input.json",
        }
        record = {
            "messageId": "message-stale-refinement",
            "receiptHandle": "opaque-receipt",
            "body": json.dumps(message),
            "attributes": {"ApproximateReceiveCount": "1"},
        }
        terminal_failures = []

        with (
            patch.object(worker, "_claim_job", return_value=True),
            patch.object(
                worker,
                "_load_input",
                return_value={
                    "action": "brief.refine",
                    "idempotencyKey": "stale-refinement-1",
                    "input": {},
                },
            ),
            patch.object(worker, "_existing_action_result", return_value=None),
            patch.object(
                worker,
                "_run_brief",
                side_effect=worker.NonRetryableJobError(
                    "The brief changed before refinement; reload the latest packet and apply feedback again."
                ),
            ),
            patch.object(
                worker,
                "_record_terminal_failure",
                side_effect=lambda scope, job_id, error: terminal_failures.append(
                    (scope, job_id, str(error))
                ),
            ),
            patch.object(worker, "aws_client") as aws_client,
        ):
            result = worker.handler({"Records": [record]}, None)

        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(len(terminal_failures), 1)
        self.assertEqual(terminal_failures[0][1], "job-stale-refinement")
        self.assertIn("reload the latest packet", terminal_failures[0][2])
        aws_client.assert_not_called()
    def test_caught_worker_failure_requests_fast_sqs_redelivery(self):
        calls = []

        class FakeSqs:
            def change_message_visibility(self, **kwargs):
                calls.append(kwargs)

        record = {
            "messageId": "message-0001",
            "receiptHandle": "opaque-receipt",
            "body": "{}",
            "attributes": {"ApproximateReceiveCount": "1"},
        }
        with (
            patch.object(worker, "JOB_QUEUE_URL", "https://sqs.example/jobs"),
            patch.object(worker, "RETRY_VISIBILITY_SECONDS", 5),
            patch.object(worker, "_process_record", side_effect=RuntimeError("retry")),
            patch.object(worker, "aws_client", return_value=FakeSqs()),
        ):
            result = worker.handler({"Records": [record]}, None)

        self.assertEqual(
            result, {"batchItemFailures": [{"itemIdentifier": "message-0001"}]}
        )
        self.assertEqual(
            calls,
            [
                {
                    "QueueUrl": "https://sqs.example/jobs",
                    "ReceiptHandle": "opaque-receipt",
                    "VisibilityTimeout": 5,
                }
            ],
        )

    def test_catchup_is_read_only_while_another_user_updates_the_project(self):
        runtime_calls = []
        shared_project = {"version": 7}

        class FakeAgentCore:
            def invoke_agent_runtime(self, **kwargs):
                runtime_calls.append(kwargs)
                shared_project["version"] += 1
                return {
                    "response": BytesIO(
                        json.dumps(
                            {
                                "provider": "agentcore",
                                "answer": "Role-aware approved-packet catch-up.",
                                "metadata": {"toolCalls": ["get_latest_brief", "get_project_state", "generate_catchup"]},
                            }
                        ).encode("utf-8")
                    )
                }

        document = {
            "action": "catchup.generate",
            "idempotencyKey": "catchup-demo-0001",
            "input": {
                "audienceRole": "Solutions Architect",
                "focus": "What changed?",
                "meetingNotes": "",
                "modelPreference": "nova-pro",
            },
        }
        approved = (
            {"approvedPacketVersion": 4},
            {
                "response": {"technical": ["Approved evidence"]},
                "request": {"company": "Apex Mutual"},
            },
        )
        with (
            patch.object(worker, "AGENT_RUNTIME_ARN", "runtime-arn"),
            patch.object(worker, "_approved_document", return_value=approved),
            patch.object(worker, "_scope_token", return_value="signed-scope"),
            patch.object(worker, "aws_client", return_value=FakeAgentCore()) as clients,
        ):
            result = worker._run_agent(SCOPE, document)

        payload = json.loads(runtime_calls[0]["payload"].decode("utf-8"))
        self.assertEqual(payload["action"], "generate_catchup")
        self.assertFalse(payload["confirmWrite"])
        self.assertEqual(payload["approvedBrief"]["technical"], ["Approved evidence"])
        self.assertEqual(result["provider"], "agentcore")
        self.assertEqual(result["metadata"]["approvedPacketVersion"], 4)
        self.assertEqual(shared_project["version"], 8)
        clients.assert_called_once_with("bedrock-agentcore")

    def test_agent_context_limit_is_non_retryable_and_does_not_expose_runtime_details(self):
        response = {"response": BytesIO(json.dumps({
            "errorCode": "AGENT_CONTEXT_TOO_LARGE", "retryable": False,
            "error": "Internal model details and private input must not be forwarded",
        }).encode())}
        with self.assertRaises(worker.NonRetryableJobError) as raised:
            worker._read_runtime_response(response)
        self.assertIn("approved brief has not changed", str(raised.exception))
        self.assertNotIn("Internal model details", str(raised.exception))

    def test_successful_agent_response_is_not_changed_by_terminal_error_handling(self):
        expected = {"provider": "agentcore", "projectAnswer": "Grounded handoff", "metadata": {"fallbackUsed": False}}
        self.assertEqual(worker._read_runtime_response({"response": BytesIO(json.dumps(expected).encode())}), expected)

    def test_catchup_rejects_write_capable_tool_trace(self):
        class FakeAgentCore:
            def invoke_agent_runtime(self, **_kwargs):
                return {
                    "response": BytesIO(
                        b'{"provider":"agentcore","answer":"catch-up","metadata":{"toolCalls":["save_project_update"]}}'
                    )
                }

        document = {
            "action": "catchup.generate",
            "idempotencyKey": "catchup-demo-0002",
            "input": {"audienceRole": "PM"},
        }
        approved = (
            {"approvedPacketVersion": 2},
            {"response": {"technical": ["Approved"]}, "request": {}},
        )
        with (
            patch.object(worker, "AGENT_RUNTIME_ARN", "runtime-arn"),
            patch.object(worker, "_approved_document", return_value=approved),
            patch.object(worker, "_scope_token", return_value="signed-scope"),
            patch.object(worker, "aws_client", return_value=FakeAgentCore()),
        ):
            with self.assertRaisesRegex(RuntimeError, "write-capable"):
                worker._run_agent(SCOPE, document)

    def test_agent_jobs_share_memory_but_use_isolated_runtime_sessions(self):
        runtime_calls = []

        class FakeAgentCore:
            def invoke_agent_runtime(self, **kwargs):
                runtime_calls.append(kwargs)
                return {
                    "response": BytesIO(
                        b'{"provider":"agentcore","answer":"catch-up","metadata":{}}'
                    )
                }

        approved = (
            {"approvedPacketVersion": 2},
            {"response": {"technical": ["Approved"]}, "request": {}},
        )
        documents = [
            {
                "action": "catchup.generate",
                "idempotencyKey": f"catchup-session-{index}",
                "input": {"audienceRole": "PM"},
            }
            for index in (1, 2)
        ]
        with (
            patch.object(worker, "AGENT_RUNTIME_ARN", "runtime-arn"),
            patch.object(worker, "_approved_document", return_value=approved),
            patch.object(worker, "_scope_token", return_value="signed-scope"),
            patch.object(worker, "aws_client", return_value=FakeAgentCore()),
        ):
            results = [worker._run_agent(SCOPE, document) for document in documents]

        runtime_session_ids = [
            call["runtimeSessionId"] for call in runtime_calls
        ]
        self.assertNotEqual(runtime_session_ids[0], runtime_session_ids[1])
        self.assertTrue(all(len(value) >= 33 for value in runtime_session_ids))
        self.assertEqual(
            results[0]["metadata"]["agentSessionId"],
            results[1]["metadata"]["agentSessionId"],
        )
        self.assertEqual(
            results[0]["metadata"]["agentRuntimeSessionId"],
            runtime_session_ids[0],
        )
        self.assertEqual(
            results[1]["metadata"]["agentRuntimeSessionId"],
            runtime_session_ids[1],
        )
    def test_handoff_rejects_a_stale_approved_packet_version(self):
        document = {
            "action": "handoff.generate",
            "idempotencyKey": "handoff-demo-0001",
            "input": {
                "audienceRole": "Solutions Architect",
                "expectedApprovedPacketVersion": 3,
            },
        }
        approved = (
            {"approvedPacketVersion": 4},
            {"response": {"technical": ["Approved"]}, "request": {}},
        )
        with (
            patch.object(worker, "AGENT_RUNTIME_ARN", "runtime-arn"),
            patch.object(worker, "_approved_document", return_value=approved),
        ):
            with self.assertRaisesRegex(ValueError, "changed before handoff"):
                worker._run_agent(SCOPE, document)



class MeetingWorkflowTests(unittest.TestCase):
    def test_public_demo_scope_separates_client_and_evidence_ids(self):
        meeting_contracts.assert_public_demo_scope(
            BLUE_SCOPE, meeting_contracts.SCENARIO_ID
        )
        with self.assertRaises(meeting_contracts.RetrievalScopeError):
            meeting_contracts.assert_public_demo_scope(
                {**BLUE_SCOPE, "clientId": "another-client"},
                meeting_contracts.SCENARIO_ID,
            )
        with self.assertRaises(meeting_contracts.RetrievalScopeError):
            meeting_contracts.assert_public_demo_scope(
                BLUE_SCOPE, "another-scenario"
            )

    def test_meeting_request_requires_fixed_scenario_and_approved_version(self):
        request = {
            "action": "meeting.process",
            "clientId": BLUE_SCOPE["clientId"],
            "projectId": BLUE_SCOPE["projectId"],
            "sessionId": BLUE_SCOPE["sessionId"],
            "idempotencyKey": "meeting-process-0001",
            "input": {
                "scenarioId": meeting_contracts.SCENARIO_ID,
                "meetingId": meeting_contracts.DEFAULT_MEETING_ID,
                "audioUploadId": "upload-meeting-0001",
                "expectedApprovedPacketVersion": 4,
                "enablePiiRedaction": False,
            },
        }
        validated = common.validate_job_request(request)
        self.assertEqual(validated["input"]["expectedApprovedPacketVersion"], 4)
        self.assertNotIn("enablePiiRedaction", validated["input"])

        request["input"]["scenarioId"] = "another-scenario"
        with self.assertRaises(common.AuthorizationError):
            common.validate_job_request(request)

    def test_meeting_request_requires_an_explicit_audio_upload(self):
        request = {
            "action": "meeting.process",
            "clientId": BLUE_SCOPE["clientId"],
            "projectId": BLUE_SCOPE["projectId"],
            "sessionId": BLUE_SCOPE["sessionId"],
            "idempotencyKey": "meeting-process-no-upload",
            "input": {
                "scenarioId": meeting_contracts.SCENARIO_ID,
                "meetingId": meeting_contracts.DEFAULT_MEETING_ID,
                "expectedApprovedPacketVersion": 4,
            },
        }
        with self.assertRaisesRegex(ValueError, "audioUploadId"):
            common.validate_job_request(request)

    def test_meeting_dynamodb_serializer_converts_nested_floats(self):
        encoded = meeting._typed(
            {
                "confidence": 0.91,
                "segments": [{"timestampStart": 12.5}],
            }
        )
        self.assertEqual(encoded["M"]["confidence"], {"N": "0.91"})
        self.assertEqual(
            encoded["M"]["segments"]["L"][0]["M"]["timestampStart"],
            {"N": "12.5"},
        )

    def test_transcribe_uses_a_scoped_full_private_output_object(self):
        self.assertEqual(
            meeting._transcript_output_key("pillarprep-job-123"),
            "transcripts/public-demo/blue-mesa-payments/full-pillarprep-job-123.json",
        )

    def test_start_transcription_persists_full_private_pointer(self):
        captured = {}

        class FakeS3:
            def head_object(self, **_kwargs):
                return {}

        class FakeDynamo:
            def put_item(self, **kwargs):
                captured["continuation"] = kwargs["Item"]

        class FakeTranscribe:
            def start_transcription_job(self, **kwargs):
                captured["request"] = kwargs

        clients = {
            "s3": FakeS3(),
            "dynamodb": FakeDynamo(),
            "transcribe": FakeTranscribe(),
        }
        document = {
            "input": {
                "scenarioId": "blue-mesa-payments",
                "meetingId": "blue-mesa-discovery",
                "audioUploadId": "upload-meeting-0001",
                "expectedApprovedPacketVersion": 4,
            }
        }
        with (
            patch.object(meeting, "LIVE_AI_ENABLED", True),
            patch.object(meeting, "MEETING_EVIDENCE_BUCKET", "evidence-bucket"),
            patch.object(meeting, "PROJECT_TABLE", "project-state"),
            patch.object(
                meeting, "aws_client", side_effect=lambda name: clients[name]
            ),
            patch.object(
                meeting,
                "_resolve_audio_upload",
                return_value=(
                    "upload-meeting-0001",
                    "audio/uploads/private/meeting.mp3",
                    "mp3",
                ),
            ),
            patch.object(meeting, "_status_job"),
        ):
            meeting.start_transcription(
                BLUE_SCOPE,
                document,
                job_id="job-123",
                input_key="jobs/input.json",
                input_version="input-v1",
                trace_id="trace-123",
                approved_packet_version=4,
            )

        continuation = captured["continuation"]
        self.assertEqual(continuation["action"]["S"], "meeting.process")
        self.assertEqual(
            continuation["transcriptMode"]["S"], "full-private"
        )
        self.assertTrue(
            captured["request"]["OutputKey"].endswith(
                "/full-pillarprep-job-123.json"
            )
        )
        self.assertNotIn("ContentRedaction", captured["request"])
        self.assertEqual(
            captured["request"]["Settings"]["MaxSpeakerLabels"],
            6,
        )

    def test_human_review_promotes_agent_analysis_without_second_model_call(self):
        promotion_idempotency = common.stable_identifier(
            "meeting-promotion", ["approval-request"], length=40
        )
        self.assertLessEqual(len(promotion_idempotency), 64)
        self.assertRegex(promotion_idempotency, r"^[a-z0-9-]+$")

        base = {
            "provider": "agentcore",
            "projectAnswer": "Pre-call handoff",
            "projectArtifacts": {
                "twoWeekPlan": [
                    {
                        "title": "Days 1-2",
                        "detail": "Validate the integration.",
                        "owner": "SA",
                        "status": "Open",
                    }
                ],
                "riskRegister": [
                    {
                        "title": "Availability",
                        "detail": "Validate RTO.",
                        "owner": "SA",
                        "status": "Open",
                    }
                ],
                "stakeholderMap": [
                    {
                        "title": "Dev Malik",
                        "detail": "Technical sponsor.",
                        "owner": "AE",
                        "status": "Engage",
                    }
                ],
                "followUpEmail": {
                    "subject": "Pre-call",
                    "body": "Prepare for the customer call.",
                },
                "nextSteps": {
                    "immediateActions": [],
                    "openQuestions": ["What is the target RTO?"],
                    "nextMeeting": {
                        "purpose": "Discovery",
                        "timing": "Week 2",
                        "attendees": ["Dev Malik"],
                    },
                    "customerSummary": "Blue Mesa needs payroll integration.",
                    "internalNotes": "Validate the integration boundary.",
                },
            },
            "citations": ["Latest approved PilarPrep brief"],
            "evidence": [],
            "metadata": {"approvedPacketVersion": 4},
        }
        proposal = {
            "meetingId": "blue-mesa-discovery",
            "analysis": {
                "meetingSummary": (
                    "Blue Mesa confirmed payroll partner integration on AWS."
                ),
                "proposedHandoffSummary": (
                    "Validate interfaces and reach the partner-certification gate."
                ),
            },
        }
        accepted = [
            {
                "category": "actions",
                "proposedUpdate": (
                    "Dev will provide the current integration diagram."
                ),
                "speaker": "Dev Malik",
                "timestampStart": 64,
                "owner": "Dev Malik",
                "targetDate": "Tuesday",
                "dependency": "Current account inventory",
            }
        ]
        promoted = handoff_promotion.promote_handoff(
            base,
            proposal,
            accepted,
            company="BlueMesa Payments",
            packet_version=4,
        )

        self.assertEqual(promoted["provider"], "agentcore")
        self.assertFalse(promoted["metadata"]["modelInvokedForApproval"])
        self.assertEqual(
            promoted["metadata"]["handoffAssembly"],
            "human-approved-meeting-promotion",
        )
        self.assertIn("Dev will provide", promoted["projectAnswer"])
        self.assertEqual(
            promoted["projectArtifacts"]["nextSteps"][
                "immediateActions"
            ][0]["owner"],
            "Dev Malik",
        )

    def test_meeting_approval_does_not_invoke_agentcore_again(self):
        proposal = {
            "proposalId": "proposal-fast",
            "meetingId": "blue-mesa-discovery",
            "analysis": {"meetingSummary": "Approved meeting summary"},
        }
        accepted = [
            {"id": "action-one", "proposedUpdate": "Provide evidence"}
        ]
        handoff = {
            "provider": "agentcore",
            "projectAnswer": "Promoted handoff",
            "metadata": {"modelInvokedForApproval": False},
        }
        with (
            patch.object(
                worker,
                "_approved_document",
                return_value=(
                    {"approvedPacketVersion": 4},
                    {"response": {"projectAnswer": "Approved"}},
                ),
            ),
            patch.object(
                meeting,
                "review_proposal",
                return_value=(proposal, accepted, []),
            ),
            patch.object(
                worker,
                "_promote_approved_meeting",
                return_value=handoff,
            ) as promote,
            patch.object(
                meeting,
                "finalize_approval",
                return_value=handoff,
            ),
            patch.object(
                worker,
                "_run_agent",
                side_effect=AssertionError("second model call is forbidden"),
            ),
        ):
            result = worker._approve_meeting(
                BLUE_SCOPE,
                {"idempotencyKey": "meeting-fast-path"},
            )

        self.assertEqual(result["projectAnswer"], "Promoted handoff")
        promote.assert_called_once()

    def test_analysis_requires_payroll_and_preserves_existing_aws_state(self):
        transcript = {
            "text": (
                "Payroll providers require integration and Blue Mesa already runs on AWS. "
                "The prior migration from on-prem assumption is incorrect. "
                "Dev will provide the current integration diagram by Tuesday. "
                "Priya will document reconciliation cutoffs by Thursday."
            ),
            "durationSeconds": 120,
        }

        def item(item_id, statement, evidence):
            return {
                "id": item_id,
                "statement": statement,
                "status": "confirmed",
                "speaker": "Dev Malik",
                "timestampStart": 10,
                "timestampEnd": 20,
                "evidenceText": evidence,
                "confidence": 0.98,
                "sourceType": "transcript",
            }

        evidence = (
            "Payroll providers require integration and Blue Mesa already runs on AWS."
        )
        analysis = {
            "meetingSummary": (
                "Blue Mesa confirmed payroll integration on its existing AWS estate."
            ),
            "proposedHandoffSummary": (
                "Validate the payroll interface and preserve the current AWS boundary."
            ),
            "citations": ["Transcript 00:10-00:20"],
            "confirmedFacts": [],
            "correctedAssumptions": [
                {
                    **item(
                        "correction-aws",
                        "Blue Mesa already operates the relevant platform on AWS.",
                        evidence,
                    ),
                    "previousAssumption": "The customer must migrate from on-prem.",
                    "meetingCorrection": (
                        "The customer already runs the relevant workloads on AWS."
                    ),
                    "affectedBriefSections": ["businessCase", "technical"],
                }
            ],
            "decisions": [],
            "openQuestions": [
                {
                    **item(
                        "prebrief-question",
                        "What security controls should be reviewed?",
                        "This question came from the approved prebrief.",
                    ),
                    "sourceType": "approved brief",
                }
            ],
            "requirements": [
                item(
                    "requirement-payroll",
                    "Integrate with payroll providers.",
                    evidence,
                )
            ],
            "risks": [],
            "scopeChanges": [],
            "actions": [
                {
                    **item(
                        "action-diagram",
                        "Dev will provide the current integration diagram by Tuesday.",
                        "Dev will provide the current integration diagram by Tuesday.",
                    ),
                    "owner": "Dev Malik",
                    "targetDate": "Tuesday",
                    "dependency": "Current account inventory",
                },
                {
                    **item(
                        "action-reconciliation",
                        "Priya will document reconciliation cutoffs by Thursday.",
                        "Priya will document reconciliation cutoffs by Thursday.",
                    ),
                    "owner": "Priya Shah",
                    "targetDate": "Thursday",
                    "dependency": "Synthetic partner file",
                },
            ],
            "stakeholderSignals": [],
        }
        validated = meeting_contracts.validate_analysis(analysis, transcript)
        self.assertIn(
            "already operates",
            validated["correctedAssumptions"][0]["statement"],
        )
        self.assertEqual(
            validated["correctedAssumptions"][0]["status"], "corrected"
        )
        self.assertEqual(validated["requirements"][0]["status"], "new")
        self.assertEqual(len(validated["actions"]), 2)

        one_action = json.loads(json.dumps(analysis))
        one_action["actions"] = one_action["actions"][:1]
        with self.assertRaisesRegex(ValueError, "at least two"):
            meeting_contracts.validate_analysis(one_action, transcript)

        missing_owner = json.loads(json.dumps(analysis))
        missing_owner["actions"][1]["owner"] = "Unassigned"
        with self.assertRaisesRegex(ValueError, "named action owners"):
            meeting_contracts.validate_analysis(missing_owner, transcript)

        self.assertIn("payroll", validated["requirements"][0]["statement"].lower())
        self.assertEqual(validated["openQuestions"], [])

        negated_correction = json.loads(json.dumps(analysis))
        negated_correction["meetingSummary"] = (
            "Blue Mesa is already on AWS and does not need to migrate from on-premises."
        )
        negated_correction["correctedAssumptions"][0]["evidenceText"] = (
            "The prior migration from on-prem assumption is incorrect."
        )
        negated_correction["correctedAssumptions"][0]["meetingCorrection"] = (
            "No initial AWS migration is required."
        )
        meeting_contracts.validate_analysis(negated_correction, transcript)

        explicit_correction = json.loads(json.dumps(analysis))
        explicit_correction["correctedAssumptions"][0]["statement"] = (
            "The prior migration from on-premises assumption is obsolete."
        )
        meeting_contracts.validate_analysis(explicit_correction, transcript)

        stale_correction_statement = json.loads(json.dumps(analysis))
        stale_correction_statement["correctedAssumptions"][0]["statement"] = (
            "Migrate from on-premises before adding the payroll integration."
        )
        normalized_correction = meeting_contracts.validate_analysis(
            stale_correction_statement, transcript
        )
        self.assertEqual(
            normalized_correction["correctedAssumptions"][0]["statement"],
            normalized_correction["correctedAssumptions"][0][
                "meetingCorrection"
            ],
        )

        timestamp_bound = json.loads(json.dumps(analysis))
        timestamp_bound["requirements"][0]["evidenceText"] = (
            "The payroll connection is a required deliverable."
        )
        transcript_with_segments = {
            **transcript,
            "segments": [
                {
                    "speaker": "Dev Malik",
                    "timestampStart": 10,
                    "timestampEnd": 20,
                    "text": evidence,
                }
            ],
        }
        canonical = meeting_contracts.validate_analysis(
            timestamp_bound, transcript_with_segments
        )
        self.assertEqual(
            canonical["requirements"][0]["evidenceText"], evidence
        )

        wrong_timestamp = json.loads(json.dumps(timestamp_bound))
        wrong_timestamp["requirements"][0]["timestampStart"] = 40
        wrong_timestamp["requirements"][0]["timestampEnd"] = 50
        transcript_with_wrong_window = {
            **transcript,
            "segments": [
                {
                    "speaker": "Taylor Brooks",
                    "timestampStart": 40,
                    "timestampEnd": 50,
                    "text": "The team will schedule a follow-up architecture review.",
                },
                {
                    "speaker": "Dev Malik",
                    "timestampStart": 70,
                    "timestampEnd": 80,
                    "text": evidence,
                },
            ],
        }
        rebound = meeting_contracts.validate_analysis(
            wrong_timestamp, transcript_with_wrong_window
        )
        self.assertEqual(
            rebound["requirements"][0]["evidenceText"], evidence
        )
        self.assertEqual(rebound["requirements"][0]["speaker"], "Dev Malik")
        self.assertEqual(rebound["requirements"][0]["timestampStart"], 70)
        self.assertEqual(rebound["requirements"][0]["timestampEnd"], 80)

        contradictory = json.loads(json.dumps(analysis))
        contradictory["requirements"][0]["statement"] = (
            "Migrate from on-prem before integrating payroll."
        )
        with self.assertRaisesRegex(
            ValueError, r"existing-on-AWS.*requirements\[0\]\.statement"
        ):
            meeting_contracts.validate_analysis(contradictory, transcript)

    def test_analysis_rejects_evidence_not_supported_by_the_transcript(self):
        transcript = {
            "text": "Payroll integration is required and Blue Mesa already runs on AWS.",
            "durationSeconds": 120,
        }
        unsupported = {
            "meetingSummary": "Payroll integration is in scope.",
            "proposedHandoffSummary": "Validate payroll integration.",
            "citations": ["Transcript"],
            **{field: [] for field in meeting_contracts.ANALYSIS_LIST_FIELDS},
        }
        unsupported["requirements"] = [
            {
                "id": "unsupported-requirement",
                "statement": "Integrate payroll.",
                "status": "confirmed",
                "speaker": "Dev Malik",
                "timestampStart": 10,
                "timestampEnd": 20,
                "evidenceText": "The customer committed to a multi-region active-active launch.",
                "confidence": 0.9,
                "sourceType": "transcript",
            }
        ]
        with self.assertRaisesRegex(ValueError, "not supported"):
            meeting_contracts.validate_analysis(unsupported, transcript)

    def test_every_proposed_change_must_be_reviewed(self):
        proposal = {
            "reviewItems": [
                {"id": "change-one", "proposedUpdate": "Use payroll APIs."},
                {"id": "change-two", "proposedUpdate": "Keep RTO open."},
            ]
        }
        with self.assertRaisesRegex(
            meeting_contracts.MeetingConflictError, "Every proposed change"
        ):
            meeting_contracts.accepted_changes(
                proposal,
                [{"id": "change-one", "decision": "accepted"}],
                "2026-08-20T00:00:00Z",
            )

        accepted, rejected = meeting_contracts.accepted_changes(
            proposal,
            [
                {"id": "change-one", "decision": "edited", "editedStatement": "Use bounded payroll APIs."},
                {"id": "change-two", "decision": "rejected"},
            ],
            "2026-08-20T00:00:00Z",
        )
        self.assertEqual(accepted[0]["proposedUpdate"], "Use bounded payroll APIs.")
        self.assertEqual(rejected[0]["decision"], "rejected")

    def test_stale_meeting_proposal_cannot_be_approved(self):
        document = {
            "input": {
                "scenarioId": meeting_contracts.SCENARIO_ID,
                "meetingId": meeting_contracts.DEFAULT_MEETING_ID,
                "proposalId": "proposal-0001",
                "expectedApprovedPacketVersion": 3,
                "dispositions": [{"id": "change-one", "decision": "accepted"}],
            }
        }
        with self.assertRaisesRegex(
            meeting_contracts.MeetingConflictError, "brief changed"
        ):
            meeting.review_proposal(
                BLUE_SCOPE, document, current_approved_version=4
            )

    def test_approved_notes_include_only_human_accepted_evidence(self):
        proposal = {
            "scenarioId": meeting_contracts.SCENARIO_ID,
            "meetingId": meeting_contracts.DEFAULT_MEETING_ID,
            "analysis": {"meetingSummary": "Payroll discovery clarified the AWS integration."},
        }
        notes = meeting.approved_meeting_notes(
            proposal,
            [
                {
                    "category": "requirement",
                    "proposedUpdate": "Integrate payroll providers through APIs and encrypted files.",
                    "speaker": "Dev Malik",
                    "timestampStart": 64,
                    "evidenceText": "Payroll integration is the primary objective.",
                    "owner": "Platform Engineering",
                }
            ],
        )
        self.assertIn("Payroll", notes)
        self.assertIn("01:04", notes)
        self.assertIn("Only the changes listed above were approved", notes)

    def test_finalize_approval_persists_latest_and_immutable_audit(self):
        calls = {"objects": [], "transactions": []}

        class FakeS3:
            def put_object(self, **kwargs):
                calls["objects"].append(kwargs)
                return {}

        class FakeDynamo:
            def get_item(self, **_kwargs):
                return {}

            def transact_write_items(self, **kwargs):
                calls["transactions"].append(kwargs)
                return {}

        clients = {"s3": FakeS3(), "dynamodb": FakeDynamo()}
        proposal = {
            "proposalId": "proposal-0001",
            "scenarioId": meeting_contracts.SCENARIO_ID,
            "meetingId": meeting_contracts.DEFAULT_MEETING_ID,
            "baseBriefVersion": 4,
            "createdAt": "2026-08-20T00:00:00Z",
            "traceId": "trace-0001",
        }
        accepted = [
            {
                "id": "change-one",
                "category": "requirement",
                "proposedUpdate": "Payroll integration is in scope.",
                "decision": "accepted",
                "timestampStart": Decimal("64.2"),
                "confidence": Decimal("0.91"),
            }
        ]
        rejected = [
            {
                "id": "change-two",
                "category": "risk",
                "proposedUpdate": "Invent an RTO.",
                "decision": "rejected",
                "confidence": Decimal("0.42"),
            }
        ]
        handoff = {
            "provider": "agentcore",
            "projectAnswer": "Approved payroll handoff",
            "metadata": {"artifactKey": "handoff/latest.json"},
        }
        with (
            patch.object(meeting, "ARTIFACT_BUCKET", "private-artifacts"),
            patch.object(meeting, "PROJECT_TABLE", "project-state"),
            patch.object(meeting, "aws_client", side_effect=lambda name: clients[name]),
        ):
            result = meeting.finalize_approval(
                BLUE_SCOPE,
                {"input": {"scenarioId": meeting_contracts.SCENARIO_ID}},
                proposal,
                accepted,
                rejected,
                handoff,
            )

        self.assertEqual(result["meetingApproval"]["acceptedCount"], 1)
        self.assertEqual(result["meetingApproval"]["rejectedCount"], 1)
        self.assertEqual(len(calls["objects"]), 2)
        self.assertTrue(
            any(item["Key"].endswith("/latest.json") for item in calls["objects"])
        )
        immutable = next(
            item for item in calls["objects"] if not item["Key"].endswith("/latest.json")
        )
        stored = json.loads(immutable["Body"].decode("utf-8"))
        self.assertEqual(stored["rejectedChanges"][0]["id"], "change-two")
        self.assertEqual(stored["acceptedChanges"][0]["timestampStart"], 64.2)
        self.assertEqual(stored["acceptedChanges"][0]["confidence"], 0.91)
        self.assertEqual(stored["rejectedChanges"][0]["confidence"], 0.42)
        self.assertEqual(len(calls["transactions"][0]["TransactItems"]), 3)

    def test_finalize_approval_supersedes_the_previous_approved_meeting(self):
        calls = {"objects": [], "transactions": []}
        previous_approval_id = "meeting-approval-previous"

        class FakeS3:
            def put_object(self, **kwargs):
                calls["objects"].append(kwargs)
                return {}

        class FakeDynamo:
            def get_item(self, **_kwargs):
                return {
                    "Item": {
                        "projectId": {"S": common.project_partition_key(BLUE_SCOPE)},
                        "sortKey": {"S": "MEETING#blue-mesa-discovery#LATEST"},
                        "approvalId": {"S": previous_approval_id},
                        "status": {"S": "approved"},
                    }
                }

            def transact_write_items(self, **kwargs):
                calls["transactions"].append(kwargs)
                return {}

        proposal = {
            "proposalId": "proposal-0002",
            "scenarioId": meeting_contracts.SCENARIO_ID,
            "meetingId": meeting_contracts.DEFAULT_MEETING_ID,
            "baseBriefVersion": 5,
            "createdAt": "2026-08-20T01:00:00Z",
            "traceId": "trace-0002",
        }
        accepted = [
            {
                "id": "change-one",
                "category": "requirement",
                "proposedUpdate": "Payroll integration remains in scope.",
                "decision": "accepted",
            }
        ]
        handoff = {
            "provider": "agentcore",
            "projectAnswer": "Updated payroll handoff",
            "metadata": {"artifactKey": "handoff/latest.json"},
        }
        clients = {"s3": FakeS3(), "dynamodb": FakeDynamo()}
        with (
            patch.object(meeting, "ARTIFACT_BUCKET", "private-artifacts"),
            patch.object(meeting, "PROJECT_TABLE", "project-state"),
            patch.object(meeting, "aws_client", side_effect=lambda name: clients[name]),
        ):
            result = meeting.finalize_approval(
                BLUE_SCOPE,
                {"input": {"scenarioId": meeting_contracts.SCENARIO_ID}},
                proposal,
                accepted,
                [],
                handoff,
            )

        self.assertEqual(
            result["meetingApproval"]["supersedesApprovalId"],
            previous_approval_id,
        )
        transaction = calls["transactions"][0]["TransactItems"]
        self.assertEqual(len(transaction), 4)
        supersede = transaction[1]["Update"]
        self.assertEqual(
            supersede["Key"]["sortKey"]["S"],
            f"MEETING#{meeting_contracts.DEFAULT_MEETING_ID}#APPROVED#{previous_approval_id}",
        )
        self.assertIn("supersededBy", supersede["UpdateExpression"])
        latest_put = transaction[-1]["Put"]
        self.assertIn("previousApprovalId", latest_put["ConditionExpression"])

    def test_uploaded_audio_is_validated_against_session_and_s3_size(self):
        updates = []
        upload_id = "11111111-2222-3333-4444-555555555555"
        object_key = (
            f"audio/uploads/{BLUE_SCOPE['tenantId']}/{BLUE_SCOPE['clientId']}/"
            f"{BLUE_SCOPE['projectId']}/{upload_id}/meeting.mp3"
        )

        class FakeDynamo:
            def get_item(self, **_kwargs):
                return {
                    "Item": {
                        "ownerId": {"S": BLUE_SCOPE["userId"]},
                        "sessionId": {"S": BLUE_SCOPE["sessionId"]},
                        "tenantId": {"S": BLUE_SCOPE["tenantId"]},
                        "clientId": {"S": BLUE_SCOPE["clientId"]},
                        "projectScopeId": {"S": BLUE_SCOPE["projectId"]},
                        "uploadId": {"S": upload_id},
                        "scenarioId": {"S": meeting_contracts.SCENARIO_ID},
                        "meetingId": {"S": meeting_contracts.DEFAULT_MEETING_ID},
                        "objectKey": {"S": object_key},
                        "mediaFormat": {"S": "mp3"},
                        "expectedSizeBytes": {"N": "4096"},
                        "status": {"S": "clean"},
                        "scanBucketName": {"S": "private-meeting-audio"},
                        "scanVersionId": {"S": "version-1"},
                        "scanETag": {"S": "etag-1"},
                        "scanResultStatus": {"S": "NO_THREATS_FOUND"},
                        "scanTagVerified": {"BOOL": True},
                    }
                }

            def update_item(self, **kwargs):
                updates.append(kwargs)
                return {}

        class FakeS3:
            def get_object_attributes(self, **_kwargs):
                return {
                    "VersionId": "version-1",
                    "ETag": "etag-1",
                    "ObjectSize": 4096,
                }

            def get_object_tagging(self, **_kwargs):
                return {
                    "TagSet": [
                        {
                            "Key": "GuardDutyMalwareScanStatus",
                            "Value": "NO_THREATS_FOUND",
                        }
                    ]
                }

        clients = {"dynamodb": FakeDynamo(), "s3": FakeS3()}
        with (
            patch.object(meeting, "PROJECT_TABLE", "project-state"),
            patch.object(meeting, "MEETING_EVIDENCE_BUCKET", "private-meeting-audio"),
            patch.object(meeting, "aws_client", side_effect=lambda name: clients[name]),
        ):
            resolved = meeting._resolve_audio_upload(
                BLUE_SCOPE,
                {"audioUploadId": upload_id},
                meeting_contracts.SCENARIO_ID,
                meeting_contracts.DEFAULT_MEETING_ID,
                job_id="job-meeting-0001",
                input_key="jobs/meeting/input.json",
                input_version="input-0001",
                trace_id="trace-meeting-0001",
                approved_packet_version=3,
            )

        self.assertEqual(resolved, (upload_id, object_key, "mp3"))
        self.assertEqual(len(updates), 1)
        self.assertEqual(
            updates[0]["ExpressionAttributeValues"][":processing"]["S"],
            "processing",
        )

class ContentSafetyTests(unittest.TestCase):
    def test_guardrail_intervention_returns_actionable_custom_input_guidance(self):
        with (
            patch.object(
                content_safety,
                "screen_payload",
                side_effect=content_safety.GuardrailIntervention("blocked"),
            ),
            patch.object(worker, "metric"),
            self.assertRaises(worker.NonRetryableJobError) as raised,
        ):
            worker._screen_ai_payload(
                {"additionalDirection": "customer context"},
                source="INPUT",
                action="brief.generate",
                trace_id="trace-safety-guidance",
            )

        self.assertIn("Describe customer facts and desired outcomes", str(raised.exception))
        self.assertIn("without instructions to ignore, override, or reveal", str(raised.exception))

    def setUp(self):
        content_safety.clear_client_cache()

    def tearDown(self):
        content_safety.clear_client_cache()

    def _screen_with_guardrail(self, value, *, action="brief.generate", source="INPUT", response="NONE"):
        calls = []

        class Guardrail:
            def apply_guardrail(self, **kwargs):
                calls.append(kwargs)
                return {"action": response}

        with (
            patch.dict(
                content_safety.os.environ,
                {
                    "CONTENT_SAFETY_ENABLED": "true",
                    # A stale deployment variable must not restore PII detection.
                    "PII_SCREENING_ENABLED": "true",
                    "BEDROCK_GUARDRAIL_ID": "guardrail-1",
                    "BEDROCK_GUARDRAIL_VERSION": "1",
                },
                clear=False,
            ),
            patch.object(content_safety, "aws_client", return_value=Guardrail()) as client,
        ):
            screened, diagnostics = content_safety.screen_payload(
                value, source=source, action=action, trace_id="trace-context-preserved"
            )

        self.assertTrue(client.call_args_list)
        self.assertTrue(all(call.args == ("bedrock-runtime",) for call in client.call_args_list))
        return screened, diagnostics, calls

    def test_all_workflows_preserve_context_without_comprehend(self):
        payload = {
            "clientId": "blue-mesa-payments",
            "notes": "Dev Malik owns payroll integration; contact dev@example.com.",
            "sections": [{"owner": "Alice", "reference": "Synthetic account 123456789"}],
            "scope": {"tenantId": "demo", "userId": "test-user"},
        }
        actions = (
            "brief.generate", "brief.refine", "handoff.generate", "catchup.generate",
            "generate_handoff", "generate_catchup", "meeting.process",
            "meeting.approve", "analyze_meeting",
        )
        for action in actions:
            for source in ("INPUT", "OUTPUT"):
                with self.subTest(action=action, source=source):
                    screened, diagnostics, calls = self._screen_with_guardrail(
                        payload, action=action, source=source
                    )
                    self.assertIs(screened, payload)
                    self.assertEqual(diagnostics["redactionCount"], 0)
                    self.assertEqual(diagnostics["piiTypes"], [])
                    self.assertEqual(diagnostics["comprehendChunks"], 0)
                    self.assertEqual(diagnostics["policyResult"], "passed")
                    self.assertEqual(calls[0]["source"], source)
                    self.assertIn(payload["notes"], calls[0]["content"][0]["text"]["text"])

    def test_private_meeting_preserves_context_and_still_applies_guardrail(self):
        payload = {"notes": "Dev Malik can be reached at dev@example.com."}
        screened, diagnostics, calls = self._screen_with_guardrail(
            payload, action="meeting.process"
        )
        self.assertIs(screened, payload)
        self.assertEqual(diagnostics["piiMode"], "preserved-private-context")
        self.assertEqual(diagnostics["redactionCount"], 0)
        self.assertEqual(diagnostics["comprehendChunks"], 0)
        self.assertIn(payload["notes"], calls[0]["content"][0]["text"]["text"])

    def test_structured_payload_guardrails_remain_bounded_and_complete(self):
        sections = [
            {"summary": f"Section {index} owner Alice can be reached at alice@example.com for approved follow-up."}
            for index in range(160)
        ]
        payload = {"clientId": "blue-mesa-payments", "sections": sections}
        screened, diagnostics, calls = self._screen_with_guardrail(payload)
        documents = [call["content"][0]["text"]["text"] for call in calls]
        self.assertIs(screened, payload)
        self.assertGreater(len(documents), 1)
        self.assertEqual(diagnostics["guardrailChunks"], len(documents))
        self.assertTrue(all(len(document) <= content_safety.MAX_GUARDRAIL_CHARS for document in documents))
        for section in sections:
            self.assertIn(section["summary"], "\n".join(documents))
        self.assertEqual(diagnostics["redactionCount"], 0)
        self.assertEqual(diagnostics["piiMode"], "disabled")

    def test_guardrail_intervention_remains_fail_closed(self):
        for source in ("INPUT", "OUTPUT"):
            with self.subTest(source=source), self.assertRaises(content_safety.GuardrailIntervention):
                self._screen_with_guardrail(
                    {"notes": "unsafe content"}, source=source, response="GUARDRAIL_INTERVENED"
                )

    def test_unknown_guardrail_response_is_rejected(self):
        with self.assertRaises(content_safety.ContentSafetyError):
            self._screen_with_guardrail({"notes": "customer context"}, response="UNKNOWN")

    def test_enabled_content_checks_require_guardrail_configuration(self):
        with (
            patch.dict(content_safety.os.environ, {
                "CONTENT_SAFETY_ENABLED": "true",
                "BEDROCK_GUARDRAIL_ID": "",
                "BEDROCK_GUARDRAIL_VERSION": "",
            }),
            patch.object(content_safety, "aws_client") as client,
            self.assertRaises(content_safety.ContentSafetyConfigurationError),
        ):
            content_safety.screen_payload({"notes": "customer context"}, source="INPUT", action="brief.generate")
        client.assert_not_called()

    def test_disabled_checks_do_not_call_any_service(self):
        payload = {"notes": "Contact Alice at alice@example.com."}
        with (
            patch.dict(content_safety.os.environ, {
                "CONTENT_SAFETY_ENABLED": "false", "PII_SCREENING_ENABLED": "true"
            }),
            patch.object(content_safety, "aws_client") as client,
        ):
            screened, diagnostics = content_safety.screen_payload(
                payload, source="INPUT", action="brief.generate"
            )
        self.assertIs(screened, payload)
        self.assertEqual(diagnostics["piiMode"], "disabled")
        self.assertEqual(diagnostics["policyResult"], "disabled")
        client.assert_not_called()


class AudioSecurityTests(unittest.TestCase):
    upload_id = "11111111-2222-3333-4444-555555555555"
    version_id = "version-1"
    etag = "etag-1"

    def _object_key(self):
        return (
            f"audio/uploads/{BLUE_SCOPE['tenantId']}/{BLUE_SCOPE['clientId']}/"
            f"{BLUE_SCOPE['projectId']}/{self.upload_id}/meeting.mp3"
        )

    def _upload(self, status="pending_scan"):
        return {
            **BLUE_SCOPE,
            "projectScopeId": BLUE_SCOPE["projectId"],
            "ownerId": BLUE_SCOPE["userId"],
            "uploadId": self.upload_id,
            "scenarioId": meeting_contracts.SCENARIO_ID,
            "meetingId": meeting_contracts.DEFAULT_MEETING_ID,
            "objectKey": self._object_key(),
            "mediaFormat": "mp3",
            "expectedSizeBytes": 4096,
            "status": status,
        }

    def _event(self, result="NO_THREATS_FOUND", scan_status="COMPLETED"):
        return {
            "version": "0",
            "id": f"event-{result.lower().replace('_', '-')}",
            "source": "aws.guardduty",
            "account": "123456789012",
            "region": "us-east-1",
            "detail-type": "GuardDuty Malware Protection Object Scan Result",
            "detail": {
                "scanStatus": scan_status,
                "resourceType": "S3_OBJECT",
                "s3ObjectDetails": {
                    "bucketName": "meeting-evidence",
                    "objectKey": self._object_key(),
                    "eTag": self.etag,
                    "versionId": self.version_id,
                },
                "scanResultDetails": {"scanResultStatus": result},
            },
        }

    def test_guardduty_results_fail_closed_except_verified_clean(self):
        cases = [
            ("NO_THREATS_FOUND", "COMPLETED", "NO_THREATS_FOUND", "clean"),
            ("THREATS_FOUND", "COMPLETED", "THREATS_FOUND", "blocked"),
            ("UNSUPPORTED", "COMPLETED", "", "scan_failed"),
            ("ACCESS_DENIED", "COMPLETED", "", "scan_failed"),
            ("FAILED", "FAILED", "", "scan_failed"),
            ("UNSUPPORTED", "SKIPPED", "", "scan_failed"),
        ]

        class Dynamo:
            def transact_write_items(self, **_kwargs):
                return {}

        for result, scan_status, managed_tag, expected in cases:
            with self.subTest(result=result, scan_status=scan_status):
                pending = self._upload()
                resolved = {
                    **pending,
                    "status": expected,
                    "scanTagVerified": expected in {"clean", "blocked"},
                    "scanResultStatus": result,
                }
                with (
                    patch.dict(
                        meeting.os.environ,
                        {
                            "EXPECTED_AWS_ACCOUNT_ID": "123456789012",
                            "AWS_REGION": "us-east-1",
                        },
                        clear=False,
                    ),
                    patch.object(
                        meeting, "MEETING_EVIDENCE_BUCKET", "meeting-evidence"
                    ),
                    patch.object(meeting, "PROJECT_TABLE", "project-state"),
                    patch.object(
                        meeting,
                        "_load_audio_upload",
                        side_effect=[pending, resolved],
                    ),
                    patch.object(
                        meeting,
                        "_object_identity",
                        return_value=(self.version_id, self.etag, 4096),
                    ) as identity,
                    patch.object(meeting, "_scan_tag", return_value=managed_tag),
                    patch.object(meeting, "aws_client", return_value=Dynamo()),
                ):
                    outcome = meeting.handle_guardduty_scan_event(
                        self._event(result, scan_status),
                        final_attempt=True,
                    )
                self.assertEqual(outcome["outcome"], expected)
                identity.assert_called_once_with(self._object_key(), version_id="")

    def test_pending_upload_reconciles_only_verified_clean_managed_tag(self):
        updates = []

        class Dynamo:
            def update_item(self, **kwargs):
                updates.append(kwargs)
                return {}

        with (
            patch.object(meeting, "MEETING_EVIDENCE_BUCKET", "meeting-evidence"),
            patch.object(meeting, "PROJECT_TABLE", "project-state"),
            patch.object(
                meeting,
                "_object_identity",
                return_value=(self.version_id, self.etag, 4096),
            ),
            patch.object(meeting, "_scan_tag", return_value="NO_THREATS_FOUND"),
            patch.object(meeting, "aws_client", return_value=Dynamo()),
        ):
            reconciled = meeting._reconcile_verified_clean_scan(
                BLUE_SCOPE, self.upload_id, self._upload()
            )

        self.assertTrue(reconciled)
        values = updates[0]["ExpressionAttributeValues"]
        self.assertEqual(values[":cleanResult"]["S"], "NO_THREATS_FOUND")
        self.assertEqual(values[":versionId"]["S"], self.version_id)
        self.assertTrue(values[":verified"]["BOOL"])

    def test_pending_upload_does_not_reconcile_a_threat_tag(self):
        with (
            patch.object(
                meeting,
                "_object_identity",
                return_value=(self.version_id, self.etag, 4096),
            ),
            patch.object(meeting, "_scan_tag", return_value="THREATS_FOUND"),
            patch.object(meeting, "aws_client") as client,
        ):
            reconciled = meeting._reconcile_verified_clean_scan(
                BLUE_SCOPE, self.upload_id, self._upload()
            )

        self.assertFalse(reconciled)
        client.assert_not_called()
    def test_clean_audio_verifies_current_identity_then_scanned_version_tag(self):
        clean = {
            **self._upload(status="clean"),
            "scanVersionId": self.version_id,
            "scanETag": self.etag,
            "scanTagVerified": True,
        }
        with (
            patch.object(
                meeting,
                "_object_identity",
                return_value=(self.version_id, self.etag, 4096),
            ) as identity,
            patch.object(
                meeting,
                "_scan_tag",
                return_value="NO_THREATS_FOUND",
            ) as scan_tag,
        ):
            meeting._verify_clean_audio(clean)

        identity.assert_called_once_with(self._object_key(), version_id="")
        scan_tag.assert_called_once_with(self._object_key(), self.version_id)
    def test_spoofed_guardduty_bucket_is_rejected(self):
        event = self._event()
        event["detail"]["s3ObjectDetails"]["bucketName"] = "attacker-bucket"
        with (
            patch.dict(
                meeting.os.environ,
                {
                    "EXPECTED_AWS_ACCOUNT_ID": "123456789012",
                    "AWS_REGION": "us-east-1",
                },
                clear=False,
            ),
            patch.object(meeting, "MEETING_EVIDENCE_BUCKET", "meeting-evidence"),
            self.assertRaises(PermissionError),
        ):
            meeting.handle_guardduty_scan_event(event)

    def test_clean_scan_does_not_start_without_human_process_request(self):
        class MeetingModule:
            MeetingConflictError = meeting_contracts.MeetingConflictError

            @staticmethod
            def handle_guardduty_scan_event(_event, **_kwargs):
                return {"outcome": "clean", "duplicate": False}

            @staticmethod
            def claim_waiting_scan_process(_result):
                return None

            @staticmethod
            def start_transcription(*_args, **_kwargs):
                raise AssertionError("Clean scan must not start Transcribe by itself")

        with patch.object(worker, "_meeting_module", return_value=MeetingModule):
            worker._process_guardduty_record(
                {"attributes": {"ApproximateReceiveCount": "1"}},
                self._event(),
            )

    def test_clean_scan_resumes_one_waiting_human_request(self):
        calls = []
        pointer = {
            **BLUE_SCOPE,
            "jobId": "job-meeting-0001",
            "action": "meeting.process",
            "inputKey": "jobs/meeting/input.json",
            "inputVersion": "input-1",
            "traceId": "trace-meeting-1",
        }

        class MeetingModule:
            MeetingConflictError = meeting_contracts.MeetingConflictError

            @staticmethod
            def handle_guardduty_scan_event(_event, **_kwargs):
                return {"outcome": "clean", "duplicate": False}

            @staticmethod
            def claim_waiting_scan_process(_result):
                return BLUE_SCOPE, pointer

            @staticmethod
            def start_transcription(*args, **kwargs):
                calls.append((args, kwargs))
                return {"transcriptionJobName": "pillarprep-job-meeting-0001"}

        with (
            patch.object(worker, "_meeting_module", return_value=MeetingModule),
            patch.object(
                worker,
                "_load_input",
                return_value={"action": "meeting.process", "input": {}},
            ),
            patch.object(
                worker,
                "_approved_document",
                return_value=({"approvedPacketVersion": 4}, {}),
            ),
        ):
            worker._process_guardduty_record(
                {"attributes": {"ApproximateReceiveCount": "1"}},
                self._event(),
            )
        self.assertEqual(len(calls), 1)

    def test_client_cannot_override_server_transcript_policy(self):
        request = {
            "action": "meeting.process",
            "clientId": BLUE_SCOPE["clientId"],
            "projectId": BLUE_SCOPE["projectId"],
            "sessionId": BLUE_SCOPE["sessionId"],
            "idempotencyKey": "meeting-process-redaction",
            "input": {
                "scenarioId": meeting_contracts.SCENARIO_ID,
                "meetingId": meeting_contracts.DEFAULT_MEETING_ID,
                "audioUploadId": "upload-meeting-0001",
                "expectedApprovedPacketVersion": 4,
                "enablePiiRedaction": False,
            },
        }
        validated = common.validate_job_request(request)
        self.assertNotIn("enablePiiRedaction", validated["input"])

    def test_legacy_or_unscoped_transcript_pointer_is_rejected_before_s3_read(self):
        with self.assertRaises(PermissionError):
            meeting.read_transcript(
                {
                    "transcriptMode": "full-private",
                    "outputKey": meeting.TRANSCRIPT_PREFIX + "redacted-pillarprep-job.json",
                }
            )

class SecurityBoundaryTests(unittest.TestCase):
    def _helper(self):
        return AudioSecurityTests(methodName="runTest")

    def test_duplicate_guardduty_event_is_idempotent(self):
        helper = self._helper()
        pending = helper._upload()
        resolved = {
            **pending,
            "status": "clean",
            "scanTagVerified": True,
            "scanResultStatus": "NO_THREATS_FOUND",
        }

        class DuplicateDynamo:
            def transact_write_items(self, **_kwargs):
                raise ClientError(
                    {
                        "Error": {
                            "Code": "TransactionCanceledException",
                            "Message": "duplicate event",
                        }
                    },
                    "TransactWriteItems",
                )

        with (
            patch.dict(
                meeting.os.environ,
                {
                    "EXPECTED_AWS_ACCOUNT_ID": "123456789012",
                    "AWS_REGION": "us-east-1",
                },
                clear=False,
            ),
            patch.object(meeting, "MEETING_EVIDENCE_BUCKET", "meeting-evidence"),
            patch.object(meeting, "PROJECT_TABLE", "project-state"),
            patch.object(
                meeting,
                "_load_audio_upload",
                side_effect=[pending, resolved],
            ),
            patch.object(
                meeting,
                "_object_identity",
                return_value=(helper.version_id, helper.etag, 4096),
            ),
            patch.object(
                meeting, "_scan_tag", return_value="NO_THREATS_FOUND"
            ),
            patch.object(meeting, "aws_client", return_value=DuplicateDynamo()),
        ):
            outcome = meeting.handle_guardduty_scan_event(helper._event())

        self.assertTrue(outcome["duplicate"])
        self.assertEqual(outcome["outcome"], "clean")

    def test_spoofed_scan_account_key_scope_version_and_etag_are_rejected(self):
        helper = self._helper()
        environment = {
            "EXPECTED_AWS_ACCOUNT_ID": "123456789012",
            "AWS_REGION": "us-east-1",
        }

        account_event = helper._event()
        account_event["account"] = "999999999999"
        with (
            patch.dict(meeting.os.environ, environment, clear=False),
            patch.object(meeting, "MEETING_EVIDENCE_BUCKET", "meeting-evidence"),
            self.assertRaises(PermissionError),
        ):
            meeting.handle_guardduty_scan_event(account_event)

        key_event = helper._event()
        key_event["detail"]["s3ObjectDetails"]["objectKey"] = (
            "audio/trusted/demo-meeting.mp3"
        )
        with (
            patch.dict(meeting.os.environ, environment, clear=False),
            patch.object(meeting, "MEETING_EVIDENCE_BUCKET", "meeting-evidence"),
            self.assertRaises(PermissionError),
        ):
            meeting.handle_guardduty_scan_event(key_event)

        wrong_scope = {**helper._upload(), "tenantId": "tenant-other"}
        with (
            patch.dict(meeting.os.environ, environment, clear=False),
            patch.object(meeting, "MEETING_EVIDENCE_BUCKET", "meeting-evidence"),
            patch.object(meeting, "_load_audio_upload", return_value=wrong_scope),
            self.assertRaises(meeting_contracts.MeetingConflictError),
        ):
            meeting.handle_guardduty_scan_event(helper._event())

        for actual_identity in (
            ("other-version", helper.etag, 4096),
            (helper.version_id, "other-etag", 4096),
        ):
            with (
                self.subTest(actual_identity=actual_identity),
                patch.dict(meeting.os.environ, environment, clear=False),
                patch.object(
                    meeting, "MEETING_EVIDENCE_BUCKET", "meeting-evidence"
                ),
                patch.object(
                    meeting, "_load_audio_upload", return_value=helper._upload()
                ),
                patch.object(
                    meeting, "_object_identity", return_value=actual_identity
                ),
                self.assertRaises(PermissionError),
            ):
                meeting.handle_guardduty_scan_event(helper._event())

    def test_uploaded_audio_read_requires_current_clean_guardduty_tag(self):
        helper = self._helper()
        item = {
            **helper._upload("clean"),
            "scanVersionId": helper.version_id,
            "scanETag": helper.etag,
            "scanTagVerified": True,
            "scanResultStatus": "NO_THREATS_FOUND",
        }
        with (
            patch.object(
                meeting,
                "_object_identity",
                return_value=(helper.version_id, helper.etag, 4096),
            ),
            patch.object(meeting, "_scan_tag", return_value="THREATS_FOUND"),
            self.assertRaises(meeting_contracts.MeetingConflictError),
        ):
            meeting._verify_clean_audio(item)

    def test_output_intervention_prevents_brief_artifact_replacement(self):
        brief_module = types.SimpleNamespace(
            _validate_brief_payload=lambda _payload: None,
            _resolve_model_id=lambda _payload: "us.amazon.nova-pro-v1:0",
            _generate_brief=lambda _payload: {
                "provider": "bedrock",
                "metadata": {"fallbackUsed": False},
                "businessCase": {"summary": "unsafe result"},
            },
        )
        document = {
            "action": "brief.generate",
            "input": {"company": "Apex Mutual"},
            "inputVersion": "input-1",
        }
        with (
            patch.object(worker, "_brief_module", return_value=brief_module),
            patch.object(
                worker,
                "_screen_ai_payload",
                side_effect=[
                    (
                        {
                            "company": "Apex Mutual",
                            "mode": "prebrief",
                            "tenantId": SCOPE["tenantId"],
                            "clientId": SCOPE["clientId"],
                            "projectId": SCOPE["projectId"],
                            "_pipelineManagedPersistence": True,
                        },
                        {"policyResult": "passed"},
                    ),
                    worker.NonRetryableJobError("unsafe output"),
                ],
            ),
            patch.object(worker, "_set_job_phase"),
            patch.object(worker, "_write_brief_draft") as write,
            self.assertRaises(worker.NonRetryableJobError),
        ):
            worker._run_brief(SCOPE, document, "job-output-blocked")

        write.assert_not_called()

    def test_iac_contains_two_event_audio_and_shared_safety_controls(self):
        pipeline = (
            BACKEND_ROOT.parent / "infrastructure" / "jobs-pipeline.yaml"
        ).read_text(encoding="utf-8")
        agentcore = (
            BACKEND_ROOT.parent / "infrastructure" / "agentcore.yaml"
        ).read_text(encoding="utf-8")
        deploy_agent = (
            BACKEND_ROOT.parent / "scripts" / "deploy-agentcore.ps1"
        ).read_text(encoding="utf-8")
        worker_source = (
            BACKEND_ROOT / "ai_worker" / "handler.py"
        ).read_text(encoding="utf-8")
        agent_service = (
            BACKEND_ROOT / "agentcore" / "runtime" / "service.py"
        ).read_text(encoding="utf-8")
        agent_meeting = (
            BACKEND_ROOT / "agentcore" / "runtime" / "meeting.py"
        ).read_text(encoding="utf-8")

        self.assertIn("AWS::GuardDuty::MalwareProtectionPlan", pipeline)
        self.assertIn("ObjectPrefixes:", pipeline)
        self.assertIn("- audio/uploads/", pipeline)
        self.assertIn("Tagging:\n          Status: ENABLED", pipeline)
        self.assertIn("GuardDutyScanResultRule:", pipeline)
        self.assertIn("TranscribeCompletionRule:", pipeline)
        self.assertIn(
            "              - Sid: InspectProtectedBucket\n"
            "                Effect: Allow\n"
            "                Action: s3:ListBucket\n"
            "                Resource: !GetAtt MeetingEvidenceBucket.Arn\n"
            "              - Sid: ScanOnlyMeetingUploads",
            pipeline,
        )
        self.assertIn("aws:SourceArn: !GetAtt GuardDutyScanResultRule.Arn", pipeline)
        self.assertIn("aws:SourceArn: !GetAtt TranscribeCompletionRule.Arn", pipeline)
        self.assertIn(
            "s3:ExistingObjectTag/GuardDutyMalwareScanStatus: NO_THREATS_FOUND",
            pipeline,
        )
        self.assertNotIn("comprehend:", pipeline)
        self.assertNotIn("comprehend:", agentcore)
        self.assertIn(
            "  AgentLambdaSdkLayer:\n"
            "    Type: AWS::Lambda::LayerVersion\n"
            "    DeletionPolicy: Retain\n"
            "    UpdateReplacePolicy: Retain",
            agentcore,
        )
        self.assertNotIn("PII_SCREENING_ENABLED:", pipeline)
        self.assertNotIn("PII_SCREENING_ENABLED:", agentcore)
        self.assertIn("CONTENT_SAFETY_ENABLED: \"true\"", pipeline)
        self.assertIn("CONTENT_SAFETY_ENABLED: \"true\"", agentcore)
        self.assertIn(r"backend\shared\content_safety.py", deploy_agent)
        self.assertGreaterEqual(worker_source.count("_screen_ai_payload("), 8)
        self.assertIn("content_safety.screen_payload(", agent_service)
        self.assertIn("content_safety.screen_payload(", agent_meeting)
if __name__ == "__main__":
    unittest.main()
