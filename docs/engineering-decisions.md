# Engineering Decisions

## Asynchronous Jobs, Not Long Browser Requests

Model and transcription latency varies. HTTP 202 plus a scoped job ID lets the browser stay responsive while SQS buffers work. A single worker routes different actions through shared authorization, state, validation, and error handling. Polling is simple to operate; push updates remain a possible improvement if traffic justifies the added complexity.

## DynamoDB and S3 Have Different Jobs

DynamoDB supports conditional updates, leases, approvals, and latest-record lookups. S3 holds larger inputs, documents, audio, and transcripts. A pointer is not the object itself. The active application table is separate from compatibility resources retained by earlier templates; this repository does not claim those resources have been retired.

## Bedrock, AgentCore, and Strands

Bedrock provides managed model inference. Straightforward brief generation/refinement calls it from the worker's generator. AgentCore hosts follow-on workflows and their governed tool access; Strands coordinates handoff and meeting-analysis work. Not every AgentCore request is a multi-tool agent loop: catch-up also has a direct inference path inside the runtime.

## Retrieval Before Authority

Bedrock Knowledge Bases and S3 Vectors retrieve approved evidence with scoped metadata. Results are rechecked before use. Evidence coverage is heuristic, not a calibrated confidence score. Missing or conflicting sources require discovery and human review, not a fabricated citation.

## Human Approval Is a Versioned Decision

Refining one tab preserves unrelated tabs but invalidates approval of the previous packet. Meeting analysis proposes changes; it does not silently overwrite approved facts. Pre-call handoff starts with an explicit action after approval. Reviewed meeting changes are promoted deterministically.

## Security Has Multiple Boundaries

Private hosting, scoped APIs, controlled upload, malware scanning, content safeguards, and output validation solve different problems. None replaces the others. Raw audio does not go directly into a model. A clean malware scan does not certify the content as appropriate, confidential-data-free, or accurate.

## Next Improvements

1. Retire compatibility APIs/workers through a separately tested infrastructure migration.
2. Add reconciliation or an outbox for partial failures across job creation, S3 input storage, and SQS publication.
3. Separate evidence submission from evidence approval and strengthen approval provenance.
4. Expand adversarial and cross-tenant testing, measured live-model quality evaluations, and deletion/retention verification.
5. Break the large React workspace into smaller workflow-owned components without changing its user-facing flow.
6. Add repeatable fresh-account deployment tests and documented restore/recovery exercises.

These are engineering tradeoffs, not features represented as already completed in the demo.
