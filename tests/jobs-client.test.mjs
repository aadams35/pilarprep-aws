import assert from "node:assert/strict";
import test from "node:test";

import {
  parseEvidenceDocuments,
  parsePipelineAccepted,
  parsePipelineStatus,
  pollPipelineJob,
} from "../frontend/src/lib/jobs-client.ts";

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
