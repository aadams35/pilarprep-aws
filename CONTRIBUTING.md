# Contributing

Keep changes scoped to a customer workflow or an infrastructure responsibility. Follow the [architecture-to-code map](docs/architecture/code-map.md) when deciding where code belongs.

1. Create a branch from `main`.
2. Use synthetic fixtures only. Never commit credentials, customer documents, recordings, exports, or generated deployment state.
3. Add tests for behavior changes, especially authorization, refinement isolation, retries, and approval versions.
4. Run `npm run verify` after installing the dependencies listed in the README.
5. Explain the behavior changed, verification performed, and any deployment implications in a pull request.

## Local Dependencies

Node.js 22.13+, npm, Python 3.12, and Chromium for Playwright are sufficient for offline verification. Use a Python virtual environment when possible and install `requirements-dev.txt`. The AgentCore runtime has its own pinned deployment dependencies in `backend/agentcore/runtime/requirements.txt`.

## Review Boundaries

- Keep API scope checks on the server; browser state is not an authorization boundary.
- Preserve unrelated tabs when refining a selected brief.
- Do not label heuristic evidence coverage as a probability of truth.
- Keep malware scanning separate from content safety and privacy policy.
- Do not deploy or invoke live models from pull-request CI.
- Do not rename physical AWS resources as part of file cleanup. Treat replacement or deletion as an explicit migration.

See [NOTICE.md](NOTICE.md) for attribution and reuse terms. Security issues should follow [SECURITY.md](SECURITY.md), not a public issue containing private evidence.
