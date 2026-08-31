from __future__ import annotations

import json
import sys
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile


AGENTCORE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENTCORE_ROOT))

fake_boto3 = sys.modules.get("boto3") or types.ModuleType("boto3")
fake_boto3.client = getattr(fake_boto3, "client", lambda *args, **kwargs: None)
fake_dynamodb = types.ModuleType("boto3.dynamodb")
fake_dynamodb_types = types.ModuleType("boto3.dynamodb.types")


class TypeSerializer:
    def serialize(self, value):
        if isinstance(value, dict):
            return {"M": {key: self.serialize(item) for key, item in value.items()}}
        if isinstance(value, list):
            return {"L": [self.serialize(item) for item in value]}
        if isinstance(value, bool):
            return {"BOOL": value}
        if isinstance(value, (int, float)):
            return {"N": str(value)}
        return {"S": str(value)}


class TypeDeserializer:
    def deserialize(self, value):
        if "M" in value:
            return {key: self.deserialize(item) for key, item in value["M"].items()}
        if "L" in value:
            return [self.deserialize(item) for item in value["L"]]
        if "BOOL" in value:
            return value["BOOL"]
        if "N" in value:
            return int(value["N"])
        return value.get("S")


fake_dynamodb_types.TypeSerializer = TypeSerializer
fake_dynamodb_types.TypeDeserializer = TypeDeserializer
sys.modules["boto3"] = fake_boto3
sys.modules["boto3.dynamodb"] = fake_dynamodb
sys.modules["boto3.dynamodb.types"] = fake_dynamodb_types
if "botocore.exceptions" not in sys.modules:
    fake_botocore = types.ModuleType("botocore")
    fake_exceptions = types.ModuleType("botocore.exceptions")
    fake_config = types.ModuleType("botocore.config")

    class ClientError(Exception):
        def __init__(self, response, operation_name="test"):
            super().__init__(operation_name)
            self.response = response

    class Config:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    fake_exceptions.ClientError = ClientError
    fake_config.Config = Config
    sys.modules["botocore"] = fake_botocore
    sys.modules["botocore.exceptions"] = fake_exceptions
    sys.modules["botocore.config"] = fake_config

from common.security import ScopeTokenError, sign_scope_token  # noqa: E402
from tools import handler as app  # noqa: E402
from tools.docx import handoff_docx_bytes  # noqa: E402


SECRET = "s" * 48
SCOPE = {
    "tenantId": "demo",
    "clientId": "bluemesa-payments",
    "projectId": "bluemesa-payments",
    "userId": "user-123",
    "sessionId": "session-123",
}


def scoped_event(tool_name: str, **values):
    return {
        "_toolName": f"project-tools___{tool_name}",
        "scopeToken": sign_scope_token(SECRET, SCOPE),
        "tenantId": SCOPE["tenantId"],
        "clientId": SCOPE["clientId"],
        "projectId": SCOPE["projectId"],
        **values,
    }


