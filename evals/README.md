# Evaluating PilarPrep Models

This suite answers a practical question: will a candidate model produce useful, grounded PilarPrep output without breaking the application's contracts?

It runs the real prompt builders and validators against 28 fictional scenarios. A run can report structure, factual requirements, evidence use, latency, token usage, an optional Strands Evals score, and a worksheet for human review.

The evaluator is separate from the application. It does not deploy infrastructure, change the model used by the app, enqueue jobs, upload audio, or write project state. Only commands with `-Live` or `--live` call paid AWS models.

## Quick Start

From the repository root in PowerShell:

```powershell
# Create the isolated evaluation environment. No model calls.
.\scripts\run-model-evals.ps1 -Setup

# See the available cases and preview the smoke set. No model calls.
.\scripts\run-model-evals.ps1 -List
.\scripts\run-model-evals.ps1 -Tag smoke

# Paid: run the three smoke cases with Nova Pro and a fixed Nova Pro judge.
.\scripts\run-model-evals.ps1 -Tag smoke -Live -MaxCalls 16
```

The smoke set covers three regressions that matter to PilarPrep: retaining payroll requirements, fully correcting a business case, and keeping objection feedback inside the objections tab. The runner prints the planned calls before it starts and stops at `-MaxCalls`, including judge calls.

## Run a Candidate Model

Preview any command before adding `-Live`:

```powershell
# Run the configured aliases with the same cases and judge.
.\scripts\run-model-evals.ps1 -Tag smoke -Models nova-pro,nova-micro,sonnet -MaxCalls 36

# Try one Bedrock Converse-compatible model without editing the config.
.\scripts\run-model-evals.ps1 -Tag smoke -Candidate 'candidate=MODEL_OR_INFERENCE_PROFILE_ID' -MaxCalls 32 -Live

# Run the complete synthetic set against Nova Pro.
.\scripts\run-model-evals.ps1 -Limit 0 -MaxCalls 120 -Live

# Focus on a workflow or repeat one case to see variation.
.\scripts\run-model-evals.ps1 -Tag refinement -Limit 0 -MaxCalls 24 -Live
.\scripts\run-model-evals.ps1 -Case generate-bluemesa-payroll -Repeats 3 -MaxCalls 24 -Live
```

`-Limit` defaults to three matching cases; `-Limit 0` removes that limit. Tags are OR-matched, while a case ID and tags are combined with AND. `-Judge none` runs deterministic checks without a quality score, but live candidate calls still cost money.

[models.json](models.json) defines the Nova Pro, Nova Micro, and Claude Sonnet aliases. Use `-Candidate name=ID` for a one-time test or add an alias for repeated work. An unavailable model, region, or permission fails visibly. The runner never maps it silently to another model.

