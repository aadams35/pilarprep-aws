from __future__ import annotations

import json
import re
from typing import Any, Mapping

from pipeline.state import require_identifier, require_string


SCENARIO_ID = "blue-mesa-payments"
CLIENT_ID = "bluemesa-payments"
DEFAULT_MEETING_ID = "blue-mesa-discovery"
DEFAULT_AUDIO_KEY = (
    "audio/public-demo/blue-mesa-payments/blue-mesa-discovery.mp3"
)
EVIDENCE_PREFIX = "evidence/public-demo/blue-mesa-payments/"
TRANSCRIPT_PREFIX = "transcripts/public-demo/blue-mesa-payments/"
ANALYSIS_LIST_FIELDS = (
    "confirmedFacts",
    "correctedAssumptions",
    "decisions",
    "openQuestions",
    "requirements",
    "risks",
    "scopeChanges",
    "actions",
    "stakeholderSignals",
)
REVIEW_FIELDS = (
    "correctedAssumptions",
    "decisions",
    "requirements",
    "risks",
    "scopeChanges",
    "actions",
)
REQUIRED_ITEM_FIELDS = (
    "id",
    "statement",
    "status",
    "speaker",
    "timestampStart",
    "timestampEnd",
    "evidenceText",
    "confidence",
    "sourceType",
)
SPEAKER_NAMES = {
    "spk_0": "Maya Chen, Account Executive",
    "spk_1": "Jordan Lee, Solutions Architect",
    "spk_2": "Ariana Cole, Chief Digital Officer",
    "spk_3": "Dev Malik, VP Infrastructure and Resilience",
    "spk_4": "Rachel Kim, Chief Risk and Compliance Officer",
    "spk_5": "Priya Shah, Director of Payment Operations",
}

CANONICAL_STATUS_BY_FIELD = {
    "confirmedFacts": "confirmed",
    "correctedAssumptions": "corrected",
    "openQuestions": "unresolved",
    "decisions": "new",
    "requirements": "new",
    "risks": "new",
    "scopeChanges": "new",
    "actions": "new",
    "stakeholderSignals": "new",
}


class MeetingConflictError(ValueError):
    """A stale or unresolved meeting update that must not be retried."""


class RetrievalScopeError(PermissionError):
    """Retrieved evidence escaped the authorized synthetic scenario."""


def as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def assert_public_demo_scope(
    scope: Mapping[str, str], scenario_id: object
) -> None:
    require_identifier(scenario_id, "scenarioId")
    if scenario_id != SCENARIO_ID or scope.get("clientId") != CLIENT_ID:
        raise RetrievalScopeError(
            "Meeting evidence is limited to the Blue Mesa public demo"
        )


def _speaker_segments(transcript: Mapping[str, Any]) -> list[dict[str, Any]]:
    results = transcript.get("results")
    if not isinstance(results, Mapping):
        raise ValueError("Transcript results are missing")
    raw_segments = results.get("audio_segments")
    segments: list[dict[str, Any]] = []
    if isinstance(raw_segments, list) and raw_segments:
        for index, raw in enumerate(raw_segments):
            if not isinstance(raw, Mapping):
                continue
            label = str(raw.get("speaker_label") or "spk_unknown")
            text = str(raw.get("transcript") or "").strip()
            if text:
                segments.append(
                    {
                        "id": f"segment-{index + 1}",
                        "speakerLabel": label,
                        "speaker": SPEAKER_NAMES.get(label, label),
                        "timestampStart": as_float(raw.get("start_time")),
                        "timestampEnd": as_float(raw.get("end_time")),
                        "text": text,
                    }
                )
    else:
        items = results.get("items")
        if not isinstance(items, list):
            raise ValueError("Transcript does not contain speaker-labeled items")
        current: dict[str, Any] | None = None
        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            alternatives = raw.get("alternatives")
            first = (
                alternatives[0]
                if isinstance(alternatives, list) and alternatives
                else {}
            )
            text = (
                str(first.get("content") or "")
                if isinstance(first, Mapping)
                else ""
            )
            if not text:
                continue
            label = str(raw.get("speaker_label") or "spk_unknown")
            start = as_float(raw.get("start_time"), -1)
            end = as_float(raw.get("end_time"), start)
            if current is None or current["speakerLabel"] != label:
                current = {
                    "id": f"segment-{len(segments) + 1}",
                    "speakerLabel": label,
                    "speaker": SPEAKER_NAMES.get(label, label),
                    "timestampStart": max(0.0, start),
                    "timestampEnd": max(0.0, end),
                    "text": text,
                }
                segments.append(current)
            else:
                punctuation = raw.get("type") == "punctuation"
                current["text"] += ("" if punctuation else " ") + text
                current["timestampEnd"] = max(
                    current["timestampEnd"], max(0.0, end)
                )
    if not segments:
        raise ValueError("Transcript did not contain usable speaker evidence")
    return segments


