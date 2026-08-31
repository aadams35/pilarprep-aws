import assert from "node:assert/strict";
import test from "node:test";
import { ApiResponseError, readApiJson } from "../frontend/src/lib/api-response.ts";

import {
  parseCurrentPacket,
  parseEvidenceDocuments,
  parsePipelineAccepted,
  parsePipelineStatus,
  pollPipelineJob,
} from "../frontend/src/lib/jobs-client.ts";

test("current packet recovery requires an exact scope and authoritative version", () => {
  const current = parseCurrentPacket(
    {
      clientId: "apex-mutual",
      projectId: "apex-mutual",
      packetVersion: 4,
      approvalStatus: "stale",
      packet: { provider: "bedrock" },
      requestContext: { company: "Apex Mutual" },
    },
    { clientId: "apex-mutual", projectId: "apex-mutual" }
  );
  assert.equal(current.packetVersion, 4);
  assert.equal(current.approvalStatus, "stale");
  assert.throws(
    () => parseCurrentPacket(
      { ...current, clientId: "another-client" },
      { clientId: "apex-mutual", projectId: "apex-mutual" }
    ),
    /outside the selected scope/
  );
  assert.throws(
    () => parseCurrentPacket(
      { ...current, packetVersion: 0 },
      { clientId: "apex-mutual", projectId: "apex-mutual" }
    ),
    /invalid current packet version/
  );
});

const accepted = {
  jobId: "job-0001",
  clientId: "apex-mutual",
  projectId: "apex-mutual",
  status: "queued",
  pollAfterMs: 750,
  idempotent: false,
};

test("pipeline polling is serialized and reports truthful states", async () => {
  const states = ["queued", "running", "validating", "saving", "complete"];
  const observed = [];
  let activeFetches = 0;
  let maximumActiveFetches = 0;
  let index = 0;

  const result = await pollPipelineJob(
    accepted,
    async (signal) => {
      assert.equal(signal.aborted, false);
      activeFetches += 1;
      maximumActiveFetches = Math.max(maximumActiveFetches, activeFetches);
      await new Promise((resolve) => setTimeout(resolve, 2));
      const status = states[index++];
      activeFetches -= 1;
      return {
        ...accepted,
        status,
        result: status === "complete" ? { provider: "bedrock" } : undefined,
      };
    },
    async () => {},
    1_000,
    { onStatus: (status) => observed.push(status) }
  );

  assert.equal(maximumActiveFetches, 1);
  assert.deepEqual(observed, ["queued", "queued", "running", "validating", "saving", "complete"]);
  assert.equal(result.provider, "bedrock");
});

test("meeting polling continues through transcription and analysis to human review", async () => {
  const states = ["queued", "transcribing", "analyzing", "review-ready"];
  const observed = [];
  let index = 0;
  const meetingResult = {
    provider: "agentcore-strands",
    action: "meeting.process",
    status: "review-ready",
    proposalId: "proposal-0001",
    reviewItems: [{ id: "change-one" }],
  };

  const result = await pollPipelineJob(
    { ...accepted, clientId: "bluemesa-payments", projectId: "bluemesa-payments" },
    async () => {
      const status = states[index++];
      return {
        ...accepted,
        clientId: "bluemesa-payments",
        projectId: "bluemesa-payments",
        status,
        phase: status,
        result: status === "review-ready" ? meetingResult : undefined,
      };
    },
    async () => {},
    1_000,
    { onStatus: (status) => observed.push(status) }
  );

  assert.deepEqual(
    observed,
    ["queued", "queued", "transcribing", "analyzing", "review-ready"]
  );
  assert.equal(result.proposalId, "proposal-0001");
});

test("polling recovers from brief transient status-request failures", async () => {
  let calls = 0;
  const progress = [];
  const result = await pollPipelineJob(
    accepted,
    async () => {
      calls += 1;
      if (calls <= 2) {
        throw new TypeError("Failed to fetch");
      }
      return {
        ...accepted,
        action: "handoff.generate",
        status: "complete",
        retryCount: 1,
        result: { provider: "agentcore" },
      };
    },
    async () => {},
    1_000,
    { onProgress: (status) => progress.push(status.retryCount) }
  );

  assert.equal(calls, 3);
  assert.deepEqual(progress, [1]);
  assert.equal(result.provider, "agentcore");
});

test("meeting polling honors repeated rate limits and reads the same completed job", async () => {
  let calls = 0;
  const delays = [];
  const notices = [];
  const states = [];
  const result = await pollPipelineJob(
    accepted,
    async () => {
      calls += 1;
      if (calls <= 4) {
        return readApiJson(new Response('{"code":"rate_limited"}', {
          status: 429, headers: { "retry-after": "30" },
        }));
      }
      return { ...accepted, status: "review-ready", result: { proposalId: "existing-proposal" } };
    },
    async (delay) => { delays.push(delay); },
    1000,
    { onRetry: (delay) => notices.push(delay), onStatus: (status) => states.push(status) }
  );
  assert.equal(calls, 5);
  assert.deepEqual(delays, [750, 30_000, 30_000, 30_000, 30_000]);
  assert.deepEqual(notices, [30_000, 30_000, 30_000, 30_000]);
  assert.deepEqual(states, ["queued", "review-ready"]);
  assert.equal(result.proposalId, "existing-proposal");
});

