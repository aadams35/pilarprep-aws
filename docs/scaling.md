# Scaling PilarPrep

PilarPrep does not need a separate AI Worker for every user. It deploys one Lambda function, and AWS can run several independent executions of that function at the same time. SQS keeps the rest of the work in order until capacity is available.

## What Happens During a Burst

Suppose ten people click **Generate** at nearly the same time:

1. API Gateway and the Jobs API validate each request, save its job state and input, and return a job ID.
2. All ten jobs enter SQS.
3. The worker's configured maximum concurrency decides how many jobs start at once.
4. The remaining jobs wait without holding open a browser request or a Lambda execution.
5. Each browser polls its own job and receives the matching result when it is ready.

With the repository default of two worker executions, two jobs can process while eight wait. Raising that setting reduces queue delay during a burst, but it does not make an individual Bedrock or AgentCore response faster.

An EC2 Auto Scaling group is not part of this design. Lambda supplies the compute scaling, and the SQS event-source mapping controls how much of that capacity PilarPrep uses.

## The Controls That Matter

| Control | What it changes | What to watch |
| --- | --- | --- |
| Worker maximum concurrency | Number of queued AI jobs processed together | Lambda account headroom, Bedrock quotas, AgentCore limits, cost |
| Lambda account concurrency | Total executions available across APIs, workers, and tools | Throttles from PilarPrep or unrelated workloads |
| SQS queue and visibility timeout | Backlog buffering and safe retry window | Oldest-message age, duplicate delivery, DLQ growth |
| API Gateway throttling | Request and polling rate accepted by the API | 429 responses and shared-IP traffic |
| WAF rate rules | Edge protection for the public site | Legitimate users behind one office or conference IP |
| Bedrock quotas | Model requests and tokens available per minute | Throttling, model choice, output size, repair calls |
| Application usage limits | How many new AI jobs a guest, user, or workspace may submit | Abuse, public-demo cost, expected audience size |

The deployment script defaults `WorkerMaximumConcurrency` to `2` and preserves an existing stack value when the parameter is omitted. Its preflight check budgets capacity for the worker and possible AgentCore tool activity:

```text
required unreserved Lambda capacity = (2 x worker limit) + 4
```

That formula is a conservative deployment check, not reserved capacity or a throughput guarantee.

## Multi-user Safety

Scaling is useful only if results stay isolated. PilarPrep enforces that in several places:

- Every job carries server-validated tenant, client, project, user, and session scope.
- Guest identities receive separate demo workspaces, even when they choose the same scenario.
- Job leases and idempotency records make repeated SQS delivery safe.
- Conditional writes prevent two edits to the same packet version from both becoming current.
- JSON and DOCX are written before the latest-version pointer advances.
- Catch-up can read approved state but cannot modify project state.

Two people editing the same shared packet can still conflict. One update wins; the other user reloads the latest version and reapplies the change. This is deliberate version protection, not live collaborative editing.

## A Practical Growth Plan

Start with the repository default and increase only from measured demand:

| Stage | Suggested worker limit | Evidence needed before moving on |
| --- | ---: | --- |
| Personal or portfolio demo | 2 | Normal generation, handoff, catch-up, and audio checks pass |
| Small group demo | 3 | Concurrent-browser test passes with no throttles, scope failures, or DLQ growth |
| Team trial | 5 | Lambda quota has comfortable headroom; WAF and API polling are tested from a shared IP |
| Wider use | Measured value | Mixed-workload load test, model quota review, cost controls, and operating targets are in place |

A simple planning estimate is:

```text
sustainable jobs per second = worker concurrency / average worker duration in seconds
```

For example, three workers averaging 40 seconds each complete roughly 4.5 jobs per minute. Retries, handoffs, and meeting analysis have different runtimes, so use real measurements rather than treating that estimate as a promise.

To deploy a tested increase while preserving the other pipeline settings:

```powershell
.\scripts\deploy-jobs-pipeline.ps1 -AllowedOrigin https://YOUR-DOMAIN -WorkerMaximumConcurrency 3
```

Review the full command and CloudFormation change set first. This script updates the complete pipeline, not only the concurrency value.

## Testing Concurrent Users

The repository includes a paid live smoke test that opens independent guest browser contexts and submits the same synthetic scenario at the same time:

```powershell
npm ci
npx playwright install chromium
npm run smoke:multiuser -- --users 4 --url https://YOUR-DOMAIN --confirm-cost
```

The test checks that each browser receives its own job and result, that duplicate submissions are blocked, and that no deterministic fallback appears. It does not certify signed-in tenant membership, a shared corporate IP, or every audio path. Run those cases separately before inviting a larger audience.

## Monitor Before Increasing Capacity

Watch worker throttles, queue depth, oldest-message age, p95 worker duration, API 429s, WAF 403s, terminal failures, and DLQ messages. Compare queue wait with worker duration before changing capacity.

If an increase creates errors, restore the previous worker limit and code while leaving the queue and stored state intact. The existing live-generation switch can pause new AI submissions during an incident, but it does not cancel jobs that are already queued or running.

## Cost

The concurrency setting itself has no separate fee. Lambda still charges for requests and execution duration, while Bedrock, AgentCore, SQS, storage, scanning, transcription, and monitoring have their own usage costs. More concurrency mainly changes how quickly accepted work runs, but it can also let more work complete in a short period.

Provisioned concurrency is a separate paid option and is not required by this design. For a public demo, keep the application limits and live-generation switch, review token estimates, and use AWS billing alerts. Neither a queue limit nor an AWS Budget is a real-time spending cutoff.

See [operations](operations.md) for troubleshooting and safe DLQ handling.
