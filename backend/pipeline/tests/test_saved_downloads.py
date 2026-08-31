import json
import unittest
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


if __name__ == "__main__":
    unittest.main()
