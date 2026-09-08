# Security

PilarPrep is a public demonstration built with synthetic customer data. Its security model is designed to keep the application, saved packets, and uploaded meeting evidence private while still allowing people to try the workflow.

## How the Demo Is Protected

| Boundary | What PilarPrep does |
| --- | --- |
| Website | CloudFront serves the React application from a private S3 origin over HTTPS. The frontend bucket is not a public website bucket. |
| Identity | Cognito provides temporary guest credentials for the bounded demo and JWT-based access for signed-in users. The backend validates identity and project scope on every request. |
| API | Requests pass through API Gateway. Browser state and client-supplied IDs are never treated as authorization by themselves. |
| Jobs | Idempotency records, leases, version checks, and controlled SQS processing protect against duplicate or stale updates. Repeated failures move to a dead-letter queue for review. |
| Stored data | Artifacts, job inputs, meeting evidence, and transcripts remain in private S3. DynamoDB holds scoped job and project state. Encryption is configured through AWS-managed or customer-managed keys. |
| Meeting audio | Signed-in users upload directly to a private bucket. GuardDuty Malware Protection must report a clean result before transcription begins. |
| Generative AI | Bedrock Guardrails screen supported requests and responses. Application validation checks structure, scope, versions, contradictions, and evidence before a result is saved. |
| Retrieved evidence | Knowledge Base searches are filtered to the authorized tenant, client, and project, then checked again before the evidence reaches a workflow. |

Human review remains part of the design. Meeting analysis proposes changes; it does not silently rewrite an approved packet or project memory.

## Deliberate Limits

The demo keeps names and roles because they are essential to stakeholder preparation. It does not use Amazon Comprehend for PII detection or redact names before model calls. Only submit information you are authorized to process, and do not enter secrets, confidential customer data, or real meeting recordings into the public demo.

GuardDuty checks uploaded files for malware. It does not judge the meaning, appropriateness, or accuracy of a conversation. Bedrock Guardrails reduce specific content risks, but they do not guarantee factual correctness. Citations and evidence-coverage indicators help a reviewer inspect a claim; they are not probabilities of truth.

This repository is not a compliance certification, penetration-test report, or production service-level commitment. A production rollout would also need formal tenant administration, retention and deletion testing, data-classification policy, stronger separation between evidence submission and approval, security testing, and a documented incident-response process.

## Reporting a Vulnerability

Use GitHub's **Security -> Report a vulnerability** option when it is available. Otherwise, contact the maintainer privately through [their GitHub profile](https://github.com/aadams35). Do not include credentials, signed URLs, customer content, or exploit details in a public issue.

A useful report includes the affected area, a synthetic reproduction, the expected and actual behavior, and the likely impact. This demonstration does not promise a response time.

## Before Publishing a Change

Run `npm run check:publication`. It looks for common credential formats, signed URLs, private-key material, machine-specific paths, forbidden local files, and broken documentation links. It is a useful backstop, not proof that a repository contains no sensitive data, so review the staged files before every push.