def transcript_evidence(
    transcript: Mapping[str, Any],
) -> dict[str, Any]:
    segments = _speaker_segments(transcript)
    return {
        "segments": segments,
        "durationSeconds": max(segment["timestampEnd"] for segment in segments),
        "speakerCount": len({segment["speakerLabel"] for segment in segments}),
        "text": "\n".join(
            (
                f"[{segment['timestampStart']:.2f}-{segment['timestampEnd']:.2f}] "
                f"{segment['speaker']}: {segment['text']}"
            )
            for segment in segments
        ),
    }


def json_from_model(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value).strip()
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Meeting analysis did not return JSON")
    parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Meeting analysis must be a JSON object")
    return parsed


def _normalized_text(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value).lower()))


_ON_PREM_MIGRATION_PATTERNS = (
    re.compile(r"\bmigrat(?:e|es|ed|ing|ion)\s+from\s+on[- ]prem(?:ises)?\b"),
    re.compile(r"\bmov(?:e|es|ed|ing)\s+from\s+on[- ]prem(?:ises)?\b"),
    re.compile(r"\binitial\s+aws\s+migration\b"),
)


def _has_affirmative_on_prem_migration(value: object) -> bool:
    text = str(value or "").lower()
    for sentence in re.split(r"(?<=[.!?;])\s+|[\r\n]+", text):
        for pattern in _ON_PREM_MIGRATION_PATTERNS:
            for match in pattern.finditer(sentence):
                before = sentence[max(0, match.start() - 90) : match.start()]
                after = sentence[match.end() : match.end() + 90]
                negated_before = re.search(
                    r"\b(?:no|not|never|without|avoid(?:s|ed|ing)?|"
                    r"rather\s+than|instead\s+of|reject(?:s|ed|ing)?)\b"
                    r"[^.!?;]{0,70}$",
                    before,
                )
                corrected_after = re.search(
                    r"^[^.!?;]{0,55}\b(?:incorrect|false|superseded|"
                    r"rejected|invalid|inaccurate|outdated|obsolete|wrong|"
                    r"mistaken|not\s+(?:required|needed|planned)|"
                    r"no\s+longer\s+(?:required|needed|planned)|"
                    r"out\s+of\s+scope)\b",
                    after,
                )
                if not negated_before and not corrected_after:
                    return True
    return False


def _confirms_existing_aws_state(value: object) -> bool:
    text = _normalized_text(value)
    if "aws" not in text:
        return False
    return bool(
        re.search(
            r"\b(?:already|currently|current|existing)\b.{0,90}\baws\b|"
            r"\b(?:runs?|operat(?:e|es|ing)|host(?:ed|ing)?)\b.{0,70}"
            r"\b(?:on|in)\s+aws\b|"
            r"\bno\s+initial\s+aws\s+migration\b",
            text,
        )
    )


def _analysis_claim_texts(
    output: Mapping[str, Any],
) -> list[tuple[str, str]]:
    values = [
        ("meetingSummary", str(output.get("meetingSummary") or "")),
        (
            "proposedHandoffSummary",
            str(output.get("proposedHandoffSummary") or ""),
        ),
    ]
    excluded = {
        "affectedBriefSections",
        "evidenceText",
        "previousAssumption",
        "sourceType",
        "speaker",
    }
    for field in ANALYSIS_LIST_FIELDS:
        for index, item in enumerate(output.get(field, [])):
            if not isinstance(item, Mapping):
                continue
            values.extend(
                (f"{field}[{index}].{key}", str(item_value))
                for key, item_value in item.items()
                if key not in excluded and isinstance(item_value, str)
            )
    return values


