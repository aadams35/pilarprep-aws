import type { AgentJobAccepted, BriefResponse } from "./types";

type AgentScope = {
  clientId: string;
  projectId: string;
  jobId?: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredString(value: unknown, field: string) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`AgentCore returned an invalid ${field}.`);
  }
  return value;
}

export function parseAgentJobAccepted(
  value: unknown,
  expected: AgentScope
): AgentJobAccepted {
  if (!isRecord(value)) {
    throw new Error("AgentCore did not return a usable scoped job.");
  }
  const jobId = requiredString(value.jobId, "job ID");
  const clientId = requiredString(value.clientId, "client scope");
  const projectId = requiredString(value.projectId, "project scope");
  if (
    clientId !== expected.clientId ||
    projectId !== expected.projectId ||
    (expected.jobId && jobId !== expected.jobId)
  ) {
    throw new Error("AgentCore returned a job outside the selected client scope.");
  }
  if (value.status !== "queued" && value.status !== "running") {
    throw new Error("AgentCore returned an invalid pending job state.");
  }
  const pollAfterMs =
    typeof value.pollAfterMs === "number" && Number.isFinite(value.pollAfterMs)
      ? Math.max(750, Math.min(value.pollAfterMs, 5000))
      : 1500;
  return { jobId, clientId, projectId, status: value.status, pollAfterMs };
}

export function assertAgentResultEnvelope(
  value: unknown,
  expected: AgentScope,
  requireAgentCore: boolean
) {
  if (!isRecord(value)) {
    throw new Error("AgentCore returned an invalid result.");
  }
  if (
    typeof value.projectAnswer !== "string" ||
    value.projectAnswer.trim().length < 80
  ) {
    throw new Error("AgentCore returned an incomplete catch-up response.");
  }
  if (requireAgentCore && value.provider !== "agentcore") {
    throw new Error("Live AgentCore catch-up did not complete.");
  }
  const metadata = isRecord(value.metadata) ? value.metadata : {};
  if (
    metadata.clientId !== expected.clientId ||
    metadata.projectId !== expected.projectId
  ) {
    throw new Error("AgentCore returned content outside the selected client scope.");
  }
  return value as unknown as BriefResponse;
}

export async function pollScopedAgentJob(
  accepted: AgentJobAccepted,
  fetchJob: () => Promise<{ status: number; body: unknown }>,
  wait: (milliseconds: number) => Promise<void>,
  timeoutMs = 720_000
) {
  let pending = accepted;
  let remainingMs = timeoutMs;
  while (remainingMs > 0) {
    const waitMs = Math.max(750, Math.min(pending.pollAfterMs, 5000));
    await wait(waitMs);
    remainingMs -= waitMs;
    const response = await fetchJob();
    if (response.status === 202) {
      pending = parseAgentJobAccepted(response.body, {
        jobId: accepted.jobId,
        clientId: accepted.clientId,
        projectId: accepted.projectId,
      });
      continue;
    }
    if (response.status === 200) {
      return response.body;
    }
    throw new Error("AgentCore returned an unexpected job status.");
  }
  throw new Error("AgentCore is still working after twelve minutes. Try the request again.");
}
