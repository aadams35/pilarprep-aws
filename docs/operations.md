# Operations

## Start with the Job ID

The Jobs API returns a job ID before work completes. Follow that ID through the API response, DynamoDB job record, worker logs, and model diagnostics. Do not paste customer input, bearer tokens, AWS credentials, or signed artifact URLs into public issues.

| Symptom | Check first | Then check |
| --- | --- | --- |
| Request rejected immediately | Sign-in, origin, API response, tenant/client/project scope | Cognito configuration, authorizer, application limits |
| Job remains queued | Queue age, visible messages, event-source mapping | Worker errors, concurrency limit, permissions |
| Job fails while generating | Worker error classification and action | Bedrock access, token limits, validation or Guardrail outcome |
| Refinement affects the wrong content | Selected target and base packet version | Target validation, merge isolation, stale-version rejection |
| Handoff is unavailable | Approved server packet and requested role | AgentCore permissions, tool scope, result validation |
| Audio cannot upload | Signed-in workspace, allowed scenario, signed upload, bucket CORS | Upload constraints, expiry, object metadata |
| Scan never progresses | Object scan status and GuardDuty result event | EventBridge rule, queue policy, authorized waiting job |
| Transcription stops | Transcribe job status and second event | Queue delivery, object scope/version, worker analysis failure |
| Meeting UI fails with a CloudFront page | WAF sampled requests for the job-status GET; current DynamoDB job state | Per-IP rate limits, polling cadence, HTTP response status |
| Download fails | Current scoped artifact lookup | Object existence, URL expiry, KMS permissions |

## Meeting Status and Edge Rate Limits

A blocked status request does not mean the meeting job failed. In the August 30 incident, the WAF's blanket 100-requests-per-five-minutes rule blocked four workspace job-status GETs while the same `meeting.process` job completed as `review-ready`. The browser was receiving CloudFront's HTML 403 page instead of the completed result.

Check WAF sampled request metadata and the scoped job record before retrying transcription or replaying anything from a queue. Keep request headers, user identifiers, transcript text, and signed URLs out of incident reports. Sampled requests are not a complete access log.

The frontend slows unchanged job-status polls up to five seconds apart. A 429 response delays further reads using `Retry-After` or `retryAfterSeconds`, within the original operation deadline. A recognized CloudFront blocked-request page allows at most three read retries, one minute apart. JSON authorization failures are not retried. No retry automatically submits another job, and unknown/HTML response bodies are never displayed as error text.

WAF rate-limit changes require an explicit security review. The frontend recovery fix does not change WAF, bypass CloudFront, relax sign-in requirements, or skip GuardDuty. The existing edge limit may still be reached by multiple users sharing an IP until an approved policy change separates submissions from polling traffic.

## Demo AI Usage Limits

The demo allowance is 20 AI submissions per guest identity per UTC clock hour and 200 per UTC day, shared across that identity's clients and sessions. Nova Pro remains the standard packet model. Authenticated-user (100/day), workspace (500/day), and Claude (5/day) caps are unchanged. These are application admission limits, not Bedrock credits or an AWS billing balance. They are not a global spending ceiling.

The counters record new AI submission attempts, including jobs that later fail; processing time and internal model retries do not consume extra submissions. Polling, downloads, and approval do not consume the AI allowance. Reusing an already-recorded idempotency key returns its existing job without consuming another submission.

A rejected request returns `AI_USAGE_LIMIT`, the exhausted hourly/daily window, `quota.resetsAt`, and a matching `Retry-After` header. When more than one cap is exhausted, the later reset governs. Database transaction conflicts are not quota exhaustion. Increasing a configured limit preserves the existing usage counts; do not delete counters to apply a new allowance.

## Slow Handoff Generation

Compare queue wait with worker duration before increasing concurrency. A handoff on August 31 waited 24 ms in SQS but took 181 seconds in the worker, including Strands structured-output recovery. The queue was not the bottleneck.

Handoff generation uses non-streaming Bedrock calls through Strands, requests the schema tool directly, and enables optimized latency only for Nova Pro. It keeps the same Guardrail, approved-context checks, scoped tools, Memory, and bounded recovery. The output token ceiling permits a complete packet; it is not a target length. No live draft is streamed to the page, so non-streaming does not remove a user-visible capability.

