from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from runtime.service import handle_request  # noqa: E402


BRIEF = {
    "provider": "bedrock",
    "businessCase": {
        "scenario": "BlueMesa is consolidating acquired payment systems before holiday volume while protecting merchant settlement continuity and PCI evidence.",
        "desiredOutcomes": "Agree on a low-disruption validation path, named evidence owners, and a bounded pilot decision.",
        "alignmentStatement": "Confirm that the meeting should align on merchant-trust outcomes, validate the highest-risk assumptions, and leave with named owners and a clear pilot gate.",
        "inScope": "Settlement recovery, identity boundaries, PCI evidence, rollback criteria, owners, and the bounded pilot decision.",
        "outOfScope": "A production cutover, guaranteed implementation dates, compliance certification, and broad migration approval.",
        "successCriteria": "BlueMesa corrects assumptions, accepts the evidence plan, names owners, defines pilot thresholds, and schedules the decision session.",
    },
    "technical": ["Validate settlement dependencies, identity boundaries, recovery evidence, and rollback before migration."],
    "executive": ["Protect merchant trust while creating a controlled path to faster delivery."],
    "stakeholders": ["Ariana sponsors outcomes, Dev owns resilience, and Rachel owns compliance evidence."],
    "gameplan": ["Agree on a bounded pilot with explicit go, no-go, and rollback criteria."],
    "objections": ["Modernization cannot create a public incident during peak season."],
    "citations": ["Customer-approved BlueMesa scenario"],
}

NEXT_STEPS = {
    "immediateActions": [
        {
            "action": "Map settlement dependencies and failure paths",
            "owner": "Dev Malik",
            "timing": "Within two business days",
            "dependency": "Current settlement architecture and batch schedules",
            "decisionGate": "The team agrees the bounded pilot has no unowned critical dependency",
        },
        {
            "action": "Confirm PCI and identity evidence required for the pilot",
            "owner": "Rachel Kim",
            "timing": "Before the recovery workshop",
            "dependency": "Access-boundary inventory and current audit findings",
            "decisionGate": "Rachel confirms the evidence package is sufficient for pilot review",
        },
        {
            "action": "Run and document a recovery and rollback rehearsal",
            "owner": "Dev Malik",
            "timing": "Within five business days",
            "dependency": "Approved test window and representative settlement workload",
            "decisionGate": "Measured recovery meets the agreed RTO and RPO before traffic moves",
        },
    ],
    "openQuestions": [
        "What are the accepted RTO and RPO for merchant settlement?",
        "Who has final authority to approve or stop the bounded pilot?",
    ],
    "nextMeeting": {
        "purpose": "Review recovery and PCI evidence and make the bounded pilot decision",
        "timing": "Within five business days",
        "attendees": ["Ariana Cole", "Dev Malik", "Rachel Kim", "Solutions Architect"],
    },
    "customerSummary": "BlueMesa and the account team will validate recovery, rollback, identity, and PCI evidence before deciding whether the bounded pilot may proceed.",
    "internalNotes": "Keep the holiday freeze date and recovery thresholds marked unvalidated until customer evidence is attached, and escalate any missing approval owner before delivery planning.",
}

STATE = {
    "version": 0,
    "assumptions": [],
    "decisions": [],
    "risks": [],
    "actions": [],
    "owners": [],
    "milestones": [],
    "openQuestions": [],
    "nextSteps": deepcopy(NEXT_STEPS),
}


class LocalGateway:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def call(self, name: str, arguments: dict[str, Any]):
        if name == "get_latest_brief":
            return {"brief": deepcopy(BRIEF), "metadata": {"artifactKey": "local/latest.json"}}
        if name == "get_project_state":
            return deepcopy(STATE)
        if name == "generate_catchup":
            return {"brief": deepcopy(BRIEF), "projectState": deepcopy(STATE), "audienceRole": arguments["audienceRole"]}
        if name == "save_project_update":
            STATE.update(deepcopy(arguments["update"]))
            STATE["version"] += 1
            return {**deepcopy(STATE), "stateKey": "PROJECT#STATE"}
        if name == "create_handoff_packet":
            return {
                "artifactKey": "local/handoff/latest.json",
                "docxArtifactKey": "local/handoff/latest.docx",
                "artifactRetention": "latest-only",
            }
        raise ValueError(name)


