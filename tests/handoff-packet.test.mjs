import assert from "node:assert/strict";
import test from "node:test";
import { handoffAnswerFor, mergeHandoffPacket } from "../frontend/src/lib/handoff-packet.ts";
import { normalizeBriefResponse } from "../frontend/src/lib/response.ts";

const scope = { company: "Apex Mutual", clientId: "apex-mutual", projectId: "apex-mutual", packetVersion: 3, audienceRole: "PM", focus: "What happens next?" };
const brief = normalizeBriefResponse({
  provider: "bedrock",
  businessCase: { scenario: "Apex approved a bounded discovery." },
  technical: ["Confirm the recovery target."], executive: ["Confirm the desired outcome."],
  stakeholders: ["Lena Ortiz, sponsor."], gameplan: ["Read back the scope."], objections: ["Collect evidence."],
  projectAnswer: "A provisional answer from initial generation must not appear as a handoff.",
  sourceCatalog: [{ sourceId: "src-context", title: "Customer context", evidenceSnippet: "Apex approved discovery." }],
  claims: [{ claimId: "claim-scenario", section: "businessCase", itemIndex: 0, text: "Apex approved a bounded discovery.", sourceIds: ["src-context"], evidenceStatus: "customer-provided", evidenceSnippet: "Apex approved discovery.", validationStatus: "supported-by-customer-context" }],
  evidenceCoverage: { materialClaims: 1, claimsWithApprovedSources: 1, coveragePercent: 100, statusCounts: { "customer-provided": 1 } },
  metadata: { clientId: scope.clientId, projectId: scope.projectId, packetVersion: 3, approvedPacketVersion: 3 },
}, "bedrock");
const handoff = normalizeBriefResponse({
  ...brief, provider: "agentcore", projectAnswer: "Lena will confirm scope before the technical workshop.",
  claims: undefined, sourceCatalog: undefined, evidenceCoverage: undefined,
}, "agentcore");

test("initial generation and missing handoff output never become completed pre-call content", () => {
  assert.equal(handoffAnswerFor(brief, scope), "");
  assert.equal(handoffAnswerFor(null, scope), "");
  assert.equal(normalizeBriefResponse({}, "agentcore").projectAnswer, "");
});

test("handoff retains every approved brief and its evidence when the result omits assessments", () => {
  const before = structuredClone(brief);
  const result = mergeHandoffPacket(brief, handoff, scope);
  for (const field of ["businessCase", "technical", "executive", "stakeholders", "gameplan", "objections", "sourceCatalog", "claims"]) {
    assert.deepEqual(result[field], brief[field], field);
  }
  assert.equal(result.evidenceCoverage.coveragePercent, 100);
  assert.equal(result.metadata.packetVersion, 3);
  assert.equal(handoffAnswerFor(result, scope), handoff.projectAnswer);
  assert.deepEqual(brief, before);
});

test("completed handoff and confidence survive save, reload, and normalization", () => {
  const saved = mergeHandoffPacket(brief, handoff, scope);
  const restored = normalizeBriefResponse(JSON.parse(JSON.stringify(saved)), "agentcore");
  assert.equal(handoffAnswerFor(restored, scope), handoff.projectAnswer);
  assert.deepEqual(restored.claims, saved.claims);
  assert.deepEqual(restored.sourceCatalog, saved.sourceCatalog);
  assert.deepEqual(restored.evidenceCoverage, saved.evidenceCoverage);
});

test("another company, client, project, version, role, or focus cannot display a cached answer", () => {
  const saved = mergeHandoffPacket(brief, handoff, scope);
  for (const field of ["company", "clientId", "projectId", "packetVersion", "audienceRole", "focus"]) {
    assert.equal(handoffAnswerFor(saved, { ...scope, [field]: field === "packetVersion" ? 4 : "Other" }), "", field);
  }
});

test("a stale or cross-client handoff cannot replace the approved packet", () => {
  for (const metadata of [{ clientId: "another-client" }, { projectId: "another-project" }, { approvedPacketVersion: 2 }, { packetVersion: 4 }]) {
    assert.throws(() => mergeHandoffPacket(brief, { ...handoff, metadata: { ...handoff.metadata, ...metadata } }, scope), /did not match/);
  }
  assert.throws(() => mergeHandoffPacket(brief, { ...handoff, projectAnswer: "" }, scope), /did not match/);
});

test("legacy packets without assessments remain unassessed, not artificially scored", () => {
  const legacy = { ...brief, claims: [], sourceCatalog: [], evidenceCoverage: undefined };
  const saved = mergeHandoffPacket(legacy, handoff, scope);
  assert.equal(saved.evidenceCoverage, undefined);
  assert.deepEqual(saved.claims, []);
});

test("a new handoff never inherits the old answer's assessment", () => {
  const previous = { ...brief, claims: [...brief.claims, { ...brief.claims[0], claimId: "claim-old-answer", section: "projectAnswer", text: brief.projectAnswer }] };
  const saved = mergeHandoffPacket(previous, handoff, scope);
  assert.deepEqual(saved.claims, brief.claims);
  assert.equal(saved.evidenceCoverage.materialClaims, 1);
});

test("an older packet recovers real assessments only from identical scoped server content", () => {
  const legacy = { ...brief, claims: [], sourceCatalog: [], evidenceCoverage: undefined };
  const restoredServer = { ...handoff, claims: brief.claims, sourceCatalog: brief.sourceCatalog };
  const saved = mergeHandoffPacket(legacy, restoredServer, scope);
  assert.deepEqual(saved.claims, brief.claims);
  assert.equal(saved.evidenceCoverage.coveragePercent, 100);
  const changed = mergeHandoffPacket({ ...legacy, technical: ["A different technical brief."] }, restoredServer, scope);
  assert.deepEqual(changed.claims, []);
  assert.equal(changed.evidenceCoverage, undefined);
  const unscoped = mergeHandoffPacket(legacy, { ...restoredServer, metadata: undefined }, scope);
  assert.deepEqual(unscoped.claims, []);
});
