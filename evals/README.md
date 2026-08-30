# Model Evaluation

Compare a candidate Bedrock model with Nova Pro using PilarPrep's prompts, output contracts and a fixed set of **28 synthetic scenarios**. Results include structural checks, an optional Strands Evals judge, latency, token usage and a human-review worksheet.

This is separate from the live app. It does not deploy anything, change the production model, enqueue jobs, upload recordings, or write customer state. **Only `-Live` / `--live` invokes paid models.**

## Start Here

From the repository root in PowerShell, with Python 3.12 installed:

```powershell
# One-time setup in work/model-eval-venv. No model calls.
.\scripts\run-model-evals.ps1 -Setup

# List every scenario, then preview the three regression smoke cases.
.\scripts\run-model-evals.ps1 -List
.\scripts\run-model-evals.ps1 -Tag smoke

# Paid: run those three cases with Nova Pro and a fixed Nova Pro judge.
.\scripts\run-model-evals.ps1 -Tag smoke -Live -MaxCalls 16
```

The smoke set tests payroll requirements, full business-case correction and objections-only refinement. It needs five candidate calls plus judge calls. The runner prints its plan before starting and stops at the total model-call cap, including any extra judge calls.

Nova Pro is the only default candidate. The judge is also Nova Pro unless explicitly changed. The app's production settings are never edited by this tool.

## Compare Models

Preview a comparison before adding `-Live`:

```powershell
# Compare the three configured candidates using the same judge and inputs.
.\scripts\run-model-evals.ps1 -Tag smoke -Models nova-pro,nova-micro,sonnet -MaxCalls 36

# Paid: test a new Converse-compatible model against the Nova Pro baseline.
# Replace MODEL_OR_INFERENCE_PROFILE_ID with the exact available Bedrock ID.
.\scripts\run-model-evals.ps1 -Tag smoke -Candidate 'candidate=MODEL_OR_INFERENCE_PROFILE_ID' -MaxCalls 32 -Live

# Paid: run all 28 cases against Nova Pro. Candidate calls: 52, plus judging.
.\scripts\run-model-evals.ps1 -Limit 0 -MaxCalls 120 -Live

# Paid: select one workflow or repeat a case to assess variation.
.\scripts\run-model-evals.ps1 -Tag refinement -Limit 0 -MaxCalls 24 -Live
.\scripts\run-model-evals.ps1 -Case generate-bluemesa-payroll -Repeats 3 -MaxCalls 24 -Live
```

Use `-Judge none` for deterministic checks only. This still charges for candidate generation with `-Live`, and it does **not** produce a quality score. `-Limit` defaults to three cases; use `-Limit 0` for every matching case. Tags are OR-matched; case IDs and tags are combined with AND.

[models.json](models.json) defines the current Nova Pro, Nova Micro and Sonnet aliases. Add an alias there for a reusable candidate, or pass `-Candidate name=ID` for a one-off comparison. An unsupported model, unavailable region or denied permission is reported as an error, never silently mapped to another model. This runner currently expects the standard Bedrock Converse text API; models needing different request parameters require an explicit adapter change.

### Other Platforms

