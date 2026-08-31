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

## Dead-letter Queue

The standard queue redrives repeatedly failing messages to a DLQ. A DLQ does not automatically make the failed operation safe to replay. First classify the cause, correct permissions/configuration or bad input as appropriate, and verify the job's current approval/version state. Use the restricted operator replay path for eligible jobs. Do not repeatedly replay stale approvals or permanent validation failures.

The code has limits on replay and separates retryable failures from non-retryable requests. Watch both queue age and DLQ message count; an empty error log does not prove the queue is healthy.

## Observability

The templates configure CloudWatch logs, metrics, alarms, tracing, and notification resources. Not all resources appear in the simplified path diagram. Deployment-specific outputs and CloudFormation parameters are authoritative; do not assume every optional email subscription is confirmed.

Useful indicators include queue age, failures by action, generation latency, model routing, estimated token cost, cross-scope attempts, RAG retrieval failures, clean scans, and transcription continuation events. An application's token-cost estimate is not the complete AWS bill.

## Cost Controls

SQS buffers work; the Lambda event-source mapping controls parallel processing. This bounds pressure on Bedrock and AgentCore without requiring provisioned concurrency. It does not eliminate invocation, model, storage, scanning, transcription, observability, or supporting-service costs.

Use guest/user limits, model routing, bounded tokens/retries, and the live-generation switch. Budgets and alarms provide notice rather than a guaranteed real-time spending cap. Review current AWS prices before making any cost promise.

## Optional Live Verification

The `smoke:*` scripts are separate from CI because they require authorized AWS access and may incur charges. Read their required environment variables and restrictions first. Use only synthetic data and your own stack outputs. The default `npm run verify` does not need an AWS account.

## Cleanup

Inspect `DeletionPolicy`, bucket versioning/lifecycle, and DynamoDB deletion protection before deleting any stack. Retained data, KMS keys, packaging artifacts, and other nonempty buckets may survive stack deletion and continue to incur charges. Back up only the records you are authorized to retain. Never use a recursive cleanup command against a path or bucket that has not been explicitly verified.
