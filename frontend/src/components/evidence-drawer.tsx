"use client";

import type { BriefClaim, EvidenceSourceRecord } from "@/lib/types";
import {
  evidenceStatusHeading,
  evidenceStatusLabel,
} from "@/lib/evidence-status";

type EvidenceDrawerProps = {
  source: EvidenceSourceRecord | null;
  claim: BriefClaim | null;
  onClose: () => void;
};

function readable(value: string) {
  return value.replace(/-/g, " ").replace(/^\w/, (letter) => letter.toUpperCase());
}

export function EvidenceDrawer({ source, claim, onClose }: EvidenceDrawerProps) {
  if (!source) return null;

  return (
    <aside className="evidence-drawer" role="dialog" aria-modal="false" aria-labelledby="evidence-drawer-title">
      <header>
        <div>
          <span>Approved source</span>
          <h2 id="evidence-drawer-title">{source.title}</h2>
        </div>
        <button type="button" onClick={onClose} aria-label="Close evidence details" title="Close">
          ×
        </button>
      </header>
      <div className="evidence-drawer-body">
        {claim ? (
          <section className="evidence-drawer-claim">
            <span className={`claim-evidence-status claim-evidence-status-${claim.evidenceStatus}`}>
              {evidenceStatusLabel(claim.evidenceStatus)}
            </span>
            <strong>{evidenceStatusHeading(claim.evidenceStatus)}</strong>
            <p>{claim.text}</p>
          </section>
        ) : null}
        <section>
          <span>Relevant excerpt</span>
          <blockquote>{source.evidenceSnippet || "No excerpt was retained with this packet."}</blockquote>
        </section>
        <dl>
          <div><dt>Source type</dt><dd>{readable(source.sourceType)}</dd></div>
          <div><dt>Captured</dt><dd>{source.capturedAt ? new Date(source.capturedAt).toLocaleString() : "Not recorded"}</dd></div>
          <div><dt>Freshness</dt><dd>{readable(source.freshness)}</dd></div>
          <div><dt>Approved by</dt><dd>{source.approvedBy || "Not recorded"}</dd></div>
          <div><dt>Access</dt><dd>{readable(source.accessScope)}</dd></div>
        </dl>
        <p className="evidence-drawer-note">
          Evidence coverage records source linkage. It is not a probability that a claim is true.
        </p>
      </div>
    </aside>
  );
}
