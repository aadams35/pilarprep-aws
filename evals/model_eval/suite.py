from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "evals" / "scenarios"
ACTIONS = {"brief.generate", "brief.refine", "handoff.generate", "catchup.generate", "meeting.analyze"}
TARGETS = {"businessCase", "technical", "executive", "stakeholders", "gameplan", "objections"}
ROLES = {"Solutions Architect", "PM", "Sales", "Executive", "Engineer", "New member"}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _fixture(name: str):
    path = (SCENARIOS / name).resolve()
    if path.parent != SCENARIOS.resolve() or path.suffix != ".json":
        raise ValueError("Fixtures must be JSON files directly inside evals/scenarios.")
    return read_json(path)


def _merge(base: dict, changes: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in changes.items():
        result[key] = _merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def load_cases() -> list[dict]:
    document = read_json(SCENARIOS / "cases.json")
    customers = read_json(SCENARIOS / "customers.json")
    if document.get("schemaVersion") != 1 or document.get("synthetic") is not True:
        raise ValueError("Only the versioned synthetic scenario suite is supported.")
    cases, seen = [], set()
    for raw in document["cases"]:
        case = copy.deepcopy(raw)
        name = case.get("id", "")
        if not re.fullmatch(r"[a-z0-9-]+", name) or name in seen:
            raise ValueError(f"Invalid or duplicate scenario id: {name}")
        seen.add(name)
        if case.get("action") not in ACTIONS or case.get("customer") not in customers:
            raise ValueError(f"Invalid action or customer in {name}")
        if not case.get("title") or not case.get("requirements") or not all(isinstance(item, str) and item.strip() for item in case["requirements"]):
            raise ValueError(f"Missing acceptance criteria in {name}")
        for check in case.get("checks", []):
            if not check.get("path") or not check.get("anyOf") or not all(isinstance(term, str) and term.strip() for term in check["anyOf"]):
                raise ValueError(f"Invalid term check in {name}")
        case["request"] = _merge(customers[case["customer"]], case.get("overrides", {}))
        case["request"]["mode"] = "prebrief"
        # Freeze the prompt profile across candidates. Only the invoked model changes.
        case["request"]["modelPreference"] = "nova-pro"
        if case.get("baseline"):
            case["previous"] = _merge(_fixture(case["baseline"]), case.get("previousOverrides", {}))
            case["previous"].pop("fixtureNote", None)
        if case["action"] == "brief.refine":
            if case.get("target") not in TARGETS or not case.get("previous") or not case["request"].get("feedbackNotes"):
                raise ValueError(f"Refinement needs a target, baseline and feedback: {name}")
            case["request"].update(previousBrief=copy.deepcopy(case["previous"]), refinementTarget=case["target"], baseBriefVersion=1)
        if case["action"] in {"handoff.generate", "catchup.generate"}:
            if case.get("audienceRole") not in ROLES or not case.get("previous") or not case.get("focus"):
                raise ValueError(f"Missing agent context: {name}")
        if case["action"] == "meeting.analyze":
            if case["customer"] != "bluemesa":
                raise ValueError("Meeting fixtures are restricted to synthetic BlueMesa.")
            raw_transcript = _fixture("blue-mesa-transcript.json")
            segments = [{"speaker": row["speaker"], "timestampStart": row["start"], "timestampEnd": row["end"], "text": row["text"]} for row in raw_transcript["segments"]]
            case["transcript"] = {"text": " ".join(row["text"] for row in segments), "segments": segments, "durationSeconds": max(row["timestampEnd"] for row in segments)}
        cases.append(case)
    return cases


def select_cases(cases: list[dict], *, ids: list[str] | None = None, tags: list[str] | None = None, limit: int = 0) -> list[dict]:
    unknown = set(ids or []) - {case["id"] for case in cases}
    if unknown:
        raise ValueError("Unknown case ids: " + ", ".join(sorted(unknown)))
    selected = [case for case in cases if (not ids or case["id"] in ids) and (not tags or set(tags).intersection(case.get("tags", [])))]
    if limit < 0:
        raise ValueError("Limit must be zero (all) or positive.")
    if limit:
        selected = selected[:limit]
    if not selected:
        raise ValueError("No scenarios match these filters.")
    return selected


def load_models(path: Path, aliases: list[str], candidates: list[str]) -> dict[str, dict]:
    document = read_json(path)
    if document.get("schemaVersion") != 1:
        raise ValueError("Unsupported model configuration version.")
    available = document["models"]
    selected = {}
    for alias in aliases:
        if alias not in available:
            raise ValueError(f"Unknown model alias: {alias}")
        selected[alias] = copy.deepcopy(available[alias])
    for candidate in candidates:
        alias, sep, model_id = candidate.partition("=")
        if not sep or alias in selected:
            raise ValueError("Candidates use a unique label=bedrock-model-id.")
        selected[alias] = {"modelId": model_id, "inputUsdPerMillion": None, "outputUsdPerMillion": None}
    for alias, config in selected.items():
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", alias) or not isinstance(config.get("modelId"), str) or not config["modelId"].strip():
            raise ValueError("Model labels and IDs must be nonempty and labels must be filename-safe.")
        for key in ("inputUsdPerMillion", "outputUsdPerMillion"):
            rate = config.get(key)
            if rate is not None and (isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0):
                raise ValueError(f"Invalid token price for {alias}: {key}")
    if not selected:
        raise ValueError("Select at least one model.")
    return selected


def token_cost(config: dict, usage: dict) -> float | None:
    if any(config.get(key) is None for key in ("inputUsdPerMillion", "outputUsdPerMillion")):
        return None
    return (usage.get("inputTokens", 0) * config["inputUsdPerMillion"] + usage.get("outputTokens", 0) * config["outputUsdPerMillion"]) / 1_000_000
