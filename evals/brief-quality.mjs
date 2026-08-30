import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { compareBriefVersions } from "../frontend/src/lib/brief-diff.ts";
import { GET, POST } from "../frontend/dev/brief-api.ts";

async function fetchWorker(path, init) {
  if (path !== "/api/brief") throw new Error("Unknown local evaluation endpoint");
  return init?.method === "POST" ? POST(new Request(`http://localhost${path}`, init)) : GET();
}

const lower = (value) => String(value ?? "").toLowerCase();
const wordCount = (value) => String(value ?? "").trim().split(/\s+/).filter(Boolean).length;
const countMatches = (text, pattern) => (text.match(pattern) ?? []).length;

function scoreBrief(scenario, expected, brief) {
  const sections = ["technical", "executive", "stakeholders", "gameplan", "objections"];
  const businessCaseFields = [
    "scenario",
    "desiredOutcomes",
    "alignmentStatement",
    "inScope",
    "outOfScope",
    "successCriteria",
  ];
  const businessCaseText = businessCaseFields.map((field) => brief.businessCase?.[field] ?? "").join("\n");
  const sectionText = Object.fromEntries(sections.map((section) => [section, (brief[section] ?? []).join("\n")]));
  const allText = lower([businessCaseText, ...Object.values(sectionText), brief.projectAnswer].join("\n"));
  const notes = [];
  let score = 0;

  const exactSections = sections.filter((section) => brief[section]?.length === 4).length;
  score += exactSections * 3;
  if (exactSections !== sections.length) notes.push("One or more audience sections do not contain exactly four items.");
  const businessCaseMinimumWords = {
    scenario: 70,
    desiredOutcomes: 60,
    alignmentStatement: 35,
    inScope: 55,
    outOfScope: 50,
    successCriteria: 60,
  };
  const completeBusinessCase = businessCaseFields.every((field) => wordCount(brief.businessCase?.[field]) >= 4);
  const deepBusinessCase = businessCaseFields.every(
    (field) => wordCount(brief.businessCase?.[field]) >= businessCaseMinimumWords[field]
  );
  const businessCaseAnchors = expected.requiredAnchors.filter((anchor) =>
    lower(businessCaseText).includes(lower(anchor))
  );
  const scopedBusinessCase =
    completeBusinessCase &&
    lower(brief.businessCase.scenario).includes(lower(scenario.company)) &&
    businessCaseAnchors.length >= Math.min(2, expected.requiredAnchors.length);
  const bridgesSalesAndSa =
    /\bsales\b/i.test(businessCaseText) &&
    /\bSA\b|solutions architect/i.test(businessCaseText) &&
    /known|confirmed|supplied context/i.test(businessCaseText) &&
    /assumption|hypothesis|validate/i.test(businessCaseText);
  const businessCasePass = deepBusinessCase && scopedBusinessCase && bridgesSalesAndSa;
  if (businessCasePass) score += 6;
  else notes.push("Business case lacks required depth, customer anchors, or an explicit Sales-to-SA validation bridge.");


  const briefItems = sections.flatMap((section) => brief[section] ?? []);
  const liveQuestions = briefItems.filter((item) => /ask\s*:/i.test(item)).length;
  score += Math.min(10, Math.round((liveQuestions / 20) * 10));
  if (liveQuestions < 18) notes.push(`Only ${liveQuestions}/20 brief items contain an explicit Ask: question.`);

  if (allText.includes(lower(scenario.company))) score += 5;
  if (allText.includes(lower(expected.primaryPillar))) score += 5;

  const anchorsFound = expected.requiredAnchors.filter((anchor) => allText.includes(lower(anchor)));
  score += Math.round((anchorsFound.length / expected.requiredAnchors.length) * 10);
  if (anchorsFound.length < expected.requiredAnchors.length) {
    notes.push(`Missing scenario anchors: ${expected.requiredAnchors.filter((anchor) => !anchorsFound.includes(anchor)).join(", ")}.`);
  }

  const stakeholdersFound = expected.stakeholders.filter((name) => allText.includes(lower(name)));
  score += Math.round((stakeholdersFound.length / expected.stakeholders.length) * 8);
  if (stakeholdersFound.length < expected.stakeholders.length) notes.push("Not every approved stakeholder appears in the generated packet.");

  const businessTermsFound = expected.businessTerms.filter((term) => lower(sectionText.executive).includes(lower(term)));
  score += Math.round((businessTermsFound.length / expected.businessTerms.length) * 7);

  const executiveJargon = /\b(API Gateway|Lambda|DynamoDB|CloudWatch|S3|Bedrock|EC2)\b/i.test(sectionText.executive);
  if (!executiveJargon) score += 5;
  else notes.push("Executive brief contains service-level AWS jargon.");

  if (/\b(AWS|Bedrock|Lambda|DynamoDB|CloudWatch|S3|API Gateway)\b/i.test(sectionText.technical)) score += 5;
  if ((brief.objections ?? []).every((item) => /Concern:.*Response:.*Ask:/is.test(item))) score += 5;
  else notes.push("Objection guidance does not consistently follow Concern / Response / Ask.");

  const expectedEvidenceKeys = new Set([
    ...businessCaseFields.map((_field, itemIndex) => `businessCase:${itemIndex}`),
    ...sections.flatMap((section) => Array.from({ length: 4 }, (_, itemIndex) => `${section}:${itemIndex}`)),
    "projectAnswer:0",
  ]);
  const evidence = Array.isArray(brief.evidence) ? brief.evidence : [];
  const approvedLabels = new Set(brief.citations ?? []);
  const evidenceValid = evidence.every((item) => item.sources?.length && item.sources.every((source) => approvedLabels.has(source)));
  const sourceIds = new Set((brief.sourceCatalog ?? []).map((source) => source.sourceId));
  const claims = Array.isArray(brief.claims) ? brief.claims : [];
  const claimKeys = new Set(claims.map((claim) => `${claim.section}:${claim.itemIndex}`));
  const claimsComplete = [...expectedEvidenceKeys].every((key) => claimKeys.has(key));
  const claimSourcesValid = claims.every((claim) =>
    (claim.sourceIds ?? []).every((sourceId) => sourceIds.has(sourceId))
  );
  const evidenceTransparent = claimsComplete && claimSourcesValid && evidenceValid;
  if (evidenceTransparent) score += 15;
  else notes.push("Claim classifications are incomplete or reference an unapproved source.");

  const artifacts = brief.projectArtifacts ?? {};
  const timelineIsSequenced =
    artifacts.twoWeekPlan?.length === 4 &&
    artifacts.twoWeekPlan.every((item) => /^Days?\s+\d/i.test(item.title ?? "") && /Objective:.*Output:.*Dependency:.*Exit criterion:/is.test(item.detail ?? ""));
  if (timelineIsSequenced) score += 4;
  else notes.push("Handoff timeline is not clearly sequenced with objectives, outputs, dependencies, and exit criteria.");
  const assumptionsAreSeparated =
    artifacts.riskRegister?.length === 4 &&
    artifacts.riskRegister.some((item) => /^Unvalidated assumption:/i.test(item.title ?? "") && lower(item.status) === "unvalidated");
  if (assumptionsAreSeparated) score += 4;
  else notes.push("Handoff does not clearly separate an unvalidated assumption from delivery risks.");
  if (artifacts.stakeholderMap?.length === 4) score += 4;
  if (artifacts.followUpEmail?.subject && artifacts.followUpEmail?.body) score += 3;

  const nextSteps = artifacts.nextSteps ?? {};
  const immediateActions = Array.isArray(nextSteps.immediateActions) ? nextSteps.immediateActions : [];
  const actionsComplete =
    immediateActions.length >= 3 &&
    immediateActions.every((item) =>
      ["action", "owner", "timing", "dependency", "decisionGate"].every((field) => wordCount(item?.[field]) >= 1)
    );
  const handoffComplete =
    actionsComplete &&
    nextSteps.openQuestions?.length >= 2 &&
    nextSteps.nextMeeting?.purpose &&
    nextSteps.nextMeeting?.timing &&
    nextSteps.nextMeeting?.attendees?.length >= 2 &&
    wordCount(nextSteps.customerSummary) >= 8 &&
    wordCount(nextSteps.internalNotes) >= 8;
  if (handoffComplete) score += 9;
  else notes.push("Handoff next steps are missing owners, timing, dependencies, decision gates, open questions, or the next meeting.");

  const averageWords = briefItems.reduce((sum, item) => sum + wordCount(item), 0) / Math.max(briefItems.length, 1);
  if (averageWords >= 55) score += 5;
  else notes.push(`Brief items average ${averageWords.toFixed(1)} words; target is at least 55.`);

  const companyMentions = countMatches(allText, new RegExp(lower(scenario.company).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g"));
  if (companyMentions < 3) notes.push("Customer name appears fewer than three times across the packet.");
  const directionAnchors = expected.directionAnchors ?? [];
  const directionFound = directionAnchors.filter((anchor) => allText.includes(lower(anchor)));
  if (directionFound.length !== directionAnchors.length) {
    notes.push("Additional direction is missing required scenario terms.");
  }
  const handoffText = lower(JSON.stringify(artifacts));
  const meetingAnchors = expected.meetingAnchors ?? [];
  const meetingAnchorsFound = meetingAnchors.filter((anchor) => handoffText.includes(lower(anchor)));
  if (meetingAnchorsFound.length !== meetingAnchors.length) {
    notes.push("Meeting context is not consistently represented in the handoff artifacts.");
  }
  const unsupportedClaims = claims.filter((claim) =>
    ["assumption", "conflicting-evidence", "needs-validation"].includes(claim.evidenceStatus)
  ).length;
  const evidenceStatusVariety = new Set(claims.map((claim) => claim.evidenceStatus)).size;


  return {
    score: Math.min(score, 100),
    liveQuestions,
    anchors: `${anchorsFound.length}/${expected.requiredAnchors.length}`,
    evidence: evidenceTransparent ? "pass" : "fail",
    businessCase: businessCasePass ? "pass" : "fail",
    directionCoverage: directionFound.length + "/" + directionAnchors.length,
    meetingConsistency: meetingAnchorsFound.length === meetingAnchors.length ? "pass" : "fail",
    unsupportedClaims,
    evidenceStatusVariety,
    nextSteps: handoffComplete ? "pass" : "fail",
    notes,
  };
}

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

function assertTargetedRefinement(scenarioId, baseline, refined, refinementRubric) {
  const target = refinementRubric.target;
  const comparison = compareBriefVersions(baseline, refined);

  assert.ok(
    comparison.changedPassages >= refinementRubric.minimumChangedPassages,
    `${scenarioId} ${target} refinement changed only ${comparison.changedPassages} passages`,
  );
  assert.equal(
    comparison.changedSections,
    1,
    `${scenarioId} refinement changed ${comparison.changedSections} visible sections`,
  );
  assert.deepEqual(
    comparison.changedSectionNames,
    [target],
    `${scenarioId} refinement escaped the ${target} target`,
  );

  for (const section of refinementRubric.preservedSections) {
    assert.deepEqual(
      refined[section],
      baseline[section],
      `${scenarioId} refinement changed preserved section ${section}`,
    );
  }
  assert.deepEqual(refined.projectAnswer, baseline.projectAnswer, `${scenarioId} changed projectAnswer`);
  assert.deepEqual(refined.projectArtifacts, baseline.projectArtifacts, `${scenarioId} changed projectArtifacts`);

  for (const citation of baseline.citations) {
    assert.ok(refined.citations.includes(citation), `${scenarioId} dropped citation: ${citation}`);
  }
  assert.ok(refined.citations.includes("Previous brief version"), `${scenarioId} omitted prior-version evidence`);
  assert.ok(refined.citations.includes("Refinement feedback"), `${scenarioId} omitted refinement evidence`);
  assert.equal(refined.metadata?.baseBriefVersion, 1, `${scenarioId} lost its source version`);
  assert.equal(refined.metadata?.packetVersion, 2, `${scenarioId} did not increment packet version`);
  assert.equal(refined.metadata?.refinementTarget, target, `${scenarioId} lost its target`);
  assert.deepEqual(refined.metadata?.changedSectionIds, [target], `${scenarioId} reported incorrect changed sections`);
  assert.equal(refined.metadata?.unauthorizedSectionChanges, 0, `${scenarioId} reported unauthorized changes`);
  assert.equal(refined.metadata?.refinementIsolationPassed, true, `${scenarioId} failed isolation validation`);
  assert.ok((refined.metadata?.refinementInstructionCount ?? 0) >= 2, `${scenarioId} lost refinement instructions`);
  return comparison;
}
const scenarios = JSON.parse(await readFile(new URL("../data/demo-scenarios.json", import.meta.url), "utf8"));
const rubric = JSON.parse(await readFile(new URL("../data/brief-quality-rubric.json", import.meta.url), "utf8"));
const results = [];

for (const scenario of scenarios) {
  const expected = rubric.scenarios[scenario.id];
  assert.ok(expected, `Missing quality rubric for ${scenario.id}`);
  const requestPayload = { mode: "prebrief", ...scenario };
  const requestBody = JSON.stringify(requestPayload);
  const generationStarted = performance.now();
  const response = await fetchWorker("/api/brief", {
    method: "POST",
    headers: { accept: "application/json", "content-type": "application/json", "x-pillarprep-mode": "demo" },
    body: requestBody,
  });
  assert.equal(response.status, 200, `${scenario.id} API request failed`);
  const brief = await response.json();
  const generationLatencyMs = Math.round(performance.now() - generationStarted);
  const outputBody = JSON.stringify(brief);
  const inputTokens = Number(brief.metadata?.inputTokens) || Math.ceil(requestBody.length / 4);
  const outputTokens = Number(brief.metadata?.outputTokens) || Math.ceil(outputBody.length / 4);
  const estimatedCostUsd = Number(brief.metadata?.estimatedModelCostUsd) || 0;
  const result = scoreBrief(scenario, expected, brief);
  assert.ok(result.score >= rubric.minimumScore, `${scenario.id} score ${result.score}/${rubric.minimumScore}: ${result.notes.join(" ")}`);

  assert.equal(
    result.directionCoverage,
    expected.directionAnchors.length + "/" + expected.directionAnchors.length,
    scenario.id + " ignored required additional direction"
  );
  assert.equal(result.meetingConsistency, "pass", scenario.id + " handoff lost meeting context");
  assert.ok(result.unsupportedClaims > 0, scenario.id + " hides ungrounded packet passages instead of flagging them");
  assert.ok(result.evidenceStatusVariety >= 2, scenario.id + " applies one blanket evidence status");
  assert.ok(generationLatencyMs < 2000, scenario.id + " local generation exceeded two seconds");
  const refinementResponse = await fetchWorker("/api/brief", {
    method: "POST",
    headers: { accept: "application/json", "content-type": "application/json", "x-pillarprep-mode": "demo" },
    body: JSON.stringify({
      mode: "prebrief",
      ...scenario,
      feedback: ["Risk and compliance: Lead with security and evidence"],
      feedbackDetails: [{ category: "Risk and compliance", instruction: "Lead with security and evidence" }],
      feedbackNotes: "Apply this direction throughout the Business Case and keep every other tab unchanged.",
      baseBriefVersion: 1,
      refinementTarget: rubric.refinement.target,
      previousBrief: packetSnapshot(brief),
    }),
  });
  assert.equal(refinementResponse.status, 200, `${scenario.id} refinement API request failed`);
  const refinedBrief = await refinementResponse.json();
  const refinement = assertTargetedRefinement(scenario.id, brief, refinedBrief, rubric.refinement);

  let contradictionCheck = "not applicable";
  if (scenario.id === "peakcart") {
    const correctionResponse = await fetchWorker("/api/brief", {
      method: "POST",
      headers: {
        accept: "application/json",
        "content-type": "application/json",
        "x-pillarprep-mode": "demo",
      },
      body: JSON.stringify({
        mode: "prebrief",
        ...scenario,
        feedback: ["Customer is already on AWS"],
        feedbackDetails: [
          {
            category: "Current state correction",
            instruction: "Customer is already on AWS",
          },
        ],
        feedbackNotes: (
          "Regenerate the complete Business Case from the corrected current "
          + "state and remove every on-premises migration claim."
        ),
        baseBriefVersion: 1,
        refinementTarget: "businessCase",
        previousBrief: packetSnapshot(brief),
      }),
    });
    assert.equal(correctionResponse.status, 200, "PeakCart correction request failed");
    const corrected = await correctionResponse.json();
    const correctedText = lower(Object.values(corrected.businessCase ?? {}).join("\n"));
    assert.match(correctedText, /already (?:runs?|operates?) on aws|current aws|already on aws/);
    assert.doesNotMatch(
      correctedText,
      /on[- ]premises|on[- ]prem|migrat(?:e|ing|ion) from on[- ]prem|initial aws migration|first move to aws/
    );
    contradictionCheck = "pass";
  }

  results.push({
    scenario: scenario.id,
    score: result.score,
    direction: result.directionCoverage,
    unsupported: result.unsupportedClaims,
    meetingHandoff: result.meetingConsistency,
    questions: result.liveQuestions,
    anchors: result.anchors,
    evidence: result.evidence,
    contradiction: contradictionCheck,
    latencyMs: generationLatencyMs,
    tokens: inputTokens + "/" + outputTokens,
    estimatedCostUsd: estimatedCostUsd.toFixed(6),
    businessCase: result.businessCase,
    nextSteps: result.nextSteps,
    refinement: `${refinement.changedPassages} passages / ${refinement.changedSections} sections`,
  });
}

console.table(results);
console.log(`Brief quality and target-isolated refinement eval passed at ${rubric.minimumScore}/100 for all golden scenarios.`);
