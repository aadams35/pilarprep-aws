from __future__ import annotations

import ast
import importlib.util
import types
import json
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


AGENTCORE_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTCORE_ROOT))
sys.path.insert(0, str(BACKEND_ROOT))

from runtime import meeting as meeting_runtime  # noqa: E402
from runtime import service as runtime_service  # noqa: E402
from runtime.service import _json_from_model, _validate_agent_result, handle_request  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
HANDOFF_PAYLOAD = json.loads((ROOT / "events" / "bluemesa-handoff.json").read_text())
CATCHUP_PAYLOAD = json.loads((ROOT / "events" / "bluemesa-catchup.json").read_text())
CATCHUP_PAYLOAD["approvedBrief"] = json.loads(
    json.dumps(HANDOFF_PAYLOAD["approvedBrief"])
)
CATCHUP_PAYLOAD["briefRequest"]["approvedBrief"] = json.loads(
    json.dumps(HANDOFF_PAYLOAD["approvedBrief"])
)

NEXT_STEPS = {
    "immediateActions": [
        {
            "action": "Collect settlement architecture and recovery evidence",
            "owner": "Dev Malik",
            "timing": "Within two business days",
            "dependency": "Current diagrams, overnight batch inventory, and recovery targets",
            "decisionGate": "Evidence is complete enough to run the recovery workshop",
        },
        {
            "action": "Validate PCI and identity separation evidence",
            "owner": "Rachel Kim",
            "timing": "Within five business days",
            "dependency": "Control inventory and acquired-system access model",
            "decisionGate": "Compliance accepts the evidence path for the bounded pilot",
        },
        {
            "action": "Approve or redirect the bounded pilot",
            "owner": "Ariana Cole",
            "timing": "By the end of week one",
            "dependency": "Recovery and control workshop findings",
            "decisionGate": "Sponsor records a go, pause, or redirect decision",
        },
    ],
    "openQuestions": [
        "What recovery threshold must be proven before customer traffic moves?",
        "Who approves an exception to the PCI evidence gate?",
    ],
    "nextMeeting": {
        "purpose": "Review recovery and PCI evidence and decide the bounded pilot",
        "timing": "Within five business days",
        "attendees": ["Ariana Cole", "Dev Malik", "Rachel Kim", "Solutions Architect"],
    },
    "customerSummary": "We will review the agreed recovery and PCI evidence, confirm owners, and make a bounded pilot decision before customer traffic moves.",
    "internalNotes": "Do not treat recovery or identity assumptions as confirmed until BlueMesa attaches evidence and the named approvers accept it.",
}



MODEL_RESULT = {
    "projectAnswer": (
        "BlueMesa should begin with a bounded settlement-recovery pilot that protects merchant trust, "
        "gives Rachel Kim clean PCI and identity evidence, and gives Dev Malik measurable recovery and rollback proof before customer traffic moves."
    ),
    "projectArtifacts": {
        "twoWeekPlan": [
            {"title": "Dependency map", "detail": "Map settlement dependencies.", "owner": "Dev Malik", "status": "Week 1"},
            {"title": "Recovery test", "detail": "Demonstrate rollback and recovery.", "owner": "Platform team", "status": "Week 2"},
        ],
        "riskRegister": [
            {"title": "Settlement delay", "detail": "Batch failure delays funds.", "owner": "Dev Malik", "status": "High"},
            {"title": "Evidence gap", "detail": "PCI evidence is incomplete.", "owner": "Rachel Kim", "status": "High"},
        ],
        "stakeholderMap": [
            {"title": "Sponsor", "detail": "Owns outcome.", "owner": "Ariana Cole", "status": "Sponsor"},
            {"title": "Approver", "detail": "Owns controls.", "owner": "Rachel Kim", "status": "Approver"},
        ],
        "followUpEmail": {
            "subject": "BlueMesa pilot next steps",
            "body": "We captured a bounded pilot with named owners and evidence gates before any customer traffic moves.",
        },
        "nextSteps": NEXT_STEPS,
    },
    "projectUpdate": {
        "assumptions": [{"title": "Freeze date", "detail": "Holiday freeze date is not yet confirmed.", "owner": "Ariana Cole", "status": "Validate", "source": "Approved brief"}],
        "decisions": [{"title": "Pilot", "detail": "Pilot first.", "owner": "Ariana Cole", "status": "Approved", "source": "Meeting outcomes"}],
        "risks": [{"title": "Recovery", "detail": "Not proven.", "owner": "Dev Malik", "status": "Open", "source": "Approved brief"}],
        "actions": [{"title": "Test", "detail": "Run recovery.", "owner": "Dev Malik", "status": "Next", "source": "Meeting outcomes"}],
        "nextSteps": NEXT_STEPS,
        "owners": [{"title": "Controls", "detail": "Own evidence.", "owner": "Rachel Kim", "status": "Assigned", "source": "Meeting outcomes"}],
        "milestones": [{"title": "Gate", "detail": "Review evidence.", "owner": "Ariana Cole", "status": "Planned", "source": "Approved brief"}],
        "openQuestions": [{"title": "RTO", "detail": "Confirm target.", "owner": "Dev Malik", "status": "Open", "source": "Approved brief"}],
    },
    "citations": ["Latest approved PilarPrep brief", "Approved meeting outcomes"],
}


class FakeGateway:
    calls = []

    def __enter__(self):
        type(self).calls = []
        return self

    def __exit__(self, *_args):
        return None

    def call(self, name, arguments):
        type(self).calls.append((name, arguments))
        if name == "get_latest_brief":
            return {
                "brief": dict(HANDOFF_PAYLOAD["approvedBrief"]),
                "metadata": {
                    "artifactKey": "brief/latest.json",
                    "packetVersion": 2,
                    "approvalStatus": "approved",
                },
            }
        if name == "get_project_state":
            return {"version": 2, "assumptions": [], "decisions": [], "risks": [], "actions": [], "owners": [], "milestones": [], "openQuestions": []}
        if name == "generate_catchup":
            return {"audienceRole": arguments["audienceRole"], "sources": ["brief", "state"]}
        if name == "save_project_update":
            return {**arguments["update"], "version": 3, "stateKey": "PROJECT#STATE"}
        if name == "create_handoff_packet":
            return {
                "artifactKey": "handoff/latest.json",
                "docxArtifactKey": "handoff/latest.docx",
                "docxDownloadUrl": "https://download.example/handoff.docx",
                "artifactRetention": "latest-only",
            }
        raise AssertionError(name)


def reasoner(prompt, model_id, memory):
    assert "BlueMesa" in prompt
    assert model_id.endswith("nova-pro-v1:0")
    assert memory == {"memory": "enabled"}
    if prompt.startswith('{"mode":"catchup"'):
        return {
            "projectAnswer": MODEL_RESULT["projectAnswer"],
            "citations": MODEL_RESULT["citations"],
        }
    return MODEL_RESULT