test("long-running jobs slow their polling without overlapping or losing completion", async () => {
  let calls = 0;
  const delays = [];
  await pollPipelineJob(
    { ...accepted, pollAfterMs: 1500 },
    async () => ({ ...accepted, status: ++calls <= 80 ? "transcribing" : "review-ready",
      pollAfterMs: 1500, result: calls > 80 ? { complete: true } : undefined }),
    async (delay) => { delays.push(delay); },
    1000
  );
  assert.equal(delays[0], 1500);
  assert.equal(delays.at(-1), 5000);
  assert.ok(delays.every((delay) => delay >= 1500 && delay <= 5000));
  let elapsed = 0;
  const pollsInFiveMinutes = delays.filter((delay) => (elapsed += delay) <= 300_000).length;
  assert.ok(pollsInFiveMinutes < 75, `polls: ${pollsInFiveMinutes}`);
});

test("a temporary legacy CloudFront block reconnects to the existing meeting result", async () => {
  let calls = 0;
  const delays = [];
  const result = await pollPipelineJob(accepted, async () => {
    if (++calls === 1) {
      return readApiJson(new Response('<!DOCTYPE HTML><HTML>Request blocked. Generated by cloudfront</HTML>', { status: 403 }));
    }
    return { ...accepted, status: "review-ready", result: { proposalId: "already-finished" } };
  }, async (delay) => { delays.push(delay); }, 1000);
  assert.deepEqual(delays, [750, 60_000]);
  assert.equal(calls, 2);
  assert.equal(result.proposalId, "already-finished");
});

test("authentication failures stop polling immediately instead of retrying a forbidden request", async () => {
  let calls = 0;
  await assert.rejects(pollPipelineJob(accepted, async () => {
    calls += 1;
    throw new ApiResponseError("Sign in again", 401);
  }, async () => {}, 1000), /Sign in again/);
  assert.equal(calls, 1);
});

test("rate-limit retries still respect the polling deadline", async () => {
  let calls = 0;
  const pending = pollPipelineJob(accepted, async () => {
    calls += 1;
    throw new ApiResponseError("Wait briefly", 429, 30_000);
  }, (delay) => new Promise((resolve) => setTimeout(resolve, delay === 750 ? 0 : 100)), 30);
  await assert.rejects(pending, (error) => error.name === "TimeoutError");
  assert.equal(calls, 1);
});

test("an already-cancelled request does not fetch job status", async () => {
  const controller = new AbortController();
  controller.abort(new DOMException("Page changed", "AbortError"));
  let calls = 0;
  await assert.rejects(pollPipelineJob(accepted, async () => { calls += 1; }, async () => {},
    1000, { signal: controller.signal }), (error) => error.name === "AbortError");
  assert.equal(calls, 0);
});

test("external cancellation stops polling before another request starts", async () => {
  const controller = new AbortController();
  let fetchCalls = 0;
  const pending = pollPipelineJob(
    accepted,
    async () => {
      fetchCalls += 1;
      return { ...accepted, status: "running" };
    },
    (milliseconds) =>
      new Promise((resolve) => setTimeout(resolve, milliseconds)),
    5_000,
    { signal: controller.signal }
  );

  controller.abort(new DOMException("Navigation changed.", "AbortError"));

  await assert.rejects(pending, (error) => error?.name === "AbortError");
  assert.equal(fetchCalls, 0);
});

test("polling timeout clears with an explicit timeout error", async () => {
  const pending = pollPipelineJob(
    accepted,
    async () => ({ ...accepted, status: "running" }),
    (milliseconds) =>
      new Promise((resolve) => setTimeout(resolve, milliseconds)),
    5
  );

  await assert.rejects(pending, (error) => error?.name === "TimeoutError");
});

test("pipeline contracts reject cross-client job responses", () => {
  assert.throws(
    () =>
      parsePipelineAccepted(
        { ...accepted, clientId: "another-client" },
        { clientId: "apex-mutual", projectId: "apex-mutual" }
      ),
    /outside the selected scope/
  );
  assert.throws(
    () =>
      parsePipelineStatus(
        { ...accepted, status: "running", projectId: "another-project" },
        accepted
      ),
    /outside the selected scope/
  );
});

test("evidence directory accepts only bounded lifecycle records", () => {
  const documents = parseEvidenceDocuments({
    documents: [
      {
        documentId: "approved-payroll",
        fileName: "approved-payroll.md",
        sourceTitle: "Approved payroll requirements",
        documentType: "requirements",
        source: "Customer-approved notes",
        approvalStatus: "approved",
        status: "AVAILABLE",
        version: 2,
        approvedAt: "2026-08-21T12:00:00Z",
      },
      {
        documentId: "bad-status",
        fileName: "bad.md",
        sourceTitle: "Bad record",
        documentType: "requirements",
        approvalStatus: "approved",
        status: "UNKNOWN",
        version: 1,
      },
    ],
  });

  assert.equal(documents.length, 1);
  assert.equal(documents[0].documentId, "approved-payroll");
  assert.equal(documents[0].status, "AVAILABLE");
});

test("evidence directory rejects malformed API envelopes", () => {
  assert.throws(
    () => parseEvidenceDocuments({ records: [] }),
    /invalid evidence directory/
  );
});
