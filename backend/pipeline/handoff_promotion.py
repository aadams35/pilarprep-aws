from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping


REGISTER_NAMES = (
    "assumptions",
    "decisions",
    "risks",
    "actions",
    "owners",
    "milestones",
    "openQuestions",
)


def _text(value: object, fallback: str = "") -> str:
    normalized = " ".join(str(value or "").split())
    return normalized or fallback


def _clock(seconds: object) -> str:
    try:
        value = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        value = 0
    return f"{value // 60:02d}:{value % 60:02d}"


def _source(item: Mapping[str, Any], meeting_id: str) -> str:
    speaker = _text(item.get("speaker"), "Meeting participant")
    return f"Approved meeting {meeting_id}: {speaker} at {_clock(item.get('timestampStart'))}"


def _mapping_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]


def _dedupe(
    values: list[dict[str, Any]], *, identity: str, maximum: int
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        key = _text(value.get(identity)).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= maximum:
            break
    return result


def promote_handoff(
    base_packet: Mapping[str, Any],
    proposal: Mapping[str, Any],
    accepted: list[Mapping[str, Any]],
    *,
    company: str,
    packet_version: int,
) -> dict[str, Any]:
    if not accepted:
        raise ValueError("At least one reviewed meeting change is required")
    packet = deepcopy(dict(base_packet))
    artifacts_value = packet.get("projectArtifacts")
    if not isinstance(artifacts_value, Mapping):
        raise ValueError("The approved packet has no project handoff artifacts")
    artifacts = deepcopy(dict(artifacts_value))
    next_steps_value = artifacts.get("nextSteps")
    if not isinstance(next_steps_value, Mapping):
        raise ValueError("The approved packet has no next-step handoff")
    next_steps = deepcopy(dict(next_steps_value))

    analysis_value = proposal.get("analysis")
    analysis = dict(analysis_value) if isinstance(analysis_value, Mapping) else {}
    meeting_id = _text(proposal.get("meetingId"), "customer-meeting")
    meeting_summary = _text(
        analysis.get("meetingSummary"),
        "The customer conversation produced human-reviewed updates.",
    )
    handoff_summary = _text(
        analysis.get("proposedHandoffSummary"),
        "Carry the approved customer evidence into the next decision.",
    )
    approved_lines = [
        f"{_text(item.get('category'), 'update')}: "
        f"{_text(item.get('proposedUpdate'), 'Approved meeting update')}"
        for item in accepted
    ]

    packet.update(
        {
            "provider": "agentcore",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "company": company,
            "projectAnswer": "\n\n".join(
                (
                    handoff_summary,
                    f"Meeting alignment: {meeting_summary}",
                    "Human-approved updates:\n- " + "\n- ".join(approved_lines),
                    "Only accepted or edited items were promoted. Rejected proposals remain audit history and are not project facts.",
                )
            ),
        }
    )

    plan_items: list[dict[str, Any]] = []
    risk_items: list[dict[str, Any]] = []
    immediate_actions: list[dict[str, Any]] = []
    meeting_sources: list[str] = []
    for item in accepted:
        category = _text(item.get("category"))
        statement = _text(item.get("proposedUpdate"), "Approved meeting update")
        owner = _text(item.get("owner"), _text(item.get("speaker"), "Owner TBD"))
        source = _source(item, meeting_id)
        meeting_sources.append(source)
        if category == "actions":
            plan_items.append(
                {
                    "title": f"Next: {statement}"[:180],
                    "detail": (
                        f"Human-approved from {source}. Dependency: "
                        f"{_text(item.get('dependency'), 'Confirm required inputs with the named owner.')}"
                    )[:2400],
                    "owner": owner[:120],
                    "status": "Approved after meeting",
                }
            )
            immediate_actions.append(
                {
                    "action": statement,
                    "owner": owner,
                    "timing": _text(
                        item.get("targetDate"), "Before the next customer session"
                    ),
                    "dependency": _text(
                        item.get("dependency"),
                        "Confirm required inputs with the named owner",
                    ),
                    "decisionGate": (
                        "The named owner confirms completion and the customer "
                        "accepts the required evidence."
                    ),
                }
            )
        elif category == "risks":
            risk_items.append(
                {
                    "title": statement[:180],
                    "detail": f"Human-approved risk from {source}."[:2400],
                    "owner": owner[:120],
                    "status": "Open",
                }
            )

    artifacts["twoWeekPlan"] = _dedupe(
        [*plan_items, *_mapping_list(artifacts.get("twoWeekPlan"))],
        identity="title",
        maximum=12,
    )
    artifacts["riskRegister"] = _dedupe(
        [*risk_items, *_mapping_list(artifacts.get("riskRegister"))],
        identity="title",
        maximum=12,
    )
    existing_actions = _mapping_list(next_steps.get("immediateActions"))
    next_steps["immediateActions"] = _dedupe(
        [*immediate_actions, *existing_actions],
        identity="action",
        maximum=6,
    )
    existing_meeting = next_steps.get("nextMeeting")
    existing_meeting = (
        deepcopy(dict(existing_meeting))
        if isinstance(existing_meeting, Mapping)
        else {}
    )
    existing_meeting["purpose"] = handoff_summary
    existing_meeting.setdefault("timing", "At the agreed follow-up window")
    next_steps.update(
        {
            "nextMeeting": existing_meeting,
            "customerSummary": meeting_summary,
            "internalNotes": (
                "Human-approved meeting changes:\n- " + "\n- ".join(approved_lines)
            ),
        }
    )
    artifacts["nextSteps"] = next_steps
    artifacts["followUpEmail"] = {
        "subject": f"{company} | Confirmed outcomes and next steps",
        "body": (
            f"Thank you for the conversation. We aligned on: {meeting_summary}\n\n"
            "The reviewed outcomes are:\n- "
            + "\n- ".join(approved_lines)
            + f"\n\nNext step: {handoff_summary}"
        ),
    }
    packet["projectArtifacts"] = artifacts

    citations = [
        _text(value)
        for value in packet.get("citations", [])
        if _text(value)
    ] if isinstance(packet.get("citations"), list) else []
    packet["citations"] = list(dict.fromkeys([*citations, *meeting_sources]))[:100]
    evidence = _mapping_list(packet.get("evidence"))
    evidence.append(
        {
            "section": "projectAnswer",
            "itemIndex": 0,
            "sources": meeting_sources[:3],
        }
    )
    packet["evidence"] = evidence
    metadata_value = packet.get("metadata")
    metadata = (
        deepcopy(dict(metadata_value))
        if isinstance(metadata_value, Mapping)
        else {}
    )
    metadata.update(
        {
            "approvedPacketVersion": packet_version,
            "packetVersion": packet_version,
            "handoffAssembly": "human-approved-meeting-promotion",
            "meetingAnalysisProvider": "agentcore-strands",
            "modelInvokedForApproval": False,
            "fallbackUsed": False,
        }
    )
    packet["metadata"] = metadata
    return packet


def project_update(
    current_state: Mapping[str, Any],
    accepted: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
    *,
    meeting_id: str,
) -> dict[str, Any]:
    update: dict[str, Any] = {
        register: _mapping_list(current_state.get(register))
        for register in REGISTER_NAMES
    }
    category_map = {
        "correctedAssumptions": ("assumptions", "Corrected assumption"),
        "decisions": ("decisions", "Confirmed decision"),
        "requirements": ("decisions", "Confirmed requirement"),
        "risks": ("risks", "Customer-confirmed risk"),
        "scopeChanges": ("decisions", "Approved scope change"),
        "actions": ("actions", "Approved next action"),
    }
    for item in accepted:
        category = _text(item.get("category"))
        mapped = category_map.get(category)
        if not mapped:
            continue
        register, title = mapped
        statement = _text(item.get("proposedUpdate"), "Approved meeting update")
        owner = _text(item.get("owner"), _text(item.get("speaker"), "Owner TBD"))
        source = _source(item, meeting_id)
        if category == "correctedAssumptions":
            previous = _text(item.get("originalContent")).casefold()
            if previous and not previous.startswith("no matching"):
                update["assumptions"] = [
                    value
                    for value in update["assumptions"]
                    if previous
                    not in (
                        _text(value.get("title"))
                        + " "
                        + _text(value.get("detail"))
                    ).casefold()
                ]
        update[register] = _dedupe(
            [
                {
                    "title": title,
                    "detail": statement,
                    "owner": owner,
                    "status": "Approved",
                    "source": source,
                },
                *update[register],
            ],
            identity="detail",
            maximum=25,
        )
        if category == "actions" and owner != "Owner TBD":
            update["owners"] = _dedupe(
                [
                    {
                        "title": owner,
                        "detail": statement,
                        "owner": owner,
                        "status": "Assigned",
                        "source": source,
                    },
                    *update["owners"],
                ],
                identity="title",
                maximum=25,
            )
        target_date = _text(item.get("targetDate"))
        if target_date:
            update["milestones"] = _dedupe(
                [
                    {
                        "title": target_date,
                        "detail": statement,
                        "owner": owner,
                        "status": "Planned",
                        "source": source,
                    },
                    *update["milestones"],
                ],
                identity="detail",
                maximum=25,
            )
    next_steps = artifacts.get("nextSteps")
    update["nextSteps"] = (
        deepcopy(dict(next_steps))
        if isinstance(next_steps, Mapping)
        else {}
    )
    return update
