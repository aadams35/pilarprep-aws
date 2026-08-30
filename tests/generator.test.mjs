import assert from "node:assert/strict";
import test from "node:test";

import { compareBriefVersions } from "../frontend/src/lib/brief-diff.ts";
import {
  generateBlueMesaBackupBrief,
  refinementAffectedSections,
  validateBriefRequest,
} from "../frontend/src/lib/generator.ts";

const baseRequest = {
  mode: "prebrief",
  modelPreference: "nova-pro",
  company: "BlueMesa Payments",
  industry: "Financial Services",
  meetingType: "Technical Deep Dive",
  companySize: "Enterprise",
  pillars: ["Security", "Reliability", "Operational Excellence"],
  pillarRanking: [
    { rank: 1, pillar: "Security" },
    { rank: 2, pillar: "Reliability" },
    { rank: 3, pillar: "Operational Excellence" },
  ],
  context: "BlueMesa needs a controlled modernization path before holiday volume.",
  companyValues: "Merchant trust and rigorous compliance.",
  decisionMakers: [],
  meetingNotes: "The executive sponsor wants a bounded pilot.",
  feedback: ["Keep the first draft concise"],
};

test("BlueMesa backup preserves the current request and refinement feedback", () => {
  const first = generateBlueMesaBackupBrief(baseRequest);
  const refined = generateBlueMesaBackupBrief({
    ...baseRequest,
    feedback: [...baseRequest.feedback, "Add ROI and decision criteria"],
  });
  const comparison = compareBriefVersions(first, refined);

  assert.match(first.businessCase.scenario, /technical deep dive/i);
  assert.match(first.businessCase.scenario, /controlled modernization path before holiday volume/i);
  assert.match(refined.executive[2], /Add ROI and decision criteria/);
  assert.equal(comparison.changedPassages, 2);
  assert.equal(comparison.changedSections, 2);
});

test("local demo packets expose deterministic source-backed claim coverage", () => {
  const brief = generateBlueMesaBackupBrief(baseRequest);
  const sourceIds = new Set(brief.sourceCatalog.map((source) => source.sourceId));

  assert.ok(brief.sourceCatalog.some((source) => source.label === "Customer context"));
  assert.ok(brief.sourceCatalog.some((source) => source.label === "Meeting notes"));
  assert.equal(brief.claims.length, brief.evidenceCoverage.materialClaims);
  assert.ok(brief.claims.every((claim) => claim.sourceIds.every((sourceId) => sourceIds.has(sourceId))));
  assert.equal(
    brief.evidenceCoverage.coveragePercent,
    Math.round(
      (brief.evidenceCoverage.claimsWithApprovedSources / brief.evidenceCoverage.materialClaims) * 100,
    ),
  );
  assert.match(brief.evidenceCoverage.meaning, /not a probability of truth/i);
});

test("custom scenarios receive the same provenance controls without BlueMesa carryover", () => {
  const brief = generateBlueMesaBackupBrief({
    ...baseRequest,
    company: "Harbor Manufacturing",
    industry: "Manufacturing",
    context:
      "Harbor Manufacturing needs to validate production scheduling resilience before a plant expansion.",
    companyValues: "Worker safety, predictable operations, and accountable change.",
    meetingNotes: "The operations sponsor requested a bounded resilience assessment.",
    decisionMakers: [],
    feedback: [],
  });
  const sourceIds = new Set(brief.sourceCatalog.map((source) => source.sourceId));
  const allText = JSON.stringify(brief);

  assert.match(brief.businessCase.scenario, /Harbor Manufacturing/);
  assert.doesNotMatch(allText, /Ariana Cole|Marcus Vale|Dev Malik|Rachel Kim|Priya Shah|Elena Torres/);
  assert.ok(brief.sourceCatalog.some((source) => source.label === "Customer context"));
  assert.ok(brief.sourceCatalog.some((source) => source.label === "Meeting notes"));
  assert.ok(!brief.sourceCatalog.some((source) => source.label === "Decision-maker notes"));
  assert.ok(
    brief.claims.every(
      (claim) =>
        claim.sourceIds.every((sourceId) => sourceIds.has(sourceId)) &&
        (claim.sourceIds.length > 0 ||
          ["assumption", "needs-validation", "conflicting-evidence"].includes(
            claim.evidenceStatus,
          )),
    ),
  );
});

