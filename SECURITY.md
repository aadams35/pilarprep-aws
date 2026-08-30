# Security

## Demo Boundaries

The public site is a demonstration with synthetic customer information. Do not enter secrets or confidential customer data. Private audio upload requires authentication and is currently restricted to the BlueMesa meeting workflow. Names in the demo are retained intentionally; that is not a claim that names are never personal data.

## Implemented Controls

- CloudFront with a private S3 REST origin, Origin Access Control, HTTPS, security headers, and WAF configuration.
- Cognito-backed guest IAM/SigV4 and signed-in JWT paths, followed by server-side scope checks.
- Private artifact/evidence storage, KMS configuration, and constrained download/upload authorization.
- Job idempotency, leases, version checks, controlled queue processing, and a DLQ.
- GuardDuty Malware Protection before uploaded audio reaches Transcribe.
- Bedrock Guardrails and application validation; their exact application depends on the action and deployed settings.
- Authorized-evidence retrieval with tenant/client/project checks and explicit human review before meeting proposals are committed.

## Important Limits

GuardDuty is a malware scanner, not a semantic content moderator. Bedrock Guardrails do not guarantee factual correctness. Source-coverage indicators are heuristic and can be wrong. PII detection and redaction are intentionally disabled across workflows: Comprehend is not called, and names, contact details, and other supplied context can remain in model requests and saved packets. Access controls and human review still apply. Only submit information you are authorized to process; the public demonstration remains synthetic-data-only.

This repository does not establish regulatory compliance, a penetration-test result, or a production SLA. Tenant lifecycle administration, evidence approval separation, data-subject requests, deletion testing, and broader adversarial evaluation remain production work. Retained compatibility endpoints should be reviewed before exposing a new deployment.

## Report a Vulnerability

Use the repository's **Security -> Report a vulnerability** option when available. Otherwise contact the maintainer privately through the contact information on [their GitHub profile](https://github.com/aadams35). Do not post access tokens, signed URLs, customer content, or exploit details in a public issue.

Include the affected component, a synthetic reproduction, expected/actual behavior, and impact. No response-time commitment is implied for this demonstration project.

## Publication Checks

`npm run check:publication` checks for common credential formats, signed URLs, private-key material, machine-specific paths, forbidden local files, and broken local documentation links. It is a safeguard, not proof that a repository contains no secrets. Review the exact staged files before pushing.
