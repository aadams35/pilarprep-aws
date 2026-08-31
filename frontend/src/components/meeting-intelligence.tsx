"use client";

import { useRef, useState, type ChangeEvent, type DragEvent } from "react";

import type {
  MeetingProcessResult,
  MeetingReviewDecision,
  PipelineJobState,
} from "@/lib/types";

export type MeetingDecisionMap = Record<string, MeetingReviewDecision>;
export type MeetingAudioStatus =
  | "empty"
  | "uploading"
  | "scanning"
  | "ready"
  | "blocked"
  | "scan_failed"
  | "failed";

export type MeetingAudioSelection = {
  fileName: string;
  sizeBytes: number;
  status: MeetingAudioStatus;
};

type MeetingIntelligenceProps = {
  isBlueMesa: boolean;
  isApproved: boolean;
  isHosted: boolean;
  isAuthenticated: boolean;
  authAvailable: boolean;
  result: MeetingProcessResult | null;
  decisions: MeetingDecisionMap;
  status: PipelineJobState | null;
  error: string;
  notice: string;
  isProcessing: boolean;
  isApproving: boolean;
  audio: MeetingAudioSelection;
  onSignIn: () => void;
  onUploadAudio: (file: File, consentAcknowledged: boolean) => void;
  onRemoveAudio: () => void;
  onProcess: () => void;
  onDecision: (decision: MeetingReviewDecision) => void;
  onAcceptAll: () => void;
  onApprove: () => void;
};

function timeLabel(seconds: number) {
  const safe = Math.max(0, Math.floor(seconds || 0));
  return `${String(Math.floor(safe / 60)).padStart(2, "0")}:${String(safe % 60).padStart(2, "0")}`;
}

function ProcessingClock({ label }: { label: string }) {
  return (
    <span className="processing-indicator" role="status" aria-live="polite">
      <svg className="processing-clock" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <circle cx="12" cy="12" r="8.5" />
        <path className="processing-clock-hour" d="M12 12V7.5" />
        <path className="processing-clock-minute" d="M12 12h4" />
        <circle className="processing-clock-pin" cx="12" cy="12" r="1" />
      </svg>
      <strong>{label}</strong>
    </span>
  );
}

function statusLabel(status: PipelineJobState | null, approving: boolean) {
  if (approving) return "Promoting approved changes into the handoff...";
  if (status === "queued" || status === "running") return "Meeting queued securely in AWS...";
  if (status === "waiting_for_scan") return "Waiting for the malware scan...";
  if (status === "transcribing") return "Creating the full private transcript...";
  if (status === "screening") return "Checking transcript content safety...";
  if (status === "analyzing") return "Comparing the meeting with the approved brief...";
  if (status === "review-ready") return "Meeting review is ready.";
  return "Processing the meeting...";
}

function audioStatusLabel(status: MeetingAudioStatus) {
  if (status === "uploading") return "Uploading securely...";
  if (status === "scanning") return "Scanning for malware...";
  if (status === "ready") return "Ready to process";
  if (status === "blocked") return "Upload blocked";
  if (status === "scan_failed") return "Malware scan failed";
  return "Upload failed";
}

function reviewCategoryLabel(value: string) {
  return value
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/^./, (character) => character.toUpperCase());
}

function reviewStatus(item: { category: string; supportStatus: string }) {
  const normalized = `${item.category} ${item.supportStatus}`.toLowerCase();
  if (normalized.includes("correct")) return { label: "Corrected", kind: "corrected" };
  if (normalized.includes("unresolved") || normalized.includes("open")) {
    return { label: "Unresolved", kind: "unresolved" };
  }
  return { label: "Transcript supported", kind: "supported" };
}

