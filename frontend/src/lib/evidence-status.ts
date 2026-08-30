import type { EvidenceStatus } from "./types";

const statusLabels: Record<EvidenceStatus, string> = {
  supported: "Supported",
  "partially-supported": "Partially supported",
  "customer-provided": "Supported",
  assumption: "Assumption",
  "conflicting-evidence": "Conflicting evidence",
  "needs-validation": "Unsupported",
};

const statusHeadings: Record<EvidenceStatus, string> = {
  supported: "Supported by approved evidence",
  "partially-supported": "Partially supported by approved evidence",
  "customer-provided": "Supported by customer-provided context",
  assumption: "Assumption requiring validation",
  "conflicting-evidence": "Sources conflict",
  "needs-validation": "No matching approved evidence",
};

export function evidenceStatusLabel(status: EvidenceStatus) {
  return statusLabels[status];
}

export function evidenceStatusHeading(status: EvidenceStatus) {
  return statusHeadings[status];
}
