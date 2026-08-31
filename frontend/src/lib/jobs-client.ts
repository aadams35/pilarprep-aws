import type {
  AuthorizedClientSummary,
  EvidenceDocumentRecord,
  BriefResponse,
  PipelineJobAccepted,
  PipelineJobStatus,
} from "./types";
import { readRetryDelay } from "./api-response.ts";

function pollInterval(value: unknown, fallback = 1500) {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(750, Math.min(value, 5000))
    : fallback;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredString(value: unknown, field: string) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`The Jobs API returned an invalid ${field}.`);
  }
  return value;
}

export function pipelineApiUrl(baseUrl: string, path: string) {
  return `${baseUrl.replace(/\/$/, "")}/${path.replace(/^\//, "")}`;
}

export function parsePipelineAccepted(
  value: unknown,
  expected: { clientId: string; projectId: string }
): PipelineJobAccepted {
  if (!isRecord(value)) {
    throw new Error("The Jobs API did not return a usable job.");
  }
  const status = value.status;
  const clientId = requiredString(value.clientId, "client scope");
  const projectId = requiredString(value.projectId, "project scope");
  if (
    clientId !== expected.clientId ||
    projectId !== expected.projectId ||
    (status !== "queued" && status !== "running")
  ) {
    throw new Error("The Jobs API returned a job outside the selected scope.");
  }
  return {
    jobId: requiredString(value.jobId, "job ID"),
    clientId,
    projectId,
    status,
    pollAfterMs: pollInterval(value.pollAfterMs),
    idempotent: value.idempotent === true,
  };
}

export function parsePipelineStatus(
  value: unknown,
  expected: PipelineJobAccepted
): PipelineJobStatus {
  if (!isRecord(value)) {
    throw new Error("The Jobs API returned an invalid status response.");
  }
  if (
    value.jobId !== expected.jobId ||
    value.clientId !== expected.clientId ||
    value.projectId !== expected.projectId
  ) {
    throw new Error("The Jobs API returned status outside the selected scope.");
  }
  if (
    value.status !== "queued" &&
    value.status !== "running" &&
    value.status !== "validating" &&
    value.status !== "saving" &&
    value.status !== "waiting_for_scan" &&
    value.status !== "transcribing" &&
    value.status !== "screening" &&
    value.status !== "analyzing" &&
    value.status !== "review-ready" &&
    value.status !== "approved" &&
    value.status !== "complete" &&
    value.status !== "failed"
  ) {
    throw new Error("The Jobs API returned an unknown job state.");
  }
  return value as unknown as PipelineJobStatus;
}

export async function pollPipelineJob<TResult = BriefResponse>(
  accepted: PipelineJobAccepted,
  fetchStatus: (signal: AbortSignal) => Promise<unknown>,
  wait: (milliseconds: number) => Promise<void>,
  timeoutMs = 720_000,
  options: {
    signal?: AbortSignal;
    onStatus?: (status: PipelineJobStatus["status"]) => void;
    onProgress?: (status: PipelineJobStatus) => void;
    onRetry?: (delayMs: number) => void;
  } = {}
) {
  const controller = new AbortController();
  const timeoutError = new DOMException(
    "The result could not be confirmed in time. Processing may still be running; your approved brief is unchanged.",
    "TimeoutError"
  );
  const relayAbort = () =>
    controller.abort(
      options.signal?.reason ?? new DOMException("The AI request was cancelled.", "AbortError")
    );
  options.signal?.addEventListener("abort", relayAbort, { once: true });
  if (options.signal?.aborted) relayAbort();
  const timeout = setTimeout(() => controller.abort(timeoutError), timeoutMs);
  let pollAfterMs = pollInterval(accepted.pollAfterMs);
  let consecutiveFetchFailures = 0;
  let unchangedPolls = 0;
  let previousStatus: PipelineJobStatus["status"] = accepted.status;
  options.onStatus?.(accepted.status);

  const abortReason = () =>
    controller.signal.reason ??
    new DOMException("The AI request was cancelled.", "AbortError");
  const abortableWait = (milliseconds: number) =>
    new Promise<void>((resolve, reject) => {
      if (controller.signal.aborted) {
        reject(abortReason());
        return;
      }
      const onAbort = () => {
        controller.signal.removeEventListener("abort", onAbort);
        reject(abortReason());
      };
      controller.signal.addEventListener("abort", onAbort, { once: true });
      Promise.resolve(wait(milliseconds)).then(
        () => {
          controller.signal.removeEventListener("abort", onAbort);
          resolve();
        },
        (error) => {
          controller.signal.removeEventListener("abort", onAbort);
          reject(error);
        }
      );
    });

  try {
    while (!controller.signal.aborted) {
      await abortableWait(pollAfterMs);
      let rawStatus: unknown;
      try {
        rawStatus = await fetchStatus(controller.signal);
        consecutiveFetchFailures = 0;
      } catch (error) {
        if (controller.signal.aborted) {
          throw abortReason();
        }
        consecutiveFetchFailures += 1;
        const retryDelay = readRetryDelay(error, consecutiveFetchFailures);
        if (retryDelay === undefined) throw error;
        pollAfterMs = retryDelay;
        options.onRetry?.(retryDelay);
        continue;
      }
      const status = parsePipelineStatus(rawStatus, accepted);
      unchangedPolls = status.status === previousStatus ? unchangedPolls + 1 : 0;
      previousStatus = status.status;
      options.onStatus?.(status.status);
      options.onProgress?.(status);
      if (
        status.status === "queued" ||
        status.status === "running" ||
        status.status === "validating" ||
        status.status === "saving" ||
        status.status === "waiting_for_scan" ||
        status.status === "transcribing" ||
        status.status === "screening" ||
        status.status === "analyzing"
      ) {
        pollAfterMs = Math.min(
          5000,
          pollInterval(status.pollAfterMs, pollInterval(accepted.pollAfterMs)) + unchangedPolls * 250
        );
        continue;
      }
      if (status.status === "failed") {
        throw new Error(status.error || "The AI job failed after retrying.");
      }
      if (!isRecord(status.result)) {
        throw new Error("The completed AI job did not include a result.");
      }
      return status.result as TResult;
    }
    throw abortReason();
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", relayAbort);
    if (!controller.signal.aborted) {
      controller.abort(new DOMException("Polling completed.", "AbortError"));
    }
  }
}

