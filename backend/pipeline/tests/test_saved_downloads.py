import json
import unittest
from io import BytesIO
from unittest.mock import patch

from backend.pipeline.tests.test_pipeline import SCOPE, api, common, iam_event


class SavedDownloadTests(unittest.TestCase):
    def setUp(self):
        self.key = f"{common.project_artifact_prefix(SCOPE)}/brief/approved/v000003/packet.docx"
        self.metadata = {
            "approvedPacketVersion": 3,
            "company": "Apex Mutual",
            "approvedDocxArtifactKey": self.key,
        }
        self.document = {
            "request": {"company": "Apex Mutual"},
            "response": {
                "technicalBrief": "An unchanged approved brief.",
                "metadata": {
                    "packetVersion": 3,
                    "docxDownloadUrl": "https://old.example/expired",
                },
            },
        }

    def test_latest_refreshes_link_from_active_bucket_without_mutating_approval(self):
        original = json.dumps(self.document, sort_keys=True)
        with (
            patch.object(api, "_read_latest_approved", return_value=(SCOPE, self.metadata, self.document)),
            patch.object(api, "ARTIFACT_BUCKET", "pilarprep-demo-artifacts-test"),
            patch.object(api, "aws_client") as client,
        ):
            client.return_value.generate_presigned_url.return_value = "https://new.example/fresh"
            result = api._get_latest(iam_event("GET"), SCOPE["clientId"])
            signed = client.return_value.generate_presigned_url.call_args.kwargs

        body = json.loads(result["body"])
        self.assertEqual(body["packet"]["metadata"]["docxDownloadUrl"], "https://new.example/fresh")
        self.assertEqual(body["packetVersion"], 3)
        self.assertEqual(body["packet"]["technicalBrief"], self.document["response"]["technicalBrief"])
        self.assertEqual(signed["Params"]["Bucket"], "pilarprep-demo-artifacts-test")
        self.assertEqual(signed["Params"]["Key"], self.key)
        self.assertEqual(signed["Params"]["ResponseContentDisposition"], 'attachment; filename="Apex Mutual - Brief - v3.docx"')
        self.assertEqual(signed["ExpiresIn"], 900)
        self.assertEqual(json.dumps(self.document, sort_keys=True), original)
        client.return_value.put_object.assert_not_called()

    def test_latest_does_not_trust_embedded_link_without_approved_docx(self):
        del self.metadata["approvedDocxArtifactKey"]
        with (
            patch.object(api, "_read_latest_approved", return_value=(SCOPE, self.metadata, self.document)),
            patch.object(api, "aws_client") as client,
        ):
            result = api._get_latest(iam_event("GET"), SCOPE["clientId"])
        self.assertNotIn("docxDownloadUrl", json.loads(result["body"])["packet"]["metadata"])
        client.assert_not_called()

    def test_latest_refuses_to_sign_another_projects_docx(self):
        self.metadata["approvedDocxArtifactKey"] = "tenants/other/clients/other/brief/packet.docx"
        with (
            patch.object(api, "_read_latest_approved", return_value=(SCOPE, self.metadata, self.document)),
            patch.object(api, "aws_client") as client,
        ):
            with self.assertRaises(api.ScopeAuthorizationError):
                api._get_latest(iam_event("GET"), SCOPE["clientId"])
        client.assert_not_called()

    def test_download_endpoint_uses_same_scoped_signing_path(self):
        with (
            patch.object(api, "_scope_from_query", return_value=SCOPE),
            patch.object(api, "deserialize_item", return_value=self.metadata),
            patch.object(api, "aws_client") as client,
        ):
            client.return_value.generate_presigned_url.return_value = "https://new.example/fresh"
            result = api._get_artifact(iam_event("GET"), "brief")
        body = json.loads(result["body"])
        self.assertEqual(body["artifactKey"], self.key)
        self.assertEqual(body["downloadUrl"], "https://new.example/fresh")
        self.assertEqual(body["format"], "docx")

    def test_current_returns_scoped_draft_with_authoritative_version(self):
        draft_key = f"{common.project_artifact_prefix(SCOPE)}/brief/draft/job-1/version-1/latest.json"
        draft_docx_key = f"{common.project_artifact_prefix(SCOPE)}/brief/draft/job-1/version-1/latest.docx"
        draft_metadata = {
            "packetVersion": 4,
            "approvedPacketVersion": 3,
            "approvalStatus": "stale",
            "company": "Apex Mutual",
            "draftArtifactKey": draft_key,
            "draftDocxArtifactKey": draft_docx_key,
        }
        document = {
            "request": {"company": "Apex Mutual"},
            "response": {
                "provider": "bedrock",
                "metadata": {"packetVersion": 3, "docxDownloadUrl": "expired"},
            },
        }
        with (
            patch.object(api, "_scope_from_query", return_value=SCOPE),
            patch.object(api, "deserialize_item", return_value=draft_metadata),
            patch.object(api, "ARTIFACT_BUCKET", "pilarprep-demo-artifacts-test"),
            patch.object(api, "aws_client") as client,
        ):
            client.return_value.get_item.return_value = {"Item": {"stored": {"S": "yes"}}}
            client.return_value.get_object.return_value = {
                "Body": BytesIO(json.dumps(document).encode("utf-8"))
            }
            client.return_value.generate_presigned_url.return_value = "https://new.example/draft"
            result = api._get_current(iam_event("GET"), SCOPE["clientId"])

        body = json.loads(result["body"])
        self.assertEqual(body["packetVersion"], 4)
        self.assertEqual(body["approvalStatus"], "stale")
        self.assertEqual(body["packet"]["metadata"]["packetVersion"], 4)
        self.assertEqual(body["packet"]["metadata"]["approvalStatus"], "stale")
        self.assertEqual(body["packet"]["metadata"]["docxDownloadUrl"], "https://new.example/draft")
        signed = client.return_value.generate_presigned_url.call_args.kwargs
        self.assertEqual(signed["Params"]["Key"], draft_docx_key)
        self.assertEqual(signed["ExpiresIn"], 900)

    def test_current_rejects_draft_outside_authorized_project(self):
        metadata = {
            "packetVersion": 4,
            "approvalStatus": "stale",
            "draftArtifactKey": "tenants/other/clients/other/brief/draft/latest.json",
        }
        with (
            patch.object(api, "_scope_from_query", return_value=SCOPE),
            patch.object(api, "deserialize_item", return_value=metadata),
            patch.object(api, "aws_client") as client,
        ):
            client.return_value.get_item.return_value = {"Item": {"stored": {"S": "yes"}}}
            with self.assertRaises(api.ScopeAuthorizationError):
                api._get_current(iam_event("GET"), SCOPE["clientId"])
        client.return_value.get_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
