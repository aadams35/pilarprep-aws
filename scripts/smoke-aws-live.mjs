import { Sha256 } from "@aws-crypto/sha256-js";
import { HttpRequest } from "@smithy/protocol-http";
import { SignatureV4 } from "@smithy/signature-v4";
import { execFileSync } from "node:child_process";

const region = process.env.AWS_REGION ?? "us-east-1";
const backendStack = process.env.PILLARPREP_BACKEND_STACK ?? "pillarprep-bedrock";
const frontendStack = process.env.PILLARPREP_FRONTEND_STACK ?? "pillarprep-frontend";

function awsJson(args) {
  const output = execFileSync("aws", [...args, "--region", region, "--output", "json"], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });

  return JSON.parse(output);
}

function stackOutputs(stackName) {
  const outputs = awsJson([
    "cloudformation",
    "describe-stacks",
    "--stack-name",
    stackName,
    "--query",
    "Stacks[0].Outputs",
  ]);

  return Object.fromEntries(outputs.map((item) => [item.OutputKey, item.OutputValue]));
}

async function postCognitoIdentity(target, payload) {
  const response = await fetch(`https://cognito-identity.${region}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "content-type": "application/x-amz-json-1.1",
      "x-amz-target": `AWSCognitoIdentityService.${target}`,
    },
    body: JSON.stringify(payload),
  });
  const text = await response.text();

  if (!response.ok) {
    throw new Error(`Cognito ${target} failed with HTTP ${response.status}: ${text}`);
  }

  return JSON.parse(text);
}

async function cognitoCredentials(identityPoolId) {
  const identity = await postCognitoIdentity("GetId", { IdentityPoolId: identityPoolId });
  const credentials = await postCognitoIdentity("GetCredentialsForIdentity", {
    IdentityId: identity.IdentityId,
  });
  const values = credentials.Credentials;

  if (!values?.AccessKeyId || !values.SecretKey) {
    throw new Error("Cognito did not return usable demo credentials.");
  }

  return {
    accessKeyId: values.AccessKeyId,
    secretAccessKey: values.SecretKey,
    sessionToken: values.SessionToken,
  };
}

const sleep = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

async function signedPostJson(url, payload, credentials) {
  const endpoint = new URL(url);
  const body = JSON.stringify(payload);
  const signer = new SignatureV4({
    credentials,
    region,
    service: "execute-api",
    sha256: Sha256,
  });
  const request = new HttpRequest({
    protocol: endpoint.protocol,
    hostname: endpoint.hostname,
    method: "POST",
    path: `${endpoint.pathname}${endpoint.search}`,
    headers: {
      accept: "application/json",
      "content-type": "application/json",
      host: endpoint.host,
    },
    body,
  });
  const signed = await signer.sign(request);
  const headers = { ...signed.headers };

  delete headers.host;

  return fetch(url, {
    method: "POST",
    headers,
    body,
  });
}

async function completeBriefRequest(url, payload, credentials) {
  const startedAt = Date.now();
  let response = await signedPostJson(
    url,
    {
      ...payload,
      asyncGeneration: payload.modelPreference !== "nova-micro",
    },
    credentials,
  );
  let text = await response.text();

  if (!response.ok) {
    throw new Error("Signed API returned HTTP " + response.status + ": " + text);
  }

  let parsed = JSON.parse(text);
  if (response.status !== 202) {
    return { body: parsed, elapsedMs: Date.now() - startedAt, polls: 0 };
  }

  if (!parsed.jobId || !parsed.projectId) {
    throw new Error("Async API response did not include jobId and projectId.");
  }

  const deadline = Date.now() + 240_000;
  let polls = 0;

  while (Date.now() < deadline) {
    await sleep(Math.max(750, Math.min(parsed.pollAfterMs ?? 1500, 5000)));
    polls += 1;
    response = await signedPostJson(
      url,
      {
        operation: "getBriefJob",
        jobId: parsed.jobId,
        projectId: parsed.projectId,
      },
      credentials,
    );
    text = await response.text();

    if (!response.ok) {
      throw new Error("Signed job poll returned HTTP " + response.status + ": " + text);
    }

    parsed = JSON.parse(text);
    if (response.status !== 202) {
      return { body: parsed, elapsedMs: Date.now() - startedAt, polls };
    }
  }

  throw new Error("Brief job did not complete within four minutes.");
}

const request = {
  mode: "prebrief",
  modelPreference: "nova-pro",
  company: "BlueMesa Payments",
  industry: "Financial Services",
  meetingType: "Executive Briefing",
  companySize: "Enterprise",
  pillars: ["Security", "Reliability", "Operational Excellence"],
  pillarRanking: [
    { rank: 1, pillar: "Security" },
    { rank: 2, pillar: "Reliability" },
    { rank: 3, pillar: "Operational Excellence" },
  ],
  context: "BlueMesa Payments is consolidating merchant dispute processing and customer reporting after two acquisitions. Leadership needs a phased AWS modernization before holiday volume, with explicit PCI evidence, identity separation, settlement continuity, tested recovery objectives, and rollback criteria.",
  companyValues: "Merchant trust, rigorous compliance, low-drama change management, and faster delivery only when customer impact stays protected.",
  companyValuesUrl: "https://www.bluemesa-payments.example/company/values",
  decisionMakers: [
    {
      name: "Ariana Cole",
      title: "Chief Digital Officer",
      source: "Customer-approved profile notes",
      context: "Focused on merchant trust, faster launch cycles, and board confidence during peak season.",
    },
    {
      name: "Dev Malik",
      title: "VP Infrastructure and Resilience",
      source: "Customer-approved profile notes",
      context: "Owns settlement recovery, failover evidence, RTO/RPO, and rollback readiness.",
    },
    {
      name: "Rachel Kim",
      title: "Chief Risk and Compliance Officer",
      source: "Customer-approved profile notes",
      context: "Owns PCI evidence, identity separation, and compliance approval.",
    },
  ],

  meetingNotes: "BlueMesa approved a bounded settlement-recovery pilot with named evidence owners.",
  role: "PM",
  prompt: "Create the first two-week plan.",
};
const backend = stackOutputs(backendStack);
const frontend = stackOutputs(frontendStack);
const frontendUrl = frontend.FrontendUrl;
const apiUrl = backend.BriefApiUrl;
const identityPoolId = backend.DemoIdentityPoolId;

if (!frontendUrl || !apiUrl || !identityPoolId) {
  throw new Error("Missing required CloudFormation outputs for smoke test.");
}

const site = await fetch(frontendUrl);
const unsigned = await fetch(apiUrl, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: "{}",
});
const credentials = await cognitoCredentials(identityPoolId);
const baselineRun = await completeBriefRequest(apiUrl, request, credentials);
const body = baselineRun.body;

if (site.status !== 200) {
  throw new Error(`CloudFront site returned HTTP ${site.status}.`);
}

if (unsigned.status !== 403) {
  throw new Error(`Unsigned API should return 403, got HTTP ${unsigned.status}.`);
}

if (body.provider !== "bedrock") {
  throw new Error(`Expected provider=bedrock, got ${body.provider}.`);
}
if (body.metadata?.fallbackUsed) {
  throw new Error(`Baseline used the deterministic fallback (${body.metadata.modelStopReason ?? "unknown stop reason"}): ${body.metadata.fallbackReason ?? "unknown reason"}`);
}

if (!body.metadata?.artifactKey || !body.metadata?.docxArtifactKey || !body.metadata?.docxDownloadUrl || !body.metadata?.stateKey) {
  throw new Error("Live response did not include S3 JSON, S3 DOCX, DOCX download URL, and DynamoDB state metadata.");
}

if (!body.metadata.artifactKey.startsWith("clients/") || !body.metadata.docxArtifactKey.startsWith("clients/")) {
  throw new Error("Live response did not save client-first artifact keys.");
}

if (!body.metadata.artifactKey.endsWith("/brief/latest.json") || !body.metadata.docxArtifactKey.endsWith("/brief/latest.docx")) {
  throw new Error("Live response did not save latest-only JSON and DOCX artifact keys.");
}

if (body.metadata.stateKey !== "BRIEF#LATEST") {
  throw new Error(`Expected DynamoDB stateKey BRIEF#LATEST, got ${body.metadata.stateKey}.`);
}

if (!body.metadata.docxDownloadUrl.startsWith("https://")) {
  throw new Error("Live response did not issue an HTTPS DOCX download URL.");
}

if (!body.metadata?.guardrailId || !body.metadata?.guardrailVersion) {
  throw new Error("Live response did not include Bedrock guardrail metadata.");
}

if (!(body.metadata.totalTokens > 0) || !["reported", "estimated"].includes(body.metadata.tokenUsageSource)) {
  throw new Error("Live response did not include usable token accounting.");
}

if (!(body.metadata.estimatedModelCostUsd > 0)) {
  throw new Error("Live response did not include a positive estimated model cost.");
}

const sections = ["technical", "executive", "stakeholders", "gameplan", "objections"];
const businessCaseFields = [
  "scenario",
  "desiredOutcomes",
  "alignmentStatement",
  "inScope",
  "outOfScope",
  "successCriteria",
];
if (
  !businessCaseFields.every((field) => typeof body.businessCase?.[field] === "string" && body.businessCase[field].trim()) ||
  !body.businessCase.scenario.toLowerCase().includes("bluemesa")
) {
  throw new Error("Live response did not include the complete customer-specific business case.");
}
const items = sections.flatMap((section) => body[section] ?? []);
if (!sections.every((section) => body[section]?.length === 4) || items.filter((item) => /Ask:/i.test(item)).length < 18) {
  throw new Error("Live response did not meet the long-form section and live-question contract.");
}

const approvedSources = new Set(body.citations ?? []);
const expectedEvidence = new Set([
  ...businessCaseFields.map((_field, itemIndex) => "businessCase:" + itemIndex),
  ...sections.flatMap((section) => Array.from({ length: 4 }, (_, itemIndex) => section + ":" + itemIndex)),
  "projectAnswer:0",
]);
const evidence = Array.isArray(body.evidence) ? body.evidence : [];
const evidenceKeys = new Set(evidence.map((item) => item.section + ":" + item.itemIndex));
if (
  ![...expectedEvidence].every((key) => evidenceKeys.has(key)) ||
  !evidence.every((item) => item.sources?.length && item.sources.every((source) => approvedSources.has(source)))
) {
  throw new Error("Live response did not include complete paragraph-level evidence from approved sources.");
}

const nextSteps = body.projectArtifacts?.nextSteps;
if (
  !Array.isArray(nextSteps?.immediateActions) ||
  nextSteps.immediateActions.length < 3 ||
  !nextSteps.immediateActions.every((item) =>
    ["action", "owner", "timing", "dependency", "decisionGate"].every((field) => item?.[field]?.trim())
  ) ||
  nextSteps.openQuestions?.length < 2 ||
  nextSteps.nextMeeting?.attendees?.length < 2 ||
  !nextSteps.customerSummary?.trim() ||
  !nextSteps.internalNotes?.trim()
) {
  throw new Error("Live response did not include complete, actionable handoff next steps.");
}
const specificText = JSON.stringify(body).toLowerCase();
for (const anchor of ["bluemesa payments", "merchant", "settlement", "pci", "ariana cole", "dev malik", "rachel kim"]) {
  if (!specificText.includes(anchor)) throw new Error("Live BlueMesa response is missing required anchor: " + anchor + ".");
}

if (/\b(API Gateway|Lambda|DynamoDB|CloudWatch|S3|Bedrock|EC2)\b/i.test((body.executive ?? []).join(" "))) {
  throw new Error("Live executive brief contains service-level AWS jargon.");
}

const snapshotBrief = (brief) => ({
  businessCase: structuredClone(brief.businessCase),
  technical: [...brief.technical],
  executive: [...brief.executive],
  stakeholders: [...brief.stakeholders],
  gameplan: [...brief.gameplan],
  objections: [...brief.objections],
  projectAnswer: brief.projectAnswer,
  projectArtifacts: structuredClone(brief.projectArtifacts),
  citations: [...brief.citations],
  evidence: structuredClone(brief.evidence),
});
const refinementPacketSections = [
  "businessCase",
  "technical",
  "executive",
  "stakeholders",
  "gameplan",
  "objections",
  "projectAnswer",
  "projectArtifacts",
];
const sameValue = (left, right) => JSON.stringify(left) === JSON.stringify(right);
function assertTargetIsolation(before, after, target) {
  for (const section of refinementPacketSections) {
    if (section !== target && !sameValue(before[section], after[section])) {
      throw new Error(`Live ${target} refinement changed preserved section ${section}.`);
    }
  }
  if (
    after.metadata?.refinementTarget !== target ||
    !sameValue(after.metadata?.changedSectionIds, [target]) ||
    after.metadata?.unauthorizedSectionChanges !== 0 ||
    after.metadata?.refinementIsolationPassed !== true
  ) {
    throw new Error(`Live ${target} refinement diagnostics did not confirm isolation.`);
  }
}

const previousBrief = snapshotBrief(body);
const businessRefinementRun = await completeBriefRequest(apiUrl, {
  ...request,
  feedback: ["Cost and value: Add cost and value framing"],
  feedbackDetails: [
    { category: "Cost and value", instruction: "Add cost and value framing" },
  ],
  feedbackNotes: "Carry the sponsor decision, measurable outcomes, named owners, and approval evidence through every Business Case field.",
  baseBriefVersion: 1,
  refinementTarget: "businessCase",
  previousBrief,
}, credentials);
const businessRefined = businessRefinementRun.body;
if (businessRefined.metadata?.fallbackUsed) {
  throw new Error(`Business Case refinement used the deterministic fallback: ${businessRefined.metadata.fallbackReason ?? "unknown reason"}`);
}
assertTargetIsolation(body, businessRefined, "businessCase");
const changedBusinessPassages = businessCaseFields.filter(
  (field) => body.businessCase[field] !== businessRefined.businessCase?.[field],
).length;
if (changedBusinessPassages < 4 || businessRefined.metadata?.packetVersion !== 2) {
  throw new Error(
    `Business Case refinement changed only ${changedBusinessPassages} fields or failed to increment packet version.`,
  );
}

const technicalPrevious = snapshotBrief(businessRefined);
const technicalRefinementRun = await completeBriefRequest(apiUrl, {
  ...request,
  feedback: ["Technical depth: Ask deeper architecture questions"],
  feedbackDetails: [
    { category: "Technical depth", instruction: "Ask deeper architecture questions" },
    { category: "Risk and compliance", instruction: "Strengthen RTO and RPO discovery" },
  ],
  feedbackNotes: "Deepen architecture assumptions, evidence requests, rollback ownership, RTO/RPO, and compliance questions inside Technical Brief only.",
  baseBriefVersion: 2,
  refinementTarget: "technical",
  previousBrief: technicalPrevious,
}, credentials);
const refined = technicalRefinementRun.body;
if (refined.metadata?.fallbackUsed) {
  throw new Error(`Technical Brief refinement used the deterministic fallback: ${refined.metadata.fallbackReason ?? "unknown reason"}`);
}
assertTargetIsolation(businessRefined, refined, "technical");
const changedTechnicalPassages = businessRefined.technical.filter(
  (item, index) => item !== refined.technical?.[index],
).length;
if (changedTechnicalPassages < 2 || refined.metadata?.packetVersion !== 3) {
  throw new Error(
    `Technical Brief refinement changed only ${changedTechnicalPassages} passages or failed to increment packet version.`,
  );
}
if (!sameValue(refined.businessCase, businessRefined.businessCase)) {
  throw new Error("Technical Brief refinement changed the already-refined Business Case.");
}
for (const citation of body.citations) {
  if (!refined.citations?.includes(citation)) {
    throw new Error(`Live refinements dropped citation: ${citation}`);
  }
}
if (!refined.citations?.includes("Previous brief version") || !refined.citations?.includes("Refinement feedback")) {
  throw new Error("Live refinements omitted prior-version or feedback evidence labels.");
}
if (
  refined.metadata?.artifactKey !== body.metadata.artifactKey ||
  refined.metadata?.docxArtifactKey !== body.metadata.docxArtifactKey ||
  refined.metadata?.stateKey !== "BRIEF#LATEST"
) {
  throw new Error("Live refinements did not overwrite the same latest-only JSON, DOCX, and DynamoDB state keys.");
}
const refinementRun = technicalRefinementRun;
const docxResponse = await fetch(refined.metadata.docxDownloadUrl);
const docxBytes = new Uint8Array(await docxResponse.arrayBuffer());
if (!docxResponse.ok || docxBytes[0] !== 0x50 || docxBytes[1] !== 0x4b) {
  throw new Error("Live refined DOCX download was unavailable or was not a valid ZIP-based Word package.");
}

console.table({
  cloudFront: site.status,
  unsignedApi: unsigned.status,
  provider: body.provider,
  modelId: body.metadata.modelId,
  modelStopReason: body.metadata.modelStopReason ?? "n/a",
  performanceLatency: body.metadata.performanceLatency ?? "n/a",
  fallbackUsed: body.metadata.fallbackUsed,
  guardrailId: body.metadata.guardrailId,
  guardrailVersion: body.metadata.guardrailVersion,
  clientId: body.metadata.clientId ?? "n/a",
  artifactKey: body.metadata.artifactKey,
  docxArtifactKey: body.metadata.docxArtifactKey,
  docxDownloadUrl: "issued",
  stateKey: body.metadata.stateKey,
  totalTokens: body.metadata.totalTokens,
  tokenUsageSource: body.metadata.tokenUsageSource,
  estimatedModelCostUsd: body.metadata.estimatedModelCostUsd,
  evidenceItems: body.evidence.length,
  businessCase: "complete",
  nextStepActions: nextSteps.immediateActions.length,
  qualityQuestions: items.filter((item) => /Ask:/i.test(item)).length,
  latencyMs: body.metadata.latencyMs ?? "n/a",
  businessRefinementPassages: changedBusinessPassages,
  technicalRefinementPassages: changedTechnicalPassages,
  isolatedRefinementTargets: "businessCase, technical",
  refinementInstructions: refined.metadata.refinementInstructionCount,
  baselineJobMs: baselineRun.elapsedMs,
  baselinePolls: baselineRun.polls,
  refinementJobMs: refinementRun.elapsedMs,
  refinementPolls: refinementRun.polls,
  latestOnlyOverwrite: "pass",
});