# Architecture

PilarPrep turns customer context into a meeting brief, a team handoff, and a governed follow-up workflow. The frontend is React and TypeScript; the backend is Python on AWS.

![PilarPrep AWS architecture](docs/architecture/pilarprep-aws-architecture.png)

## The Short Version

Every AI action follows the same backbone:

`browser -> CloudFront -> API Gateway -> Jobs API -> SQS -> AI Worker`

The worker then chooses the right path:

- Brief generation and refinement call Amazon Bedrock.
- Handoff, catch-up, and meeting analysis use AgentCore with Strands.
- Both paths can retrieve approved, client-scoped evidence from the Bedrock Knowledge Base.
- Validated state goes to DynamoDB; larger inputs and generated files go to private S3.

This shared pipeline keeps authentication, retries, state tracking, and error handling consistent across the product.

## From Click to Completed Packet

1. The React app sends a signed request through CloudFront to API Gateway.
2. The Jobs API checks the caller, validates the client and project scope, and creates a job record in DynamoDB.
3. Large request data is written to S3. The SQS message contains identifiers and an object reference, not the full packet.
4. The API returns `202 Accepted` with a job ID, so the browser can remain responsive and poll for status.
5. SQS starts the AI Worker when capacity is available.
6. The worker loads the input, claims a lease, retrieves permitted evidence, and routes the action.
7. The result is checked for schema, scope, version, and workflow-specific quality rules.
8. The worker saves the result and marks the job complete. The browser receives it on the next status read.

SQS delivery is at least once. Idempotency records, leases, and conditional writes prevent duplicate delivery from creating duplicate state.

## Briefs and Refinement

Bedrock generates the six briefing views and applies the configured Guardrail. Evidence retrieval happens before generation when approved sources are available.

Refinement is intentionally narrow. Feedback regenerates the complete selected tab, checks corrected facts for contradictions, and leaves every other tab untouched. A successful refinement creates a new version and makes the previous approval stale.

## Handoff and Catch-up

AgentCore hosts the follow-on workflows, while Strands coordinates their model and tool calls.

- **Handoff** turns the approved packet into role-aware preparation and next steps.
- **Catch-up** summarizes the latest approved client context for a selected role. It can read approved state but cannot change it.
- **Meeting analysis** compares a transcript with the approved packet and proposes updates for human review.

The agent receives signed tenant, client, project, user, and session scope. Retrieved evidence is filtered by metadata and checked again before use.

## Meeting Audio

Audio follows a separate, signed-in path:

1. The browser requests an authorized upload and sends the file directly to private meeting-evidence S3.
2. GuardDuty Malware Protection scans the object.
3. EventBridge sends the scan result to the shared SQS queue.
4. After the worker verifies a clean scan and the waiting job, it starts Amazon Transcribe.
5. Transcribe writes the transcript to private S3.
6. A second EventBridge rule sends the transcription result to the same queue.
7. The worker loads the transcript and routes meeting analysis to AgentCore and Strands.
8. A person accepts, edits, or rejects each proposed change before project state is updated.

SQS carries job and event references. It never carries the audio file itself. GuardDuty checks for malware; it does not decide whether meeting content is accurate, appropriate, or free of personal information.

## Where Data Lives

| Store | What it holds | Why it belongs there |
| --- | --- | --- |
| DynamoDB | Jobs, leases, idempotency, client/project state, versions, approvals, and latest pointers | Fast scoped reads and conditional updates |
| Artifact S3 | Job inputs, JSON/DOCX output, and approved packet versions | Durable storage for larger objects |
| Meeting-evidence S3 | Audio, transcripts, and approved source documents | Private object storage with upload and scan controls |
| Bedrock Knowledge Base with S3 Vectors | Searchable approved evidence and embeddings | Grounded retrieval with client/project metadata |
| AgentCore Memory | Permitted handoff and catch-up continuity | Session context for agent workflows |

Bedrock manages the foundation-model weights. PilarPrep stores prompts, context, evidence, results, and configuration, not the model itself.

## Security Boundaries

CloudFront serves the frontend from a private S3 origin and proxies workspace API requests. Cognito supports guest demo credentials and signed-in users. The backend still checks scope on every operation; browser state is never treated as authorization.

WAF, ACM, CloudWatch, X-Ray, SNS, KMS, Secrets Manager, and IAM support the paths shown in the diagram. They are collapsed there so the main request flow stays readable. The public repository is intended for synthetic demonstration data, not confidential customer information.

## Why the Design Looks This Way

- **Asynchronous jobs:** model and transcription times vary, so the browser receives a job ID instead of holding a long HTTP request open.
- **One queue and worker:** all actions share the same admission, retry, validation, and status behavior.
- **DynamoDB plus S3:** DynamoDB coordinates changing state; S3 holds large and immutable objects.
- **Retrieval before authority:** sources can inform a result, but citations and coverage indicators do not replace human judgment.
- **Versioned approval:** refinements and meeting proposals cannot silently rewrite an approved packet.

## Known Tradeoffs

This is a polished demonstration, not a production certification. The main gaps are a transactional outbox or reconciler for partial job-creation failures, stronger separation between evidence submission and approval, formal retention/deletion testing, broader adversarial tenant testing, and retirement of compatibility resources that remain in older templates.

Those compatibility handlers are packaged for safe upgrades but are not extra steps in the active browser workflow.
