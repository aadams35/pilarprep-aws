import type { BriefResponse, BusinessCase } from "./types";

export type ComparableBriefSection =
  | "businessCase"
  | "technical"
  | "executive"
  | "stakeholders"
  | "gameplan"
  | "objections";

export type BriefTextSegment = {
  text: string;
  kind: "unchanged" | "added";
};

export type BriefPassageChange = {
  section: ComparableBriefSection;
  itemIndex: number;
  current: string;
  previous?: string;
  kind: "added" | "modified" | "unchanged";
  segments: BriefTextSegment[];
};

export type BriefVersionComparison = {
  changes: BriefPassageChange[];
  removed: Array<{ section: ComparableBriefSection; itemIndex: number; previous: string }>;
  changedPassages: number;
  changedSections: number;
  changedSectionNames: ComparableBriefSection[];
};

export type BriefReviewMode = "clean" | "changes";

export type RefinementHistoryVersion = {
  company: string;
  refinementTarget?: ComparableBriefSection;
  generatedBrief: BriefResponse;
};

export function approvalAfterGeneration(
  mode: "prebrief" | "project",
  wasApproved: boolean
) {
  return mode === "prebrief"
    ? { approved: false, stale: wasApproved }
    : { approved: true, stale: false };
}

export const businessCaseFields: Array<{ key: keyof BusinessCase; label: string }> = [
  { key: "scenario", label: "Business scenario" },
  { key: "whyNow", label: "Why now" },
  { key: "currentSituation", label: "Current situation" },
  { key: "desiredOutcomes", label: "Desired outcomes" },
  { key: "successCriteria", label: "Success measures" },
  { key: "businessRisks", label: "Business risks" },
  { key: "decisionRequired", label: "Decision required" },
  { key: "inScope", label: "What we will cover" },
  { key: "outOfScope", label: "What we will not cover" },
  { key: "assumptionsAndUnknowns", label: "Assumptions and unknowns" },
  { key: "stakeholderAlignment", label: "Stakeholder alignment" },
  { key: "alignmentStatement", label: "Recommended meeting framing" },
  { key: "nextStepGuidance", label: "Next-step guidance" },
];

export function businessCasePassages(value: BusinessCase | undefined): string[] {
  return businessCaseFields.map(({ key }) => value?.[key]?.trim() ?? "");
}

export function comparableBriefSections(brief: BriefResponse) {
  return {
    businessCase: businessCasePassages(brief.businessCase),
    technical: brief.technical,
    executive: brief.executive,
    stakeholders: brief.stakeholders,
    gameplan: brief.gameplan,
    objections: brief.objections,
  } satisfies Record<ComparableBriefSection, string[]>;
}

function normalized(value: string | undefined) {
  return (value ?? "").replace(/\s+/g, " ").trim();
}

function passageTokens(value: string) {
  return value.match(/\S+\s*/g) ?? [];
}

function comparableToken(value: string) {
  return value.trim().toLocaleLowerCase();
}

export function changedTextSegments(
  previous: string | undefined,
  current: string
): BriefTextSegment[] {
  if (!previous) {
    return [{ text: current, kind: "added" }];
  }

  const currentTokens = passageTokens(current);
  const previousTokens = passageTokens(previous);
  const lengths = Array.from({ length: currentTokens.length + 1 }, () =>
    Array<number>(previousTokens.length + 1).fill(0)
  );

  for (let currentIndex = currentTokens.length - 1; currentIndex >= 0; currentIndex -= 1) {
    for (let previousIndex = previousTokens.length - 1; previousIndex >= 0; previousIndex -= 1) {
      lengths[currentIndex][previousIndex] =
        comparableToken(currentTokens[currentIndex]) === comparableToken(previousTokens[previousIndex])
          ? lengths[currentIndex + 1][previousIndex + 1] + 1
          : Math.max(
              lengths[currentIndex + 1][previousIndex],
              lengths[currentIndex][previousIndex + 1]
            );
    }
  }

  const tokenSegments: BriefTextSegment[] = [];
  let currentIndex = 0;
  let previousIndex = 0;

  while (currentIndex < currentTokens.length) {
    const currentToken = currentTokens[currentIndex];
    if (
      previousIndex < previousTokens.length &&
      comparableToken(currentToken) === comparableToken(previousTokens[previousIndex])
    ) {
      tokenSegments.push({ text: currentToken, kind: "unchanged" });
      currentIndex += 1;
      previousIndex += 1;
      continue;
    }

    if (
      previousIndex < previousTokens.length &&
      lengths[currentIndex][previousIndex + 1] > lengths[currentIndex + 1][previousIndex]
    ) {
      previousIndex += 1;
      continue;
    }

    tokenSegments.push({ text: currentToken, kind: "added" });
    currentIndex += 1;
  }

  return tokenSegments.reduce<BriefTextSegment[]>((segments, segment) => {
    const previousSegment = segments.at(-1);
    if (previousSegment?.kind === segment.kind) {
      previousSegment.text += segment.text;
      return segments;
    }

    segments.push({ ...segment });
    return segments;
  }, []);
}

export function compareBriefVersions(
  previous: BriefResponse | null | undefined,
  current: BriefResponse
): BriefVersionComparison {
  const currentSections = comparableBriefSections(current);
  const previousSections = previous ? comparableBriefSections(previous) : null;
  const changes: BriefPassageChange[] = [];
  const removed: BriefVersionComparison["removed"] = [];
  const changedSectionNames = new Set<ComparableBriefSection>();

  for (const section of Object.keys(currentSections) as ComparableBriefSection[]) {
    const currentItems = currentSections[section];
    const previousItems = previousSections?.[section] ?? [];
    const itemCount = Math.max(currentItems.length, previousItems.length);

    for (let itemIndex = 0; itemIndex < itemCount; itemIndex += 1) {
      const currentPassage = currentItems[itemIndex];
      const previousPassage = previousItems[itemIndex];

      if (!currentPassage && previousPassage) {
        removed.push({ section, itemIndex, previous: previousPassage });
        changedSectionNames.add(section);
        continue;
      }

      if (!currentPassage) continue;
      const kind = !previousPassage
        ? "added"
        : normalized(currentPassage) === normalized(previousPassage)
          ? "unchanged"
          : "modified";

      if (kind !== "unchanged") changedSectionNames.add(section);
      changes.push({
        section,
        itemIndex,
        current: currentPassage,
        previous: previousPassage,
        kind,
        segments:
          kind === "unchanged"
            ? [{ text: currentPassage, kind: "unchanged" }]
            : changedTextSegments(previousPassage, currentPassage),
      });
    }
  }

  return {
    changes,
    removed,
    changedPassages:
      changes.filter((item) => item.kind !== "unchanged").length + removed.length,
    changedSections: changedSectionNames.size,
    changedSectionNames: [...changedSectionNames],
  };
}

export function comparisonForSelectedRefinement(
  history: RefinementHistoryVersion[],
  currentIndex: number,
  activeSection: ComparableBriefSection
): BriefVersionComparison | null {
  if (currentIndex < 0) return null;
  const current = history[currentIndex];
  if (!current || current.refinementTarget !== activeSection) return null;

  const clientKey = current.company.trim().toLowerCase();
  const previous = history
    .slice(currentIndex + 1)
    .find((entry) => entry.company.trim().toLowerCase() === clientKey);
  if (!previous) return null;

  const comparison = compareBriefVersions(
    previous.generatedBrief,
    current.generatedBrief
  );
  return comparison.changedSectionNames.some(
    (section) => section !== activeSection
  )
    ? null
    : comparison;
}
