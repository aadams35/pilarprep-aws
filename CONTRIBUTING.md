# Contributing

Thanks for taking a look at PilarPrep. The easiest changes to review are small, connected to one user workflow or AWS responsibility, and backed by a test that explains the expected behavior.

## Making a Change

1. Create a branch from `main`.
2. Use synthetic data only. Never commit credentials, customer documents, recordings, exported state, or generated deployment files.
3. Use the repository layout in the README and surrounding tests to find the right owner for the change.
4. Add focused tests, especially for authorization, refinement isolation, retries, and approval versions.
5. Run `npm run verify`.
6. In the pull request, explain what changed for the user, how it was verified, and whether it affects deployment.

## Local Setup

Offline verification uses Node.js 22.13+, npm, Python 3.12, and Chromium for Playwright. A Python virtual environment is recommended; install `requirements-dev.txt` inside it. AgentCore packages its own pinned runtime dependencies from `backend/agentcore/runtime/requirements.txt`.

## Guardrails for Contributors

- Keep authorization and scope checks on the server. Browser state is not a security boundary.
- A refinement may regenerate the selected brief tab, but it must preserve every other tab.
- Describe evidence coverage as traceability, not a probability that a claim is true.
- Keep malware scanning, content safety, and privacy policy as separate concerns.
- Pull-request checks must not deploy infrastructure or invoke paid models.
- Treat changes to stateful AWS resources as migrations, even when the desired change appears cosmetic.

Report suspected vulnerabilities privately to the maintainer rather than posting sensitive details in a public issue. See [NOTICE.md](NOTICE.md) for attribution and reuse terms.
