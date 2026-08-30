from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.model_eval import adapters, inference, report, suite
from evals.model_eval.__main__ import main


class FakeBedrock:
    meta = SimpleNamespace(region_name="us-east-1")

    def __init__(self, response=None):
        self.requests = []
        self.response = response

    def converse(self, **request):
        self.requests.append(request)
        if self.response:
            return copy.deepcopy(self.response)
        tool = request["toolConfig"]["tools"][0]["toolSpec"]["name"]
        return {"stopReason":"tool_use", "output":{"message":{"role":"assistant","content":[{"toolUse":{"toolUseId":"eval-tool", "name":tool, "input":{"score":0.9,"test_pass":True,"reason":"The supplied evidence supports the outcome, role-specific next step and factual boundaries.","label":"quality_review"}}}]}},"usage":{"inputTokens":100,"outputTokens":30,"totalTokens":130},"metrics":{"latencyMs":1}}


def response(value, reason="end_turn"):
    return {"stopReason":reason, "output":{"message":{"role":"assistant","content":[{"text":json.dumps(value)}]}},"usage":{"inputTokens":100,"outputTokens":50,"totalTokens":150}}


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.cases = suite.load_cases()

    def test_28_cases_cover_every_workflow_and_target(self):
        self.assertEqual(len(self.cases), 28)
        self.assertEqual(Counter(case["action"] for case in self.cases), {"brief.generate":12,"brief.refine":6,"handoff.generate":3,"catchup.generate":3,"meeting.analyze":4})
        self.assertEqual({case["target"] for case in self.cases if case["action"] == "brief.refine"}, suite.TARGETS)

    def test_meetings_are_bluemesa_only(self):
        for case in self.cases:
            if case["action"] == "meeting.analyze":
                self.assertEqual(case["customer"], "bluemesa")
                self.assertTrue(case["transcript"]["segments"])

    def test_all_prompt_shapes_are_valid_and_deterministic(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                built = adapters.build_prompts(case)
                self.assertEqual(built["promptHash"], adapters.build_prompts(case)["promptHash"])
                for part in built["parts"]:
                    self.assertIsInstance(part["content"], list)
                    self.assertTrue(part["system"])
                    self.assertTrue(all(isinstance(block, dict) for block in part["content"]))
                self.assertIn(case["request"]["company"], json.dumps(built))

    def test_filters_and_unknown_case(self):
        self.assertEqual(len(suite.select_cases(self.cases, tags=["smoke"])), 3)
        self.assertEqual(len(suite.select_cases(self.cases, tags=["refinement"])), 6)
        with self.assertRaises(ValueError):
            suite.select_cases(self.cases, ids=["typo"])
        with self.assertRaises(ValueError):
            suite.select_cases(self.cases, tags=["missing"])

    def test_baseline_and_context_are_not_mutated(self):
        first = next(case for case in self.cases if case["id"] == "refine-business-aws-correction")
        first["previous"]["technical"][0] = "mutated"
        second = next(case for case in suite.load_cases() if case["id"] == first["id"])
        self.assertNotEqual(first["previous"]["technical"][0], second["previous"]["technical"][0])

    def test_custom_model_is_not_silently_mapped_to_pro(self):
        selected = suite.load_models(suite.ROOT / "evals/models.json", ["nova-pro"], ["candidate=vendor.new-model-v1:0"])
        self.assertEqual(selected["candidate"]["modelId"], "vendor.new-model-v1:0")
        with self.assertRaises(ValueError):
            suite.load_models(suite.ROOT / "evals/models.json", ["typo"], [])

    def test_missing_prices_are_unknown(self):
        self.assertIsNone(suite.token_cost({}, {"inputTokens":100}))
        self.assertAlmostEqual(suite.token_cost({"inputUsdPerMillion":1,"outputUsdPerMillion":2}, {"inputTokens":100,"outputTokens":50}), 0.0002)

    def test_plan_and_list_do_not_contact_aws(self):
        with patch("evals.model_eval.__main__._aws", side_effect=AssertionError("No AWS in preview")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--tag","smoke"]), 0)
            self.assertEqual(main(["--list"]), 0)

    def test_plan_rejects_oversized_paid_run(self):
        with patch("evals.model_eval.__main__._aws", side_effect=AssertionError("Preflight must not run")), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--live","--limit","0","--max-calls","1"]), 2)


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case["id"]:case for case in suite.load_cases()}

    def test_rejects_truncated_or_non_json_output(self):
        with self.assertRaises(ValueError):
            adapters.parse_response(response({"x":1}, "max_tokens"))
        with self.assertRaises(ValueError):
            adapters.parse_response(response([]))
        self.assertEqual(adapters.parse_response(response({"x":1})), {"x":1})

    def test_forbidden_extra_target_is_not_repaired_away(self):
        case = self.cases["refine-executive-outcomes"]
        output = {"executive":case["previous"]["executive"],"businessCase":case["previous"]["businessCase"],"citations":["Customer context"]}
        checks = adapters.validate_output(case, output, adapters.build_prompts(case))
        self.assertFalse(next(item for item in checks if item["name"] == "production-contract")["passed"])

    def test_stale_aws_migration_and_unchanged_passages_fail(self):
        case = self.cases["refine-technical-payroll"]
        output = {"technical":copy.deepcopy(case["previous"]["technical"]),"citations":["Customer context"]}
        output["technical"][0] += " Plan an initial migration from on-premises to AWS."
        checks = adapters.validate_output(case, output, adapters.build_prompts(case))
        self.assertFalse(next(item for item in checks if item["name"] == "refinement-isolation-and-corrections")["passed"])

    def test_valid_target_merge_preserves_every_other_section(self):
        case = self.cases["refine-technical-payroll"]
        output = {"technical":[item + " BlueMesa is already on AWS. Validate the payroll idempotency and reconciliation evidence with the named owner." for item in case["previous"]["technical"]],"citations":["Customer context","Refinement feedback"]}
        before = copy.deepcopy(case)
        checks = adapters.validate_output(case, output, adapters.build_prompts(case))
        self.assertTrue(all(item["passed"] for item in checks), checks)
        self.assertEqual(case, before)

    def test_catchup_cannot_emit_project_updates(self):
        case = self.cases["catchup-new-member"]
        output = {"projectAnswer":"Payroll context with Priya Shah and the existing AWS platform. " * 4,"citations":["Approved brief"],"projectUpdate":{}}
        checks = adapters.validate_output(case, output, adapters.build_prompts(case))
        self.assertFalse(checks[0]["passed"])

    def test_invented_source_label_fails(self):
        with self.assertRaises(ValueError):
            adapters._citation_check({"citations":["Invented research"]}, ["Customer context"])

    def test_meeting_fabricated_quote_fails(self):
        case = self.cases["meeting-retain-names"]
        from pipeline.meeting_contracts import ANALYSIS_LIST_FIELDS
        output = {key:[] for key in ANALYSIS_LIST_FIELDS}
        output.update(meetingSummary="Payroll discussion",proposedHandoffSummary="Review proposals",citations=["00:00-00:20"])
        output["confirmedFacts"] = [{"id":"fact-1","statement":"Release approved","status":"confirmed","speaker":"Ariana Cole","timestampStart":0,"timestampEnd":20,"evidenceText":"The release is approved.","confidence":1,"sourceType":"meeting transcript"}]
        with self.assertRaisesRegex(ValueError, "Unsupported quote"):
            adapters._strict_meeting(output, case)

    def test_anchor_presence_is_not_a_quality_score(self):
        case = self.cases["generate-bluemesa-payroll"]
        checks = adapters.validate_output(case, {"businessCase":{"scenario":"payroll","desiredOutcomes":"payroll"},"technical":["reconciliation"]}, adapters.build_prompts(case))
        self.assertFalse(checks[0]["passed"])
        self.assertTrue(any(item["name"].startswith("anchor:") and item["passed"] for item in checks))


class RunnerTests(unittest.TestCase):
    def test_missing_judge_dependency_fails_before_aws(self):
        with patch("evals.model_eval.inference.require_judge_dependencies", side_effect=ValueError("Install evals/requirements.txt")), patch("evals.model_eval.__main__._aws", side_effect=AssertionError("No AWS before dependencies pass")) as aws, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--live", "--case", "catchup-new-member"]), 2)
            aws.assert_not_called()

    def test_call_limit_prevents_additional_api_requests(self):
        fake, calls = FakeBedrock(response({"ok":True})), []
        client = inference.MeteredClient(fake, inference.CallBudget(1), calls, kind="candidate",config={},guardrail={})
        client.converse(modelId="test")
        with self.assertRaises(inference.BudgetExceeded):
            client.converse(modelId="test")
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["estimatedTokenCostUsd"])

    def test_mocked_live_run_writes_reports_and_no_customer_state(self):
        answer = {"projectAnswer":"BlueMesa is preparing payroll partner integration on its existing AWS platform. Priya Shah owns operations, Rachel Kim controls risk approval and Dev Malik owns technical readiness. Discovery is approved but funding, retention and recovery objectives still require evidence.","citations":["Approved brief"]}
        fake = FakeBedrock(response(answer))
        with tempfile.TemporaryDirectory() as temp, patch("evals.model_eval.__main__._aws", return_value=(None,None,{"guardrailIdentifier":"test","guardrailVersion":"1"},fake)), contextlib.redirect_stdout(io.StringIO()):
            result = main(["--live","--case","catchup-new-member","--judge","none","--output",temp])
            self.assertEqual(result, 0)
            folder = next(Path(temp).iterdir())
            saved = json.loads((folder / "results.json").read_text())
            self.assertFalse(saved["run"]["customerStateWrites"])
            self.assertEqual(saved["summary"][0]["judgeCoverage"], 0)
            self.assertEqual(saved["results"][0]["status"], "checks_passed")
            self.assertTrue((folder / "report.md").exists())
            self.assertTrue((folder / "review.csv").exists())
            self.assertIn("guardrailConfig", fake.requests[0])

    def test_failures_stay_in_summary_denominator(self):
        rows = [{"model":"test","output":None,"calls":[],"checks":[],"judge":None,"status":"error"},{"model":"test","output":{"ok":True},"calls":[],"checks":[{"passed":True}],"judge":{"scoreOutOf10":9},"status":"passed","generationMs":123}]
        result = report.summarize(rows)[0]
        self.assertEqual((result["trials"], result["generated"], result["judgeCoverage"]), (2,1,1))
        self.assertIsNone(result["estimatedTokenCostUsd"])

    @unittest.skipUnless(importlib.util.find_spec("strands_evals"), "Install evals/requirements.txt for SDK integration tests")
    def test_real_strands_evaluator_with_fake_bedrock_transport(self):
        import boto3
        from strands.models import BedrockModel
        from botocore.config import Config
        model = BedrockModel(boto_session=boto3.Session(aws_access_key_id="testing",aws_secret_access_key="testing",region_name="us-east-1"),boto_client_config=Config(retries={"total_max_attempts":1}),model_id="us.amazon.nova-pro-v1:0",streaming=False)
        fake, calls = FakeBedrock(), []
        model.client = inference.MeteredClient(fake, inference.CallBudget(4), calls, kind="judge",config={},guardrail={})
        case = next(case for case in suite.load_cases() if case["id"] == "catchup-new-member")
        result = inference.evaluate_with_model(case,{"projectAnswer":"A synthetic evaluation response."},adapters.build_prompts(case),model)
        self.assertEqual(result["scoreOutOf10"], 9)
        self.assertTrue(calls)
        self.assertTrue(all(call["kind"] == "judge" for call in calls))


if __name__ == "__main__":
    unittest.main()