def _evidence_supported(evidence: str, transcript_text: str) -> bool:
    evidence_norm = _normalized_text(evidence)
    transcript_norm = _normalized_text(transcript_text)
    if len(evidence_norm) >= 8 and evidence_norm in transcript_norm:
        return True
    evidence_tokens = {
        token for token in evidence_norm.split() if len(token) >= 4
    }
    if not evidence_tokens:
        return False
    transcript_tokens = set(transcript_norm.split())
    return len(evidence_tokens & transcript_tokens) / len(evidence_tokens) >= 0.72


def _timestamp_segment(
    segments: object, start: float, end: float
) -> Mapping[str, Any] | None:
    if not isinstance(segments, list):
        return None
    candidates: list[tuple[float, float, Mapping[str, Any]]] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        segment_start = as_float(segment.get("timestampStart"), -1)
        segment_end = as_float(segment.get("timestampEnd"), -1)
        if segment_start < 0 or segment_end < segment_start:
            continue
        if segment_start > end + 1 or segment_end < start - 1:
            continue
        overlap = max(0.0, min(end, segment_end) - max(start, segment_start))
        distance = abs(segment_start - start) + abs(segment_end - end)
        candidates.append((-overlap, distance, segment))
    if not candidates:
        return None
    return min(candidates, key=lambda value: (value[0], value[1]))[2]


def _statement_match(statement: str, evidence: str) -> tuple[int, float]:
    ignored = {
        "about",
        "after",
        "before",
        "being",
        "customer",
        "from",
        "should",
        "their",
        "there",
        "these",
        "this",
        "those",
        "with",
    }
    statement_tokens = {
        token
        for token in _normalized_text(statement).split()
        if len(token) >= 4 and token not in ignored
    }
    if not statement_tokens:
        return (0, 0.0)
    evidence_tokens = set(_normalized_text(evidence).split())
    matched = statement_tokens & evidence_tokens
    return (len(matched), len(matched) / len(statement_tokens))


def _statement_supported(statement: str, evidence: str) -> bool:
    matched, ratio = _statement_match(statement, evidence)
    return matched >= 2 and ratio >= 0.35


def _statement_segment(
    segments: object, statement: str, start: float, end: float
) -> Mapping[str, Any] | None:
    if not isinstance(segments, list):
        return None
    candidates: list[tuple[float, int, float, Mapping[str, Any]]] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        text = str(segment.get("text") or "").strip()
        segment_start = as_float(segment.get("timestampStart"), -1)
        segment_end = as_float(segment.get("timestampEnd"), -1)
        if not text or segment_start < 0 or segment_end < segment_start:
            continue
        matched, ratio = _statement_match(statement, text)
        if matched < 2 or ratio < 0.35:
            continue
        distance = abs(segment_start - start) + abs(segment_end - end)
        candidates.append((-ratio, -matched, distance, segment))
    if not candidates:
        return None
    return min(candidates, key=lambda value: (value[0], value[1], value[2]))[3]


