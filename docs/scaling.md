# Multi-user operation and scaling

## How capacity works

PilarPrep uses one AI Worker Lambda function. AWS can run several executions of that function at once. SQS holds the remaining jobs; creating more copies of the function is unnecessary.

Page viewing and editing inputs do not occupy an AI worker. Generation, refinement, approval, handoff, catch-up, and meeting continuations share the queue. A model call occupies its worker while awaiting the response. Transcription runs separately and returns a completion event through the queue.

The queue's `MaximumConcurrency` controls processing capacity, not the number of people allowed to open the site. Raising it can reduce queue waiting, but does not make an individual Bedrock response faster.

## Verified starting point

Read from the US East (N. Virginia) deployment on August 30, 2026. Recheck these values before each capacity change; AWS account quotas are not universal defaults.

| Control | Observed setting | Implication |
| --- | --- | --- |
| Account Lambda concurrency | 10, all unreserved | API, workers, and agent-tool functions share a small pool |
| SQS worker concurrency | 2 before this release | Two jobs execute while additional jobs wait |
| Worker | 1,024 MB, 600-second timeout | Long model calls occupy capacity |
| SQS | Batch 1, no batch window, 3 receives, visibility 3,600 seconds | Partial batch responses and bounded retries remain enabled |
| HTTP API default throttle | 4 requests/second, burst 8 | Submission and status polling share this limit |
| CloudFront WAF IP rate limit | 100 requests per 300 seconds | Shared office networks can exhaust it through reading and polling |
| Nova Pro cross-region inference | 500 requests/minute; 2,000,000 tokens/minute | A job can consume several model requests, including repair/tool calls |
| Guest limits | 12 AI actions/hour, 30/day per identity | Isolated browser sessions are not a global spend cap |
| Authenticated limits | 100 actions/user/day, 500/tenant/day | Keep these independent of queue concurrency |

The above Nova quota is for standard cross-region inference. Latency-optimized inference has separate, much lower quotas in this account. Do not assume a worker increase is safe after changing model or inference mode.

## Multi-user correctness

- Guest workspaces derive separate tenant scopes from their verified identity. Matching scenario names do not grant access to another guest's jobs or artifacts.
- Signed-in users are authorized server-side for their tenant/client/project. Browser-supplied identifiers are not authorization.
- A job lease and idempotency records protect against duplicate delivery. Generation/refinement uses an optimistic version check: two edits to the same version cannot both become the latest draft.
- Draft JSON and DOCX are written under an operation-specific path. The database pointer advances only after both are written and the version check succeeds. A losing edit cannot overwrite the winning objects.
- Handoffs also use operation-specific artifact paths. Existing idempotency records retain their recorded paths, including legacy paths.
- Successfully superseded mutable artifacts are cleaned up after the pointer update. Approved snapshots are not deleted. Cleanup failures emit `ArtifactCleanupFailures` and do not undo a successful save.
- Catch-up remains read-only. It no longer mistakes another user's legitimate project update for a catch-up write; runtime action permissions and tool validation still apply.

Two people editing the same shared packet may receive a version-conflict message. They must reload and reapply their change. This is deliberate conflict protection, not simultaneous document co-editing.

## First capacity increase

Increase the SQS event mapping from **2 to 3** worker executions. Retain one worker function and the same queue. Do not add provisioned concurrency or change reserved concurrency.

The deployment script preserves `WorkerMaximumConcurrency` when omitted. Its conservative preflight budgets one possible tool invocation per worker plus four executions for APIs and other functions:

```text
required unreserved Lambda capacity = (2 x worker limit) + 4
3 workers require at least 10 unreserved executions
```

This is a planning check, not a reservation or a guarantee. Unrelated workloads and unusually high tool fan-out can consume remaining capacity. Watch account concurrency and throttles during the test.

For an existing deployment with its other settings explicitly preserved:

```powershell
.\scripts\deploy-jobs-pipeline.ps1 -AllowedOrigin https://pilarprep.app -WorkerMaximumConcurrency 3
```

Review the script's remaining parameters before running it: it is a full pipeline deployment, not only a concurrency change. For an emergency capacity rollback, set the existing SQS event source's maximum concurrency back to 2 in the Lambda console, then reconcile the stack parameter so a later deployment does not restore the higher setting.

## Phased plan

| Phase | Worker limit | Prerequisites and exit criteria |
| --- | --- | --- |
| Small public demo | 3 | Four-browser generation smoke succeeds; no cross-workspace data, worker throttles, or new DLQ failures |
| Team trial | 5 | Request account concurrency of at least 100; approve WAF read/submission separation; load-test signed-in users sharing an office IP |
| Broader demo | 10 | Repeat mixed brief/handoff/catch-up/audio tests; recheck model tokens/minute and requests/minute; tune API throttles from measured polling traffic |
| Beyond demo | Measured, not preset | Add admission/backlog limits, global spend controls, production SLOs, and separate priority queues only if mixed long-running jobs demonstrably starve short work |

For a rough capacity forecast, sustainable job rate is worker concurrency divided by average worker duration. At 40 seconds per job, three workers process about 4.5 jobs/minute and ten about 15/minute. These are planning examples, not measured production promises; retries, audio analysis, and handoffs have different durations. Keep utilization below saturation to limit queue waits.

The WAF change requires separate approval. Recommended next step: retain strict submission rate limiting while allowing a larger read-only API allowance. Do not disable WAF, bypass the signed-in origin, or loosen authorization to make a load test pass.