class RuntimeTests(unittest.TestCase):
    def invoke(self, payload):
        return handle_request(
            payload,
            gateway_factory=FakeGateway,
            reasoner=reasoner,
            memory_factory=lambda _scope: nullcontext({"memory": "enabled"}),
        )

    def test_handoff_preserves_brief_assessments_in_response_and_saved_packet(self):
        payload = json.loads(json.dumps(HANDOFF_PAYLOAD))
        approved = payload["approvedBrief"]
        approved["sourceCatalog"] = [{
            "sourceId": "src-approved-context",
            "title": "Customer-approved BlueMesa scenario",
            "evidenceSnippet": "The customer approved the bounded discovery scope.",
        }]
        approved["claims"] = [{
            "claimId": "claim-approved-technical",
            "section": "technical",
            "itemIndex": 0,
            "text": approved["technical"][0],
            "sourceIds": ["src-approved-context"],
            "evidenceStatus": "customer-provided",
            "validationStatus": "supported-by-customer-context",
            "evidenceSnippet": "The customer approved the bounded discovery scope.",
        }]
        approved["evidenceCoverage"] = {
            "materialClaims": 1,
            "claimsWithApprovedSources": 1,
            "coveragePercent": 100,
            "statusCounts": {"customer-provided": 1},
            "meaning": "Coverage measures approved source linkage, not probability of truth.",
        }
        payload["briefRequest"]["approvedBrief"] = approved
        with patch.dict(HANDOFF_PAYLOAD, {"approvedBrief": approved}):
            result = self.invoke(payload)
        persisted = next(args["packet"] for name, args in FakeGateway.calls if name == "create_handoff_packet")
        for packet in (result, persisted):
            for field in ("sourceCatalog", "claims", "evidenceCoverage"):
                self.assertEqual(packet.get(field), approved[field], field)
            self.assertEqual(packet["metadata"]["packetVersion"], 2)
            self.assertEqual(packet["metadata"]["handoffAudienceRole"], payload["audienceRole"])
            self.assertEqual(packet["metadata"]["handoffFocus"], payload["focus"])

    def test_handoff_reads_before_reasoning_then_performs_governed_writes(self):
        result = self.invoke(HANDOFF_PAYLOAD)
        names = [name for name, _arguments in FakeGateway.calls]
        self.assertEqual(
            names,
            [
                "get_latest_brief",
                "get_project_state",
                "save_project_update",
                "create_handoff_packet",
            ],
        )
        self.assertEqual(result["provider"], "agentcore")
        self.assertEqual(result["businessCase"]["scenario"], HANDOFF_PAYLOAD["approvedBrief"]["businessCase"]["scenario"])
        self.assertEqual(result["metadata"]["projectVersion"], 3)
        self.assertTrue(result["metadata"]["memoryUsed"])
        self.assertEqual(result["metadata"]["docxArtifactKey"], "handoff/latest.docx")
        self.assertTrue(FakeGateway.calls[2][1]["confirmWrite"])
        self.assertEqual(FakeGateway.calls[2][1]["update"]["nextSteps"]["immediateActions"][0]["owner"], "Dev Malik")
        persisted_packet = FakeGateway.calls[3][1]["packet"]
        self.assertEqual(persisted_packet["metadata"]["projectVersion"], 3)
        self.assertEqual(persisted_packet["metadata"]["approvedPacketVersion"], 2)
        self.assertIn(
            "create_handoff_packet",
            persisted_packet["metadata"]["toolCalls"],
        )
        self.assertEqual(result["projectArtifacts"]["nextSteps"]["nextMeeting"]["timing"], "Within five business days")
        self.assertEqual(len(result["projectArtifacts"]["nextSteps"]["openQuestions"]), 2)
        self.assertRegex(result["projectArtifacts"]["twoWeekPlan"][0]["title"], r"^Days 1-2:")
        self.assertTrue(result["projectArtifacts"]["riskRegister"][0]["title"].startswith("Unvalidated assumption:"))
        self.assertEqual(result["projectArtifacts"]["riskRegister"][0]["status"], "Unvalidated")
        self.assertIn("Customer-approved BlueMesa scenario", result["citations"])
        self.assertEqual(result["evidence"][0]["section"], "technical")
        self.assertEqual(result["evidence"][-1]["section"], "projectAnswer")
        self.assertTrue(
            all(
                source in result["citations"]
                for item in result["evidence"]
                for source in item["sources"]
            )
        )

    def test_handoff_screens_only_user_context_before_prompt_assembly(self):
        calls = []

        def screen(value, *, source, **_kwargs):
            calls.append((source, json.loads(json.dumps(value))))
            return value, {"source": source, "policyResult": "passed"}

        with patch.object(
            runtime_service.content_safety,
            "screen_payload",
            side_effect=screen,
        ):
            result = self.invoke(HANDOFF_PAYLOAD)

        self.assertEqual(result["provider"], "agentcore")
        self.assertEqual(calls[0][0], "INPUT")
        self.assertEqual(set(calls[0][1]), {"focus", "meetingNotes"})
        self.assertNotIn("approvedBrief", calls[0][1])
        self.assertEqual(calls[-1][0], "OUTPUT")

    def test_handoff_validator_defaults_optional_action_details_without_duplicate_next_steps(self):
        raw = json.loads(json.dumps(MODEL_RESULT))
        first_action = raw["projectArtifacts"]["nextSteps"]["immediateActions"][0]
        first_action.pop("dependency")
        first_action["decisionGate"] = None
        raw["projectUpdate"].pop("nextSteps")

        validated = _validate_agent_result(raw)
        action = validated["projectArtifacts"]["nextSteps"]["immediateActions"][0]
        self.assertIn("confirm", action["dependency"].lower())
        self.assertIn("evidence", action["decisionGate"].lower())
        self.assertEqual(
            validated["projectUpdate"]["nextSteps"],
            validated["projectArtifacts"]["nextSteps"],
        )

    def test_handoff_schema_accepts_optional_action_details_without_duplicate_next_steps(self):
        raw = json.loads(json.dumps(MODEL_RESULT))
        first_action = raw["projectArtifacts"]["nextSteps"]["immediateActions"][0]
        first_action.pop("dependency")
        first_action["decisionGate"] = None
        raw["projectUpdate"].pop("nextSteps")

        try:
            model = runtime_service._handoff_output_model()
        except ModuleNotFoundError:
            self.skipTest("Pydantic is installed only in the packaged AgentCore runtime")
        parsed = model.model_validate(raw).model_dump()

        action = parsed["projectArtifacts"]["nextSteps"]["immediateActions"][0]
        self.assertIsNone(action["dependency"])
        self.assertIsNone(action["decisionGate"])
        self.assertNotIn("nextSteps", parsed["projectUpdate"])

    def test_handoff_rejects_a_snapshot_that_differs_from_stored_brief(self):
        payload = json.loads(json.dumps(HANDOFF_PAYLOAD))
        payload["approvedBrief"]["technical"] = ["Tampered browser snapshot"]
        with self.assertRaisesRegex(ValueError, "latest stored brief"):
            self.invoke(payload)
        self.assertEqual([name for name, _arguments in FakeGateway.calls], ["get_latest_brief"])

    def test_handoff_rejects_a_tampered_business_case(self):
        payload = json.loads(json.dumps(HANDOFF_PAYLOAD))
        payload["approvedBrief"]["businessCase"]["scenario"] = "Unapproved replacement scenario"
        with self.assertRaisesRegex(ValueError, "latest stored brief"):
            self.invoke(payload)
        self.assertEqual([name for name, _arguments in FakeGateway.calls], ["get_latest_brief"])

    def test_catchup_rejects_a_snapshot_that_differs_from_latest_stored_brief(self):
        payload = json.loads(json.dumps(CATCHUP_PAYLOAD))
        payload["approvedBrief"]["technical"] = ["Stale browser packet"]
        with self.assertRaisesRegex(ValueError, "latest stored brief"):
            self.invoke(payload)
        self.assertEqual([name for name, _arguments in FakeGateway.calls], ["get_latest_brief"])

    def test_catchup_is_role_aware_and_read_only(self):
        result = self.invoke(CATCHUP_PAYLOAD)
        names = [name for name, _arguments in FakeGateway.calls]
        self.assertEqual(
            names,
            ["get_latest_brief", "get_project_state", "generate_catchup"],
        )
        self.assertEqual(FakeGateway.calls[2][1]["audienceRole"], "New member")
        self.assertNotIn("create_handoff_packet", names)
        self.assertIn("merchant trust", result["projectAnswer"])
        self.assertGreaterEqual(len(result["projectArtifacts"]["nextSteps"]["immediateActions"]), 3)
        self.assertTrue(result["projectArtifacts"]["riskRegister"][0]["title"].startswith("Unvalidated assumption:"))

    def test_solutions_architect_catchup_has_specific_grounded_requirements_and_is_read_only(self):
        payload = json.loads(json.dumps(CATCHUP_PAYLOAD))
        payload["audienceRole"] = "Solutions Architect"
        payload["focus"] = "What architecture assumptions must I validate?"
        payload["briefRequest"]["role"] = "Solutions Architect"
        payload["briefRequest"]["prompt"] = payload["focus"]
        captured = {}

        def sa_reasoner(prompt, model_id, memory):
            captured["prompt"] = prompt
            captured["modelId"] = model_id
            captured["memory"] = memory
            return {
                "citations": list(MODEL_RESULT["citations"]),
                "projectAnswer": (
                    "Solutions Architect view: BlueMesa's approved business case establishes merchant trust and settlement continuity as the customer outcomes. "
                    "Treat topology, identity boundaries, RTO/RPO, PCI scope, workload metrics, cost, observability, and rollback as hypotheses until customer artifacts validate them. "
                    "Evaluate AWS services only when they answer a customer decision, record risks and dependencies with owners, and use the recovery evidence as the next approval gate. "
                    "Schedule the next technical session with Dev Malik, Rachel Kim, the platform team, and the Solutions Architect to decide whether the bounded pilot can proceed."
                ),
            }
        result = handle_request(
            payload,
            gateway_factory=FakeGateway,
            reasoner=sa_reasoner,
            memory_factory=lambda _scope: nullcontext({"memory": "enabled"}),
        )
        names = [name for name, _arguments in FakeGateway.calls]

        self.assertEqual(names, ["get_latest_brief", "get_project_state", "generate_catchup"])
        self.assertTrue(captured["prompt"].startswith('{"mode":"catchup"'))
        self.assertIn('"audienceRole":"Solutions Architect"', captured["prompt"])
        self.assertNotIn('"audienceRequirements"', captured["prompt"])
        self.assertIn('"technicalBrief"', captured["prompt"])
        self.assertIn('"allowedSourceLabels"', captured["prompt"])
        self.assertIn("Solutions Architect view", result["projectAnswer"])
        self.assertNotIn("save_project_update", names)
        self.assertNotIn("create_handoff_packet", names)
    def test_handoff_and_catchup_use_the_latest_refined_packet(self):
        refined_brief = json.loads(json.dumps(HANDOFF_PAYLOAD["approvedBrief"]))
        refined_brief["technical"][0] += " Refined recovery evidence question."
        refined_brief["executive"][0] += " Refined sponsor value framing."
        refined_brief["citations"] = list(
            dict.fromkeys(
                list(refined_brief.get("citations", []))
                + ["Refinement feedback"]
            )
        )

        class RefinedGateway(FakeGateway):
            def call(self, name, arguments):
                if name == "get_latest_brief":
                    type(self).calls.append((name, arguments))
                    return {
                        "brief": json.loads(json.dumps(refined_brief)),
                        "metadata": {
                            "artifactKey": "brief/latest.json",
                            "briefVersion": "refined-v8",
                        },
                    }
                return super().call(name, arguments)

        handoff_payload = json.loads(json.dumps(HANDOFF_PAYLOAD))
        handoff_payload["approvedBrief"] = json.loads(json.dumps(refined_brief))
        captured_prompts = []

        def refined_reasoner(prompt, model_id, memory):
            captured_prompts.append(prompt)
            self.assertIn("Refined recovery evidence question", prompt)
            self.assertIn("Refined sponsor value framing", prompt)
            return reasoner(prompt, model_id, memory)

        handoff = handle_request(
            handoff_payload,
            gateway_factory=RefinedGateway,
            reasoner=refined_reasoner,
            memory_factory=lambda _scope: nullcontext({"memory": "enabled"}),
        )
        catchup_payload = json.loads(json.dumps(CATCHUP_PAYLOAD))
        catchup_payload["approvedBrief"] = json.loads(json.dumps(refined_brief))
        catchup_payload["briefRequest"]["approvedBrief"] = json.loads(json.dumps(refined_brief))
        catchup = handle_request(
            catchup_payload,
            gateway_factory=RefinedGateway,
            reasoner=refined_reasoner,
            memory_factory=lambda _scope: nullcontext({"memory": "enabled"}),
        )

        self.assertIn("Refined recovery evidence question", handoff["technical"][0])
        self.assertIn("Refined sponsor value framing", handoff["executive"][0])
        self.assertIn("Refined recovery evidence question", catchup["technical"][0])
        self.assertIn("Refined sponsor value framing", catchup["executive"][0])
        self.assertEqual(len(captured_prompts), 2)
    def test_same_project_session_is_forwarded_on_second_request(self):
        captured = []

        def memory_factory(scope):
            captured.append((scope["tenantId"], scope["clientId"], scope["projectId"], scope["sessionId"]))
            return nullcontext({"memory": "enabled"})

        for payload in (HANDOFF_PAYLOAD, CATCHUP_PAYLOAD):
            handle_request(
                payload,
                gateway_factory=FakeGateway,
                reasoner=reasoner,
                memory_factory=memory_factory,
            )
        self.assertEqual(captured[0], captured[1])

    def test_agent_result_rejects_invented_source_labels(self):
        invented = json.loads(json.dumps(MODEL_RESULT))
        invented["projectUpdate"]["risks"][0]["source"] = "Unverified internet claim"

        with self.assertRaisesRegex(ValueError, "approved evidence set"):
            handle_request(
                HANDOFF_PAYLOAD,
                gateway_factory=FakeGateway,
                reasoner=lambda *_args: invented,
                memory_factory=lambda _scope: nullcontext({"memory": "enabled"}),
            )

    def test_ungrounded_agent_result_is_rejected(self):
        ungrounded = {**MODEL_RESULT, "citations": []}
        with self.assertRaisesRegex(ValueError, "approved source citation"):
            _validate_agent_result(ungrounded)

    def test_catchup_discards_unapproved_source_labels(self):
        generated = {
            "projectAnswer": "Grounded catch-up narrative.",
            "citations": ["Invented source", "Approved brief"],
        }
        runtime_service._normalize_catchup_sources(
            generated,
            ["Approved brief", "Latest approved PilarPrep brief"],
        )
        self.assertEqual(generated["citations"], ["Approved brief"])

    def test_handoff_canonicalizes_only_known_source_aliases(self):
        generated = {
            "citations": [
                "approved brief",
                "current project state",
                "memory supplied in this request",
                "Invented brief from the internet",
            ],
            "projectUpdate": {
                register: [] for register in runtime_service.REGISTER_NAMES
            },
        }
        generated["projectUpdate"]["actions"] = [
            {"source": "current project state"}
        ]
        allowed = [
            "Latest approved PilarPrep brief",
            "Approved meeting outcomes",
            "DynamoDB project state",
            "AgentCore project memory",
        ]

        runtime_service._normalize_handoff_sources(generated, allowed)

        self.assertEqual(
            generated["citations"],
            [
                "Latest approved PilarPrep brief",
                "DynamoDB project state",
                "AgentCore project memory",
            ],
        )
        self.assertEqual(
            generated["projectUpdate"]["actions"][0]["source"],
            "DynamoDB project state",
        )

    def test_handoff_rejects_unknown_register_source(self):
        generated = {
            "citations": ["Approved brief"],
            "projectUpdate": {
                register: [] for register in runtime_service.REGISTER_NAMES
            },
        }
        generated["projectUpdate"]["risks"] = [
            {"source": "Unverified internet source"}
        ]
        with self.assertRaisesRegex(ValueError, "projectUpdate.risks\\[0\\]"):
            runtime_service._normalize_handoff_sources(
                generated,
                ["Latest approved PilarPrep brief"],
            )

    def test_catchup_uses_canonical_source_when_model_labels_are_invalid(self):
        generated = {
            "projectAnswer": "Grounded catch-up narrative.",
            "citations": ["Invented source"],
        }
        runtime_service._normalize_catchup_sources(
            generated,
            ["Latest approved PilarPrep brief"],
        )
        self.assertEqual(
            generated["citations"], ["Latest approved PilarPrep brief"]
        )

    def test_catchup_missing_model_citations_uses_approved_canonical_source(self):
        generated = {
            "projectAnswer": (
                "The approved packet confirms the customer context and current "
                "project position. Start with the recorded decisions, validate "
                "the open assumptions with their owners, and use the saved "
                "handoff as the working plan for the next customer session. "
                "Keep new findings separated from confirmed facts until the "
                "customer validates them."
            ),
            "citations": [],
        }

        result = handle_request(
            CATCHUP_PAYLOAD,
            gateway_factory=FakeGateway,
            reasoner=lambda *_args: generated,
            memory_factory=lambda _scope: nullcontext({"memory": "enabled"}),
        )

        self.assertIn("Latest approved PilarPrep brief", result["citations"])
        self.assertEqual(
            result["metadata"]["toolCalls"],
            ["get_latest_brief", "get_project_state", "generate_catchup"],
        )

    def test_strands_structured_output_is_read_without_text_content(self):
        expected = {
            "projectAnswer": MODEL_RESULT["projectAnswer"],
            "citations": MODEL_RESULT["citations"],
        }

        class StructuredOutput:
            def model_dump(self):
                return expected

        class AgentResult:
            structured_output = StructuredOutput()
            message = {"content": []}

            def __str__(self):
                return ""

        self.assertEqual(_json_from_model(AgentResult()), expected)

    def test_strands_tool_use_payload_is_read_when_text_content_is_empty(self):
        expected = {
            "projectAnswer": MODEL_RESULT["projectAnswer"],
            "citations": MODEL_RESULT["citations"],
        }

        class AgentResult:
            structured_output = None
            message = {"content": [{"toolUse": {"input": expected}}]}

            def __str__(self):
                return ""

        self.assertEqual(_json_from_model(AgentResult()), expected)

    def test_strands_message_text_is_read_when_rendered_value_is_empty(self):
        expected = {
            "projectAnswer": MODEL_RESULT["projectAnswer"],
            "citations": MODEL_RESULT["citations"],
        }

        class AgentResult:
            structured_output = None
            message = {"content": [{"text": f"```json\n{json.dumps(expected)}\n```"}]}

            def __str__(self):
                return ""

        self.assertEqual(_json_from_model(AgentResult()), expected)

    def test_strands_invalid_json_is_repaired_once(self):
        expected = {
            "projectAnswer": MODEL_RESULT["projectAnswer"],
            "citations": MODEL_RESULT["citations"],
        }
        prompts = []

        def agent(prompt, **_options):
            prompts.append(prompt)
            return "not json" if len(prompts) == 1 else json.dumps(expected)

        self.assertEqual(runtime_service._invoke_json_agent(agent, "original"), expected)
        self.assertEqual(len(prompts), 2)
        self.assertIn("entire answer", prompts[1])

    def test_handoff_schema_failure_is_repaired_once(self):
        invalid = json.loads(json.dumps(MODEL_RESULT))
        invalid["projectArtifacts"]["riskRegister"] = invalid["projectArtifacts"][
            "riskRegister"
        ][:1]
        calls = []

        def reasoner(prompt, _model_id, _session_manager):
            calls.append(json.loads(prompt))
            return invalid if len(calls) == 1 else MODEL_RESULT

        result = runtime_service._reason_and_validate_agent_result(
            '{"mode":"handoff"}',
            "amazon.nova-pro-v1:0",
            object(),
            reasoner,
        )

        self.assertEqual(result["projectAnswer"], MODEL_RESULT["projectAnswer"])
        self.assertEqual(len(calls), 2)
        self.assertIn("riskRegister", calls[1]["schemaRepair"]["validationError"])

    def test_schema_repair_uses_a_distinct_strands_agent_id(self):
        captured = {}
        fake_strands = types.ModuleType("strands")
        fake_models = types.ModuleType("strands.models")

        class BedrockModel:
            def __init__(self, **_options):
                pass

        class Agent:
            def __init__(self, **options):
                captured.update(options)

            def __call__(self, *_args, **_kwargs):
                return json.dumps(MODEL_RESULT)

        fake_strands.Agent = Agent
        fake_models.BedrockModel = BedrockModel
        prompt = json.dumps(
            {
                "mode": "handoff",
                "schemaRepair": {"validationError": "riskRegister requires 2 items"},
            }
        )
        with (
            patch.dict(
                sys.modules,
                {"strands": fake_strands, "strands.models": fake_models},
            ),
            patch.object(runtime_service, "_handoff_output_model", return_value=dict),
            patch.dict(
                runtime_service.os.environ,
                {"BEDROCK_GUARDRAIL_ID": "", "BEDROCK_GUARDRAIL_VERSION": ""},
            ),
        ):
            result = runtime_service._default_reasoner(
                prompt,
                "us.amazon.nova-pro-v1:0",
                {"memory": "available"},
            )

        self.assertEqual(result["projectAnswer"], MODEL_RESULT["projectAnswer"])
        self.assertEqual(captured["agent_id"], "pilarprep-handoff-repair")

    def test_guarded_user_content_excludes_approved_evidence(self):
        guarded = json.loads(
            runtime_service._guarded_user_content(
                {
                    "focus": "Prepare the evidence plan.",
                    "approvedMeetingOutcomes": "Customer-approved notes.",
                    "latestApprovedBrief": {"technical": ["Trusted brief"]},
                    "currentProjectState": {"risks": ["Trusted state"]},
                }
            )
        )
        self.assertEqual(
            guarded,
            {
                "focus": "Prepare the evidence plan.",
                "approvedMeetingOutcomes": "Customer-approved notes.",
            },
        )

    def test_handoff_context_preserves_facts_and_assessments_without_duplicate_history(self):
        approved = json.loads(json.dumps(HANDOFF_PAYLOAD["approvedBrief"]))
        approved["claims"] = [{
            "section": "technical", "itemIndex": 0, "text": "duplicate" * 30_000,
            "evidenceSnippet": "duplicate evidence" * 10_000,
            "evidenceStatus": "needs-validation", "validationStatus": "unsupported-no-matching-source",
            "sourceIds": [],
        }]
        approved["sourceCatalog"] = [{"sourceId": "source-1", "evidenceSnippet": "Payroll is on AWS."}]
        approved["projectAnswer"] = "old handoff" * 30_000
        approved["metadata"] = {"oldDiagnostics": "old" * 50_000}
        latest = {
            "brief": approved,
            "requestContext": {
                "context": "The customer is already on AWS and needs payroll integration.",
                "decisionMakers": [{"name": "Ariana Cole", "roleType": "decision-maker"}],
                "additionalDirection": "Keep payroll ownership explicit.",
                "approvedBrief": approved,
            },
            "metadata": {"packetVersion": 4, "approvalStatus": "approved", "docxDownloadUrl": "private-url"},
        }
        before = json.dumps(latest, sort_keys=True)
        request = {**HANDOFF_PAYLOAD, "action": "create_handoff"}
        prompt = runtime_service._prompt(request, latest, {"version": 2}, None, ["Approved brief"], [])
        parsed = json.loads(prompt)
        context = parsed["latestApprovedBrief"]
        for field in ("businessCase", "technical", "executive", "stakeholders", "gameplan", "objections"):
            self.assertEqual(context["brief"][field], approved[field])
        self.assertEqual(context["brief"]["claims"][0]["evidenceStatus"], "needs-validation")
        self.assertEqual(context["brief"]["sourceCatalog"], approved["sourceCatalog"])
        self.assertIn("already on AWS", prompt)
        self.assertIn("Ariana Cole", prompt)
        self.assertIn("payroll ownership", prompt)
        self.assertNotIn("old handoff", prompt)
        self.assertNotIn("private-url", prompt)
        self.assertLess(len(prompt), 20_000)
        self.assertEqual(json.dumps(latest, sort_keys=True), before)

    def test_context_limit_rejects_instead_of_cutting_json_or_customer_facts(self):
        for action in ("create_handoff", "generate_catchup"):
            with self.subTest(action=action):
                latest = {"brief": HANDOFF_PAYLOAD["approvedBrief"]}
                request = {**HANDOFF_PAYLOAD, "action": action, "meetingNotes": "Required fact. " * 10_000}
                with self.assertRaises(runtime_service.AgentContextLimitError):
                    runtime_service._prompt(request, latest, {}, None, [], [])

    def test_oversized_context_never_calls_the_model_or_writes_project_state(self):
        class OversizedGateway(FakeGateway):
            def call(self, name, arguments):
                result = super().call(name, arguments)
                if name == "get_latest_brief":
                    result["requestContext"] = {"context": "Important customer facts. " * 10_000}
                return result

        with patch.object(runtime_service, "_default_reasoner") as model:
            with self.assertRaises(runtime_service.AgentContextLimitError):
                handle_request(
                    HANDOFF_PAYLOAD, gateway_factory=OversizedGateway, reasoner=model,
                    memory_factory=lambda _scope: nullcontext(None),
                )
        model.assert_not_called()
        self.assertEqual([name for name, _arguments in OversizedGateway.calls], ["get_latest_brief", "get_project_state"])

    def test_guardrail_size_error_is_terminal_for_every_handoff_model(self):
        for model_id in runtime_service.MODEL_IDS.values():
            with self.subTest(model_id=model_id):
                fake_strands = types.ModuleType("strands")
                fake_models = types.ModuleType("strands.models")
                calls = []

                class Agent:
                    def __init__(self, **options):
                        self.options = options

                    def __call__(self, *_args, **_kwargs):
                        calls.append(1)
                        raise RuntimeError("ThrottlingException: Input text size (2095 text units) exceeds the maximum allowed (1000 text units) for the content filter policy")

                fake_strands.Agent = Agent
                fake_models.BedrockModel = lambda **_options: None
                with (
                    patch.dict(sys.modules, {"strands": fake_strands, "strands.models": fake_models}),
                    patch.object(runtime_service, "_handoff_output_model", return_value=dict),
                    patch.object(runtime_service, "_invoke_direct_json_reasoner") as recovery,
                ):
                    with self.assertRaises(runtime_service.AgentContextLimitError):
                        runtime_service._default_reasoner('{"mode":"handoff"}', model_id, None)
                self.assertEqual(len(calls), 1)
                recovery.assert_not_called()

    def test_capacity_throttling_is_not_mislabeled_as_an_input_size_failure(self):
        self.assertFalse(runtime_service._is_input_size_error(RuntimeError("ThrottlingException: Too many requests")))
        self.assertFalse(runtime_service._is_input_size_error(RuntimeError("ModelErrorException: Model processing failed")))

    def test_runtime_entrypoint_returns_a_safe_terminal_context_error(self):
        sdk = types.ModuleType("bedrock_agentcore")

        class App:
            def entrypoint(self, function):
                return function

        sdk.BedrockAgentCoreApp = App
        spec = importlib.util.spec_from_file_location("handoff_entrypoint_test", ROOT / "runtime" / "main.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"bedrock_agentcore": sdk}):
            spec.loader.exec_module(module)
        with patch.object(module, "handle_request", side_effect=runtime_service.AgentContextLimitError()):
            result = module.invoke({}, None)
        self.assertEqual(result["errorCode"], "AGENT_CONTEXT_TOO_LARGE")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["error"], runtime_service.CONTEXT_LIMIT_MESSAGE)
        with patch.object(module, "handle_request", side_effect=RuntimeError("temporary capacity error")):
            with self.assertRaisesRegex(RuntimeError, "temporary capacity error"):
                module.invoke({}, None)

    def test_structured_handoff_uses_guarded_content_and_output_model(self):
        expected = {
            "projectAnswer": MODEL_RESULT["projectAnswer"],
            "citations": MODEL_RESULT["citations"],
        }
        captured = {}

        class StructuredOutput:
            def model_dump(self):
                return expected

        class AgentResult:
            structured_output = StructuredOutput()
            message = {"content": []}

        class OutputModel:
            pass

        def agent(prompt, **options):
            captured["prompt"] = prompt
            captured["options"] = options
            return AgentResult()

        result = runtime_service._invoke_json_agent(
            agent,
            '{"mode":"handoff"}',
            guarded_content='{"focus":"customer input"}',
            output_model=OutputModel,
        )

        self.assertEqual(result, expected)
        self.assertEqual(
            captured["options"]["structured_output_model"],
            OutputModel,
        )
        self.assertEqual(captured["prompt"][0]["text"], '{"mode":"handoff"}')
        self.assertEqual(
            captured["prompt"][1]["guardContent"]["text"]["qualifiers"],
            ["guard_content"],
        )

    def test_handoff_uses_non_streaming_structured_output_with_model_specific_latency(self):
        for model_id in (
            "us.amazon.nova-pro-v1:0",
            "us.amazon.nova-micro-v1:0",
            "global.anthropic.claude-sonnet-4-6",
        ):
            with self.subTest(model_id=model_id):
                captured = {"invocations": 0}
                fake_strands = types.ModuleType("strands")
                fake_models = types.ModuleType("strands.models")

                class BedrockModel:
                    def __init__(self, **options):
                        captured["model"] = options

                class StructuredOutput:
                    def model_dump(self):
                        return MODEL_RESULT

                class Agent:
                    def __init__(self, **options):
                        captured["agent"] = options

                    def __call__(self, prompt, **options):
                        captured["invocations"] += 1
                        captured["prompt"] = prompt
                        captured["output"] = options
                        return types.SimpleNamespace(structured_output=StructuredOutput())

                fake_strands.Agent = Agent
                fake_models.BedrockModel = BedrockModel
                with (
                    patch.dict(sys.modules, {"strands": fake_strands, "strands.models": fake_models}),
                    patch.object(runtime_service, "_handoff_output_model", return_value=StructuredOutput),
                    patch.dict(runtime_service.os.environ, {
                        "BEDROCK_GUARDRAIL_ID": "guardrail-test",
                        "BEDROCK_GUARDRAIL_VERSION": "2",
                    }),
                ):
                    result = runtime_service._default_reasoner(
                        '{"mode":"handoff","focus":"Approved customer goals"}',
                        model_id,
                        {"memory": "available"},
                    )
                self.assertEqual(result, MODEL_RESULT)
                self.assertEqual(captured["invocations"], 1)
                self.assertFalse(captured["model"]["streaming"])
                self.assertEqual(captured["model"]["guardrail_id"], "guardrail-test")
                self.assertEqual(captured["model"]["guardrail_version"], "2")
                self.assertEqual(captured["model"]["model_id"], model_id)
                self.assertEqual(captured["model"]["boto_client_config"].retries["total_max_attempts"], 1)
                self.assertIsNone(captured["agent"]["retry_strategy"])
                self.assertIsNone(captured["agent"]["callback_handler"])
                self.assertEqual(captured["agent"]["session_manager"], {"memory": "available"})
                self.assertIn("directly through the StructuredOutput", captured["agent"]["system_prompt"])
                self.assertNotIn("- Return JSON only.", captured["agent"]["system_prompt"])
                self.assertEqual(captured["output"]["structured_output_model"], StructuredOutput)
                self.assertIn("guardContent", captured["prompt"][1])
                if "nova-pro" in model_id:
                    self.assertEqual(captured["model"]["additional_args"], {"performanceConfig": {"latency": "optimized"}})
                else:
                    self.assertNotIn("additional_args", captured["model"])

    def test_structured_handoff_repair_does_not_request_another_text_draft(self):
        prompts = []

        class OutputModel:
            pass

        def agent(prompt, **_options):
            prompts.append(prompt)
            return "invalid output" if len(prompts) == 1 else json.dumps(MODEL_RESULT)

        result = runtime_service._invoke_json_agent(
            agent, "original request", guarded_content="customer facts", output_model=OutputModel,
        )
        self.assertEqual(result, MODEL_RESULT)
        self.assertEqual(len(prompts), 2)
        self.assertIn("directly through the OutputModel", prompts[1][0]["text"])
        self.assertEqual(prompts[0][1], prompts[1][1])

    def test_handoff_model_diagnostics_exclude_customer_content(self):
        result = types.SimpleNamespace(
            message={"content": [{"text": json.dumps(MODEL_RESULT)}]},
            metrics=types.SimpleNamespace(
                cycle_count=1, accumulated_usage={"inputTokens": 1200, "outputTokens": 800},
            ),
        )
        with self.assertLogs(runtime_service.LOGGER, level="INFO") as logged:
            runtime_service._invoke_json_agent(lambda *_args, **_kwargs: result, "private-customer-input")
        self.assertNotIn("private-customer-input", logged.output[0])
        self.assertNotIn(MODEL_RESULT["projectAnswer"], logged.output[0])
        diagnostics = json.loads(logged.records[0].getMessage())
        self.assertEqual(diagnostics["accumulatedModelCalls"], 1)
        self.assertEqual(diagnostics["inputTokens"], 1200)
        self.assertEqual(diagnostics["outputTokens"], 800)

    def test_handoff_reports_context_and_generation_timings(self):
        response = self.invoke(HANDOFF_PAYLOAD)
        timings = response["metadata"]["agentTimingsMs"]
        self.assertEqual(set(timings), {"contextPreparation", "generationAndValidation"})
        self.assertTrue(all(isinstance(value, int) and value >= 0 for value in timings.values()))

    def test_strands_tool_protocol_error_uses_direct_bedrock_recovery(self):
        expected = {
            "projectAnswer": MODEL_RESULT["projectAnswer"],
            "citations": MODEL_RESULT["citations"],
        }
        prompt_payload = {"mode": "handoff", "focus": "Build the handoff."}
        fake_strands = types.ModuleType("strands")
        fake_models = types.ModuleType("strands.models")

        class BedrockModel:
            def __init__(self, **_options):
                pass

        class FailingAgent:
            def __init__(self, **_options):
                pass

            def __call__(self, *_args, **_kwargs):
                raise RuntimeError(
                    "modelStreamErrorException: Model produced invalid sequence "
                    "as part of ToolUse"
                )

        fake_strands.Agent = FailingAgent
        fake_models.BedrockModel = BedrockModel
        with (
            patch.dict(
                sys.modules,
                {"strands": fake_strands, "strands.models": fake_models},
            ),
            patch.object(
                runtime_service,
                "_handoff_output_model",
                return_value=dict,
            ),
            patch.object(
                runtime_service,
                "_invoke_direct_json_reasoner",
                return_value=expected,
            ) as direct,
            patch.dict(
                runtime_service.os.environ,
                {"BEDROCK_GUARDRAIL_ID": "", "BEDROCK_GUARDRAIL_VERSION": ""},
            ),
        ):
            result = runtime_service._default_reasoner(
                json.dumps(prompt_payload),
                "us.amazon.nova-pro-v1:0",
                {"memory": "available"},
            )

        self.assertEqual(result, expected)
        direct.assert_called_once_with(
            json.dumps(prompt_payload),
            "us.amazon.nova-pro-v1:0",
            prompt_payload,
        )

    def test_direct_recovery_keeps_guardrails_and_supported_latency_profile(self):
        expected = {
            "projectAnswer": MODEL_RESULT["projectAnswer"],
            "citations": MODEL_RESULT["citations"],
        }
        captured = {}

        class RuntimeClient:
            def converse(self, **kwargs):
                captured.update(kwargs)
                return {
                    "output": {
                        "message": {
                            "content": [{"text": json.dumps(expected)}],
                        }
                    }
                }

        prompt_payload = {"mode": "handoff", "focus": "Approved customer focus"}
        with (
            patch.object(
                runtime_service.boto3,
                "client",
                return_value=RuntimeClient(),
            ),
            patch.dict(
                runtime_service.os.environ,
                {
                    "BEDROCK_GUARDRAIL_ID": "guardrail-123",
                    "BEDROCK_GUARDRAIL_VERSION": "1",
                },
            ),
        ):
            result = runtime_service._invoke_direct_json_reasoner(
                json.dumps(prompt_payload),
                "us.amazon.nova-pro-v1:0",
                prompt_payload,
            )

        self.assertEqual(result, expected)
        self.assertEqual(captured["performanceConfig"], {"latency": "optimized"})
        self.assertEqual(captured["inferenceConfig"]["maxTokens"], 5000)
        self.assertEqual(
            captured["guardrailConfig"]["guardrailIdentifier"],
            "guardrail-123",
        )
        self.assertEqual(
            captured["messages"][0]["content"][1]["guardContent"]["text"][
                "qualifiers"
            ],
            ["guard_content"],
        )
        self.assertIn("do not call tools", captured["system"][0]["text"])

    def test_default_catchup_reasoner_uses_bedrock_converse(self):
        expected = {
            "projectAnswer": MODEL_RESULT["projectAnswer"],
            "citations": MODEL_RESULT["citations"],
        }
        captured = {}

        class FakeBedrockRuntime:
            def converse(self, **kwargs):
                captured.update(kwargs)
                return {
                    "output": {
                        "message": {
                            "content": [{"text": json.dumps(expected)}],
                        }
                    }
                }

        with (
            patch.object(runtime_service.boto3, "client", return_value=FakeBedrockRuntime()) as client_mock,
            patch.dict(
                runtime_service.os.environ,
                {"BEDROCK_GUARDRAIL_ID": "", "BEDROCK_GUARDRAIL_VERSION": ""},
            ),
        ):
            result = runtime_service._default_reasoner(
                '{"mode":"catchup","audienceRole":"New member"}',
                "us.amazon.nova-pro-v1:0",
                {"memory": "available"},
            )

        self.assertEqual(result, expected)
        client_mock.assert_called_once()
        self.assertEqual(client_mock.call_args.args, ("bedrock-runtime",))
        self.assertEqual(client_mock.call_args.kwargs["region_name"], "us-east-1")
        self.assertEqual(client_mock.call_args.kwargs["config"].retries["total_max_attempts"], 1)
        self.assertEqual(captured["modelId"], "us.amazon.nova-pro-v1:0")
        self.assertEqual(captured["inferenceConfig"]["maxTokens"], 1200)
        self.assertEqual(captured["performanceConfig"], {"latency": "optimized"})
        self.assertIn("Audience guidance:", captured["system"][0]["text"])
        self.assertIn("where to start", captured["system"][0]["text"])
        self.assertNotIn("guardrailConfig", captured)

    def test_nova_micro_catchup_omits_unsupported_latency_profile(self):
        captured = {}
        expected = {
            "projectAnswer": MODEL_RESULT["projectAnswer"],
            "citations": MODEL_RESULT["citations"],
        }

        class RuntimeClient:
            def converse(self, **kwargs):
                captured.update(kwargs)
                return {
                    "output": {
                        "message": {"content": [{"text": json.dumps(expected)}]}
                    }
                }

        with (
            patch.object(runtime_service.boto3, "client", return_value=RuntimeClient()),
            patch.dict(
                runtime_service.os.environ,
                {"BEDROCK_GUARDRAIL_ID": "", "BEDROCK_GUARDRAIL_VERSION": ""},
            ),
        ):
            result = runtime_service._default_reasoner(
                '{"mode":"catchup","audienceRole":"New member"}',
                "us.amazon.nova-micro-v1:0",
                {"memory": "available"},
            )

        self.assertEqual(result, expected)
        self.assertEqual(captured["modelId"], "us.amazon.nova-micro-v1:0")
        self.assertEqual(captured["inferenceConfig"]["topP"], 0.7)
        self.assertNotIn("performanceConfig", captured)

    def test_claude_catchup_uses_larger_standard_profile(self):
        captured = {}
        expected = {
            "projectAnswer": MODEL_RESULT["projectAnswer"],
            "citations": MODEL_RESULT["citations"],
        }

        class RuntimeClient:
            def converse(self, **kwargs):
                captured.update(kwargs)
                return {
                    "output": {
                        "message": {
                            "content": [{"text": json.dumps(expected)}]
                        }
                    }
                }

        prompt = json.dumps(
            {
                "mode": "catchup",
                "audienceRole": "New member",
                "customerContext": "Approved",
            }
        )
        with patch.object(runtime_service.boto3, "client", return_value=RuntimeClient()):
            result = runtime_service._default_reasoner(
                prompt,
                "global.anthropic.claude-sonnet-4-6",
                {"memory": "available"},
            )

        self.assertEqual(result, expected)
        self.assertEqual(captured["inferenceConfig"]["maxTokens"], 2500)
        self.assertNotIn("topP", captured["inferenceConfig"])
        self.assertNotIn("performanceConfig", captured)
    def test_agentcore_entrypoint_uses_required_context_parameter_name(self):
        tree = ast.parse((ROOT / "runtime" / "main.py").read_text())
        entrypoint = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "invoke"
        )
        self.assertEqual(
            [argument.arg for argument in entrypoint.args.args[:2]],
            ["payload", "context"],
        )