def validate_analysis(
    value: object,
    transcript: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Meeting analysis must be an object")
    output = dict(value)
    output["meetingSummary"] = require_string(
        output.get("meetingSummary"), "meetingSummary", maximum=8000
    )
    output["proposedHandoffSummary"] = require_string(
        output.get("proposedHandoffSummary"),
        "proposedHandoffSummary",
        maximum=8000,
    )
    citations = output.get("citations")
    if not isinstance(citations, list) or not all(
        isinstance(item, str) and item.strip() for item in citations
    ):
        raise ValueError("Meeting analysis citations are required")
    output["citations"] = list(
        dict.fromkeys(item.strip() for item in citations)
    )[:30]

    transcript_text = str(transcript["text"])
    duration = float(transcript["durationSeconds"])
    ids: set[str] = set()
    for field in ANALYSIS_LIST_FIELDS:
        raw_items = output.get(field)
        if not isinstance(raw_items, list):
            raise ValueError(f"{field} must be an array")
        normalized_items = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, Mapping):
                raise ValueError(f"{field}[{index}] must be an object")
            item = dict(raw)
            for required in REQUIRED_ITEM_FIELDS:
                if required not in item:
                    raise ValueError(f"{field}[{index}] is missing {required}")
            source_type = require_string(
                item.get("sourceType"),
                f"{field}[{index}].sourceType",
                maximum=80,
            )
            if _normalized_text(source_type) not in {
                "transcript",
                "meeting transcript",
                "speaker labeled transcript",
            }:
                continue
            item["sourceType"] = "meeting transcript"
            item_id = require_identifier(item.get("id"), f"{field}[{index}].id")
            if item_id in ids:
                raise ValueError("Meeting analysis item ids must be unique")
            ids.add(item_id)
            for string_field, maximum in (
                ("statement", 4000),
                ("status", 80),
                ("speaker", 160),
                ("sourceType", 80),
                ("evidenceText", 2500),
            ):
                item[string_field] = require_string(
                    item.get(string_field),
                    f"{field}[{index}].{string_field}",
                    maximum=maximum,
                )
            item["status"] = CANONICAL_STATUS_BY_FIELD[field]
            start = as_float(item.get("timestampStart"), -1)
            end = as_float(item.get("timestampEnd"), -1)
            if start < 0 or end < start or end > duration + 1:
                raise ValueError(f"{field}[{index}] has invalid timestamps")
            item["timestampStart"] = start
            item["timestampEnd"] = end
            confidence = as_float(item.get("confidence"), -1)
            if confidence < 0 or confidence > 1:
                raise ValueError(f"{field}[{index}] has invalid confidence")
            item["confidence"] = confidence
            if not _evidence_supported(item["evidenceText"], transcript_text):
                segment = _timestamp_segment(
                    transcript.get("segments"), start, end
                )
                canonical = (
                    str(segment.get("text") or "").strip()
                    if isinstance(segment, Mapping)
                    else ""
                )
                if not canonical or not _statement_supported(
                    item["statement"], canonical
                ):
                    segment = _statement_segment(
                        transcript.get("segments"),
                        item["statement"],
                        start,
                        end,
                    )
                    canonical = (
                        str(segment.get("text") or "").strip()
                        if isinstance(segment, Mapping)
                        else ""
                    )
                if not canonical or not _statement_supported(
                    item["statement"], canonical
                ):
                    raise ValueError(
                        f"{field}[{index}] evidence is not supported by the transcript"
                    )
                item["evidenceText"] = canonical[:2500]
                item["speaker"] = str(
                    segment.get("speaker") or item["speaker"]
                )[:160]
                item["timestampStart"] = as_float(
                    segment.get("timestampStart"), start
                )
                item["timestampEnd"] = as_float(
                    segment.get("timestampEnd"), end
                )
            if field == "actions":
                for action_field, fallback, maximum in (
                    ("owner", "Unassigned", 160),
                    ("targetDate", "Not set", 160),
                    ("dependency", "None stated", 1000),
                ):
                    item[action_field] = require_string(
                        item.get(action_field) or fallback,
                        f"{field}[{index}].{action_field}",
                        maximum=maximum,
                    )
            if field == "correctedAssumptions":
                for correction_field in (
                    "previousAssumption",
                    "meetingCorrection",
                ):
                    item[correction_field] = require_string(
                        item.get(correction_field),
                        f"{field}[{index}].{correction_field}",
                        maximum=3000,
                    )
                if _has_affirmative_on_prem_migration(
                    item["meetingCorrection"]
                ):
                    raise ValueError(
                        f"{field}[{index}].meetingCorrection contradicts the "
                        "confirmed existing-on-AWS state"
                    )
                if _has_affirmative_on_prem_migration(item["statement"]):
                    if not _confirms_existing_aws_state(
                        item["meetingCorrection"]
                    ):
                        raise ValueError(
                            f"{field}[{index}] does not provide a validated "
                            "existing-AWS correction"
                        )
                    # Preserve the obsolete wording in previousAssumption and
                    # expose the corrected current truth as the user-facing
                    # statement used by the handoff.
                    item["statement"] = item["meetingCorrection"]
                affected = item.get("affectedBriefSections")
                if not isinstance(affected, list) or not affected:
                    raise ValueError(
                        f"{field}[{index}] requires affectedBriefSections"
                    )
                item["affectedBriefSections"] = [
                    require_string(
                        section,
                        f"{field}[{index}].affectedBriefSections",
                        maximum=80,
                    )
                    for section in affected[:10]
                ]
            normalized_items.append(item)
        output[field] = normalized_items

    if not output["correctedAssumptions"]:
        raise ValueError(
            "Meeting analysis must include the transcript's corrected AWS assumption"
        )
    if len(output["actions"]) < 2:
        raise ValueError(
            "Meeting analysis must include at least two transcript-grounded actions"
        )
    unassigned_owners = {
        "",
        "unassigned",
        "owner tbd",
        "tbd",
        "not assigned",
    }
    assigned_actions = [
        item
        for item in output["actions"]
        if _normalized_text(str(item.get("owner") or "")) not in unassigned_owners
    ]
    if len(assigned_actions) < 2:
        raise ValueError(
            "Meeting analysis must preserve at least two named action owners"
        )

    meaningful_text = json.dumps(
        {
            "summary": output["meetingSummary"],
            "requirements": output["requirements"],
            "risks": output["risks"],
            "scope": output["scopeChanges"],
            "actions": output["actions"],
        },
        ensure_ascii=True,
    ).lower()
    if "payroll" not in meaningful_text:
        raise ValueError("Meeting analysis omitted the payroll objective")
    conflicting_paths = [
        path
        for path, text in _analysis_claim_texts(output)
        if _has_affirmative_on_prem_migration(text)
    ]
    if conflicting_paths:
        raise ValueError(
            "Meeting analysis contradicted the confirmed existing-on-AWS state "
            f"in {', '.join(conflicting_paths[:5])}"
        )
    return output