test("BlueMesa backup enforces a deep Business Case and canonical SA handoff", () => {
  const brief = generateBlueMesaBackupBrief({
    ...baseRequest,
    decisionMakers: undefined,
    role: "Solutions Architect",
    prompt: "What architecture assumptions must I validate?",
  });
  assert.match(brief.stakeholders.join("\n"), /Priya Shah|Elena Torres/);
  assert.doesNotMatch(brief.technical.join("\n"), /migrat(?:e|ing|ion) from on-premises/i);
  const minimumWords = {
    scenario: 70,
    whyNow: 45,
    currentSituation: 50,
    desiredOutcomes: 60,
    successCriteria: 60,
    businessRisks: 45,
    decisionRequired: 40,
    inScope: 55,
    outOfScope: 50,
    assumptionsAndUnknowns: 50,
    stakeholderAlignment: 45,
    alignmentStatement: 35,
    nextStepGuidance: 50,
  };

  for (const [field, minimum] of Object.entries(minimumWords)) {
    assert.ok(brief.businessCase[field].trim().split(/\s+/).length >= minimum, field);
  }

  assert.match(Object.values(brief.businessCase).join(" "), /Sales/i);
  assert.match(Object.values(brief.businessCase).join(" "), /\bSA\b|Solutions Architect/i);
  assert.equal(brief.claims.filter((item) => item.section === "businessCase").length, 13);
  assert.ok(brief.evidence.filter((item) => item.section === "businessCase").length < 13);
  assert.ok(brief.evidenceCoverage.statusCounts.supported > 0);
  assert.ok(brief.evidenceCoverage.statusCounts["needs-validation"] > 0);
  assert.ok(brief.projectArtifacts.twoWeekPlan.every((item) => /^Days?\s+\d/i.test(item.title)));
  assert.ok(brief.projectArtifacts.twoWeekPlan.every((item) => /Objective:.*Output:.*Dependency:.*Exit criterion:/is.test(item.detail)));
  assert.ok(brief.projectArtifacts.riskRegister.some((item) => /^Unvalidated assumption:/i.test(item.title) && item.status === "Unvalidated"));
  assert.match(brief.projectAnswer, /current-state architecture/i);
  assert.match(brief.projectAnswer, /RTO\/RPO/i);
  assert.match(brief.projectAnswer, /customer-confirmed facts/i);
  assert.match(brief.projectAnswer, /technical validation session/i);
});
function packetSnapshot(brief) {
  return {
    businessCase: { ...brief.businessCase },
    technical: [...brief.technical],
    executive: [...brief.executive],
    stakeholders: [...brief.stakeholders],
    gameplan: [...brief.gameplan],
    objections: [...brief.objections],
    projectAnswer: brief.projectAnswer,
    projectArtifacts: structuredClone(brief.projectArtifacts),
    citations: [...brief.citations],
    evidence: structuredClone(brief.evidence),
  };
}

test("technical refinement revises the Technical Brief and preserves unrelated Executive copy", () => {
  const first = generateBlueMesaBackupBrief({ ...baseRequest, feedback: [] });
  const request = {
    ...baseRequest,
    feedback: ["Technical depth: Add stronger technical depth"],
    feedbackDetails: [
      {
        category: "Technical depth",
        instruction: "Add stronger technical depth",
      },
    ],
    baseBriefVersion: 2,
    refinementTarget: "technical",
    previousBrief: packetSnapshot(first),
  };
  const refined = generateBlueMesaBackupBrief(request);
  const comparison = compareBriefVersions(first, refined);

  assert.ok(refined.technical.every((item) => item.includes("Refinement direction:")));
  assert.deepEqual(refined.executive, first.executive);
  assert.deepEqual(refined.businessCase, first.businessCase);
  assert.deepEqual(refined.gameplan, first.gameplan);
  assert.deepEqual(refined.projectAnswer, first.projectAnswer);
  assert.deepEqual(refined.projectArtifacts, first.projectArtifacts);
  assert.equal(comparison.changedPassages, 4);
  assert.deepEqual(comparison.changedSectionNames, ["technical"]);
  assert.ok(refined.citations.includes("Previous brief version"));
  assert.ok(refined.citations.includes("Refinement feedback"));
  assert.equal(refined.metadata.baseBriefVersion, 2);
  assert.equal(refined.metadata.refinementTarget, "technical");
  assert.deepEqual(refined.metadata.changedSectionIds, ["technical"]);
  assert.equal(refined.metadata.unauthorizedSectionChanges, 0);
  assert.equal(refined.metadata.refinementIsolationPassed, true);
});

