"use client";

export type OpportunityGateStatus =
  | "not-started"
  | "in-progress"
  | "blocked"
  | "ready"
  | "complete";

export type OpportunityGate = {
  id: string;
  name: string;
  status: OpportunityGateStatus;
  owner: string;
  evidence: string;
  nextAction: string;
  confirmed: boolean;
};

type OpportunityGatesProps = {
  gates: OpportunityGate[];
  disabled?: boolean;
  onStatusChange: (id: string, status: OpportunityGateStatus) => void;
  onConfirm: (id: string) => void;
};

const statusOptions: Array<{ value: OpportunityGateStatus; label: string }> = [
  { value: "not-started", label: "Not started" },
  { value: "in-progress", label: "In progress" },
  { value: "blocked", label: "Blocked" },
  { value: "ready", label: "Ready" },
  { value: "complete", label: "Complete" },
];

export function OpportunityGates({
  gates,
  disabled = false,
  onStatusChange,
  onConfirm,
}: OpportunityGatesProps) {
  const confirmedCount = gates.filter((gate) => gate.confirmed).length;

  return (
    <section className="opportunity-gates" aria-labelledby="opportunity-gates-title">
      <header className="opportunity-gates-header">
        <div>
          <p>Human decision</p>
          <h2 id="opportunity-gates-title">Opportunity gates</h2>
          <span>AI suggests the starting point. The account team confirms every status.</span>
        </div>
        <strong>{confirmedCount}/{gates.length} confirmed</strong>
      </header>

      <div className="opportunity-gate-list">
        {gates.map((gate) => (
          <article
            className={`opportunity-gate opportunity-gate-${gate.status}${gate.confirmed ? " opportunity-gate-confirmed" : ""}`}
            key={gate.id}
          >
            <div className="opportunity-gate-main">
              <span aria-hidden="true" />
              <div>
                <strong>{gate.name}</strong>
                <small>{gate.owner}</small>
              </div>
            </div>
            <div className="opportunity-gate-context">
              <p><b>Evidence</b><span title={gate.evidence}>{gate.evidence}</span></p>
              <p><b>Next action</b><span title={gate.nextAction}>{gate.nextAction}</span></p>
            </div>
            <div className="opportunity-gate-actions">
              <label>
                <span className="sr-only">Status for {gate.name}</span>
                <select
                  value={gate.status}
                  disabled={disabled}
                  onChange={(event) =>
                    onStatusChange(gate.id, event.target.value as OpportunityGateStatus)
                  }
                >
                  {statusOptions.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                disabled={disabled || gate.confirmed}
                onClick={() => onConfirm(gate.id)}
              >
                {gate.confirmed ? "Confirmed" : "Confirm"}
              </button>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