`handoff_model_completed` records duration, accumulated model calls, and token usage without prompt or response text. The packet's `metadata.agentTimingsMs` separates context preparation from generation and validation. These phase timings are not pure Bedrock inference time. A structured-output recovery warning indicates additional model work; a low queue wait does not rule that out.

An August 31 failure restored 95 AgentCore Memory events containing 2.9 million characters of repeated packet context. Bedrock returned a Guardrails input-size error as `ThrottlingException`; nested SDK retries stretched a failed attempt to 196 seconds. Batch handoffs now use fresh, project-scoped Memory sessions for each invocation, including queue retries. The approved brief and DynamoDB project state provide continuity; old conversations remain stored but are not replayed into new handoffs.

Handoff prompts retain all six brief sections, customer inputs, evidence sources, and claim support assessments while omitting duplicated claim text, previous handoff outputs, and download/diagnostic metadata. Context is encoded as complete JSON and rejected when over the size budget, never cut mid-document. `agent_context_prepared` records prompt character count without customer text. Botocore and Strands do not add transient retries inside the handoff; SQS owns those retries. Permanent size errors return `AGENT_CONTEXT_TOO_LARGE` and terminate the job without requeueing or modifying approved content. Guardrails, source validation, and scoped persistence remain enforced for Nova Pro, Nova Micro, and Claude Sonnet.

## Brief and Handoff State

Pre-call context starts empty. A completed handoff is displayed only when its customer, client, project, approved packet version, audience, and focus match the selected view. Navigating back to a matching saved handoff reuses it without submitting another job. Changing the customer cancels the browser request and prevents late results from replacing the new workspace.

Evidence assessments belong to the packet. Handoff generation preserves the approved brief's claims, source records, and coverage in both the returned response and the saved handoff. Targeted refinement reassesses the selected tab only, retains other tabs' assessments, and recalculates coverage from the current claims. Corrected context receives a distinct source reference so unchanged tabs do not silently point to rewritten evidence.

Coverage is the fraction of assessed claims linked to approved sources, not a probability of truth. Older packets without assessment records remain unassessed. Editing feedback does not remove the last valid assessment; intake changes are marked as pending. A failed refinement leaves the previous packet intact and displays an error next to it.

## Dead-letter Queue

The standard queue redrives repeatedly failing messages to a DLQ. A DLQ does not automatically make the failed operation safe to replay. First classify the cause, correct permissions/configuration or bad input as appropriate, and verify the job's current approval/version state. Use the restricted operator replay path for eligible jobs. Do not repeatedly replay stale approvals or permanent validation failures.

The code has limits on replay and separates retryable failures from non-retryable requests. Watch both queue age and DLQ message count; an empty error log does not prove the queue is healthy.

## Observability

See [Multi-user operation and scaling](scaling.md) for account headroom, the configurable SQS worker limit, concurrent-user checks, alert thresholds, and capacity rollback.

The templates configure CloudWatch logs, metrics, alarms, tracing, and notification resources. Not all resources appear in the simplified path diagram. Deployment-specific outputs and CloudFormation parameters are authoritative; do not assume every optional email subscription is confirmed.

Useful indicators include queue age, failures by action, generation latency, model routing, estimated token cost, cross-scope attempts, RAG retrieval failures, clean scans, and transcription continuation events. An application's token-cost estimate is not the complete AWS bill.

## Cost Controls

SQS buffers work; the Lambda event-source mapping controls parallel processing. This bounds pressure on Bedrock and AgentCore without requiring provisioned concurrency. It does not eliminate invocation, model, storage, scanning, transcription, observability, or supporting-service costs.

Use guest/user limits, model routing, bounded tokens/retries, and the live-generation switch. Budgets and alarms provide notice rather than a guaranteed real-time spending cap. Review current AWS prices before making any cost promise.

## Optional Live Verification

The `smoke:*` scripts are separate from CI because they require authorized AWS access and may incur charges. Read their required environment variables and restrictions first. Use only synthetic data and your own stack outputs. The default `npm run verify` does not need an AWS account.

## Cleanup

Inspect `DeletionPolicy`, bucket versioning/lifecycle, and DynamoDB deletion protection before deleting any stack. Retained data, KMS keys, packaging artifacts, and other nonempty buckets may survive stack deletion and continue to incur charges. Back up only the records you are authorized to retain. Never use a recursive cleanup command against a path or bucket that has not been explicitly verified.
