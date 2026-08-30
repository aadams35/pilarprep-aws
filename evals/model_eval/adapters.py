from __future__ import annotations

import copy
import json
import re
import sys

from .suite import ROOT, fingerprint


def production_modules():
    # Runtime packages use the same top-level imports in their Lambda bundles.
    for path in (ROOT / "backend", ROOT / "backend" / "agentcore"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from backend.bedrock import brief_generator as brief
    from runtime import service, meeting
    return brief, service, meeting


def build_prompts(case: dict, *, guardrails: bool = True) -> dict:
    brief, service, meeting = production_modules()
    action, request = case["action"], copy.deepcopy(case["request"])
    parts = []
    if action.startswith("brief."):
        routes = brief.BRIEF_GENERATION_ROUTES if action == "brief.generate" else (("refinement", (case["target"],)),)
        for route, sections in routes:
            trusted, context = brief._build_prompt_parts(request, sections if action == "brief.generate" else None)
            content = [{"text": context}]
            if guardrails:
                content.append({"guardContent": {"text": {"text": brief._guardrail_request_content(context), "qualifiers": ["guard_content"]}}})
            parts.append({"route": route, "sections": list(sections), "system": brief._system_prompt() + "\n\n" + trusted, "content": content})
        labels = brief._source_labels(request)
    elif action in {"handoff.generate", "catchup.generate"}:
        agent_request = {"action": "generate_catchup" if action == "catchup.generate" else "generate_handoff", "audienceRole": case["audienceRole"], "focus": case["focus"], "meetingNotes": request.get("meetingNotes", "")}
        latest = {"brief": case["previous"], "request": request, "requestContext": request, "approvalStatus": "approved", "packetVersion": 1}
        state = {"assumptions": [], "decisions": [], "risks": [], "actions": [], "owners": [], "milestones": [], "openQuestions": []}
        evidence = request.get("approvedEvidenceSources", [])
        labels = list(dict.fromkeys(["Approved brief", "Customer context", "Decision-maker notes", "Stakeholder notes", "Discovery charter", *[source["sourceTitle"] for source in evidence]]))
        context = service._prompt(agent_request, latest, state, None, labels, evidence)
        content = service._agent_prompt_content(context, service._guarded_user_content(json.loads(context))) if guardrails else [{"text": context}]
        parts.append({"route": action, "sections": [], "system": service.CATCHUP_SYSTEM_PROMPT if action == "catchup.generate" else service.SYSTEM_PROMPT, "content": content})
    else:
        evidence = {"meetingTranscript": case["transcript"], "approvedBrief": case["previous"], "scenarioContext": request, "retrievedEvidence": request.get("approvedEvidenceSources", []), "currentProjectState": {}}
        parts.append({"route": action, "sections": [], "system": meeting.MEETING_SYSTEM_PROMPT, "content": meeting._meeting_prompt_content(evidence, guardrail_enabled=guardrails)})
        labels = []
    return {"parts": parts, "allowedSourceLabels": labels, "promptHash": fingerprint(parts)}


def _text(value) -> str:
    if isinstance(value, dict):
        return " ".join(_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    return value if isinstance(value, str) else str(value)


def at_path(value, path: str):
    if path == "$":
        return value
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _citation_check(output: dict, labels: list[str]):
    citations = output.get("citations")
    if not isinstance(citations, list) or not citations or any(not isinstance(item, str) or item not in labels for item in citations):
        raise ValueError("Citations must reference nonempty supplied source labels.")


def _strict_meeting(output: dict, case: dict):
    from pipeline.meeting_contracts import ANALYSIS_LIST_FIELDS, CANONICAL_STATUS_BY_FIELD, REQUIRED_ITEM_FIELDS, validate_analysis
    allowed = set(ANALYSIS_LIST_FIELDS) | {"meetingSummary", "proposedHandoffSummary", "citations"}
    if set(output) != allowed:
        raise ValueError("Meeting analysis must contain exactly the required analysis fields.")
    for field in ANALYSIS_LIST_FIELDS:
        if not isinstance(output[field], list):
            raise ValueError(f"Meeting {field} must be an array.")
        for item in output[field]:
            if not isinstance(item, dict) or not set(REQUIRED_ITEM_FIELDS).issubset(item):
                raise ValueError(f"Incomplete meeting item in {field}.")
            if item["status"] != CANONICAL_STATUS_BY_FIELD[field] or item["sourceType"] != "meeting transcript":
                raise ValueError(f"Wrong status or source type in {field}.")
            quote = re.sub(r"\s+", " ", str(item["evidenceText"])).strip().casefold()
            matches = [segment for segment in case["transcript"]["segments"] if quote and quote in re.sub(r"\s+", " ", segment["text"]).casefold() and segment["speaker"] == item["speaker"]]
            if not matches or not any(segment["timestampStart"] <= float(item["timestampStart"]) <= float(item["timestampEnd"]) <= segment["timestampEnd"] for segment in matches):
                raise ValueError(f"Unsupported quote, speaker or timestamp in {field}.")
            required = ("owner", "targetDate", "dependency") if field == "actions" else ()
            if any(not isinstance(item.get(key), str) or not item[key].strip() for key in required):
                raise ValueError("Every action requires an owner, timing and dependency.")
    if len(output["actions"]) < 2 or not output["correctedAssumptions"] or not output["citations"]:
        raise ValueError("The supplied meeting contains corrections and multiple actions; they must not be omitted.")
    validate_analysis(copy.deepcopy(output), case["transcript"])


def validate_output(case: dict, output: dict, prompts: dict) -> list[dict]:
    brief, service, _meeting = production_modules()
    checks = []

    def check(name, fn):
        try:
            fn()
            checks.append({"name": name, "passed": True})
        except (ValueError, TypeError, KeyError, AssertionError) as error:
            checks.append({"name": name, "passed": False, "reason": str(error)[:800]})

    def contract():
        if case["action"] == "brief.generate":
            brief._validate_generation_route(output, brief.REFINEMENT_TARGETS)
        elif case["action"] == "brief.refine":
            normalized = copy.deepcopy(output)
            if case["target"] == "objections":
                normalized["objections"] = brief._canonical_objections(output.get("objections"))
            brief._validate_complete_refinement_target(normalized, case["request"])
        elif case["action"] == "handoff.generate":
            if set(output) != {"projectAnswer", "projectArtifacts", "projectUpdate", "citations"}:
                raise ValueError("Handoff must contain only the required top-level fields.")
            for action in output.get("projectArtifacts", {}).get("nextSteps", {}).get("immediateActions", []):
                if any(not isinstance(action.get(key), str) or not action[key].strip() for key in ("action", "owner", "timing", "dependency", "decisionGate")):
                    raise ValueError("Immediate actions must include owner, timing, dependency and decision gate.")
            service._validate_agent_result(copy.deepcopy(output))
            service._assert_grounded_sources(output, prompts["allowedSourceLabels"])
        elif case["action"] == "catchup.generate":
            if set(output) != {"projectAnswer", "citations"}:
                raise ValueError("Catch-up must not create or update project artifacts or registers.")
            service._validate_catchup_result(output)
        else:
            _strict_meeting(output, case)

    check("production-contract", contract)
    if case["action"] != "meeting.analyze":
        check("source-labels", lambda: _citation_check(output, prompts["allowedSourceLabels"]))
    if case["action"] == "brief.refine":
        def refinement():
            target = case["target"]
            canonical = copy.deepcopy(output)
            if target == "objections":
                canonical[target] = brief._canonical_objections(output.get(target))
            diagnostic = brief._contradiction_diagnostics(canonical, case["request"])
            if not diagnostic["contradictionValidationPassed"]:
                raise ValueError("Contradictions: " + ", ".join(diagnostic["contradictionFindings"]))
            previous = brief._brief_snapshot(case["request"], "previousBrief")
            after = brief._preserve_unaffected_refinement_sections(copy.deepcopy(canonical), case["request"])
            for key in brief.REFINEMENT_PACKET_SECTIONS:
                if key != target and after[key] != previous[key]:
                    raise ValueError(f"Non-target section changed: {key}")
            old, new = previous[target], canonical[target]
            if isinstance(new, dict):
                unchanged = [key for key in old if old[key] == new.get(key)]
            else:
                unchanged = [str(index) for index, item in enumerate(old) if index < len(new) and item == new[index]]
            if unchanged:
                raise ValueError("Target passages were not regenerated: " + ", ".join(unchanged))
        check("refinement-isolation-and-corrections", refinement)
    for index, rule in enumerate(case.get("checks", [])):
        value = _text(at_path(output, rule["path"])).casefold()
        checks.append({"name": f"anchor:{rule['path']}:{index}", "passed": any(term.casefold() in value for term in rule["anyOf"]), "reason": "Presence check only; the judge assesses whether the concept is used correctly."})
    for rule in case.get("forbidden", []):
        text = _text(at_path(output, rule["path"])).casefold()
        checks.append({"name": f"forbidden:{rule['path']}", "passed": not any(term.casefold() in text for term in rule["terms"])})
    return checks


def parse_response(response: dict) -> dict:
    if response.get("stopReason") not in {"end_turn", "stop_sequence"}:
        raise ValueError("Generation did not complete cleanly: " + str(response.get("stopReason")))
    text = "".join(item.get("text", "") for item in response.get("output", {}).get("message", {}).get("content", []))
    brief, _service, _meeting = production_modules()
    return brief._parse_json_object(text)


def generate(case: dict, prompts: dict, client, model_id: str, max_tokens: int) -> dict:
    combined = {}
    for part in prompts["parts"]:
        response = client.converse(modelId=model_id, system=[{"text": part["system"]}], messages=[{"role": "user", "content": part["content"]}], inferenceConfig={"maxTokens": max_tokens, "temperature": 0.1})
        value = parse_response(response)
        if case["action"] == "brief.generate":
            unexpected = set(value) - set(part["sections"]) - {"citations"}
            if unexpected or not all(key in value for key in part["sections"]):
                raise ValueError(f"Route {part['route']} returned the wrong section keys.")
        for key, item in value.items():
            if key == "citations":
                if not isinstance(item, list) or not all(isinstance(label, str) for label in item):
                    raise ValueError("Citations must be strings.")
                combined[key] = list(dict.fromkeys(combined.get(key, []) + item))
            elif key in combined:
                raise ValueError(f"Duplicate section from another route: {key}")
            else:
                combined[key] = item
    return combined
