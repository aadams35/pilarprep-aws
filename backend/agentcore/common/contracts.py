from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .identifiers import require_identifier
from .security import validate_scope


ACTIONS = {"create_handoff", "generate_catchup", "analyze_meeting"}
AUDIENCE_ROLES = {"Sales", "Solutions Architect", "Executive", "PM", "Engineer", "New member"}
MODEL_PREFERENCES = {"nova-pro", "nova-micro", "claude-sonnet-4.6"}
REGISTER_NAMES = (
    "assumptions",
    "decisions",
    "risks",
    "actions",
    "owners",
    "milestones",
    "openQuestions",
)


def require_string(
    value: object,
    field: str,
    *,
    minimum: int = 1,
    maximum: int = 12_000,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if len(normalized) < minimum or len(normalized) > maximum:
        raise ValueError(f"{field} must contain {minimum}-{maximum} characters")
    return normalized


def optional_string(value: object, field: str, maximum: int = 12_000) -> str:
    if value in (None, ""):
        return ""
    return require_string(value, field, maximum=maximum)


def validate_router_request(payload: object) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("Request body must be a JSON object")

    action = require_string(payload.get("action"), "action", maximum=32)
    if action not in ACTIONS:
        raise ValueError("unsupported AgentCore action")

    audience_role = require_string(
        payload.get("audienceRole", "Solutions Architect"), "audienceRole", maximum=32
    )
    if audience_role not in AUDIENCE_ROLES:
        raise ValueError("audienceRole is not supported")

    model_preference = require_string(
        payload.get("modelPreference", "nova-pro"),
        "modelPreference",
        maximum=32,
    )
    if model_preference not in MODEL_PREFERENCES:
        raise ValueError(
            "modelPreference must be nova-pro, nova-micro, or claude-sonnet-4.6"
        )

    confirm_write = payload.get("confirmWrite", False)
    if not isinstance(confirm_write, bool):
        raise ValueError("confirmWrite must be a boolean")
    if action == "create_handoff" and not confirm_write:
        raise ValueError("create_handoff requires explicit write confirmation")
    if action == "analyze_meeting" and confirm_write:
        raise ValueError("analyze_meeting is read-only")

    brief_request = payload.get("briefRequest")
    if not isinstance(brief_request, Mapping):
        raise ValueError("briefRequest must be an object")

    approved_brief = payload.get("approvedBrief")
    if not isinstance(approved_brief, Mapping) or not approved_brief:
        raise ValueError(f"{action} requires a non-empty approvedBrief object")

    supplied_tenant = payload.get("tenantId")
    supplied_user = payload.get("userId")

    result = {
        "action": action,
        "tenantId": (
            require_identifier(supplied_tenant, "tenantId")
            if supplied_tenant not in (None, "")
            else ""
        ),
        "userId": (
            require_identifier(supplied_user, "userId")
            if supplied_user not in (None, "")
            else ""
        ),
        "clientId": require_identifier(payload.get("clientId"), "clientId"),
        "projectId": require_identifier(payload.get("projectId"), "projectId"),
        "sessionId": require_identifier(payload.get("sessionId"), "sessionId"),
        "audienceRole": audience_role,
        "focus": optional_string(payload.get("focus"), "focus", maximum=500),
        "meetingNotes": optional_string(
            payload.get("meetingNotes"), "meetingNotes", maximum=20_000
        ),
        "modelPreference": model_preference,
        "confirmWrite": confirm_write,
        "idempotencyKey": require_identifier(
            payload.get("idempotencyKey"), "idempotencyKey"
        ),
        "approvedBrief": deepcopy(dict(approved_brief))
        if isinstance(approved_brief, Mapping)
        else None,
        "briefRequest": deepcopy(dict(brief_request)),
    }
    if action == "analyze_meeting":
        brief_version = payload.get("briefVersion")
        if (
            isinstance(brief_version, bool)
            or not isinstance(brief_version, int)
            or brief_version < 1
        ):
            raise ValueError("analyze_meeting requires a positive briefVersion")
        transcript = payload.get("meetingTranscript")
        if (
            not isinstance(transcript, Mapping)
            or not isinstance(transcript.get("segments"), list)
            or not isinstance(transcript.get("text"), str)
        ):
            raise ValueError(
                "analyze_meeting requires a speaker-labeled transcript"
            )
        result.update(
            {
                "scenarioId": require_identifier(
                    payload.get("scenarioId"), "scenarioId"
                ),
                "meetingId": require_identifier(
                    payload.get("meetingId"), "meetingId"
                ),
                "knowledgeBaseId": require_string(
                    payload.get("knowledgeBaseId"),
                    "knowledgeBaseId",
                    maximum=32,
                ),
                "briefVersion": brief_version,
                "meetingTranscript": deepcopy(dict(transcript)),
                "repairReason": optional_string(
                    payload.get("repairReason"), "repairReason", maximum=1000
                ),
            }
        )
    return result


def validate_runtime_request(payload: object) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("Runtime payload must be an object")
    validated = validate_router_request(payload)
    scope = payload.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("Runtime payload is missing validated scope")
    validated["scope"] = validate_scope(scope)
    validated["scopeToken"] = require_string(
        payload.get("scopeToken"), "scopeToken", maximum=4096
    )
    validated["traceId"] = require_identifier(payload.get("traceId"), "traceId")
    return validated


def _normalize_register_item(value: object, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} entries must be objects")
    title = require_string(value.get("title"), f"{field}.title", maximum=180)
    detail = require_string(value.get("detail"), f"{field}.detail", maximum=2000)
    return {
        "title": title,
        "detail": detail,
        "owner": optional_string(value.get("owner"), f"{field}.owner", maximum=120),
        "status": optional_string(value.get("status"), f"{field}.status", maximum=80),
        "source": require_string(value.get("source"), f"{field}.source", maximum=240),
    }


def normalize_project_next_steps(value: object) -> dict[str, Any]:
    if value in (None, {}):
        return {
            "immediateActions": [],
            "openQuestions": [],
            "nextMeeting": {"purpose": "", "timing": "", "attendees": []},
            "customerSummary": "",
            "internalNotes": "",
        }
    if not isinstance(value, Mapping):
        raise ValueError("projectUpdate.nextSteps must be an object")

    actions = value.get("immediateActions", [])
    if not isinstance(actions, list) or len(actions) > 12:
        raise ValueError("projectUpdate.nextSteps.immediateActions must contain at most 12 items")
    normalized_actions: list[dict[str, str]] = []
    for index, action in enumerate(actions):
        field = f"projectUpdate.nextSteps.immediateActions[{index}]"
        if not isinstance(action, Mapping):
            raise ValueError(f"{field} must be an object")
        normalized_actions.append(
            {
                "action": require_string(action.get("action"), f"{field}.action", maximum=500),
                "owner": require_string(action.get("owner"), f"{field}.owner", maximum=180),
                "timing": require_string(action.get("timing"), f"{field}.timing", maximum=180),
                "dependency": require_string(action.get("dependency"), f"{field}.dependency", maximum=1000),
                "decisionGate": require_string(action.get("decisionGate"), f"{field}.decisionGate", maximum=1000),
            }
        )

    questions = value.get("openQuestions", [])
    if not isinstance(questions, list) or len(questions) > 12:
        raise ValueError("projectUpdate.nextSteps.openQuestions must contain at most 12 items")
    normalized_questions = [
        require_string(question, f"projectUpdate.nextSteps.openQuestions[{index}]", maximum=1000)
        for index, question in enumerate(questions)
    ]

    meeting = value.get("nextMeeting", {})
    if not isinstance(meeting, Mapping):
        raise ValueError("projectUpdate.nextSteps.nextMeeting must be an object")
    attendees = meeting.get("attendees", [])
    if not isinstance(attendees, list) or len(attendees) > 20:
        raise ValueError("projectUpdate.nextSteps.nextMeeting.attendees must contain at most 20 items")

    return {
        "immediateActions": normalized_actions,
        "openQuestions": normalized_questions,
        "nextMeeting": {
            "purpose": optional_string(meeting.get("purpose"), "projectUpdate.nextSteps.nextMeeting.purpose", maximum=1000),
            "timing": optional_string(meeting.get("timing"), "projectUpdate.nextSteps.nextMeeting.timing", maximum=180),
            "attendees": [
                require_string(attendee, f"projectUpdate.nextSteps.nextMeeting.attendees[{index}]", maximum=180)
                for index, attendee in enumerate(attendees)
            ],
        },
        "customerSummary": optional_string(value.get("customerSummary"), "projectUpdate.nextSteps.customerSummary", maximum=4000),
        "internalNotes": optional_string(value.get("internalNotes"), "projectUpdate.nextSteps.internalNotes", maximum=4000),
    }


def normalize_project_update(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("projectUpdate must be an object")

    result: dict[str, Any] = {}
    for register in REGISTER_NAMES:
        entries = value.get(register, [])
        if not isinstance(entries, list) or len(entries) > 25:
            raise ValueError(f"projectUpdate.{register} must be an array of at most 25 items")
        result[register] = [
            _normalize_register_item(entry, f"projectUpdate.{register}")
            for entry in entries
        ]
    result["nextSteps"] = normalize_project_next_steps(value.get("nextSteps"))
    return result


def empty_project_state() -> dict[str, Any]:
    return {
        "version": 0,
        **{register: [] for register in REGISTER_NAMES},
        "nextSteps": normalize_project_next_steps(None),
    }