class FakeBody:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class ToolTests(unittest.TestCase):
    def test_artifact_args_use_kms_and_guest_retention(self):
        key_arn = "arn:aws:kms:us-east-1:111122223333:key/test"
        with patch.object(app, "DATA_KMS_KEY_ARN", key_arn):
            guest = app._s3_artifact_args(SCOPE)
            workspace = app._s3_artifact_args(
                {**SCOPE, "tenantId": "tenant-acme"}
            )

        self.assertEqual(guest["ServerSideEncryption"], "aws:kms")
        self.assertEqual(guest["SSEKMSKeyId"], key_arn)
        self.assertEqual(guest["Tagging"], "RetentionClass=guest-temporary")
        self.assertNotIn("Tagging", workspace)

    def test_s3_client_forces_sigv4_for_kms_presigned_downloads(self):
        with patch.object(app.boto3, "client", return_value=object()) as client:
            app._client("s3")

        config = client.call_args.kwargs["config"]
        self.assertEqual(config.signature_version, "s3v4")

    def test_scope_token_blocks_cross_client_read(self):
        event = scoped_event("get_latest_brief")
        event["clientId"] = "another-client"
        with patch.object(app, "_scope_secret", return_value=SECRET):
            with self.assertRaises(ScopeTokenError):
                app.handler(event, None)

    def test_get_latest_brief_uses_approved_pointer(self):
        requests = []
        approved_key = (
            "tenants/demo/clients/bluemesa-payments/projects/bluemesa-payments/"
            "brief/approved/v000002/packet.json"
        )

        class FakeS3:
            def get_object(self, **kwargs):
                requests.append(kwargs)
                document = {
                    "scope": {
                        "tenantId": SCOPE["tenantId"],
                        "clientId": SCOPE["clientId"],
                        "projectId": SCOPE["projectId"],
                    },
                    "response": {
                        "technical": ["Approved"],
                        "metadata": {
                            "packetVersion": 2,
                            "approvalStatus": "approved",
                        },
                    },
                    "approvedAt": "now",
                    "packetVersion": 2,
                    "approvalStatus": "approved",
                }
                return {"Body": FakeBody(json.dumps(document).encode("utf-8"))}

        class FakeDynamo:
            def get_item(self, **_kwargs):
                return {
                    "Item": {
                        "approvedArtifactKey": {"S": approved_key},
                        "approvedPacketVersion": {"N": "2"},
                    }
                }

        clients = {"s3": FakeS3(), "dynamodb": FakeDynamo()}
        with (
            patch.object(app, "ARTIFACT_BUCKET", "private-artifacts"),
            patch.object(app, "PROJECT_TABLE", "project-state"),
            patch.object(app, "_client", side_effect=lambda name: clients[name]),
        ):
            result = app.get_latest_brief(SCOPE)

        self.assertEqual(result["brief"]["technical"], ["Approved"])
        self.assertEqual(result["metadata"]["packetVersion"], 2)
        self.assertEqual(result["metadata"]["approvalStatus"], "approved")
        self.assertEqual(result["metadata"]["source"], "approved-pointer")
        self.assertEqual(requests[0]["Key"], approved_key)

    def test_get_latest_brief_accepts_scoped_latest_pointer(self):
        approved_key = (
            "tenants/demo/clients/bluemesa-payments/projects/bluemesa-payments/"
            "brief/latest.json"
        )

        class FakeS3:
            def get_object(self, **_kwargs):
                document = {
                    "scope": {
                        "tenantId": SCOPE["tenantId"],
                        "clientId": SCOPE["clientId"],
                        "projectId": SCOPE["projectId"],
                    },
                    "response": {
                        "technical": ["Approved"],
                        "metadata": {
                            "packetVersion": 2,
                            "approvalStatus": "approved",
                        },
                    },
                    "packetVersion": 2,
                    "approvalStatus": "approved",
                }
                return {"Body": FakeBody(json.dumps(document).encode("utf-8"))}

        class FakeDynamo:
            def get_item(self, **_kwargs):
                return {
                    "Item": {
                        "approvedArtifactKey": {"S": approved_key},
                        "approvedPacketVersion": {"N": "2"},
                    }
                }

        clients = {"s3": FakeS3(), "dynamodb": FakeDynamo()}
        with (
            patch.object(app, "ARTIFACT_BUCKET", "private-artifacts"),
            patch.object(app, "PROJECT_TABLE", "project-state"),
            patch.object(app, "_client", side_effect=lambda name: clients[name]),
        ):
            result = app.get_latest_brief(SCOPE)

        self.assertEqual(result["brief"]["technical"], ["Approved"])
        self.assertEqual(result["metadata"]["source"], "scoped-latest-pointer")

    def test_get_latest_brief_rejects_cross_scope_pointer(self):
        class FakeDynamo:
            def get_item(self, **_kwargs):
                return {
                    "Item": {
                        "approvedArtifactKey": {
                            "S": (
                                "tenants/another-tenant/clients/bluemesa-payments/"
                                "projects/bluemesa-payments/brief/approved/"
                                "v000002/packet.json"
                            )
                        },
                        "approvedPacketVersion": {"N": "2"},
                    }
                }

        with (
            patch.object(app, "PROJECT_TABLE", "project-state"),
            patch.object(app, "_client", return_value=FakeDynamo()),
        ):
            with self.assertRaises(PermissionError):
                app.get_latest_brief(SCOPE)

    def test_legacy_demo_fallback_is_limited_to_configured_client(self):
        other_scope = {
            **SCOPE,
            "clientId": "another-client",
            "projectId": "another-client",
        }
        requested_keys = []

        class FakeDynamo:
            def get_item(self, **_kwargs):
                return {}

        def read_json(key):
            requested_keys.append(key)
            return None

        with (
            patch.object(app, "PROJECT_TABLE", "project-state"),
            patch.object(app, "ALLOW_LEGACY_DEMO_BRIEF", True),
            patch.object(app, "DEMO_ALLOWED_CLIENT_IDS", {"bluemesa-payments"}),
            patch.object(app, "_client", return_value=FakeDynamo()),
            patch.object(app, "_read_json_object", side_effect=read_json),
        ):
            with self.assertRaises(LookupError):
                app.get_latest_brief(other_scope)

        self.assertEqual(requested_keys, [])

    def test_explicitly_allowed_demo_client_can_read_only_its_latest_brief(self):
        apex_scope = {
            **SCOPE,
            "clientId": "apex-mutual",
            "projectId": "apex-mutual",
        }
        requested_keys = []

        class FakeDynamo:
            def get_item(self, **_kwargs):
                return {}

        def read_json(key):
            requested_keys.append(key)
            if key == "clients/apex-mutual/brief/latest.json":
                return {"response": {"technical": ["Approved Apex brief"]}}
            return None

        with (
            patch.object(app, "PROJECT_TABLE", "project-state"),
            patch.object(app, "ALLOW_LEGACY_DEMO_BRIEF", True),
            patch.object(app, "DEMO_ALLOWED_CLIENT_IDS", {"apex-mutual"}),
            patch.object(app, "_client", return_value=FakeDynamo()),
            patch.object(app, "_read_json_object", side_effect=read_json),
        ):
            result = app.get_latest_brief(apex_scope)

        self.assertEqual(result["brief"]["technical"], ["Approved Apex brief"])
        self.assertEqual(requested_keys, ["clients/apex-mutual/brief/latest.json"])

    def test_solutions_architect_catchup_uses_architecture_and_evidence_lenses(self):
        with (
            patch.object(app, "get_latest_brief") as latest_mock,
            patch.object(app, "get_project_state") as state_mock,
        ):
            result = app.generate_catchup(
                SCOPE,
                "Solutions Architect",
                "What architecture assumptions must I validate?",
            )

        latest_mock.assert_not_called()
        state_mock.assert_not_called()
        self.assertEqual(result["audienceRole"], "Solutions Architect")
        self.assertIn("architecture assumptions and unknowns", result["recommendedLenses"])
        self.assertIn("required evidence", result["recommendedLenses"])
        self.assertIn("next technical session", result["recommendedLenses"])

    def test_save_project_update_uses_version_condition_and_scoped_partition(self):
        captured = {}

        class FakeDynamoDB:
            def transact_write_items(self, **kwargs):
                captured.update(kwargs)

        update = {
            register: [
                {
                    "title": f"{register} item",
                    "detail": "Grounded detail",
                    "owner": "Owner",
                    "status": "Open",
                    "source": "Approved brief",
                }
            ]
            for register in (
                "assumptions",
                "decisions",
                "risks",
                "actions",
                "owners",
                "milestones",
                "openQuestions",
            )
        }
        update["nextSteps"] = {
            "immediateActions": [
                {
                    "action": "Run recovery workshop",
                    "owner": "Dev Malik",
                    "timing": "Within five days",
                    "dependency": "Recovery evidence",
                    "decisionGate": "Recovery threshold is accepted",
                }
            ],
            "openQuestions": ["Who approves the threshold?"],
            "nextMeeting": {
                "purpose": "Review evidence",
                "timing": "Friday",
                "attendees": ["Dev Malik", "SA"],
            },
            "customerSummary": "Review evidence and agree on the next decision.",
            "internalNotes": "Keep assumptions open until evidence is approved.",
        }
        with (
            patch.object(app, "PROJECT_TABLE", "project-table"),
            patch.object(app, "_client", return_value=FakeDynamoDB()),
        ):
            result = app.save_project_update(
                SCOPE,
                update,
                expected_version=2,
                idempotency_key="save-test-001",
                confirm_write=True,
            )

        transaction = captured["TransactItems"]
        partition = transaction[1]["Update"]["Key"]["projectId"]["S"]
        self.assertEqual(
            partition,
            "TENANT#demo|CLIENT#bluemesa-payments|PROJECT#bluemesa-payments",
        )
        self.assertIn("#version = :expectedVersion", transaction[1]["Update"]["ConditionExpression"])
        self.assertIn("expiresAt", transaction[0]["Put"]["Item"])
        self.assertEqual(result["version"], 3)
        self.assertEqual(
            result["nextSteps"]["immediateActions"][0]["owner"],
            "Dev Malik",
        )

    def test_material_writes_require_confirmation(self):
        with self.assertRaises(PermissionError):
            app.save_project_update(
                SCOPE,
                {},
                expected_version=0,
                idempotency_key="save-test-002",
                confirm_write=False,
            )

    def test_handoff_docx_is_valid_zip(self):
        packet = {
            "company": "BlueMesa Payments",
            "businessCase": {
                "scenario": "BlueMesa needs a controlled settlement modernization decision.",
                "desiredOutcomes": "Agree on a bounded pilot.",
                "alignmentStatement": "Confirm outcomes and evidence.",
                "inScope": "Recovery and PCI evidence.",
                "outOfScope": "Production cutover.",
                "successCriteria": "Named owners and an accepted gate.",
            },
            "projectAnswer": "A grounded project summary.",
            "projectArtifacts": {
                "twoWeekPlan": [{"title": "Pilot", "detail": "Run it"}],
                "riskRegister": [{"title": "Risk", "detail": "Track it"}],
                "stakeholderMap": [{"title": "Sponsor", "detail": "Align"}],
                "followUpEmail": {"subject": "Next steps", "body": "Details"},
                "nextSteps": {
                    "immediateActions": [
                        {
                            "action": "Run recovery workshop",
                            "owner": "Dev Malik",
                            "timing": "Friday",
                            "dependency": "Recovery evidence",
                            "decisionGate": "Recovery threshold accepted",
                        }
                    ],
                    "openQuestions": ["Who approves the threshold?"],
                    "nextMeeting": {
                        "purpose": "Review recovery evidence",
                        "timing": "Friday",
                        "attendees": ["Dev Malik", "SA"],
                    },
                    "customerSummary": "Review evidence and agree on the bounded pilot.",
                    "internalNotes": "Keep assumptions open until evidence is accepted.",
                },
            },
            "citations": ["Approved brief"],
        }
        data = handoff_docx_bytes(packet, SCOPE)
        with ZipFile(BytesIO(data)) as docx:
            self.assertIn("word/numbering.xml", docx.namelist())
            self.assertIn("word/footer1.xml", docx.namelist())
            document = docx.read("word/document.xml").decode("utf-8")
            styles = docx.read("word/styles.xml").decode("utf-8")
        self.assertIn("PilarPrep Project Handoff | BlueMesa Payments", document)
        self.assertIn("Approved Source Labels", document)
        self.assertIn('w:numId w:val="1"', document)
        self.assertIn("SourceNote", styles)
        self.assertIn("Business Case", document)
        self.assertIn("Next Steps", document)
        self.assertIn("Decision gate: Recovery threshold accepted", document)
        self.assertIn("Customer-Facing Summary", document)

    def test_handoff_writes_replacement_before_purging_old_versions(self):
        packet = {
            "projectAnswer": "Grounded handoff ready for delivery.",
            "projectArtifacts": {
                "twoWeekPlan": [{"title": "Pilot", "detail": "Run it"}],
            },
        }
        operations = []
        deleted = []

        class FakeS3:
            def put_object(self, **kwargs):
                version = "new-json" if kwargs["Key"].endswith(".json") else "new-docx"
                operations.append(("put", kwargs["Key"], version))
                return {"VersionId": version}

            def list_object_versions(self, **kwargs):
                operations.append(("list", kwargs["Prefix"]))
                return {
                    "Versions": [
                        {"Key": kwargs["Prefix"] + "latest.json", "VersionId": "new-json"},
                        {"Key": kwargs["Prefix"] + "latest.docx", "VersionId": "new-docx"},
                        {"Key": kwargs["Prefix"] + "latest.json", "VersionId": "old-json"},
                        {"Key": kwargs["Prefix"] + "latest.docx", "VersionId": "old-docx"},
                    ],
                    "DeleteMarkers": [],
                    "IsTruncated": False,
                }

            def delete_objects(self, **kwargs):
                operations.append(("delete", len(kwargs["Delete"]["Objects"])))
                deleted.extend(kwargs["Delete"]["Objects"])

            def generate_presigned_url(self, *_args, **_kwargs):
                return "https://download.example/handoff.docx"

        class FakeDynamoDB:
            def put_item(self, **_kwargs):
                operations.append(("ddb",))

        s3 = FakeS3()
        dynamodb = FakeDynamoDB()

        def fake_client(service_name):
            return s3 if service_name == "s3" else dynamodb

        with (
            patch.object(app, "ARTIFACT_BUCKET", "private-artifacts"),
            patch.object(app, "PROJECT_TABLE", "project-table"),
            patch.object(app, "_idempotency_record", return_value={}),
            patch.object(app, "_client", side_effect=fake_client),
        ):
            result = app.create_handoff_packet(
                SCOPE,
                packet,
                audience="Engineer",
                idempotency_key="handoff-test-write",
                confirm_write=True,
            )

        self.assertEqual([operation[0] for operation in operations[:3]], ["put", "put", "list"])
        self.assertEqual(
            {(item["Key"], item["VersionId"]) for item in deleted},
            {
                ("tenants/demo/clients/bluemesa-payments/projects/bluemesa-payments/handoff/handoff-test-write/latest.json", "old-json"),
                ("tenants/demo/clients/bluemesa-payments/projects/bluemesa-payments/handoff/handoff-test-write/latest.docx", "old-docx"),
            },
        )
        self.assertFalse(result["idempotent"])

    def test_distinct_handoffs_never_write_the_same_objects(self):
        stored = {}

        class FakeS3:
            def put_object(self, **request):
                stored[request["Key"]] = request["Body"]
                return {}

            def generate_presigned_url(self, *_args, **_kwargs):
                return "https://download.example/handoff.docx"

        class FakeDatabase:
            def put_item(self, **_request):
                return {}

        s3 = FakeS3()
        with patch.object(app, "_idempotency_record", return_value={}), patch.object(app, "_client", side_effect=lambda service: s3 if service == "s3" else FakeDatabase()):
            results = [app.create_handoff_packet(SCOPE, {
                "projectAnswer": label,
                "projectArtifacts": {"twoWeekPlan": [{"title": "Pilot", "detail": label}]},
            }, audience="Engineer", idempotency_key=label, confirm_write=True) for label in ["first-user", "second-user"]]
        self.assertEqual(len(stored), 4)
        self.assertNotEqual(results[0]["artifactKey"], results[1]["artifactKey"])

    def test_duplicate_handoff_rejects_cross_project_artifact_keys(self):
        with patch.object(app, "_idempotency_record", return_value={"artifactKey": "tenants/other/latest.json", "docxArtifactKey": "tenants/other/latest.docx"}), patch.object(app, "_client") as client:
            with self.assertRaises(PermissionError):
                app.create_handoff_packet(SCOPE, {"projectAnswer": "Answer", "projectArtifacts": {"twoWeekPlan": []}}, audience="PM", idempotency_key="old-job", confirm_write=True)
            client.return_value.head_object.assert_not_called()

    def test_duplicate_handoff_returns_a_fresh_download_url(self):
        packet = {
            "projectAnswer": "Previously stored grounded handoff.",
            "projectArtifacts": {"twoWeekPlan": [{"title": "Pilot", "detail": "Run it"}]},
        }
        presigned = []

        class FakeS3:
            def head_object(self, **kwargs):
                return {}

            def generate_presigned_url(self, operation, **kwargs):
                presigned.append({"operation": operation, **kwargs})
                return "https://download.example/handoff.docx"

        with (
            patch.object(app, "ARTIFACT_BUCKET", "private-artifacts"),
            patch.object(app, "_idempotency_record", return_value={
                "artifactKey": "tenants/demo/clients/bluemesa-payments/projects/bluemesa-payments/handoff/latest.json",
                "docxArtifactKey": "tenants/demo/clients/bluemesa-payments/projects/bluemesa-payments/handoff/latest.docx",
            }),
            patch.object(app, "_client", return_value=FakeS3()),
        ):
            result = app.create_handoff_packet(
                SCOPE,
                packet,
                audience="Engineer",
                idempotency_key="handoff-test-001",
                confirm_write=True,
            )

        self.assertTrue(result["idempotent"])
        self.assertEqual(result["docxDownloadUrl"], "https://download.example/handoff.docx")
        self.assertEqual(presigned[0]["ExpiresIn"], 3600)
        self.assertEqual(presigned[0]["Params"]["Key"], "tenants/demo/clients/bluemesa-payments/projects/bluemesa-payments/handoff/latest.docx")


if __name__ == "__main__":
    unittest.main()
