import assert from "node:assert/strict";
import test from "node:test";

import {
  assertAgentResultEnvelope,
  parseAgentJobAccepted,
  pollScopedAgentJob,
} from "../frontend/src/lib/agent-jobs.ts";

const scope = { clientId: "apex-mutual", projectId: "apex-mutual" };
const accepted = {
  jobId: "job-123",
  ...scope,
  status: "queued",
  pollAfterMs: 750,
};

test("polling accepts queued and running before the scoped completed result", async () => {
  const responses = [
    { status: 202, body: { ...accepted, status: "running" } },
    { status: 200, body: { provider: "agentcore", projectAnswer: "A".repeat(100), metadata: scope } },
  ];
  const waited = [];
  const result = await pollScopedAgentJob(
    accepted,
    async () => responses.shift(),
    async (milliseconds) => void waited.push(milliseconds),
  );
  assert.equal(result.provider, "agentcore");
  assert.deepEqual(waited, [750, 750]);
});

test("polling rejects a cross-client pending response", async () => {
  await assert.rejects(
    pollScopedAgentJob(
      accepted,
      async () => ({ status: 202, body: { ...accepted, clientId: "other-client" } }),
      async () => {},
    ),
    /outside the selected client scope/,
  );
});

test("polling times out without returning stale content", async () => {
  await assert.rejects(
    pollScopedAgentJob(
      accepted,
      async () => ({ status: 202, body: { ...accepted, status: "running" } }),
      async () => {},
      750,
    ),
    /still working after twelve minutes/,
  );
});

test("catch-up result must be AgentCore, detailed, and selected-client scoped", () => {
  const valid = {
    provider: "agentcore",
    projectAnswer: "Grounded catch-up guidance for the selected approved client packet. ".repeat(2),
    metadata: scope,
  };
  assert.equal(assertAgentResultEnvelope(valid, scope, true).provider, "agentcore");
  assert.throws(
    () => assertAgentResultEnvelope({ ...valid, provider: "bedrock" }, scope, true),
    /did not complete/,
  );
  assert.throws(
    () => assertAgentResultEnvelope({ ...valid, metadata: { ...scope, clientId: "other-client" } }, scope, true),
    /outside the selected client scope/,
  );
  assert.throws(
    () => assertAgentResultEnvelope({ ...valid, projectAnswer: "short" }, scope, true),
    /incomplete/,
  );
});

test("pending job envelope rejects malformed state", () => {
  assert.throws(
    () => parseAgentJobAccepted({ ...accepted, status: "complete" }, scope),
    /invalid pending job state/,
  );
});