test("cost refinement updates Business Case only", () => {
  const first = generateBlueMesaBackupBrief({ ...baseRequest, feedback: [] });
  const refined = generateBlueMesaBackupBrief({
    ...baseRequest,
    feedback: ["Cost and value: Add cost and value framing"],
    feedbackDetails: [
      {
        category: "Cost and value",
        instruction: "Add cost and value framing",
      },
    ],
    refinementTarget: "businessCase",
    previousBrief: packetSnapshot(first),
    baseBriefVersion: 3,
  });

  assert.ok(Object.values(refined.businessCase).every((item) => item.includes("Cost and value")));
  for (const section of [
    "technical",
    "executive",
    "stakeholders",
    "gameplan",
    "objections",
    "projectAnswer",
    "projectArtifacts",
  ]) {
    assert.deepEqual(refined[section], first[section], section);
  }
});

test("free-text refinement updates only its explicit Objection Simulator target", () => {
  const first = generateBlueMesaBackupBrief({ ...baseRequest, feedback: [] });
  const request = {
    ...baseRequest,
    feedback: [],
    feedbackNotes:
      "Make the acquisition deadline, named owners, and approval evidence explicit in every relevant view",
    refinementTarget: "objections",
    previousBrief: packetSnapshot(first),
    baseBriefVersion: 4,
  };
  const refined = generateBlueMesaBackupBrief(request);
  const affected = refinementAffectedSections(request);
  const comparison = compareBriefVersions(first, refined);

  assert.ok(
    refined.objections.every((item) => item.includes("Additional direction"))
  );
  assert.deepEqual(affected, ["objections"]);
  assert.equal(comparison.changedSections, 1);
  assert.deepEqual(comparison.changedSectionNames, ["objections"]);
  for (const section of [
    "businessCase",
    "technical",
    "executive",
    "stakeholders",
    "gameplan",
    "projectAnswer",
    "projectArtifacts",
  ]) {
    assert.deepEqual(refined[section], first[section], section);
  }
});

test("local Business Case correction removes stale on-premises claims", () => {
  const first = generateBlueMesaBackupBrief({
    ...baseRequest,
    context:
      "BlueMesa has an on-premises estate and is considering a migration to AWS.",
    feedback: [],
  });
  const refined = generateBlueMesaBackupBrief({
    ...baseRequest,
    context:
      "BlueMesa has an on-premises estate and is considering a migration to AWS.",
    feedback: ["Customer context: Customer is already on AWS"],
    feedbackDetails: [
      {
        category: "Customer context",
        instruction: "Customer is already on AWS",
      },
    ],
    refinementTarget: "businessCase",
    previousBrief: packetSnapshot(first),
    baseBriefVersion: 5,
  });

  const businessCaseText = Object.values(refined.businessCase).join(" ");
  assert.doesNotMatch(businessCaseText, /on[- ]prem/i);
  assert.doesNotMatch(
    businessCaseText,
    /migrat(?:e|ing|ion).{0,80}to\s+aws/i
  );
  assert.match(businessCaseText, /already on AWS/i);
  for (const section of [
    "technical",
    "executive",
    "stakeholders",
    "gameplan",
    "objections",
    "projectAnswer",
    "projectArtifacts",
  ]) {
    assert.deepEqual(refined[section], first[section], section);
  }
});


