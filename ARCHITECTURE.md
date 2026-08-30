# Architecture

PilarPrep is a React/TypeScript frontend with a Python serverless backend. The [service diagram](docs/architecture/pilarprep-aws-architecture.png) intentionally focuses on the active workflow; the [code map](docs/architecture/code-map.md) shows where each component is implemented.

## Browser and Access

CloudFront serves the static app from a private S3 REST origin using Origin Access Control. The workspace API is available through the same origin under `/api/`. WAF, HTTPS redirects, response headers, and API-origin verification protect that boundary.

Guest demo requests use Cognito Identity Pool credentials and SigV4. Signed-in workspaces use a Cognito User Pool and JWT-authorized routes. Authorization is checked again in the application using tenant, client, project, user, and job scope. Seeing the public JavaScript does not grant access to private artifacts.

## Generate and Refine

1. The browser submits a scoped job to API Gateway and the Jobs API Lambda.
2. The API validates input and identity, writes job/idempotency state to DynamoDB, stores the full input in S3, and sends a small reference message to SQS.
3. HTTP 202 returns the job ID. The browser polls for status; the request is not held open for model generation.
4. The AI Worker claims the job, loads its input, applies the action's safety and scope checks, retrieves permitted evidence, and calls Bedrock.
5. For refinement, the complete selected tab is regenerated and validated. Other tabs are preserved, and a changed version requires fresh approval.
6. The worker validates and persists the result. The browser retrieves the completed response through the API.

Queue delivery is at least once. Conditional writes, job leases, and version checks control duplicate work and stale updates. S3, DynamoDB, and SQS submission are not a single atomic transaction; an outbox/reconciliation mechanism remains a production improvement.

## Handoff and Catch-up

The diagram keeps AgentCore as a single component; its tools and memory are deliberately not expanded into separate boxes.

After approval, the user explicitly starts the pre-call handoff. Handoff and catch-up enter the same Jobs API and queue, then route to AgentCore. Strands coordinates handoff tools, while the runtime has a direct Bedrock catch-up path and bounded recovery behavior. AgentCore is the runtime; Strands is the orchestration library; Bedrock hosts the model.

The agent receives signed scope and can use governed tools for approved packets and project context. Retrieval is metadata-filtered and revalidated against the requested scope. AgentCore Memory supports handoff/catch-up continuity; meeting analysis does not use that conversational memory. Catch-up does not mutate the business project's approved state, although its job/result records and permitted conversation memory are still stored.

## Audio and Meeting Analysis

1. A signed-in user requests an authorized upload and sends audio directly to private S3.
2. GuardDuty Malware Protection scans the uploaded object. Its result reaches an EventBridge rule, then the shared SQS queue.
3. The worker checks the authorized waiting job, object identity, and clean-scan result before starting Transcribe. The worker does not wait for the entire recording in one invocation.
4. Transcribe writes its transcript to private S3. Its completion/failure event follows a second EventBridge rule to the same queue.
5. A subsequent worker invocation reads the transcript and sends meeting context to AgentCore/Strands for comparison with the approved brief.
6. A person accepts, edits, or rejects the proposals. Promotion uses the reviewed decisions; it is not another unconstrained model rewrite.

The two EventBridge boxes in the diagram represent different rules/event types, not two separate event buses. SQS carries event/job references, not the audio file. GuardDuty scans for malware; it is not a PII or inappropriate-language classifier. The bounded meeting demo preserves names and full-text transcript context, with application safety controls still applied.

## Data and Retrieval

- **DynamoDB:** job status, scope, leases, idempotency, project state, versions, approvals, and latest-packet pointers.
- **Artifact S3:** full job inputs, JSON/DOCX outputs, and versioned approved packets. Latest pointers do not imply that every previous approved object is deleted.
- **Meeting evidence S3:** private audio, transcripts, and approved source material.
- **Knowledge Base and S3 Vectors:** indexed approved evidence and embeddings, with scope metadata checked during retrieval.

Foundation-model weights are managed by Bedrock, not stored in the application's S3 buckets. Retrieved sources, prompts, configuration, generated documents, and conversational memory are separate kinds of data.

## Supporting Security and Operations

These services support the main routes rather than adding more processing stages:

| Service | Role |
| --- | --- |
| AWS WAF | Protects the CloudFront edge, including rate-limiting rules. The signed-in workspace's `/api/` requests also travel through CloudFront; the React-to-API line is a logical connection. |
| SQS dead-letter queue | Retains repeatedly failed deliveries after the source queue's three-receive limit, for up to 14 days. It is a failure destination, not a parallel generation path or an automatic replay loop. |
| CloudWatch and X-Ray | Collect logs, application/service metrics, and enabled Lambda traces. Alarms cover failures, queue backlog, and processing delays. |
| Amazon SNS | Receives operations-alarm notifications. Email delivery additionally requires a configured and confirmed subscription. |
| AWS KMS | Provides the deployed customer-managed encryption key for application data and queues. |
| Secrets Manager | Holds internal scope-signing and CloudFront-to-API verification secrets. Browser clients never receive these secrets. |
| IAM | Restricts the service roles and permitted resource access throughout the workflow. |

GuardDuty scans uploaded files for malware, and Bedrock Guardrails enforce AI content policies. PII detection and redaction are not performed: Comprehend calls, permissions, and enablement settings have been removed. The shared content checks return the original context unchanged when the content policy passes.

Transcribe still produces the meeting text, and Bedrock through Strands compares it with the approved packet and retrieved evidence. Human-approved corrections still update project state and the handoff. Knowledge Base evidence ingestion is a separate operation; removing Comprehend changes neither retrieval nor packet correction behavior.

The diagram reflects the repository's configuration. Verify deployed settings after applying the templates; updating documentation alone does not change AWS resources. WAF is attached to CloudFront through the existing external Web ACL rather than a new managed ACL created by this repository.

## Scope of This Repository

The active frontend uses the unified Jobs API. The Bedrock and AgentCore templates retain earlier API/worker resources for deployment compatibility; these are not additional hops in the diagram. Their handlers remain tested and are clearly separated from the active entry points. Physical resource names and established environment-variable names are retained to avoid replacing deployed resources solely for cosmetic consistency.

This is a working demonstration, not a claim of production certification. See [security boundaries](SECURITY.md) and [engineering decisions](docs/engineering-decisions.md).