## Repeatable live check

This intentionally performs paid inference. It opens independent guest browser contexts, selects the same BlueMesa scenario, and submits one Nova Pro generation per user at the same time. It validates unique job/workspace scope, completion in the matching browser, no fallback, no browser errors, and no duplicate submission. It never stores browser credentials.

```powershell
npm ci
npx playwright install chromium
npm run smoke:multiuser -- --users 4 --confirm-cost
```

Allowed burst sizes are 2-6 users. Do not run repeated unattended bursts. Preserve the JSON console summaries as deployment evidence; job IDs can be correlated with structured worker logs without printing customer content.

The pre-change public check completed four Nova Pro generations in 38.7, 39.0, 84.4, and 86.4 seconds, with no API 429 responses. This small sample establishes a baseline, not a latency percentile or a production capacity certification.

### Release verification

The same four-user burst was repeated after deploying the worker cap of three:

| Measurement | Two workers | Three workers |
| --- | --- | --- |
| Completed users | 4/4 | 4/4 |
| Individual completion times | 38.7, 39.0, 84.4, 86.4 seconds | 40.1, 40.1, 42.0, 85.5 seconds |
| Average completion time | 62.1 seconds | 51.9 seconds |
| Average queue wait from worker logs | 25.2 seconds | 12.7 seconds |
| API 429s / duplicate submissions / job retries | 0 / 0 / 0 | 0 / 0 / 0 |

The extra execution lets a third user start promptly; the fourth still waits. End-to-end burst duration was nearly unchanged, so this is not evidence of faster model generation. Repeat larger mixed-workload samples before making latency guarantees.

The Jobs and AgentCore stacks reached `UPDATE_COMPLETE`. The live mapping reports maximum concurrency 3, batch size 1, and partial batch responses. The two new alarms reported `OK`, and no worker throttles were recorded over the verification window. The public generation, approval, AgentCore handoff, and DOCX-download smoke also passed on Nova Pro.

Local verification passed 70 frontend unit tests, 310 backend tests, 34 evaluation tests, 19 browser tests, the offline quality evaluation, type checking, lint, publication checks, and the production build. Live catch-up was not rerun: its existing standard-quality routing selects Micro. Catch-up's read-only behavior during another user's update is covered by the backend regression test; all paid tests in this release used Nova Pro.

The eight burst generations reported approximately $0.14 in application-estimated model cost combined. This excludes other AWS services and is not an invoice or a future price guarantee. Existing DLQ contents were left untouched.

Before opening a larger group, also test with authorized signed-in accounts:

1. Two users in separate clients cannot read each other's job, brief, handoff, or audio.
2. Two authorized users in the same project submit competing edits; exactly one version wins and its JSON and DOCX agree.
3. One user requests catch-up while another saves a project update.
4. Brief and handoff generation proceed while an authorized BlueMesa upload passes its malware scan and transcription.
5. Several users on one IP can read and poll without the WAF blocking legitimate traffic.

The guest smoke cannot certify signed-in tenant membership, shared-office WAF behavior, or the complete audio workflow. Unit tests cover the shared-project write race and catch-up concurrency independently of AWS identities.

## Monitoring and rollback

- Worker `Throttles`: alarm on any throttle in one minute. Lower queue concurrency if the account is exhausted; do not increase it further.
- Queue depth: existing alarm when more than five jobs remain visible for two minutes.
- Oldest message age: existing alarm above five minutes. Investigate stuck jobs, model throttling, API errors, and worker duration before raising capacity.
- Worker duration: existing p95 warning at 480 seconds, below the 600-second timeout.
- API 429s: existing alert at five per five minutes. Polling backs off to five seconds; raising worker capacity alone will not fix API throttling.
- DLQ and terminal failures: inspect safe job metadata and trace IDs, fix the cause, then use the existing scoped replay procedure. Never blindly replay or delete the DLQ.
- Artifact cleanup: investigate failed deletions and remove only demonstrably unreferenced mutable candidates. Interrupted writes can still leave orphan candidates; a periodic scoped orphan reconciler is future work.

If a rollout increases errors, restore the prior worker/tool code and concurrency 2, retain the same storage, and preserve the queue. Existing legacy draft paths remain readable. Pause new AI submissions with the existing kill switch during a serious incident; this does not automatically cancel work already queued or running.

## Cost

The maximum-concurrency setting itself has no extra charge. Lambda executions still incur request and memory-duration charges; model tokens, SQS operations, storage, monitoring, and agent services are also billed. More concurrency primarily changes how quickly accepted work runs, but can increase total spend by allowing more work to complete. Provisioned concurrency is a separate paid warm-capacity option and is not enabled by this plan.

Keep Nova Pro for testing, preserve per-user quotas and the AI kill switch, inspect estimated tokens/cost per job, and monitor AWS billing. A public demo with fresh guest identities cannot guarantee a $1/day ceiling through per-identity quotas or a concurrency cap. Add global admission/spend controls before wider promotion. Budgets alerts are not an immediate hard spending cap.

## References

- [Lambda scaling with SQS](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html)
- [Lambda concurrency](https://docs.aws.amazon.com/lambda/latest/dg/configuration-concurrency.html)
- [Bedrock token quota accounting](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html)
- Repository sources: `infrastructure/jobs-pipeline.yaml`, `scripts/deploy-jobs-pipeline.ps1`, `backend/ai_worker/handler.py`, `backend/agentcore/tools/handler.py`, `frontend/src/lib/jobs-client.ts`.