test("every refinable tab changes only itself in the local fallback", () => {
  const targets = [
    "businessCase",
    "technical",
    "executive",
    "stakeholders",
    "gameplan",
    "objections",
  ];
  const packetSections = [
    ...targets,
    "projectAnswer",
    "projectArtifacts",
  ];
  const first = generateBlueMesaBackupBrief({ ...baseRequest, feedback: [] });

  for (const target of targets) {
    const refined = generateBlueMesaBackupBrief({
      ...baseRequest,
      feedback: ["Customer context: Make the customer-specific decision explicit"],
      feedbackDetails: [
        {
          category: "Customer context",
          instruction: "Make the customer-specific decision explicit",
        },
      ],
      refinementTarget: target,
      previousBrief: packetSnapshot(first),
      baseBriefVersion: 5,
    });

    assert.notDeepEqual(refined[target], first[target], target);
    for (const section of packetSections) {
      if (section !== target) {
        assert.deepEqual(refined[section], first[section], `${target} changed ${section}`);
      }
    }
    assert.equal(refined.metadata.refinementTarget, target);
    assert.equal(refined.metadata.packetVersion, 6);
    assert.deepEqual(refined.metadata.changedSectionIds, [target]);
    assert.equal(refined.metadata.unauthorizedSectionChanges, 0);
    assert.equal(refined.metadata.refinementIsolationPassed, true);
  }
});

test("refinement validation rejects missing and invalid targets", () => {
  const first = generateBlueMesaBackupBrief({ ...baseRequest, feedback: [] });
  const refinement = {
    ...baseRequest,
    feedback: ["Technical depth: Add stronger technical depth"],
    previousBrief: packetSnapshot(first),
    baseBriefVersion: 1,
  };

  assert.match(validateBriefRequest(refinement), /refinementTarget/);
  assert.match(
    validateBriefRequest({ ...refinement, refinementTarget: "wholePacket" }),
    /refinementTarget/,
  );
});

test("person roles distinguish decision authority from stakeholder influence", () => {
  const brief = generateBlueMesaBackupBrief({
    ...baseRequest,
    decisionMakers: [
      {
        name: "Ariana Cole",
        title: "Chief Digital Officer",
        source: "Customer-approved profile notes",
        context: "Owns the final modernization decision.",
        roleType: "decision-maker",
      },
      {
        name: "Luis Ramirez",
        title: "Platform Engineering Lead",
        source: "Customer-approved profile notes",
        context: "Shapes load-test evidence and implementation confidence.",
        roleType: "stakeholder",
        organizationalRole: "Technical evaluator",
        influence: "high",
        stance: "champion",
      },
    ],
  });

  const stakeholderText = brief.stakeholders.join(" ");
  assert.match(stakeholderText, /Ariana Cole.*Decision-maker/i);
  assert.match(stakeholderText, /Luis Ramirez.*Stakeholder/i);
  assert.match(stakeholderText, /Do not imply approval authority/i);
  assert.doesNotMatch(stakeholderText, /Luis Ramirez.{0,180}personally need to approve/is);
  assert.match(
    validateBriefRequest({
      ...baseRequest,
      decisionMakers: [{ name: "Bad role", title: "Reviewer", context: "", roleType: "approver" }],
    }),
    /roleType/,
  );
});

test("Claude Sonnet 4.6 is accepted as a live model preference", () => {
  assert.equal(
    validateBriefRequest({ ...baseRequest, modelPreference: "claude-sonnet-4.6" }),
    null,
  );
});
test("additional direction is reflected in local business case output", () => {
  const brief = generateBlueMesaBackupBrief({
    ...baseRequest,
    company: "Apex Mutual",
    additionalDirection:
      "The customer must interface with payroll for benefit deductions, identity handoffs, privacy review, cutover, and reconciliation.",
  });
  const businessCaseText = Object.values(brief.businessCase).join(" ").toLowerCase();

  assert.match(businessCaseText, /payroll/);
  assert.ok(brief.citations.includes("Additional direction"));
});
