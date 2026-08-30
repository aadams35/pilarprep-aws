from __future__ import annotations

import argparse
import importlib.metadata
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .report import write_json, write_reports
from .suite import ROOT, fingerprint, load_cases, load_models, select_cases


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate PilarPrep model quality without changing the deployed application. Default: free plan only.")
    parser.add_argument("--live", action="store_true", help="Explicitly authorize paid Bedrock candidate and judge calls.")
    parser.add_argument("--list", action="store_true", help="List every scenario, with no AWS calls.")
    parser.add_argument("--models", nargs="+", default=["nova-pro"])
    parser.add_argument("--candidate", action="append", default=[], metavar="LABEL=MODEL_ID")
    parser.add_argument("--model-config", type=Path, default=ROOT / "evals" / "models.json")
    parser.add_argument("--judge", default="nova-pro", help="Fixed model alias for Strands Evals, or 'none' for deterministic checks only.")
    parser.add_argument("--case", action="append", dest="ids")
    parser.add_argument("--tag", action="append", dest="tags")
    parser.add_argument("--limit", type=int, default=3, help="Maximum scenarios; 0 selects all matching scenarios.")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=24, help="Hard cap on ALL candidate and judge Converse requests, including SDK repairs.")
    parser.add_argument("--max-tokens", type=int, default=4800, help="Same output cap for every candidate and route.")
    parser.add_argument("--timeout", type=int, default=180, help="Read timeout per model call, in seconds.")
    parser.add_argument("--profile", default="pillarprep-deployer")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--guardrail-stack", default="pillarprep-bedrock")
    parser.add_argument("--guardrail-id")
    parser.add_argument("--guardrail-version")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "model-evals")
    parser.add_argument("--seed", type=int, default=42, help="Reproducible candidate ordering to reduce order effects.")
    return parser.parse_args(argv)


def plan(args):
    cases = load_cases()
    if args.list:
        for case in cases:
            print(f"{case['id']:38} {case['action']:18} {case['title']}")
        return None
    if args.repeats < 1 or args.max_calls < 1 or not 256 <= args.max_tokens <= 16384 or not 1 <= args.timeout <= 600:
        raise ValueError("Repeats/call limit must be positive; tokens 256-16384; timeout 1-600 seconds.")
    if bool(args.guardrail_id) != bool(args.guardrail_version):
        raise ValueError("Supply both --guardrail-id and --guardrail-version, or neither.")
    cases = select_cases(cases, ids=args.ids, tags=args.tags, limit=args.limit)
    models = load_models(args.model_config, args.models, args.candidate)
    judge = None if args.judge == "none" else load_models(args.model_config, [args.judge], [])[args.judge]
    minimum = sum(3 if case["action"] == "brief.generate" else 1 for case in cases) * len(models) * args.repeats
    total = len(cases) * len(models) * args.repeats
    print(f"Scenarios: {len(cases)} | Models: {', '.join(models)} | Repeats: {args.repeats}")
    print(f"Candidate calls if every route completes: {minimum}; judge assessments: {total if judge else 0}.")
    print(f"Hard total call limit: {args.max_calls}; candidate output cap: {args.max_tokens}; judge output cap: 1600.")
    print("Costs depend on actual tokens, model prices and content checks. No dollar budget is implied by the call limit.")
    if not args.live:
        print("PLAN ONLY: no AWS calls, model charges, customer writes, or generated scores. Add --live to execute.")
    elif minimum > args.max_calls:
        raise ValueError("The candidate plan exceeds --max-calls. Reduce cases/models/repeats or explicitly raise the limit.")
    return cases, models, judge


def _aws(args):
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=10, read_timeout=args.timeout, retries={"total_max_attempts": 1, "mode": "standard"})
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    identity = session.client("sts", config=config).get_caller_identity()
    if identity["Arn"].endswith(":root"):
        raise ValueError("Use an assumed evaluation role, not AWS root credentials.")
    identifier, version = args.guardrail_id, args.guardrail_version
    if not identifier:
        stack = session.client("cloudformation", config=config).describe_stacks(StackName=args.guardrail_stack)
        outputs = {item["OutputKey"]: item["OutputValue"] for item in stack["Stacks"][0].get("Outputs", [])}
        identifier, version = outputs.get("BedrockGuardrailId"), outputs.get("BedrockGuardrailVersion")
    if not identifier or not version:
        raise ValueError("Content Guardrail configuration was not found. Supply a valid ID and version explicitly.")
    guardrail = {"guardrailIdentifier": identifier, "guardrailVersion": version, "trace": "enabled"}
    client = session.client("bedrock-runtime", config=config)
    return session, config, guardrail, client


def _version():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def safe_error(error):
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", "AwsError")
        return f"{code}: check the evaluation role, model access, region and quota. No automatic fallback was used."
    return f"{type(error).__name__}: {str(error)[:650]}"


