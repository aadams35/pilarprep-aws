import assert from "node:assert/strict";
import test from "node:test";

import {
  approvalAfterGeneration,
  businessCasePassages,
  changedTextSegments,
  compareBriefVersions,
  comparisonForSelectedRefinement,
} from "../frontend/src/lib/brief-diff.ts";

function makeBrief(overrides = {}) {
  return {
    provider: "demo",
    businessCase: {
      scenario: "Apex Mutual is validating a controlled portal modernization.",
      whyNow: "A customer deadline makes a bounded decision necessary now.",
      currentSituation: "The current AWS footprint and operating model require validation.",
      desiredOutcomes: "Agree on outcomes, owners, evidence, and a bounded pilot.",
      successCriteria: "Named owners, measurable evidence, and a scheduled decision.",
      businessRisks: "Unvalidated assumptions could delay approval and create avoidable rework.",
      decisionRequired: "Decide whether the evidence supports the next bounded validation step.",
      inScope: "Outcomes, risks, evidence, owners, and the pilot gate.",
      outOfScope: "Final architecture, production dates, and guaranteed savings.",
      assumptionsAndUnknowns: "Architecture facts, baseline measures, and approval ownership remain to validate.",
      stakeholderAlignment: "Sales, the SA, and customer owners need one shared decision path.",
      alignmentStatement: "Confirm the purpose and decision path before architecture commitments.",
      nextStepGuidance: "Assign evidence owners and schedule the next decision checkpoint.",
    },
    technical: ["Validate identity boundaries and recovery evidence."],
    executive: ["Protect customer trust and make the pilot decision visible."],
    stakeholders: ["Confirm the CIO's approval criteria and evidence needs."],
    gameplan: ["Restate the business case before discussing architecture."],
    objections: ["Concern: disruption. Response: bound the pilot. Ask: what must be proven?"],
    projectAnswer: "Carry the approved brief into delivery planning.",
    projectArtifacts: {
      twoWeekPlan: [],
      riskRegister: [],
      stakeholderMap: [],
      followUpEmail: { subject: "", body: "" },
      nextSteps: {
        immediateActions: [],
        openQuestions: [],
        nextMeeting: { purpose: "", timing: "", attendees: [] },
        customerSummary: "",
        internalNotes: "",
      },
    },
    citations: ["Customer context"],
    evidence: [],
    metadata: {
      generatedAt: "2026-08-12T00:00:00.000Z",
      mode: "prebrief",
      projectId: "apex-mutual",
      modelId: "demo",
      modelTier: "demo",
      requestedModelTier: "demo",
      fallbackUsed: false,
      latencyMs: 1,
      estimatedCostUsd: 0,
      artifactRetention: "latest-only",
    },
    ...overrides,
  };
}

test("compares brief revisions at paragraph level across business and audience sections", () => {
  const previous = makeBrief();
  const current = makeBrief({
    businessCase: {
      ...previous.businessCase,
      scenario: "Apex Mutual is validating a controlled two-wave portal modernization.",
    },
    technical: [
      "Validate identity boundaries, recovery evidence, and rollback ownership.",
      "Map the audit evidence required before the pilot gate.",
    ],
    objections: [],
  });

  const comparison = compareBriefVersions(previous, current);

  assert.equal(comparison.changedPassages, 4);
  assert.equal(comparison.changedSections, 3);
  assert.equal(
    comparison.changes.find((item) => item.section === "businessCase")?.kind,
    "modified",
  );
  assert.deepEqual(
    comparison.changes
      .filter((item) => item.section === "technical")
      .map((item) => item.kind),
    ["modified", "added"],
  );
  assert.deepEqual(comparison.removed, [
    {
      section: "objections",
      itemIndex: 0,
      previous: previous.objections[0],
    },
  ]);
});

test("normalizes harmless whitespace and preserves business-case field order", () => {
  const previous = makeBrief();
  const current = makeBrief({
    technical: ["  Validate   identity boundaries and recovery evidence.  "],
  });

  assert.equal(compareBriefVersions(previous, current).changedPassages, 0);
  assert.deepEqual(businessCasePassages(previous.businessCase), [
    previous.businessCase.scenario,
    previous.businessCase.whyNow,
    previous.businessCase.currentSituation,
    previous.businessCase.desiredOutcomes,
    previous.businessCase.successCriteria,
    previous.businessCase.businessRisks,
    previous.businessCase.decisionRequired,
    previous.businessCase.inScope,
    previous.businessCase.outOfScope,
    previous.businessCase.assumptionsAndUnknowns,
    previous.businessCase.stakeholderAlignment,
    previous.businessCase.alignmentStatement,
    previous.businessCase.nextStepGuidance,
  ]);
});

test("marks only revised wording while retaining readable current copy", () => {
  const segments = changedTextSegments(
    "Validate identity boundaries and recovery evidence before launch.",
    "Validate identity boundaries, rollback ownership, and recovery evidence before launch.",
  );

  assert.equal(segments.map((segment) => segment.text).join(""), "Validate identity boundaries, rollback ownership, and recovery evidence before launch.");
  assert.deepEqual(
    segments.filter((segment) => segment.kind === "added").map((segment) => segment.text.trim()),
    ["boundaries, rollback ownership,"],
  );
  assert.ok(segments.some((segment) => segment.kind === "unchanged"));
});
test("refining an approved pre-brief expires approval while project generation does not", () => {
  assert.deepEqual(approvalAfterGeneration("prebrief", true), {
    approved: false,
    stale: true,
  });
  assert.deepEqual(approvalAfterGeneration("prebrief", false), {
    approved: false,
    stale: false,
  });
  assert.deepEqual(approvalAfterGeneration("project", true), {
    approved: true,
    stale: false,
  });
});

test("a fresh packet never inherits change review from an older technical refinement", () => {
  const original = makeBrief();
  const technicalRefinement = makeBrief({
    technical: ["Validate identity boundaries, rollback ownership, and recovery evidence."],
  });
  const freshPacket = makeBrief({
    technical: ["A fresh technical brief generated from the current customer context."],
  });
  const history = [
    { company: "Apex Mutual", generatedBrief: freshPacket },
    {
      company: "Apex Mutual",
      refinementTarget: "technical",
      generatedBrief: technicalRefinement,
    },
    { company: "Apex Mutual", generatedBrief: original },
  ];

  assert.equal(
    comparisonForSelectedRefinement(history, 0, "technical"),
    null,
  );
  assert.equal(
    comparisonForSelectedRefinement(history, 1, "businessCase"),
    null,
  );
  assert.ok(
    comparisonForSelectedRefinement(history, 1, "technical")?.changedPassages,
  );
});