def compare_meeting_to_brief(
    scenario_id: str,
    meeting_id: str,
    brief_version: int,
    approved_brief: Mapping[str, Any],
    analysis: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if scenario_id != SCENARIO_ID:
        raise RetrievalScopeError("Cross-scenario comparison is forbidden")
    require_identifier(meeting_id, "meetingId")
    if isinstance(brief_version, bool) or brief_version < 1:
        raise ValueError("briefVersion must be positive")
    brief_text = json.dumps(approved_brief, ensure_ascii=True).lower()
    review_items: list[dict[str, Any]] = []
    for category in REVIEW_FIELDS:
        values = analysis.get(category)
        if not isinstance(values, list):
            continue
        for raw in values:
            if not isinstance(raw, Mapping):
                continue
            statement = str(raw.get("statement") or "")
            tokens = [
                token
                for token in re.findall(r"[a-z0-9]+", statement.lower())
                if len(token) >= 5
            ]
            original = "No matching statement in the approved brief."
            if category == "correctedAssumptions":
                original = str(raw.get("previousAssumption") or original)
            elif tokens and any(token in brief_text for token in tokens[:5]):
                original = (
                    "Related approved-brief content exists; review this meeting "
                    "evidence before replacing or extending it."
                )
            review_items.append(
                {
                    "id": raw["id"],
                    "category": category,
                    "originalContent": original,
                    "proposedUpdate": statement,
                    "speaker": raw["speaker"],
                    "timestampStart": raw["timestampStart"],
                    "timestampEnd": raw["timestampEnd"],
                    "evidenceText": raw["evidenceText"],
                    "confidence": raw["confidence"],
                    "supportStatus": raw["status"],
                    "required": True,
                }
            )
    return review_items


def accepted_changes(
    proposal: Mapping[str, Any],
    dispositions: list[Mapping[str, Any]],
    reviewed_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items = proposal.get("reviewItems")
    if not isinstance(items, list):
        raise ValueError("Meeting proposal has no review items")
    by_id = {
        str(item.get("id")): dict(item)
        for item in items
        if isinstance(item, Mapping) and item.get("id")
    }
    decisions = {
        str(item.get("id")): item
        for item in dispositions
        if isinstance(item, Mapping)
    }
    if set(decisions) != set(by_id):
        detail = sorted(set(by_id) ^ set(decisions))
        raise MeetingConflictError(
            "Every proposed change must be reviewed before approval: "
            + ", ".join(detail[:8])
        )
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item_id, item in by_id.items():
        disposition = decisions[item_id]
        decision = disposition.get("decision")
        reviewed = {
            **item,
            "decision": decision,
            "reviewedAt": reviewed_at,
        }
        if decision == "edited":
            reviewed["proposedUpdate"] = require_string(
                disposition.get("editedStatement"),
                "editedStatement",
                maximum=2000,
            )
            reviewed["edited"] = True
            accepted.append(reviewed)
        elif decision == "accepted":
            reviewed["edited"] = False
            accepted.append(reviewed)
        elif decision == "rejected":
            rejected.append(reviewed)
        else:
            raise ValueError("Unknown review decision")
    if not accepted:
        raise MeetingConflictError(
            "At least one meeting update must be accepted or edited"
        )
    return accepted, rejected