export function parseAuthorizedClients(value: unknown) {
  if (!isRecord(value) || !Array.isArray(value.clients)) {
    throw new Error("The Jobs API returned an invalid client directory.");
  }
  return value.clients.filter((item): item is AuthorizedClientSummary => {
    return (
      isRecord(item) &&
      typeof item.clientId === "string" &&
      typeof item.projectId === "string" &&
      typeof item.company === "string" &&
      typeof item.hasApprovedBrief === "boolean" &&
      typeof item.hasHandoff === "boolean"
    );
  });
}

const evidenceStatuses = new Set([
  "STORED",
  "INGESTION_PENDING",
  "INGESTING",
  "AVAILABLE",
  "DELETION_PENDING",
  "DELETING",
  "DELETION_FAILED",
  "INGESTION_FAILED",
]);

export function parseEvidenceDocuments(value: unknown): EvidenceDocumentRecord[] {
  if (!isRecord(value) || !Array.isArray(value.documents)) {
    throw new Error("The Jobs API returned an invalid evidence directory.");
  }
  return value.documents.filter((item): item is EvidenceDocumentRecord => {
    if (!isRecord(item)) return false;
    return (
      typeof item.documentId === "string" &&
      typeof item.fileName === "string" &&
      typeof item.sourceTitle === "string" &&
      typeof item.documentType === "string" &&
      item.approvalStatus === "approved" &&
      typeof item.status === "string" &&
      evidenceStatuses.has(item.status) &&
      typeof item.version === "number"
    );
  });
}

export function parseLatestPacket(value: unknown) {
  if (!isRecord(value) || !isRecord(value.packet)) {
    throw new Error("The Jobs API returned an invalid approved packet.");
  }
  return {
    clientId: requiredString(value.clientId, "client scope"),
    projectId: requiredString(value.projectId, "project scope"),
    packetVersion:
      typeof value.packetVersion === "number" ? value.packetVersion : 1,
    approvedAt: typeof value.approvedAt === "string" ? value.approvedAt : "",
    packet: value.packet as unknown as BriefResponse,
    requestContext: isRecord(value.requestContext) ? value.requestContext : {},
  };
}

export function parseCurrentPacket(
  value: unknown,
  expected: { clientId: string; projectId: string }
) {
  if (!isRecord(value) || !isRecord(value.packet)) {
    throw new Error("The Jobs API returned an invalid current packet.");
  }
  const approvalStatus = value.approvalStatus;
  if (approvalStatus !== "draft" && approvalStatus !== "stale" && approvalStatus !== "approved") {
    throw new Error("The Jobs API returned an invalid current packet status.");
  }
  const packetVersion = value.packetVersion;
  if (typeof packetVersion !== "number" || !Number.isInteger(packetVersion) || packetVersion < 1) {
    throw new Error("The Jobs API returned an invalid current packet version.");
  }
  const clientId = requiredString(value.clientId, "client scope");
  const projectId = requiredString(value.projectId, "project scope");
  if (clientId !== expected.clientId || projectId !== expected.projectId) {
    throw new Error("The Jobs API returned a current packet outside the selected scope.");
  }
  return {
    clientId,
    projectId,
    packetVersion,
    approvalStatus,
    packet: value.packet as unknown as BriefResponse,
    requestContext: isRecord(value.requestContext) ? value.requestContext : {},
  };
}