def execute(args, cases, models, judge):
    from .adapters import build_prompts, generate, validate_output
    from .inference import BudgetExceeded, CallBudget, MeteredClient, judge_output, require_judge_dependencies

    # Fail on local input/prompt errors before creating a run or invoking any model.
    prompts = {case["id"]: build_prompts(case) for case in cases}
    if judge:
        require_judge_dependencies()
    session, client_config, guardrail, raw_client = _aws(args)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    directory = args.output.resolve() / run_id
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        "runId": run_id, "startedAt": datetime.now(timezone.utc).isoformat(), "gitCommit": _version(),
        "mode": "direct-model-quality", "region": args.region, "models": models, "judge": judge,
        "caseIds": [case["id"] for case in cases], "datasetHash": fingerprint(cases),
        "promptHashes": {name: value["promptHash"] for name, value in prompts.items()},
        "settings": {"temperature": 0.1, "maxTokens": args.max_tokens, "judgeMaxTokens": 1600, "latencyTier": "standard", "promptProfile": "fixed-nova-pro", "repeats": args.repeats, "seed": args.seed, "maxCalls": args.max_calls, "timeoutSeconds": args.timeout},
        "guardrail": guardrail, "status": "running", "customerStateWrites": False,
        "limitations": ["Frozen evidence, not live retrieval", "Direct Bedrock text reasoning, not AgentCore tool orchestration", "No queue, authentication or audio transcription test", "AI scores need human review", "Candidate calls are first-attempt with no repair or fallback"],
    }
    for package in ("boto3", "strands-agents", "strands-agents-evals"):
        try:
            manifest.setdefault("packages", {})[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            manifest.setdefault("packages", {})[package] = "not installed"
    write_json(directory / "inputs.json", {"cases": cases, "prompts": prompts})
    rows, tasks = [], []
    randomizer = random.Random(args.seed)
    for repeat in range(1, args.repeats + 1):
        for case in cases:
            aliases = list(models)
            randomizer.shuffle(aliases)
            for alias in aliases:
                row = {"caseId": case["id"], "title": case["title"], "action": case["action"], "model": alias, "modelId": models[alias]["modelId"], "repeat": repeat, "status": "not_run", "calls": [], "checks": [], "judge": None, "output": None}
                rows.append(row)
                tasks.append((case, alias, row))
    budget = CallBudget(args.max_calls)
    write_reports(directory, manifest, rows)
    print(f"Reports: {directory}")
    interrupted = False
    try:
        for index, (case, alias, row) in enumerate(tasks, 1):
            print(f"[{index}/{len(tasks)}] {case['id']} / {alias}", flush=True)
            started = time.monotonic()
            client = MeteredClient(raw_client, budget, row["calls"], kind="candidate", config=models[alias], guardrail=guardrail)
            try:
                row["output"] = generate(case, prompts[case["id"]], client, models[alias]["modelId"], args.max_tokens)
                row["generationMs"] = round((time.monotonic() - started) * 1000)
                row["checks"] = validate_output(case, row["output"], prompts[case["id"]])
                row["status"] = "checks_passed" if all(check["passed"] for check in row["checks"]) else "checks_failed"
                if judge:
                    try:
                        row["judge"] = judge_output(case, row["output"], prompts[case["id"]], session=session, client_config=client_config, config=judge, budget=budget, calls=row["calls"], guardrail=guardrail)
                        if row["status"] == "checks_passed":
                            row["status"] = "passed" if row["judge"]["test_pass"] else "quality_failed"
                    except Exception as error:
                        row["status"] = "judge_error"
                        row["error"] = safe_error(error)
            except BudgetExceeded as error:
                row["status"], row["error"] = "budget_skipped", str(error)
            except Exception as error:
                row["status"], row["error"] = "error", safe_error(error)
            row["totalMs"] = round((time.monotonic() - started) * 1000)
            row.setdefault("generationMs", row["totalMs"])
            print(f"  {row['status']} | {len(row['calls'])} model calls", flush=True)
            manifest["modelCalls"] = budget.used
            write_reports(directory, manifest, rows)
    except KeyboardInterrupt:
        interrupted = True
        for row in rows:
            if row["status"] == "not_run":
                row["status"] = "interrupted"
    finally:
        manifest["status"] = "interrupted" if interrupted else "completed"
        manifest["modelCalls"] = budget.used
        manifest["finishedAt"] = datetime.now(timezone.utc).isoformat()
        write_reports(directory, manifest, rows)
    print(f"Finished. Read {directory / 'report.md'}")
    return 130 if interrupted else (1 if any(row["status"] not in {"passed", "checks_passed"} for row in rows) else 0)


def main(argv=None):
    args = arguments(argv)
    try:
        selected = plan(args)
        return execute(args, *selected) if selected and args.live else 0
    except (Exception, KeyboardInterrupt) as error:
        print(safe_error(error), file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
