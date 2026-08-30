import type { RefinementTarget } from "./types";

export type RefinementDraft = {
  feedback: string[];
  feedbackNotes: string;
};

export type RefinementDrafts = Record<RefinementTarget, RefinementDraft>;

export const refinementTargets: RefinementTarget[] = [
  "businessCase",
  "technical",
  "executive",
  "stakeholders",
  "gameplan",
  "objections",
];

function emptyDraft(): RefinementDraft {
  return { feedback: [], feedbackNotes: "" };
}

export function createRefinementDrafts(): RefinementDrafts {
  return {
    businessCase: emptyDraft(),
    technical: emptyDraft(),
    executive: emptyDraft(),
    stakeholders: emptyDraft(),
    gameplan: emptyDraft(),
    objections: emptyDraft(),
  };
}

function cleanFeedback(value: unknown) {
  if (!Array.isArray(value)) {
    return [];
  }

  return value
    .filter(
      (item): item is string =>
        typeof item === "string" && Boolean(item.trim())
    )
    .map((item) => item.trim())
    .filter((item, index, items) => items.indexOf(item) === index);
}

export function normalizeRefinementDrafts(
  value: unknown,
  legacyTarget: RefinementTarget = "businessCase",
  legacyFeedback?: unknown,
  legacyNotes?: unknown
): RefinementDrafts {
  const normalized = createRefinementDrafts();

  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    const source = value as Record<string, unknown>;
    for (const target of refinementTargets) {
      const draft = source[target];
      if (typeof draft !== "object" || draft === null || Array.isArray(draft)) {
        continue;
      }
      const record = draft as Record<string, unknown>;
      normalized[target] = {
        feedback: cleanFeedback(record.feedback),
        feedbackNotes:
          typeof record.feedbackNotes === "string"
            ? record.feedbackNotes
            : "",
      };
    }
    return normalized;
  }

  normalized[legacyTarget] = {
    feedback: cleanFeedback(legacyFeedback),
    feedbackNotes: typeof legacyNotes === "string" ? legacyNotes : "",
  };
  return normalized;
}

export function cloneRefinementDrafts(
  value: RefinementDrafts
): RefinementDrafts {
  return Object.fromEntries(
    refinementTargets.map((target) => [
      target,
      {
        feedback: [...value[target].feedback],
        feedbackNotes: value[target].feedbackNotes,
      },
    ])
  ) as RefinementDrafts;
}

export function toggleRefinementFeedback(
  drafts: RefinementDrafts,
  target: RefinementTarget,
  option: string
): RefinementDrafts {
  const feedback = drafts[target].feedback;
  return {
    ...drafts,
    [target]: {
      ...drafts[target],
      feedback: feedback.includes(option)
        ? feedback.filter((item) => item !== option)
        : [...feedback, option],
    },
  };
}

export function refinementDraftChanged(
  draft: RefinementDraft,
  applied: RefinementDraft
) {
  return (
    draft.feedback.length !== applied.feedback.length ||
    draft.feedback.some((item) => !applied.feedback.includes(item)) ||
    draft.feedbackNotes.trim() !== applied.feedbackNotes.trim()
  );
}