The Python entry point also works on Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r evals/requirements.txt
python -m evals.model_eval --tag smoke
python -m evals.model_eval --tag smoke --live --max-calls 16
```

Run `python -m evals.model_eval --help` for every option. The PowerShell launcher always uses its isolated environment and can be invoked by its absolute path from another folder.

## Scenarios

| Workflow | Count | What it tests |
|---|---:|---|
| Generate | 12 | Nine customer profiles; payroll integration; on-premises, hybrid and AWS distinctions; company values; sparse evidence; unvisited URLs; untrusted source instructions |
| Refine | 6 | All six tabs, whole-target regeneration, corrected facts, names and roles, objection isolation, preserved non-target tabs |
| Handoff | 3 | SA evidence needs, PM ownership and dependencies, Sales commitments and approval boundaries |
| Catch-up | 3 | New member, executive and engineer perspectives; useful read-only output from approved context |
| Meeting analysis | 4 | Transcript-backed corrections, distinct owners, unresolved launch approval and retained participant names |

The customer set covers BlueMesa Payments, Apex Mutual, Northstar Health, PeakCart Retail, ForgeWorks Manufacturing, LumenStream Media, CedarCloud SaaS, Harbor Logistics and SeedSpark. All names, facts, evidence excerpts and `.example` URLs are fictional. Nothing is fetched from those URLs.

- [customers.json](scenarios/customers.json): customer context, stakeholders, values, priorities and frozen evidence.
- [cases.json](scenarios/cases.json): scenarios, feedback, acceptance criteria, topic checks and prohibited content.
- [blue-mesa-packet.json](scenarios/blue-mesa-packet.json): hand-authored starting packet for refinement and follow-on tests, **not** a generated answer or fallback.
- [blue-mesa-transcript.json](scenarios/blue-mesa-transcript.json): synthetic transcript with speaker labels and fixture timestamps. It confirms the existing AWS platform while correcting API-first and retention assumptions.

Only BlueMesa has meeting-analysis cases. These run on transcript text, not the MP3, so they do not exercise GuardDuty, Transcribe or upload permissions. The timestamps are evaluation fixtures, not alignment measurements of the demo recording.

## How Scoring Works

1. **Deterministic checks:** validate the production JSON contracts, required sections, source labels, topic anchors, target isolation and existing contradiction rules. Meeting proposals must quote the supplied transcript with the matching speaker and timestamp. Topic presence or a valid citation label is not proof of factual support.
2. **Strands Evals review:** the fixed judge considers factual correctness, instruction adherence, evidence faithfulness, audience usefulness and bounded next steps. It returns a score out of 10 and an explanation. A score of at least 7.5, no material factual/instruction failure, and passing deterministic checks are required for a `passed` result.
3. **Human review:** read the output beside its evidence, record factual errors and score usefulness in `review.csv`. Do not choose a model from its average score alone.

Candidate identity is withheld from the judge, and candidate ordering is randomized with a recorded seed. Keep the judge fixed across comparisons. A Nova judge can still favor its own model family; review samples blind where practical, rerun finalists and use a different fixed judge as a cross-check. Scores are not calibrated probabilities or proof that hallucinations are absent.

The judge uses the open-source [Strands Evals SDK](https://github.com/strands-agents/evals), pinned in [requirements.txt](requirements.txt). It is not a new service in the production architecture.

The grading rubric and scenario acceptance criteria are trusted system instructions. All customer context, baseline packets, transcripts and candidate responses remain inside the Guardrail's screened evidence block. This avoids treating the judge's own grading instructions as a customer prompt attack without exempting candidate content or disabling the policy. Genuine blocks produce `candidate_blocked` or `judge_blocked`, never a fabricated score. Rule type, confidence and processing units are recorded without copying matched sensitive text from the Guardrail trace.

Objection objects are normalized to the same `Concern` / `Response` / `Ask` format used in production before completeness checks. Missing fields and cross-tab changes still fail; the original generated response is preserved in the report.

## Reports

Each paid run creates an ignored directory at `outputs/model-evals/<run-id>/`:

| File | Contents |
|---|---|
| `report.md` | Comparison summary and per-case failures |
| `results.json` | Raw outputs, checks, judge reasoning, per-call tokens/timings/errors, model IDs and run settings |
| `inputs.json` | Exact synthetic case data and built prompts used in that run |
| `summary.csv` | Per-model trial counts, coverage, latency, tokens and estimated token cost |
| `review.csv` | Blank human scores, factual errors and notes; never populated with invented reviews |

Reports are checkpointed after each trial, including failures. Interrupted runs retain completed work. `results.json` records the Git commit, dataset/prompt hashes, package versions, Guardrail version and inference settings. Do not publish these output directories if you later adapt the suite to private data.

`checkStatus` preserves the structural result even if judging subsequently fails. `judge_blocked`, `judge_error` and `judge_budget_skipped` mean the output has no quality score. Inspect per-call `guardrail` decisions and `stopReason` before attributing these failures to the candidate model.

Exit codes: `0` means all requested trials passed their enabled checks; `1` means evaluation failures, errors or budget skips; `2` means setup/plan failure; `130` means interruption. Without a judge, success is explicitly `checks_passed`, not a quality endorsement. A rerun creates a new directory and invokes the models again; it does not resume for free.

## AWS Access and Cost Controls

The default profile is `pillarprep-deployer`, in `us-east-1`. Override with `-Profile` and `-Region`. Use an assumed role; root credentials are rejected. Refresh the profile's existing login method when expired, then verify identity with `aws sts get-caller-identity --profile pillarprep-deployer`. Never paste exported credentials into the suite or commit them.

The role needs `bedrock:InvokeModel` on the selected model/inference profiles and `bedrock:ApplyGuardrail` on the configured Guardrail. Cross-region inference may require destination-model permissions as well. The runner discovers `BedrockGuardrailId` and `BedrockGuardrailVersion` through `cloudformation:DescribeStacks` on `pillarprep-bedrock`. Alternatively, use the Python CLI's `--guardrail-id` and `--guardrail-version` together. No S3, DynamoDB, SQS or deployment write permission is needed by the evaluation code.

The SDK sends candidate and judge requests with the configured Guardrail. Missing Guardrail configuration fails setup; it is not disabled to improve scores. A safety block in the untrusted-evidence case is a Guardrail outcome to inspect, not proof that the model cannot follow ordinary instructions. Other blocks, access denials and truncation remain visible as errors.

- `-MaxCalls` caps all candidate and judge Converse attempts, including SDK judge repairs. It is **not a dollar budget**.
- Candidate requests run sequentially with no automatic retry, repair, fallback or provisioned capacity. AWS SDK retries are disabled. Model quotas and network failures can still interrupt a trial.
- `-MaxTokens` defaults to 4,800 output tokens **per candidate route**, with 1,600 for the judge. A generated packet uses three routes; other actions use one.
- Costs include candidate tokens, judge tokens and Guardrail processing. No free-tier eligibility is assumed.
- Optional per-million-token rates in [models.json](models.json) start at `null`. Fill them only after checking the exact model, region and inference tier against [official Bedrock pricing](https://aws.amazon.com/bedrock/pricing/). Reports label missing rates/usage unknown, never zero.
- [pricing/2026-08-30.json](pricing/2026-08-30.json) is a dated standard-inference snapshot for the initial three-model comparison. Pass `--model-config evals/pricing/2026-08-30.json` to reproduce its token estimates. Recheck prices before future runs; it is not a live price feed.
- Reported cost is a token-only estimate, not an AWS bill. It excludes Guardrail processing and other service charges. Candidate and judge calls are individually recorded; shared judge cost appears in each candidate's total.

## What This Benchmark Does Not Prove

The adapters reuse the application's prompt builders and validators, but intentionally freeze the **Nova Pro prompt profile**, temperature `0.1`, standard latency tier, evidence and output limits for every candidate. This isolates model choice; it does not reproduce Micro's model-specific tuning, production repair loops or optimized inference settings. The judge uses temperature `0`.

Handoff, catch-up and meeting cases call Bedrock directly with the production reasoning prompts. They do not run the deployed Strands tools or AgentCore Runtime. Retrieval evidence is frozen rather than queried from the Knowledge Base. There is no authentication, customer-isolation, queue, S3/DynamoDB persistence, audio processing or full-browser test here. Generation time excludes judge time and contains no SQS wait.

Some production contradiction rules are deliberately lexical and can flag negated mentions such as "not on-premises." Review those failures separately from actual factual errors. Keyword matches and source-label checks cannot establish semantic entailment. The judge and human review cover that gap, imperfectly.

Use the existing application/unit/browser tests and authorized live smoke tests for system behavior. A candidate should pass those checks before any production model switch.

## Verify the Runner Without Charges

```powershell
.\work\model-eval-venv\Scripts\python.exe -m unittest discover evals/tests -v
.\scripts\run-model-evals.ps1 -Tag smoke
```

Tests mock the Bedrock transport, including the actual Strands Evals scoring integration. CI installs the pinned SDK, runs these tests and previews the smoke plan without AWS credentials or paid calls. The existing `npm run eval:briefs` remains the deterministic demo-rubric check; it is not replaced by or confused with a live model benchmark.