Linux and macOS users can call the Python entry point directly:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r evals/requirements.txt
python -m evals.model_eval --tag smoke
python -m evals.model_eval --tag smoke --live --max-calls 16
```

## Scenario Coverage

| Workflow | Cases | What they look for |
| --- | ---: | --- |
| Generate | 12 | Customer variety, payroll requirements, cloud-state distinctions, values, sparse evidence, and untrusted source text |
| Refine | 6 | Every brief tab, complete target regeneration, corrected facts, named roles, and untouched non-target tabs |
| Handoff | 3 | Architecture evidence, ownership, dependencies, commitments, and approval boundaries |
| Catch-up | 3 | Useful read-only summaries for a new member, executive, and engineer |
| Meeting analysis | 4 | Transcript-backed corrections, named owners, unresolved decisions, and retained participant names |

The customer data spans nine fictional organizations. [customers.json](scenarios/customers.json) holds the source context, [cases.json](scenarios/cases.json) holds the tests and acceptance criteria, and the BlueMesa packet and transcript files provide known starting points for follow-on workflows.

The meeting cases use transcript text, not the MP3. They evaluate the analysis contract but do not test upload authorization, GuardDuty, Amazon Transcribe, EventBridge, or SQS.

## How a Result Is Judged

Each trial can pass through three layers:

1. **Deterministic checks** validate the production JSON shape, required sections, source labels, topic coverage, target isolation, and known contradictions.
2. **Strands Evals** scores factual correctness, instruction following, evidence faithfulness, audience usefulness, and bounded next steps. The default pass threshold is 7.5 out of 10, with no material factual failure and all deterministic checks passing.
3. **Human review** asks a person to read the response beside its evidence and record factual errors and usefulness in `review.csv`.

A citation label or keyword match is not proof that a claim is supported. A judge score is not a calibrated probability, and a model should not be selected from its average score alone. Keep the judge fixed during a comparison, inspect failures, review examples blind when practical, and rerun finalists.

The judge uses the open-source [Strands Evals SDK](https://github.com/strands-agents/evals), pinned in [requirements.txt](requirements.txt). It is an evaluation dependency, not another production service.

Customer context, baseline packets, transcripts, and candidate responses remain inside the Guardrail-screened evidence block. Genuine safety blocks remain visible as `candidate_blocked` or `judge_blocked`; the suite never invents a replacement answer or score.

## Reports

A paid run creates an ignored folder at `outputs/model-evals/<run-id>/`:

| File | What it is for |
| --- | --- |
| `report.md` | Readable summary and per-case failures |
| `results.json` | Raw outputs, checks, judge reasoning, model IDs, tokens, timings, and errors |
| `inputs.json` | Exact synthetic inputs and prompts used by the run |
| `summary.csv` | Trial counts, pass coverage, latency, tokens, and configured token-cost estimates |
| `review.csv` | Blank fields for human scores, factual errors, and notes |

Reports are checkpointed after every trial. A new run creates a new folder and invokes models again; it does not resume without cost. Exit code `0` means all enabled checks passed, `1` means at least one failure or budget skip, `2` means setup failed, and `130` means the run was interrupted.

Do not publish a report after adapting the suite to private data.

## AWS Access and Cost

The default profile is `pillarprep-deployer` in `us-east-1`. Use an assumed role, never root credentials, and confirm the account before a paid run. The evaluator needs Bedrock model invocation and Guardrail permissions; it does not need write access to S3, DynamoDB, SQS, or CloudFormation.

The runner discovers the Guardrail from the PilarPrep stack. Missing Guardrail settings stop the run rather than disabling safety. Cross-region models can require permissions for destination models as well as the inference profile.

- `-MaxCalls` limits candidate and judge attempts. It is not a dollar budget.
- Candidate requests run sequentially with no automatic fallback or application repair loop.
- Output defaults to 4,800 tokens per candidate route and 1,600 for the judge.
- Token prices in [models.json](models.json) begin as unknown. Add a rate only after checking the exact model, region, and tier against [AWS Bedrock pricing](https://aws.amazon.com/bedrock/pricing/).
- [pricing/2026-08-30.json](pricing/2026-08-30.json) is a dated snapshot for reproducibility, not a live price feed.
- Reported cost covers configured token rates only. It is not an AWS invoice and excludes Guardrails and supporting services.

## What the Suite Does Not Prove

The benchmark freezes the Nova Pro prompt profile and common inference settings so model choice is the main variable. It does not reproduce every production retry, Micro-specific tuning, or latency mode.

Handoff, catch-up, and meeting cases call Bedrock with the production reasoning prompts, but they do not run the deployed AgentCore Runtime, Strands tools, queue, storage, or browser. Evidence retrieval uses frozen fixtures instead of querying the live Knowledge Base. Use the application's tests and authorized live smoke checks before changing a production model.

## Verify Without Model Charges

```powershell
.\work\model-eval-venv\Scripts\python.exe -m unittest discover evals/tests -v
.\scripts\run-model-evals.ps1 -Tag smoke
```

These tests mock Bedrock, including the Strands Evals integration. The existing `npm run eval:briefs` command remains the deterministic demo-quality check; it is separate from live candidate evaluation.
