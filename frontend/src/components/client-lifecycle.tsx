"use client";

export type LifecycleStageId =
  | "research"
  | "insights"
  | "discovery"
  | "meeting-prep"
  | "follow-up";

export type LifecycleStageStatus =
  | "complete"
  | "current"
  | "available"
  | "attention"
  | "locked";

export type LifecycleStage = {
  id: LifecycleStageId;
  label: string;
  shortLabel: string;
  status: LifecycleStageStatus;
  detail: string;
};

type ClientLifecycleProps = {
  stages: LifecycleStage[];
  selectedStage: LifecycleStageId;
  onSelect: (stage: LifecycleStageId) => void;
};

function statusLabel(status: LifecycleStageStatus) {
  if (status === "complete") return "Completed";
  if (status === "current") return "Current stage";
  if (status === "attention") return "Needs attention";
  if (status === "locked") return "Complete earlier stages first";
  return "Available";
}

export function ClientLifecycle({
  stages,
  selectedStage,
  onSelect,
}: ClientLifecycleProps) {
  return (
    <nav className="client-lifecycle" aria-label="Customer lifecycle">
      {stages.map((stage, index) => {
        const selected = selectedStage === stage.id;
        const disabled = stage.status === "locked";

        return (
          <button
            key={stage.id}
            className={`client-lifecycle-stage client-lifecycle-stage-${stage.status}${selected ? " client-lifecycle-stage-selected" : ""}`}
            type="button"
            disabled={disabled}
            aria-current={selected ? "step" : undefined}
            aria-label={`${index + 1}. ${stage.label}. ${statusLabel(stage.status)}. ${stage.detail}`}
            title={disabled ? statusLabel(stage.status) : stage.detail}
            onClick={() => onSelect(stage.id)}
          >
            <span className="client-lifecycle-index" aria-hidden="true">
              {index + 1}
            </span>
            <span className="client-lifecycle-copy">
              <strong>{stage.shortLabel}</strong>
              <small>{statusLabel(stage.status)}</small>
            </span>
          </button>
        );
      })}
    </nav>
  );
}