export function MeetingIntelligence({
  isBlueMesa,
  isApproved,
  isHosted,
  isAuthenticated,
  authAvailable,
  result,
  decisions,
  status,
  error,
  notice,
  isProcessing,
  isApproving,
  audio,
  onSignIn,
  onUploadAudio,
  onRemoveAudio,
  onProcess,
  onDecision,
  onAcceptAll,
  onApprove,
}: MeetingIntelligenceProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [isDraggingAudio, setIsDraggingAudio] = useState(false);
  const [durationSeconds, setDurationSeconds] = useState(0);
  const [consentAcknowledged, setConsentAcknowledged] = useState(false);
  const [consentError, setConsentError] = useState("");
  const canUsePrivateAudio = isBlueMesa && isApproved && isHosted && isAuthenticated;

  function selectAudio(file: File | undefined) {
    if (!file) return;
    if (!canUsePrivateAudio) {
      setConsentError("Sign in to the private workspace before selecting meeting audio.");
      return;
    }
    if (!consentAcknowledged) {
      setConsentError("Confirm recording authorization before choosing audio.");
      return;
    }
    setConsentError("");
    const objectUrl = URL.createObjectURL(file);
    const probe = new Audio();
    probe.preload = "metadata";
    probe.onloadedmetadata = () => {
      setDurationSeconds(Number.isFinite(probe.duration) ? probe.duration : 0);
      URL.revokeObjectURL(objectUrl);
    };
    probe.onerror = () => URL.revokeObjectURL(objectUrl);
    probe.src = objectUrl;
    onUploadAudio(file, true);
  }

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    selectAudio(event.target.files?.[0]);
    event.target.value = "";
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setIsDraggingAudio(false);
    if (!canUsePrivateAudio || !consentAcknowledged) return;
    selectAudio(event.dataTransfer.files?.[0]);
  }

  const audioReady = audio.status === "ready" || audio.status === "scanning";
  const reviewedCount = result
    ? result.reviewItems.filter((item) => Boolean(decisions[item.id])).length
    : 0;
  const allReviewed = Boolean(result?.reviewItems.length) && reviewedCount === result?.reviewItems.length;
  const acceptedCount = Object.values(decisions).filter(
    (item) => item.decision === "accepted" || item.decision === "edited"
  ).length;
  const canProcess = canUsePrivateAudio && audioReady && !isProcessing && !isApproving;
  const canApprove = allReviewed && acceptedCount > 0 && !isProcessing && !isApproving;
  let activeWorkflowStep = 1;
  if (audio.status === "scanning" || status === "waiting_for_scan") {
    activeWorkflowStep = 2;
  } else if (status === "transcribing" || (audioReady && !result)) {
    activeWorkflowStep = 3;
  } else if (status === "screening") {
    activeWorkflowStep = 4;
  } else if (status === "analyzing") {
    activeWorkflowStep = 5;
  } else if (status === "approved") {
    activeWorkflowStep = 7;
  } else if (result && !allReviewed) {
    activeWorkflowStep = 6;
  } else if (isApproving || (result && allReviewed)) {
    activeWorkflowStep = 7;
  }
  const workflowSteps = [
    "Private audio upload",
    "Malware scan",
    "Full transcript",
    "Content safety",
    "AI comparison",
    "Human review",
    "Next-step handoff",
  ];

  return (
    <section
      className="meeting-intelligence"
      aria-labelledby="meeting-intelligence-title"
      aria-busy={
        isProcessing ||
        isApproving ||
        audio.status === "uploading" ||
        audio.status === "scanning"
      }
    >
      <header className="meeting-intelligence-header">
        <div>
          <p>Meeting intelligence</p>
          <h2 id="meeting-intelligence-title">Turn the conversation into governed project context</h2>
          <span>
            Compare the meeting recording with the approved brief, then review the proposed updates.
          </span>
        </div>
      </header>

      <ol className="meeting-intelligence-flow" aria-label="Meeting intelligence workflow">
        {workflowSteps.map((label, index) => {
          const step = index + 1;
          const state = step < activeWorkflowStep ? "complete" : step === activeWorkflowStep ? "active" : "pending";
          return (
            <li
              className={`meeting-flow-step meeting-flow-step-${state}`}
              key={label}
              aria-current={state === "active" ? "step" : undefined}
            >
              <b>{step}</b>
              <span>{label}</span>
            </li>
          );
        })}
      </ol>

      {!isBlueMesa ? (
        <div className="meeting-gate-note">
          Meeting audio is currently available only in the bounded <strong>BlueMesa Payments</strong> demo. Your custom scenario remains unchanged.
        </div>
      ) : !isApproved ? (
        <div className="meeting-gate-note">
          Approve the Blue Mesa briefing packet first. Meeting analysis is always anchored to an approved version.
        </div>
      ) : !isHosted ? (
        <div className="meeting-gate-note">
          The live AWS jobs endpoint is required for transcription and AgentCore analysis.
        </div>
      ) : !authAvailable ? (
        <div className="meeting-gate-note">
          Private meeting upload is not configured in this environment.
        </div>
      ) : !isAuthenticated ? (
        <div className="meeting-gate-note meeting-sign-in-gate">
          <div>
            <strong>Sign in before uploading meeting audio</strong>
            <span>The recording, transcript, and review are isolated to your private workspace.</span>
          </div>
          <button type="button" onClick={onSignIn}>Sign in</button>
        </div>
      ) : null}

      <label className="meeting-upload-consent">
        <input
          type="checkbox"
          checked={consentAcknowledged}
          disabled={!canUsePrivateAudio || audio.status !== "empty"}
          onChange={(event) => {
            setConsentAcknowledged(event.target.checked);
            setConsentError("");
          }}
        />
        <span>
          Only upload recordings you are authorized to process. Meeting audio and names are retained temporarily to create the transcript, analysis, and handoff.
        </span>
      </label>
      {consentError ? <div className="meeting-consent-error" role="alert">{consentError}</div> : null}

      <div
        className={`meeting-audio-upload ${isDraggingAudio ? "meeting-audio-upload-dragging" : ""}`}
        aria-disabled={!canUsePrivateAudio || !consentAcknowledged}
        onDragEnter={(event) => {
          event.preventDefault();
          if (canUsePrivateAudio && consentAcknowledged) setIsDraggingAudio(true);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={() => setIsDraggingAudio(false)}
        onDrop={handleDrop}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".mp3,.wav,.m4a,audio/mpeg,audio/wav,audio/mp4"
          onChange={handleFileChange}
          hidden
        />
        {audio.status === "empty" ? (
          <div className="meeting-audio-empty">
            <div>
              <strong>Upload the meeting recording</strong>
              <span>Choose an MP3, WAV, or M4A file.</span>
            </div>
            <div className="meeting-audio-actions">
              <button
                type="button"
                disabled={!canUsePrivateAudio || !consentAcknowledged}
                onClick={() => inputRef.current?.click()}
              >
                Choose audio
              </button>
            </div>
          </div>
        ) : (
          <div className="meeting-audio-file">
            <div>
              <strong>{audio.fileName}</strong>
              <span>
                {(audio.sizeBytes / (1024 * 1024)).toFixed(1)} MB
                {durationSeconds > 0 ? ` · ${timeLabel(durationSeconds)}` : ""}
                {" · " + audioStatusLabel(audio.status)}
              </span>
            </div>
            <div>
              <button type="button" disabled={!canUsePrivateAudio || audio.status === "uploading" || isProcessing} onClick={() => inputRef.current?.click()}>Replace</button>
              <button type="button" disabled={audio.status === "uploading" || isProcessing} onClick={onRemoveAudio}>Remove</button>
            </div>
          </div>
        )}
      </div>


      <div className="meeting-action-row">
        <button className="meeting-primary-action" type="button" disabled={!canProcess} onClick={onProcess}>
          {isProcessing ? <ProcessingClock label={statusLabel(status, false)} /> : result ? "Process uploaded audio again" : "Process meeting audio"}
        </button>
        <p>
          The analysis is read-only until every proposed update is accepted, edited, or rejected.
        </p>
      </div>

      {isProcessing || isApproving ? (
        <div className="meeting-processing-state">
          <ProcessingClock label={statusLabel(status, isApproving)} />
          <span>You can continue reading and navigating while AWS completes this step.</span>
        </div>
      ) : null}

      {error ? <div className="meeting-error" role="alert">{error}</div> : null}
      {notice ? <div className="meeting-notice" role="status">{notice}</div> : null}

      {result ? (
        <div className="meeting-review-workspace">
          <div className="meeting-summary-band">
            <div>
              <p>Meeting summary</p>
              <strong>{result.analysis.meetingSummary}</strong>
            </div>
            <dl>
              <div><dt>Duration</dt><dd>{timeLabel(result.transcript.durationSeconds)}</dd></div>
              <div><dt>Speakers</dt><dd>{result.transcript.speakerCount}</dd></div>
              <div><dt>Proposals</dt><dd>{result.reviewItems.length}</dd></div>
              <div><dt>Reviewed</dt><dd>{reviewedCount}/{result.reviewItems.length}</dd></div>
            </dl>
          </div>

          <div className="meeting-evidence-outcomes" aria-label="Brief comparison outcomes">
            {[
              {
                key: "confirmed",
                title: "Confirmed by the call",
                detail: "What the prebrief got right",
                items: result.analysis.confirmedFacts,
              },
              {
                key: "corrected",
                title: "Corrected by the call",
                detail: "What the prebrief needs to change",
                items: result.analysis.correctedAssumptions,
              },
              {
                key: "unresolved",
                title: "Still unresolved",
                detail: "What the next conversation must close",
                items: result.analysis.openQuestions,
              },
            ].map((group) => (
              <article className={`meeting-evidence-outcome meeting-evidence-outcome-${group.key}`} key={group.key}>
                <div>
                  <span>{group.items.length}</span>
                  <div><strong>{group.title}</strong><small>{group.detail}</small></div>
                </div>
                {group.items.length ? (
                  <ul>
                    {group.items.slice(0, 3).map((item) => (
                      <li key={item.id}>{item.meetingCorrection || item.statement}</li>
                    ))}
                  </ul>
                ) : (
                  <p>No transcript-grounded items in this category.</p>
                )}
              </article>
            ))}
          </div>

          <div className="meeting-review-columns">
            <div className="meeting-change-review">
              <div className="meeting-section-heading">
                <div><p>Human review</p><h3>Proposed project updates</h3></div>
                <div className="meeting-section-actions">
                  <span>{acceptedCount} selected for handoff</span>
                  <button
                    type="button"
                    disabled={isProcessing || isApproving}
                    onClick={onAcceptAll}
                  >
                    Accept all reviewed changes
                  </button>
                </div>
              </div>
              <div className="meeting-review-list">
                {result.reviewItems.map((item) => {
                  const decision = decisions[item.id];
                  const isEditing = decision?.decision === "edited";
                  const status = reviewStatus(item);
                  return (
                    <article className="meeting-review-item" key={item.id}>
                      <div className="meeting-review-item-heading">
                        <span>{reviewCategoryLabel(item.category)}</span>
                        <div>
                          <em className={`meeting-support-status meeting-support-status-${status.kind}`}>{status.label}</em>
                          <small>{Math.round(item.confidence * 100)}% confidence</small>
                        </div>
                      </div>
                      <div className="meeting-content-comparison">
                        <div><b>Approved brief</b><p>{item.originalContent || "No matching approved content."}</p></div>
                        <div><b>Proposed update</b><p>{item.proposedUpdate}</p></div>
                      </div>
                      <blockquote>
                        <strong>{item.speaker} at {timeLabel(item.timestampStart)}</strong>
                        <span>{item.evidenceText}</span>
                      </blockquote>
                      {isEditing ? (
                        <label className="meeting-edit-field">
                          <span>Edited project statement</span>
                          <textarea
                            value={decision.editedStatement ?? item.proposedUpdate}
                            onChange={(event) => onDecision({ id: item.id, decision: "edited", editedStatement: event.target.value })}
                          />
                        </label>
                      ) : null}
                      <div className="meeting-review-actions" role="group" aria-label={`Review ${item.category}`}>
                        <button className={decision?.decision === "accepted" ? "is-selected" : ""} type="button" onClick={() => onDecision({ id: item.id, decision: "accepted" })}>Accept</button>
                        <button className={isEditing ? "is-selected" : ""} type="button" onClick={() => onDecision({ id: item.id, decision: "edited", editedStatement: decision?.editedStatement || item.proposedUpdate })}>Edit</button>
                        <button className={decision?.decision === "rejected" ? "is-rejected" : ""} type="button" onClick={() => onDecision({ id: item.id, decision: "rejected" })}>Reject</button>
                      </div>
                    </article>
                  );
                })}
              </div>
            </div>

            <aside className="meeting-evidence-panel">
              <div className="meeting-section-heading">
                <div><p>Source record</p><h3>Timestamped transcript</h3></div>
              </div>
              <div className="meeting-transcript-list">
                {result.transcript.segments.map((segment) => (
                  <div key={segment.id}>
                    <span>{timeLabel(segment.timestampStart)}</span>
                    <p><strong>{segment.speaker}</strong>{segment.text}</p>
                  </div>
                ))}
              </div>
              {result.citations.length ? (
                <details className="meeting-citations">
                  <summary>Grounding sources ({result.citations.length})</summary>
                  <ul>{result.citations.map((citation) => <li key={citation}>{citation}</li>)}</ul>
                </details>
              ) : null}
            </aside>
          </div>

          <footer className="meeting-approval-bar">
            <div>
              <strong>{allReviewed ? "Review complete" : `${result.reviewItems.length - reviewedCount} decisions remaining`}</strong>
              <span>Only accepted or edited statements will update project state and the handoff.</span>
            </div>
            <button type="button" disabled={!canApprove} onClick={onApprove}>
              {isApproving ? <ProcessingClock label="Approving next-step handoff..." /> : "Approve Next-Step Handoff"}
            </button>
          </footer>
        </div>
      ) : null}
    </section>
  );
}
