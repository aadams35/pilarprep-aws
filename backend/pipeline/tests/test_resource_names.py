import copy
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("resource_migration", ROOT / "scripts" / "migrate_resource_names.py")
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class ResourceNamingTests(unittest.TestCase):
    def test_names_are_consistent_and_globally_scoped(self):
        for purpose in ["web-assets", "artifacts", "meeting-evidence", "deployments", "evidence-vectors"]:
            value = migration.resource_name(purpose, "123456789012", "us-east-1")
            self.assertEqual(value, f"pilarprep-demo-{purpose}-123456789012-us-east-1")
            self.assertLessEqual(len(value), 63)

    def test_invalid_or_overlong_names_are_rejected(self):
        for args in [("web-assets", "wrong", "us-east-1"), ("x" * 64, "123456789012", "us-east-1"),
                     ("../other", "123456789012", "us-east-1")]:
            with self.assertRaises(ValueError):
                migration.resource_name(*args)

    def test_detach_rewrites_only_the_selected_bucket(self):
        original = {
            "ref": {"Ref": "Store"},
            "arn": {"Fn::GetAtt": ["Store", "Arn"]},
            "domain": {"Fn::GetAtt": "Store.RegionalDomainName"},
            "policy": {"Fn::Sub": "${Store.Arn}/tenants/*"},
            "other": {"Ref": "OtherStore"},
            "variable": {"Fn::Sub": ["${Store}/${Folder}", {"Folder": {"Ref": "Folder"}}]},
        }
        unchanged = copy.deepcopy(original)
        result = migration.literal_bucket_references(original, "Store", "new-store", "us-east-1")
        self.assertEqual(original, unchanged)
        self.assertEqual(result["ref"], "new-store")
        self.assertEqual(result["arn"], "arn:aws:s3:::new-store")
        self.assertEqual(result["domain"], "new-store.s3.us-east-1.amazonaws.com")
        self.assertEqual(result["policy"], {"Fn::Sub": "arn:aws:s3:::new-store/tenants/*"})
        self.assertEqual(result["other"], {"Ref": "OtherStore"})
        self.assertEqual(result["variable"]["Fn::Sub"][0], "new-store/${Folder}")

    def test_unknown_bucket_attributes_fail_closed(self):
        with self.assertRaises(ValueError):
            migration.literal_bucket_references({"Fn::GetAtt": ["Store", "WebsiteURL"]}, "Store", "new-store", "us-east-1")

    def test_naming_changes_leave_compute_and_auth_unchanged(self):
        for kind in ["core", "jobs", "frontend"]:
            source = migration.load_template((ROOT / "infrastructure" / migration.FILES[kind]).read_text())
            result = migration.naming_template(kind, source)
            for key, resource in source["Resources"].items():
                if resource["Type"] in {"AWS::Serverless::Function", "AWS::IAM::Role", "AWS::Cognito::UserPool",
                                        "AWS::DynamoDB::Table", "AWS::Serverless::HttpApi"}:
                    self.assertEqual(result["Resources"][key], resource)

    def test_bucket_names_remain_optional_for_legacy_stacks(self):
        for kind, parameter in [("core", "ArtifactBucketName"), ("jobs", "MeetingEvidenceBucketName"),
                                ("jobs", "EvidenceVectorBucketName")]:
            template = migration.load_template((ROOT / "infrastructure" / migration.FILES[kind]).read_text())
            self.assertEqual(template["Parameters"][parameter]["Default"], "")
        frontend = migration.load_template((ROOT / "infrastructure" / migration.FILES["frontend"]).read_text())
        self.assertEqual(frontend["Resources"]["FrontendBucket"]["DeletionPolicy"], "Retain")
        self.assertEqual(frontend["Resources"]["FrontendBucket"]["UpdateReplacePolicy"], "Retain")

    def test_mutating_phases_require_apply(self):
        runner = object.__new__(migration.Migration)
        runner.args = SimpleNamespace(apply=False)
        with self.assertRaises(ValueError):
            runner.require_apply()
        with self.assertRaises(ValueError):
            runner.deploy_template("frontend", {})

    def test_summary_does_not_label_proposed_names_as_live(self):
        state = {"account": "123456789012", "region": "us-east-1", "environment": "demo",
                 "targets": {"artifacts": "planned-store"}, "steps": ["prepare"],
                 "stacks": {"frontend": {"outputs": {"CloudFrontDistributionId": "example"}}}}
        summary = migration.public_summary(state)
        self.assertEqual(summary["targetBucketNames"], state["targets"])
        self.assertEqual(summary["completedPhases"], ["prepare"])
        self.assertNotIn("bucketNames", summary)
        self.assertNotIn("activeBucketNames", summary)

    def test_cutover_refuses_to_detach_without_both_retention_policies(self):
        for policies in [{}, {"DeletionPolicy": "Retain"}, {"UpdateReplacePolicy": "Retain"}]:
            runner = object.__new__(migration.Migration)
            runner.args = SimpleNamespace(apply=True)
            runner.state = {"steps": ["prepare"], "targets": {"web-assets": "new-store"}}
            runner.template = MagicMock(return_value={"Resources": {"FrontendBucket": policies}})
            runner.deploy_template = MagicMock()
            runner.copy_objects = MagicMock()
            with self.assertRaisesRegex(RuntimeError, "retention"):
                runner.cutover("frontend")
            runner.deploy_template.assert_not_called()
            runner.copy_objects.assert_not_called()

    def test_backend_cutover_requires_paused_queue(self):
        runner = object.__new__(migration.Migration)
        runner.args = SimpleNamespace(apply=True)
        runner.state = {"steps": ["prepare"]}
        runner.template = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "Pause the queue"):
            runner.cutover("core")
        runner.template.assert_not_called()

    def test_resume_requires_all_storage_consumers_and_audio_safety_steps(self):
        runner = object.__new__(migration.Migration)
        runner.args = SimpleNamespace(apply=True)
        runner.state = {"steps": ["prepare", "cutover-core", "cutover-jobs"]}
        runner.copy_objects = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "Finish storage consumers"):
            runner.resume()
        runner.copy_objects.assert_not_called()

    def test_completed_resume_does_not_recopy_live_data(self):
        runner = object.__new__(migration.Migration)
        runner.args = SimpleNamespace(apply=True)
        runner.state = {"steps": ["resume"]}
        runner.copy_objects = MagicMock()
        runner.log = MagicMock()
        runner.resume()
        runner.copy_objects.assert_not_called()

    def test_existing_secrets_use_previous_value_not_a_logged_value(self):
        runner = object.__new__(migration.Migration)
        runner.stack = MagicMock(return_value={"Parameters": [{"ParameterKey": "Secret"}, {"ParameterKey": "Bucket"}]})
        actual = runner.parameters("core", {"Parameters": {"Secret": {}, "Bucket": {}, "Optional": {"Default": ""}}},
                                   {"Bucket": "renamed-store"})
        self.assertEqual(actual, [{"ParameterKey": "Secret", "UsePreviousValue": True},
                                  {"ParameterKey": "Bucket", "ParameterValue": "renamed-store"}])

    def test_current_audio_is_not_copied_during_initial_preparation(self):
        runner = object.__new__(migration.Migration)
        runner.account = "123456789012"
        runner.state = {"sources": {"meeting-evidence": "old"}, "targets": {"meeting-evidence": "new"}, "copies": {}}
        runner.object_map = MagicMock(side_effect=[{"audio/uploads/private/meeting.mp3": {"etag": "a", "size": 10}}, {}])
        runner.s3 = MagicMock()
        runner.save = MagicMock()
        runner.log = MagicMock()
        runner.copy_objects("meeting-evidence")
        runner.s3.copy_object.assert_not_called()

    def test_audio_copy_never_reuses_a_guardduty_clean_tag(self):
        runner = object.__new__(migration.Migration)
        runner.account = "123456789012"
        runner.state = {"sources": {"meeting-evidence": "old"}, "targets": {"meeting-evidence": "new"}, "copies": {}}
        key = "audio/uploads/private/meeting.mp3"
        runner.object_map = MagicMock(side_effect=[{key: {"etag": "a", "size": 10}}, {}])
        runner.s3 = MagicMock()
        runner.s3.head_object.side_effect = [{"VersionId": "old-v"}, {"ContentLength": 10, "ETag": "b"}]
        runner.s3.get_object_tagging.return_value = {"TagSet": [
            {"Key": migration.MALWARE_TAG, "Value": "NO_THREATS_FOUND"}, {"Key": "purpose", "Value": "meeting"},
        ]}
        runner.s3.copy_object.return_value = {"VersionId": "new-v"}
        runner.save = MagicMock()
        runner.log = MagicMock()
        runner.copy_objects("meeting-evidence", audio_keys={key})
        request = runner.s3.copy_object.call_args.kwargs
        self.assertEqual(request["Tagging"], "purpose=meeting")
        self.assertEqual(request["CopySource"]["VersionId"], "old-v")
        self.assertEqual(request["CopySourceIfMatch"], "a")
        runner.s3.delete_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
