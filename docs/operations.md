# Operating PilarPrep

PilarPrep handles AI work asynchronously. The browser receives a job ID, the request waits in SQS until a worker is available, and the page checks the job until it completes. That job ID is the best starting point whenever something goes wrong.

Never place customer input, credentials, bearer tokens, or signed artifact URLs in a public issue or shared troubleshooting note.

## Quick Triage

| What the user sees | Check first | Likely next step |
| --- | --- | --- |
| Request rejected immediately | Sign-in state, allowed origin, response code, and client/project scope | Check Cognito, API authorization, or usage limits |
| Job stays queued | Queue depth, oldest-message age, and worker event mapping | Check worker concurrency, throttles, and permissions |
| Generation fails | Worker log for the job ID and action | Check model access, Guardrail result, output limits, or validation |
| Refinement changes the wrong tab | Requested target and base packet version | Check target isolation and stale-version handling |
| Approval reports a newer version | Latest draft version in DynamoDB | Reload the packet and approve the current version |
| Handoff is unavailable | Approved packet version and requested audience | Check AgentCore invocation, scoped tools, and result validation |
| Audio upload fails | Signed-in workspace, upload authorization, expiry, and bucket CORS | Check object limits and upload metadata |
| Malware scan never finishes | GuardDuty result and first EventBridge delivery | Check the event rule, queue policy, and waiting job |
| Transcription never finishes | Transcribe status and second EventBridge delivery | Check transcript location, queue delivery, and meeting job scope |
| Page displays a CloudFront 403 | WAF sampled requests and the job's current state | Reduce polling pressure or review the approved WAF policy |
| Download fails | Latest scoped artifact pointer | Check object existence, URL expiry, and encryption permissions |

## Follow One Job

1. Capture the job ID from the `202 Accepted` response or the browser's status request.
2. Find the matching DynamoDB record and confirm its tenant, client, project, action, version, and status.
3. If it is queued, compare queue age with available worker capacity.
4. If it is running, find the structured worker logs with the same job ID and trace ID.
5. Separate queue wait from model or AgentCore duration. More workers can reduce a backlog, but they do not make one model response faster.
6. If the job completed, confirm that the result pointer and packet version match what the browser requested.

The application records timing and error categories without logging prompt or response bodies. Use those fields before increasing timeouts or replaying work.

## Queue and Dead-letter Handling

SQS may deliver a message more than once. The worker uses leases, idempotency records, and conditional writes so a duplicate delivery cannot create a second approved result.

The standard deployment allows three receives before a repeatedly failing message moves to the dead-letter queue. A DLQ entry is evidence to investigate, not a request to replay blindly. Fix the underlying permission, configuration, model, or input problem first. Then use the scoped replay path only when the current packet version and approval state still make the job valid.

Watch both visible queue depth and oldest-message age. A quiet application log does not prove that the queue is healthy.

## Meeting Audio

The meeting path has two asynchronous pauses:

`private upload -> malware scan -> queue -> Transcribe -> queue -> meeting analysis`

The audio object itself never travels through SQS. Each event carries identifiers and an object reference. The worker verifies the clean scan, scope, expected object version, and waiting job before starting transcription. When Transcribe finishes, the second event lets the worker load the transcript and continue through AgentCore and Strands.

If the UI stops at one stage, inspect that stage's event and job state before restarting the whole workflow. Re-uploading the same recording can create a new object version and make an older continuation intentionally invalid.

## Slow AI Work

Start by asking where the time was spent:

- A long queue wait points to worker capacity, account concurrency, or a downstream throttle.
- A short queue wait and long worker duration point to model generation, AgentCore tool calls, validation, or repair attempts.
- A fast completed job with a slow page points to polling, API throttling, WAF behavior, or browser rendering.

PilarPrep keeps prompts bounded, reuses AWS clients between warm Lambda invocations, and lets SQS own transient retries. AgentCore handoffs use a fresh project-scoped session for each queued invocation so an old conversation cannot grow the prompt indefinitely. Permanent size or validation failures stop clearly instead of consuming every queue retry.

Do not raise worker concurrency to solve a slow individual response. It only allows more jobs to run at the same time and can increase pressure on Bedrock, AgentCore, Lambda, and account quotas.

## Demo Usage Limits

The deployment defaults are:

| Scope | Default allowance |
| --- | ---: |
| Guest identity | 20 AI submissions per UTC hour and 200 per UTC day |
| Signed-in user | 100 per UTC day |
| Workspace | 500 per UTC day |
| Claude Sonnet | 5 per UTC day |

These are application admission limits, not Bedrock credits or a guaranteed spending ceiling. A submitted job counts even when it later fails. Polling, downloads, and approval do not count as new AI submissions, and a repeated idempotency key returns the existing job.

An exhausted limit returns `AI_USAGE_LIMIT`, the affected window, a reset time, and `Retry-After`. Raising a configured limit preserves the existing counters; deleting counters is not the way to apply a new allowance.

## Packet State and Evidence

A handoff is reusable only when its tenant, client, project, approved packet version, audience, and focus match the current request. Catch-up reads the latest approved packet and remains read-only.

Evidence coverage is the share of assessed claims linked to approved sources. It is not a truth score. A refinement reassesses the selected tab, keeps untouched tabs unchanged, and makes the previous approval stale. A failed refinement leaves the last valid packet available.

Meeting analysis follows a similar rule: transcript-backed changes remain proposals until a person accepts, edits, or rejects them. Only accepted changes can update project state or feed the next handoff.

## Monitoring

The infrastructure templates configure CloudWatch logs, metrics, tracing, alarms, and an optional notification path. The most useful signals are:

- visible jobs and oldest SQS message age
- worker throttles, duration, and failures by action
- DLQ message count
- Bedrock and AgentCore latency, token use, and validation retries
- API 429 and CloudFront/WAF 403 responses
- failed scope checks or cross-client access attempts
- GuardDuty scan outcomes and transcription continuation events
- retrieval failures and evidence coverage changes

Application cost estimates cover model tokens only when pricing is configured. They are not the AWS bill.

## Live Checks and Cost

The `smoke:*` commands use deployed AWS resources and can incur charges. Read each script first, use synthetic data, and run it only with an authorized profile. The normal `npm run verify` suite is offline.

SQS controls how quickly accepted work reaches Lambda; it does not remove the cost of Lambda execution, model tokens, AgentCore, storage, malware scanning, transcription, or monitoring. Keep the usage limits, bounded output sizes, retry limits, and live-generation switch in place. AWS Budgets sends alerts but is not an immediate shutdown control.

## Cleanup

Before deleting a stack, inspect its retention and deletion policies. Versioned buckets, KMS keys, deployment artifacts, and retained DynamoDB data may survive stack deletion and continue to incur charges. Back up only data you are authorized to keep, and verify every bucket or path before removing anything.

See [scaling](scaling.md) for worker capacity, multi-user behavior, and a measured rollout plan.
