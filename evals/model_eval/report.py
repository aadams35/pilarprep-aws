from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean


def write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def summarize(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[row["model"]].append(row)
    summaries = []
    for model, trials in sorted(groups.items()):
        calls = [call for trial in trials for call in trial.get("calls", [])]
        scored = [trial["judge"]["scoreOutOf10"] for trial in trials if trial.get("judge")]
        generated = [trial for trial in trials if trial.get("output") is not None]
        generation_times = sorted(trial["generationMs"] for trial in generated)
        costs = [call.get("estimatedTokenCostUsd") for call in calls]
        summaries.append({
            "model": model, "trials": len(trials), "generated": len(generated),
            "candidateBlocked": sum(trial.get("status") == "candidate_blocked" for trial in trials),
            "judgeBlocked": sum(trial.get("status") == "judge_blocked" for trial in trials),
            "checksPassed": sum(bool(trial.get("checks")) and all(check["passed"] for check in trial["checks"]) for trial in trials),
            "qualityPassed": sum(trial.get("status") == "passed" for trial in trials),
            "judgeCoverage": len(scored), "judgeMeanOutOf10": round(mean(scored), 2) if scored else None,
            "meanGenerationMs": round(mean(generation_times)) if generation_times else None,
            "maxGenerationMs": max(generation_times) if generation_times else None,
            "modelCalls": len(calls),
            "knownInputTokens": sum((call.get("usage") or {}).get("inputTokens", 0) for call in calls),
            "knownOutputTokens": sum((call.get("usage") or {}).get("outputTokens", 0) for call in calls),
            "callsWithUnknownUsage": sum(call.get("usage") is None for call in calls),
            "estimatedTokenCostUsd": round(sum(costs), 6) if costs and all(cost is not None for cost in costs) else None,
        })
    return summaries


def _cell(value):
    return str(value if value is not None else "not measured").replace("|", "/").replace("\n", " ")


def _csv_value(value):
    value = "" if value is None else str(value)
    return "'" + value if value.startswith(("=", "+", "-", "@")) else value


def write_reports(directory: Path, manifest: dict, rows: list[dict]):
    summary = summarize(rows)
    write_json(directory / "results.json", {"run": manifest, "summary": summary, "results": rows})
    lines = ["# PilarPrep Model Evaluation", "", f"Run: `{manifest['runId']}`", "", "This is a direct model-quality benchmark using production prompts and fixed synthetic evidence. It does not test the deployed queue, authentication, audio transcription, RAG retrieval, or persistence.", "", "An anchor match is not proof of correctness. Judge scores are advisory; review the raw output and evidence. Token-cost estimates exclude Guardrails and other AWS charges. Missing prices or usage are reported as unknown, never as free.", "", "| Model | Generated | Checks passed | Judge coverage | Judge / 10 | Mean generation | Token cost |", "|---|---|---|---|---|---|---|"]
    for item in summary:
        total = item["trials"]
        time = f"{item['meanGenerationMs'] / 1000:.1f}s" if item["meanGenerationMs"] is not None else "not measured"
        cost = f"${item['estimatedTokenCostUsd']:.4f}" if item["estimatedTokenCostUsd"] is not None else "unknown"
        lines.append(f"| {_cell(item['model'])} | {item['generated']}/{total} | {item['checksPassed']}/{total} | {item['judgeCoverage']}/{total} | {_cell(item['judgeMeanOutOf10'])} | {time} | {cost} |")
    lines.extend(["", "## Individual Results", "", "| Case | Model | Repeat | Status | Judge / 10 | Review |", "|---|---|---|---|---|---|"])
    for index, row in enumerate(rows, 1):
        issues = [check["name"] for check in row.get("checks", []) if not check["passed"]]
        if row.get("error"):
            issues.append(row["error"])
        lines.append(f"| {_cell(row['caseId'])} | {_cell(row['model'])} | {row['repeat']} | {_cell(row['status'])} | {_cell((row.get('judge') or {}).get('scoreOutOf10'))} | {_cell('; '.join(issues) or 'Inspect output and judge reasoning')} |")
    lines.extend(["", "## Review Notes", "", "- `results.json` contains raw model output, every call's timing/usage, deterministic checks and the judge's explanation.", "- `candidate_blocked` and `judge_blocked` identify the screened phase. Per-call Guardrail diagnostics retain rule decisions, not matched text. A blocked judge has no quality score; structural checks remain in `checkStatus`.", "- `review.csv` is a human-review worksheet; blank scores are deliberately not populated by the model.", "- Generation latency excludes the judge and contains no SQS queue wait. Failed and budget-skipped trials stay in the denominator.", "- No retry, repair or demo fallback is applied to candidate responses. Strands may make multiple judge calls; all count toward the hard limit.", "- Keep the same judge, prompts, evidence and inference settings when comparing runs. A judge can favor its own model family; manually review samples and rerun finalists.", ""])
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    with (directory / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        if summary:
            writer = csv.DictWriter(stream, fieldnames=list(summary[0]))
            writer.writeheader()
            writer.writerows({key: _csv_value(value) for key, value in item.items()} for item in summary)
    review_path = directory / "review.csv"
    # Do not erase handwritten scores when a partial run writes another checkpoint.
    if not review_path.exists():
        with review_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["caseId", "model", "repeat", "humanScoreOutOf10", "factualErrors", "notes"])
            writer.writerows([_csv_value(row["caseId"]), _csv_value(row["model"]), row["repeat"], "", "", ""] for row in rows)
