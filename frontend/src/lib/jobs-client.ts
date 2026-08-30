import type {
  AuthorizedClientSummary,
  EvidenceDocumentRecord,
  BriefResponse,
  PipelineJobAccepted,
  PipelineJobStatus,
} from "./types";

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
    pollAfterMs:
      typeof value.pollAfterMs === "number" && Number.isFinite(value.pollAfterMs)
        ? Math.max(750, Math.min(value.pollAfterMs, 5000))
        : 1500,
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
  } = {}
) {
  const controller = new AbortController();
  const timeoutError = new DOMException(
    "The AI job is still running after twelve minutes. Try again.",
    "TimeoutError"
  );
  const relayAbort = () =>
    controller.abort(
      options.signal?.reason ?? new DOMException("The AI request was cancelled.", "AbortError")
    );
  options.signal?.addEventListener("abort", relayAbort, { once: true });
  const timeout = setTimeout(() => controller.abort(timeoutError), timeoutMs);
  let pollAfterMs = accepted.pollAfterMs;
  let consecutiveFetchFailures = 0;
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
      const waitMs = Math.max(750, Math.min(pollAfterMs, 5000));
      await abortableWait(waitMs);
      let rawStatus: unknown;
      try {
        rawStatus = await fetchStatus(controller.signal);
        consecutiveFetchFailures = 0;
      } catch (error) {
        if (controller.signal.aborted) {
          throw abortReason();
        }
        consecutiveFetchFailures += 1;
        if (consecutiveFetchFailures > 3) {
          throw error;
        }
        pollAfterMs = Math.min(4_000, 750 * 2 ** consecutiveFetchFailures);
        continue;
      }
      const status = parsePipelineStatus(rawStatus, accepted);
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
        pollAfterMs = status.pollAfterMs ?? pollAfterMs;
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