class MeetingAgenticRagTests(unittest.TestCase):
    def test_meeting_reasoner_uses_the_complete_structured_output_model(self):
        tree = ast.parse((ROOT / "runtime" / "meeting.py").read_text())
        factory = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_meeting_output_model"
        )
        output_class = next(
            node
            for node in ast.walk(factory)
            if isinstance(node, ast.ClassDef)
            and node.name == "MeetingAnalysisOutput"
        )
        fields = {
            node.target.id
            for node in output_class.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
        }
        self.assertTrue(
            {"meetingSummary", "proposedHandoffSummary", "citations"}.issubset(
                fields
            )
        )
        self.assertIn(
            "at least two distinct transcript-grounded actions",
            meeting_runtime.MEETING_SYSTEM_PROMPT,
        )
        reasoner = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_reason"
        )
        structured_calls = [
            keyword
            for node in ast.walk(reasoner)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "structured_output_model"
        ]
        self.assertEqual(len(structured_calls), 1)

        content = meeting_runtime._meeting_prompt_content(
            {
                "task": "Compare the meeting.",
                "meetingTranscript": {
                    "text": "Blue Mesa already operates on AWS."
                },
            },
            guardrail_enabled=True,
        )
        self.assertEqual(
            content[0],
            {"text": "{\"task\":\"Compare the meeting.\"}"},
        )
        self.assertNotIn("meetingTranscript", content[0]["text"])
        guarded = content[1]["guardContent"]["text"]
        self.assertIn("already operates on AWS", guarded["text"])
        self.assertEqual(guarded["qualifiers"], ["guard_content"])

        unguarded = meeting_runtime._meeting_prompt_content(
            {"meetingTranscript": {"text": "Synthetic transcript."}},
            guardrail_enabled=False,
            instruction="Repair. ",
        )
        self.assertEqual(unguarded[0]["text"], "Repair. {}")
        self.assertEqual(
            unguarded[1],
            {
                "text": (
                    "{\"meetingTranscript\":{\"text\":\"Synthetic transcript.\"}}"
                )
            },
        )

        repair_content = meeting_runtime._meeting_prompt_content(
            {
                "task": "Compare the meeting.",
                "repairReason": (
                    "Meeting analysis contradicted the confirmed "
                    "existing-on-AWS state in meetingSummary"
                ),
                "meetingTranscript": {"text": "Synthetic transcript."},
            },
            guardrail_enabled=False,
        )
        self.assertIn("VALIDATION REPAIR REQUIRED", repair_content[0]["text"])
        self.assertIn("already operates on AWS", repair_content[0]["text"])
        self.assertIn("meetingSummary", repair_content[0]["text"])
        self.assertNotIn("repairReason", repair_content[0]["text"])

        invoke_calls = [
            node
            for node in ast.walk(reasoner)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "invoke"
        ]
        self.assertEqual(len(invoke_calls), 2)
        agent_calls = [
            node
            for node in ast.walk(reasoner)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Agent"
        ]
        self.assertEqual(len(agent_calls), 1)
        self.assertNotIn(
            "session_manager",
            {keyword.arg for keyword in agent_calls[0].keywords},
        )

    def test_meeting_parser_reads_structured_tool_payload(self):
        expected = {"meetingSummary": "Payroll meeting analyzed."}

        class AgentResult:
            structured_output = None
            message = {"content": [{"toolUse": {"input": expected}}]}

            def __str__(self):
                return ""

        self.assertEqual(meeting_runtime._json_from_model(AgentResult()), expected)

    def meeting_request(self):
        return {
            "action": "analyze_meeting",
            "scenarioId": meeting_runtime.SCENARIO_ID,
            "meetingId": "blue-mesa-discovery",
            "briefVersion": 2,
            "approvedBrief": json.loads(json.dumps(HANDOFF_PAYLOAD["approvedBrief"])),
            "meetingTranscript": {
                "segments": [
                    {
                        "id": "segment-1",
                        "speaker": "Dev Malik",
                        "speakerLabel": "spk_2",
                        "timestampStart": 0,
                        "timestampEnd": 8,
                        "text": "Blue Mesa is already on AWS and payroll integration is in scope.",
                    }
                ],
                "durationSeconds": 8,
                "speakerCount": 1,
                "text": "Blue Mesa is already on AWS and payroll integration is in scope.",
            },
            "knowledgeBaseId": "KB12345678",
            "scope": {
                "tenantId": "demo",
                "clientId": meeting_runtime.CLIENT_ID,
                "projectId": meeting_runtime.CLIENT_ID,
                "userId": "user-123",
                "sessionId": "session-123",
            },
            "scopeToken": "signed-scope",
            "traceId": "trace-meeting-0001",
        }

    def test_retrieval_uses_exact_filters_and_rejects_bad_metadata(self):
        captured = {}

        class Retrieval:
            def retrieve(self, **kwargs):
                captured.update(kwargs)
                return {
                    "retrievalResults": [
                        {
                            "content": {"text": "Approved payroll objective."},
                            "metadata": {
                                "scenarioId": meeting_runtime.SCENARIO_ID,
                                "approved": True,
                                "visibility": "public-demo",
                                "documentType": "business-objective",
                                "sourceTitle": "Business objective",
                            },
                            "score": 0.91,
                        }
                    ]
                }

        tools = meeting_runtime.BoundedMeetingTools(
            self.meeting_request(), object(), retrieval_client=Retrieval()
        )
        evidence = tools.retrieve_scenario_evidence(
            "payroll objective",
            meeting_runtime.SCENARIO_ID,
            ["business-objective"],
        )
        filters = captured["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]["andAll"]
        self.assertIn(
            {"equals": {"key": "scenarioId", "value": meeting_runtime.SCENARIO_ID}},
            filters,
        )
        self.assertIn({"equals": {"key": "approved", "value": True}}, filters)
        self.assertEqual(evidence[0]["sourceTitle"], "Business objective")

        class EscapedRetrieval:
            def retrieve(self, **_kwargs):
                return {
                    "retrievalResults": [
                        {
                            "content": {"text": "Another customer."},
                            "metadata": {
                                "scenarioId": "another-scenario",
                                "approved": True,
                                "visibility": "public-demo",
                                "documentType": "business-objective",
                            },
                        }
                    ]
                }

        escaped = meeting_runtime.BoundedMeetingTools(
            self.meeting_request(), object(), retrieval_client=EscapedRetrieval()
        )
        with self.assertRaises(meeting_runtime.RetrievalScopeError):
            escaped.retrieve_scenario_evidence(
                "payroll",
                meeting_runtime.SCENARIO_ID,
                ["business-objective"],
            )

    def test_tool_budget_stops_a_fourth_external_read(self):
        class Gateway:
            def call(self, name, _arguments):
                if name == "get_latest_brief":
                    return {"brief": HANDOFF_PAYLOAD["approvedBrief"]}
                if name == "get_project_state":
                    return {"version": 1}
                raise AssertionError(name)

        class Retrieval:
            def retrieve(self, **_kwargs):
                return {
                    "retrievalResults": [
                        {
                            "content": {"text": "Approved payroll evidence."},
                            "metadata": {
                                "scenarioId": meeting_runtime.SCENARIO_ID,
                                "approved": True,
                                "visibility": "public-demo",
                                "documentType": "business-objective",
                            },
                        }
                    ]
                }

        tools = meeting_runtime.BoundedMeetingTools(
            self.meeting_request(), Gateway(), retrieval_client=Retrieval()
        )
        tools.get_latest_approved_brief(meeting_runtime.SCENARIO_ID)
        tools.get_project_state(meeting_runtime.SCENARIO_ID)
        tools.retrieve_scenario_evidence(
            "payroll", meeting_runtime.SCENARIO_ID, ["business-objective"]
        )
        with self.assertRaises(meeting_runtime.ToolLimitError):
            tools.get_stakeholder_profile(
                meeting_runtime.SCENARIO_ID, "dev-malik"
            )
        self.assertEqual(len(tools.tool_calls), 3)

    def test_cross_client_request_is_rejected_before_tools_run(self):
        request = self.meeting_request()
        request["scope"]["clientId"] = "another-client"
        with self.assertRaises(meeting_runtime.RetrievalScopeError):
            meeting_runtime.analyze_meeting(
                request,
                gateway_factory=lambda: nullcontext(object()),
                memory_factory=lambda _scope: nullcontext({}),
            )

    def test_analysis_performs_three_bounded_reads_and_no_writes(self):
        calls = []
        safety_calls = []

        def screen(value, *, source, **_kwargs):
            safety_calls.append((source, json.loads(json.dumps(value))))
            return value, {"source": source, "policyResult": "passed"}

        class Gateway:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def call(self, name, _arguments):
                calls.append(name)
                if name == "get_latest_brief":
                    return {
                        "brief": json.loads(json.dumps(HANDOFF_PAYLOAD["approvedBrief"])),
                        "metadata": {"packetVersion": 2, "approvalStatus": "approved"},
                    }
                if name == "get_project_state":
                    return {"version": 2, "decisions": [], "risks": []}
                raise AssertionError(name)

        class Retrieval:
            def retrieve(self, **_kwargs):
                return {
                    "retrievalResults": [
                        {
                            "content": {"text": "Blue Mesa already runs on AWS. Payroll integration is approved for discovery."},
                            "metadata": {
                                "scenarioId": meeting_runtime.SCENARIO_ID,
                                "approved": True,
                                "visibility": "public-demo",
                                "documentType": "current-aws-environment",
                                "sourceTitle": "Current AWS environment",
                            },
                        }
                    ]
                }

        request = self.meeting_request()
        with (
            patch.object(meeting_runtime.boto3, "client", return_value=Retrieval()),
            patch.object(
                meeting_runtime,
                "_reason",
                return_value={"meetingSummary": "Payroll meeting analyzed."},
            ) as reasoner,
            patch.object(
                meeting_runtime.content_safety,
                "screen_payload",
                side_effect=screen,
            ),
        ):
            def fail_if_memory_is_loaded(_scope):
                raise AssertionError("one-shot meeting analysis must be stateless")

            result = meeting_runtime.analyze_meeting(
                request,
                gateway_factory=Gateway,
                memory_factory=fail_if_memory_is_loaded,
            )

        self.assertEqual(calls, ["get_latest_brief", "get_project_state"])
        self.assertEqual(result["provider"], "agentcore-strands")
        self.assertEqual(result["retrieval"]["toolCallCount"], 3)
        self.assertEqual(result["retrieval"]["retrievalRounds"], 1)
        self.assertFalse(result["model"]["fallbackUsed"])
        self.assertEqual(safety_calls[0][0], "INPUT")
        self.assertEqual(
            set(safety_calls[0][1]), {"meetingTranscript", "repairReason"}
        )
        self.assertNotIn("latestApprovedBrief", safety_calls[0][1])
        reasoning_input = reasoner.call_args.args[0]
        self.assertIn("latestApprovedBrief", reasoning_input)
        self.assertIn("approvedRetrievedEvidence", reasoning_input)
        self.assertEqual(safety_calls[-1][0], "OUTPUT")
if __name__ == "__main__":
    unittest.main()
