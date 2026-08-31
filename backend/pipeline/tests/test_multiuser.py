import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

try:
    from .test_pipeline import SCOPE, common, worker
except ImportError:
    from test_pipeline import SCOPE, common, worker


class ConcurrentPacketTests(unittest.TestCase):
    def test_losing_refinement_cannot_replace_winning_json_or_docx(self):
        objects = {}
        pointer = {}
        first_json = threading.Event()
        second_docx = threading.Event()
        first_commit = threading.Event()

        class Storage:
            def put_object(self, **request):
                name = threading.current_thread().name
                key = request["Key"]
                if name == "loser" and key.endswith(".json"):
                    self_wait(first_json)
                objects[key] = request["Body"]
                if name == "winner" and key.endswith(".json"):
                    first_json.set()
                if name == "loser" and key.endswith(".docx"):
                    second_docx.set()
                return {}

            def generate_presigned_url(self, _operation, **request):
                return "https://example.test/" + request["Params"]["Key"]

            def list_object_versions(self, **request):
                return {"Versions": [{"Key": key, "VersionId": "v1"}
                        for key in objects if key.startswith(request["Prefix"])]}

            def delete_objects(self, **request):
                for item in request["Delete"]["Objects"]:
                    objects.pop(item["Key"], None)

        class Database:
            def update_item(self, **request):
                values = request["ExpressionAttributeValues"]
                if values[":jobId"]["S"] == "winner":
                    self_wait(second_docx)
                    pointer.update(json=values[":draftArtifactKey"]["S"], docx=values[":draftDocxArtifactKey"]["S"])
                    first_commit.set()
                    return {}
                self_wait(first_commit)
                raise worker.ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

        def self_wait(event):
            if not event.wait(5):
                raise AssertionError("Concurrent save did not reach its synchronization point")

        def save(job):
            threading.current_thread().name = job
            return worker._write_brief_draft(SCOPE, {"company": "Apex Mutual", "baseBriefVersion": 1},
                {"technical": [job]}, action="brief.refine", job_id=job, input_version="input-1")

        clients = {"s3": Storage(), "dynamodb": Database()}
        brief_module = SimpleNamespace(_brief_docx_bytes=lambda _input, packet, _metadata: packet["technical"][0].encode())
        with patch.object(worker, "aws_client", side_effect=lambda name: clients[name]), \
             patch.object(worker, "_brief_module", return_value=brief_module), \
             patch.object(worker, "_brief_latest", return_value={"packetVersion": 1}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                winner = pool.submit(save, "winner")
                loser = pool.submit(save, "loser")
                winner.result(timeout=10)
                with self.assertRaises(worker.NonRetryableJobError):
                    loser.result(timeout=10)
        self.assertEqual(json.loads(objects[pointer["json"]])["response"]["technical"], ["winner"])
        self.assertEqual(objects[pointer["docx"]], b"winner")
        self.assertEqual(set(objects), set(pointer.values()))

    def test_cleanup_deletes_only_superseded_mutable_objects(self):
        root = common.project_artifact_prefix(SCOPE)
        old_key = f"{root}/brief/draft/older/latest.json"
        kept_key = f"{root}/brief/draft/newer/latest.json"
        deleted = []

        class Storage:
            def list_object_versions(self, **request):
                return {"Versions": [
                    {"Key": old_key, "VersionId": "old"},
                    {"Key": kept_key, "VersionId": "new"},
                ]}

            def delete_objects(self, **request):
                deleted.extend(request["Delete"]["Objects"])

        with patch.object(worker, "aws_client", return_value=Storage()):
            worker._cleanup_replaced_artifacts(SCOPE, [old_key, kept_key], {kept_key})
        self.assertEqual(deleted, [{"Key": old_key, "VersionId": "old"}])

    def test_retried_job_cannot_overwrite_or_delete_its_committed_draft(self):
        objects = {}
        s3 = MagicMock()
        s3.put_object.side_effect = lambda **request: objects.update({request["Key"]: request["Body"]}) or {}
        s3.generate_presigned_url.return_value = "https://example.test/download"
        s3.list_object_versions.side_effect = lambda **request: {"Versions": [
            {"Key": key, "VersionId": "v1"} for key in objects if key.startswith(request["Prefix"])
        ]}
        def delete(**request):
            for item in request["Delete"]["Objects"]:
                objects.pop(item["Key"], None)
            return {}
        s3.delete_objects.side_effect = delete
        database = MagicMock()
        database.update_item.side_effect = [{}, worker.ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")]
        brief_module = SimpleNamespace(_brief_docx_bytes=lambda _input, packet, _metadata: packet["technical"][0].encode())
        with patch.object(worker, "aws_client", side_effect=lambda service: s3 if service == "s3" else database), patch.object(worker, "_brief_module", return_value=brief_module):
            first = worker._write_brief_draft(SCOPE, {"baseBriefVersion": 1}, {"technical": ["committed"]}, action="brief.refine", job_id="same-job", input_version="input-1")
            with self.assertRaises(worker.NonRetryableJobError):
                worker._write_brief_draft(SCOPE, {"baseBriefVersion": 1}, {"technical": ["retry"]}, action="brief.refine", job_id="same-job", input_version="input-1")
        self.assertEqual(len(objects), 2)
        self.assertEqual(objects[first["metadata"]["docxArtifactKey"]], b"committed")

    def test_partial_delete_failure_is_observable_without_undoing_commit(self):
        key = f"{common.project_artifact_prefix(SCOPE)}/brief/draft/old/latest.json"
        storage = MagicMock()
        storage.list_object_versions.return_value = {"Versions": [{"Key": key, "VersionId": "v1"}]}
        storage.delete_objects.return_value = {"Errors": [{"Key": key, "Code": "AccessDenied"}]}
        with patch.object(worker, "aws_client", return_value=storage), patch.object(worker, "metric") as metric:
            worker._cleanup_replaced_artifacts(SCOPE, [key], set())
        metric.assert_called_once_with("ArtifactCleanupFailures")

    def test_cleanup_cannot_delete_approved_or_other_client_artifacts(self):
        root = common.project_artifact_prefix(SCOPE)
        for key in [f"{root}/brief/approved/v000001/packet.json", "tenants/other/clients/other/projects/other/handoff/latest.json"]:
            with self.subTest(key=key), patch.object(worker, "aws_client") as client, patch.object(worker, "metric") as metric:
                worker._cleanup_replaced_artifacts(SCOPE, [key], set())
                client.assert_not_called()
                metric.assert_called_once_with("ArtifactCleanupFailures")

    def test_cleanup_failure_does_not_invalidate_a_committed_packet(self):
        key = f"{common.project_artifact_prefix(SCOPE)}/handoff/old/latest.json"
        with patch.object(worker, "aws_client", side_effect=RuntimeError("storage unavailable")), patch.object(worker, "metric") as metric:
            worker._cleanup_replaced_artifacts(SCOPE, [key], set())
        metric.assert_called_once_with("ArtifactCleanupFailures")

    def test_private_workspaces_have_independent_artifact_and_job_keys(self):
        other = {**SCOPE, "tenantId": common.identity_tenant_id("guest", "other-browser"), "userId": "other-user"}
        self.assertNotEqual(common.project_artifact_prefix(SCOPE), common.project_artifact_prefix(other))
        self.assertNotEqual(common.job_key(SCOPE, "same-job"), common.job_key(other, "same-job"))


class CapacityConfigurationTests(unittest.TestCase):
    def test_queue_capacity_is_configurable_without_duplicating_workers(self):
        from scripts.migrate_resource_names import load_template
        root = Path(__file__).resolve().parents[3]
        template = load_template((root / "infrastructure/jobs-pipeline.yaml").read_text())
        setting = template["Parameters"]["WorkerMaximumConcurrency"]
        self.assertEqual((setting["Default"], setting["MinValue"], setting["MaxValue"]), (2, 2, 50))
        worker = template["Resources"]["AiWorkerFunction"]["Properties"]
        events = [event["Properties"] for event in worker["Events"].values() if event["Type"] == "SQS"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["ScalingConfig"], {"MaximumConcurrency": {"Ref": "WorkerMaximumConcurrency"}})
        self.assertEqual(events[0]["BatchSize"], 1)
        self.assertIn("ReportBatchItemFailures", events[0]["FunctionResponseTypes"])
        self.assertNotIn("ReservedConcurrentExecutions", worker)
        self.assertEqual(template["Resources"]["AiWorkerThrottleAlarm"]["Properties"]["MetricName"], "Throttles")
        self.assertEqual(template["Resources"]["ArtifactCleanupFailureAlarm"]["Properties"]["MetricName"], "ArtifactCleanupFailures")

    def test_deployment_preserves_capacity_and_checks_account_headroom(self):
        script = (Path(__file__).resolve().parents[3] / "scripts/deploy-jobs-pipeline.ps1").read_text()
        self.assertIn('PSBoundParameters.ContainsKey("WorkerMaximumConcurrency")', script)
        self.assertIn('ParameterName "WorkerMaximumConcurrency"', script)
        self.assertIn('lambda get-account-settings', script)
        self.assertIn('(2 * $WorkerMaximumConcurrency) + 4', script)
        self.assertIn('UnreservedConcurrentExecutions -lt $requiredCapacity', script)


if __name__ == "__main__":
    unittest.main()