def local_reasoner(prompt: str, _model_id: str, _memory: Any):
    roles = ("Sales", "Solutions Architect", "Executive", "PM", "Engineer", "New member")
    role = next((item for item in roles if f'"audienceRole":"{item}"' in prompt), "PM")
    project_answer = (
        "Solutions Architect view: confirmed context says BlueMesa is protecting merchant trust while consolidating acquired payment platforms before holiday volume; the current topology and freeze date remain unvalidated. "
        "Translate the business outcome into evidence requests for settlement dependencies, identity and data boundaries, RTO/RPO, PCI controls, workload metrics, rollback, observability, cost boundaries, and operating ownership. "
        "Evaluate AWS services only where they answer a decision, keep each architecture hypothesis separate from customer-confirmed facts, and assign every risk and dependency to an owner. "
        "The next technical session should include Dev Malik, Rachel Kim, the platform team, and the Solutions Architect to decide whether recovery and control evidence are sufficient for the bounded pilot."
        if role == "Solutions Architect"
        else (
            f"{role} view: BlueMesa is protecting merchant trust while consolidating two acquired payment platforms. "
            "Start with the bounded settlement-recovery pilot, validate PCI and identity evidence with Rachel Kim, "
            "and do not move customer traffic until Dev Malik has demonstrated recovery and rollback criteria."
        )
    )
    return {
        "projectAnswer": project_answer,
        "projectArtifacts": {
            "twoWeekPlan": [
                {"title": "Days 1-2: Map settlement dependencies", "detail": "Objective: map batch and identity boundaries. Output: approved dependency map. Dependency: current diagrams and schedules. Exit criterion: no critical path lacks an owner.", "owner": "Dev Malik", "status": "Ready"},
                {"title": "Days 3-5: Run recovery rehearsal", "detail": "Objective: test recovery and rollback. Output: measured RTO/RPO evidence. Dependency: representative workload and test window. Exit criterion: Dev accepts the recovery result.", "owner": "Platform team", "status": "Planned"}
            ],
            "riskRegister": [
                {"title": "Unvalidated assumption: holiday freeze date is known", "detail": "Confirm the customer-approved freeze date before committing the pilot sequence.", "owner": "Ariana Cole", "status": "Unvalidated"},
                {"title": "Settlement delay", "detail": "Overnight failure could delay merchant funds.", "owner": "Dev Malik", "status": "High"},
                {"title": "PCI evidence gap", "detail": "Acquired identities may not have clean separation evidence.", "owner": "Rachel Kim", "status": "High"}
            ],
            "stakeholderMap": [
                {"title": "Executive sponsor", "detail": "Owns outcome and holiday-readiness checkpoint.", "owner": "Ariana Cole", "status": "Sponsor"},
                {"title": "Control approver", "detail": "Approves PCI and identity evidence.", "owner": "Rachel Kim", "status": "Approver"}
            ],
            "followUpEmail": {"subject": "BlueMesa pilot evidence and owners", "body": "We captured the bounded pilot, named owners, and proof required before customer traffic moves. The next session will validate settlement recovery and PCI evidence."},
            "nextSteps": deepcopy(NEXT_STEPS),
        },
        "projectUpdate": {
            "assumptions": [{"title": "Holiday volume window", "detail": "The pilot must complete before the holiday freeze; confirm the exact date.", "owner": "Ariana Cole", "status": "Validate", "source": "Approved brief"}],
            "decisions": [{"title": "Bounded pilot first", "detail": "No broad cutover before evidence review.", "owner": "Ariana Cole", "status": "Approved", "source": "Meeting outcomes"}],
            "risks": [{"title": "Settlement disruption", "detail": "Recovery is not demonstrated.", "owner": "Dev Malik", "status": "Open", "source": "Approved brief"}],
            "actions": [{"title": "Recovery rehearsal", "detail": "Run and document recovery evidence.", "owner": "Dev Malik", "status": "Next", "source": "Meeting outcomes"}],
            "owners": [{"title": "Compliance evidence", "detail": "Own PCI and identity evidence.", "owner": "Rachel Kim", "status": "Assigned", "source": "Meeting outcomes"}],
            "milestones": [{"title": "Holiday readiness gate", "detail": "Review pilot evidence before traffic movement.", "owner": "Ariana Cole", "status": "Planned", "source": "Approved brief"}],
            "openQuestions": [{"title": "Recovery objectives", "detail": "Confirm accepted RTO and RPO.", "owner": "Dev Malik", "status": "Open", "source": "Approved brief"}],
            "nextSteps": deepcopy(NEXT_STEPS),
        },
        "citations": ["Customer-approved BlueMesa scenario", "Approved meeting outcomes"]
    }


def run_payload(payload: dict[str, Any]):
    return handle_request(
        payload,
        gateway_factory=LocalGateway,
        reasoner=local_reasoner,
        memory_factory=lambda _scope: nullcontext({"localMemory": True}),
    )


def run_event(name: str):
    payload = json.loads((ROOT / "events" / name).read_text(encoding="utf-8"))
    return run_payload(payload)


if __name__ == "__main__":
    handoff = run_event("bluemesa-handoff.json")
    catchup_payload = json.loads((ROOT / "events" / "bluemesa-catchup.json").read_text(encoding="utf-8"))
    handoff_payload = json.loads((ROOT / "events" / "bluemesa-handoff.json").read_text(encoding="utf-8"))
    catchup_payload["approvedBrief"] = deepcopy(handoff_payload["approvedBrief"])
    catchup_payload["briefRequest"]["approvedBrief"] = deepcopy(handoff_payload["approvedBrief"])
    catchup = run_payload(catchup_payload)
    sa_catchup_payload = deepcopy(catchup_payload)
    sa_catchup_payload["audienceRole"] = "Solutions Architect"
    sa_catchup_payload["focus"] = "What architecture assumptions must I validate?"
    sa_catchup_payload["briefRequest"]["role"] = "Solutions Architect"
    sa_catchup_payload["briefRequest"]["prompt"] = sa_catchup_payload["focus"]
    sa_catchup_payload["idempotencyKey"] = "catchup-bluemesa-sa-demo-000001"
    sa_catchup = run_payload(sa_catchup_payload)
    print(json.dumps({"handoff": handoff, "catchup": catchup, "solutionsArchitectCatchup": sa_catchup}, indent=2))
